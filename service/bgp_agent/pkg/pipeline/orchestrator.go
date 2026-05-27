package pipeline

import (
	"context"
	"fmt"
	"log"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"bgp_agent/pkg/export"
	"bgp_agent/pkg/fib"
	"bgp_agent/pkg/storage"
)

// PeerSnapshot 一致性检查用的 peer 维度指标。
type PeerSnapshot struct {
	Window      string `json:"window"`
	VRF         string `json:"vrf"`
	NeighborIP  string `json:"neighbor_ip"`
	SourceIP    string `json:"source_ip"`
	PfxRcd      uint32 `json:"pfx_rcd"`
	RibCount    int64  `json:"rib_count"`
	Established bool   `json:"established"`
}

// IngestFunc 从 ADJ-IN 灌持久 RIB。
type IngestFunc func(ctx context.Context, window, vrf, neighbor, sourceIP string) (ingested, removed int, err error)

// ListPeersFunc 列出需做 drift 检测的 peer。
type ListPeersFunc func(ctx context.Context) []PeerSnapshot

// Orchestrator RIB ingest → FIB recompute → export/kernel reconcile 编排。
type Orchestrator struct {
	store  *storage.Storage
	fibEng *fib.Engine
	export *export.Coordinator
	kernel *export.KernelInstaller

	ingestPeer IngestFunc
	listPeers  ListPeersFunc

	mu                 sync.Mutex
	jobs               map[string]*Job
	seq                uint64
	ingestInflight     map[string]string // peerKey -> jobID
	ingestWindowCount  map[string]int    // window -> 进行中的 ingest 数
	fibInflight        map[string]string // window -> jobID
	fibPending         map[string]bool   // window 在 FIB job 运行期间又收到重算请求
	kernelInflight     map[string]string // window -> jobID
}

func New(
	store *storage.Storage,
	fibEng *fib.Engine,
	exportCoord *export.Coordinator,
	kernel *export.KernelInstaller,
) *Orchestrator {
	return &Orchestrator{
		store:             store,
		fibEng:            fibEng,
		export:            exportCoord,
		kernel:            kernel,
		jobs:              make(map[string]*Job),
		ingestInflight:    make(map[string]string),
		ingestWindowCount: make(map[string]int),
		fibInflight:       make(map[string]string),
		fibPending:        make(map[string]bool),
		kernelInflight:    make(map[string]string),
	}
}

func (o *Orchestrator) SetIngestPeer(fn IngestFunc)   { o.ingestPeer = fn }
func (o *Orchestrator) SetListPeers(fn ListPeersFunc) { o.listPeers = fn }

func ribGapMin() uint64 { return RibGapMin() }

func peerKey(window, vrf, neighbor, sourceIP string) string {
	window = strings.TrimSpace(window)
	vrf = strings.TrimSpace(vrf)
	neighbor = strings.TrimSpace(neighbor)
	if window == storage.WindowDownstream && strings.TrimSpace(sourceIP) != "" {
		return fmt.Sprintf("%s|%s|%s|%s", window, vrf, neighbor, sourceIP)
	}
	return fmt.Sprintf("%s|%s|%s", window, vrf, neighbor)
}

func (o *Orchestrator) newJob(kind JobKind, window, vrf, neighbor, sourceIP string) *Job {
	id := fmt.Sprintf("job-%d", atomic.AddUint64(&o.seq, 1))
	j := &Job{
		ID:         id,
		Kind:       kind,
		Status:     JobPending,
		Window:     window,
		VRF:        vrf,
		NeighborIP: neighbor,
		SourceIP:   sourceIP,
	}
	o.jobs[id] = j
	return j
}

func (o *Orchestrator) GetJob(id string) (Job, bool) {
	o.mu.Lock()
	defer o.mu.Unlock()
	j, ok := o.jobs[id]
	if !ok {
		return Job{}, false
	}
	return j.snapshot(), true
}

// IngestJobForPeer 查询 peer 是否已有后台 ingest 在跑。
func (o *Orchestrator) IngestJobForPeer(window, vrf, neighbor, sourceIP string) (jobID string, inflight bool) {
	key := peerKey(window, vrf, neighbor, sourceIP)
	o.mu.Lock()
	defer o.mu.Unlock()
	id, ok := o.ingestInflight[key]
	return id, ok
}

func (o *Orchestrator) setJob(id string, fn func(*Job)) {
	o.mu.Lock()
	defer o.mu.Unlock()
	j, ok := o.jobs[id]
	if !ok || fn == nil {
		return
	}
	fn(j)
}

func (o *Orchestrator) incWindowIngest(window string) {
	window = normalizeWindow(window)
	o.mu.Lock()
	o.ingestWindowCount[window]++
	o.mu.Unlock()
}

func (o *Orchestrator) decWindowIngestAndMaybeFib(window string) {
	window = normalizeWindow(window)
	o.mu.Lock()
	if o.ingestWindowCount[window] > 0 {
		o.ingestWindowCount[window]--
	}
	idle := o.ingestWindowCount[window] == 0
	o.mu.Unlock()
	if idle {
		o.scheduleFibRecompute(window)
	}
}

func (o *Orchestrator) windowIngestIdle(window string) bool {
	window = normalizeWindow(window)
	o.mu.Lock()
	n := o.ingestWindowCount[window]
	o.mu.Unlock()
	return n == 0
}

func (o *Orchestrator) ingestJobsTerminal(ids []string) bool {
	if len(ids) == 0 {
		return true
	}
	for _, id := range ids {
		j, ok := o.GetJob(id)
		if !ok {
			return false
		}
		if j.Status != JobDone && j.Status != JobError {
			return false
		}
	}
	return true
}

// EnqueueIngest 后台 ingest；同 window 全部 ingest 结束后才触发 FIB 重算。
func (o *Orchestrator) EnqueueIngest(window, vrf, neighbor, sourceIP string) (string, bool) {
	if o.ingestPeer == nil {
		return "", false
	}
	key := peerKey(window, vrf, neighbor, sourceIP)
	o.mu.Lock()
	if existing, ok := o.ingestInflight[key]; ok {
		o.mu.Unlock()
		return existing, false
	}
	j := o.newJob(JobKindIngest, window, vrf, neighbor, sourceIP)
	o.ingestInflight[key] = j.ID
	o.mu.Unlock()

	o.incWindowIngest(window)
	go o.runIngest(j.ID, window, vrf, neighbor, sourceIP, key)
	return j.ID, true
}

func (o *Orchestrator) runIngest(jobID, window, vrf, neighbor, sourceIP, key string) {
	defer func() {
		o.mu.Lock()
		delete(o.ingestInflight, key)
		o.mu.Unlock()
		o.decWindowIngestAndMaybeFib(window)
	}()

	o.setJob(jobID, func(j *Job) {
		j.Status = JobRunning
		j.StartedAt = time.Now()
		j.Message = "ingesting from ADJ-IN"
	})

	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Hour)
	defer cancel()

	ingested, removed, err := o.ingestPeer(ctx, window, vrf, neighbor, sourceIP)
	if err != nil {
		o.setJob(jobID, func(j *Job) {
			j.Status = JobError
			j.Message = err.Error()
			j.Ingested = ingested
			j.Removed = removed
			j.FinishedAt = time.Now()
		})
		log.Printf("pipeline ingest %s: %v", jobID, err)
		return
	}

	o.setJob(jobID, func(j *Job) {
		j.Ingested = ingested
		j.Removed = removed
		j.Status = JobDone
		j.Message = "ingest complete"
		j.FinishedAt = time.Now()
	})
	log.Printf("pipeline ingest %s done: ingested=%d removed=%d", jobID, ingested, removed)
}

// startFibJob 启动 FIB job；若已有 job 在跑则标记 fibPending 并返回现有 job ID。
func (o *Orchestrator) startFibJob(window string) string {
	if o.fibEng == nil {
		return ""
	}
	window = normalizeWindow(window)
	o.mu.Lock()
	if existing, ok := o.fibInflight[window]; ok {
		o.fibPending[window] = true
		o.mu.Unlock()
		return existing
	}
	j := o.newJob(JobKindFib, window, "", "", "")
	o.fibInflight[window] = j.ID
	id := j.ID
	o.mu.Unlock()
	go o.runFibRecompute(id, window)
	return id
}

// scheduleFibRecompute 请求 FIB 全量重算（运行中则排队下一轮）。
func (o *Orchestrator) scheduleFibRecompute(window string) string {
	return o.startFibJob(window)
}

// EnqueueFibRecompute 异步全 window FIB 重算；started=false 表示并入已有 job 或排队下一轮。
func (o *Orchestrator) EnqueueFibRecompute(window string) (string, bool) {
	window = normalizeWindow(window)
	o.mu.Lock()
	_, inflight := o.fibInflight[window]
	o.mu.Unlock()
	id := o.scheduleFibRecompute(window)
	return id, id != "" && !inflight
}

func (o *Orchestrator) runFibRecompute(jobID, window string) {
	o.setJob(jobID, func(j *Job) {
		j.Status = JobRunning
		j.StartedAt = time.Now()
		j.Message = "recomputing FIB from RIB"
	})

	ctx, cancel := context.WithTimeout(context.Background(), 4*time.Hour)
	defer cancel()

	err := o.fibEng.RecomputeAll(ctx, window, func(processed, total int64) {
		o.setJob(jobID, func(j *Job) {
			j.Processed = processed
			j.Total = total
			if processed == 0 && total > 0 {
				j.Message = "collecting prefixes from RIB"
			} else if processed > 0 {
				j.Message = "recomputing FIB from RIB"
			}
		})
	})
	if err != nil {
		o.mu.Lock()
		delete(o.fibInflight, window)
		delete(o.fibPending, window)
		o.mu.Unlock()
		o.setJob(jobID, func(j *Job) {
			j.Status = JobError
			j.Message = err.Error()
			j.FinishedAt = time.Now()
		})
		log.Printf("pipeline fib %s: %v", jobID, err)
		return
	}

	o.setJob(jobID, func(j *Job) {
		j.Status = JobDone
		j.Message = "fib recompute complete"
		j.FinishedAt = time.Now()
	})
	log.Printf("pipeline fib %s done window=%s", jobID, window)

	if o.export != nil {
		o.export.Reconcile(ctx)
	}
	if o.kernel != nil {
		o.EnqueueKernelReconcile(window)
	}

	o.mu.Lock()
	delete(o.fibInflight, window)
	rerun := o.fibPending[window]
	delete(o.fibPending, window)
	o.mu.Unlock()

	if rerun {
		log.Printf("pipeline fib %s window=%s scheduling pending rerun", jobID, window)
		o.startFibJob(window)
	}
}

// EnqueueKernelReconcile 将持久 FIB 流式装入内核策略表（2110/2111 等）。
func (o *Orchestrator) EnqueueKernelReconcile(window string) (jobID string, started bool) {
	window = normalizeWindow(window)
	if o.kernel == nil || o.fibEng == nil {
		return "", false
	}
	o.mu.Lock()
	if id, ok := o.kernelInflight[window]; ok {
		o.mu.Unlock()
		return id, false
	}
	j := o.newJob(JobKindKernel, window, "", "", "")
	o.kernelInflight[window] = j.ID
	o.mu.Unlock()
	go o.runKernelReconcile(j.ID, window)
	return j.ID, true
}

func (o *Orchestrator) runKernelReconcile(jobID, window string) {
	defer func() {
		o.mu.Lock()
		delete(o.kernelInflight, window)
		o.mu.Unlock()
	}()
	o.setJob(jobID, func(j *Job) {
		j.Status = JobRunning
		j.StartedAt = time.Now()
		j.Message = "installing FIB into kernel routing table"
	})
	ctx := context.Background()
	o.kernel.ReconcileFromFib(ctx, window, o.fibEng)
	o.setJob(jobID, func(j *Job) {
		j.Status = JobDone
		j.Message = "kernel reconcile complete"
		j.FinishedAt = time.Now()
	})
	log.Printf("pipeline kernel %s done window=%s", jobID, window)
}

// RepairWindow 检测 drift：必要时 ingest peers，然后 FIB recompute。
func (o *Orchestrator) RepairWindow(ctx context.Context, window string) (string, bool) {
	window = normalizeWindow(window)
	if o.listPeers == nil {
		id, ok := o.EnqueueFibRecompute(window)
		j := o.wrapRepairJob(id, window)
		return j, ok
	}
	peers := o.listPeers(ctx)
	gapMin := ribGapMin()
	if window == storage.WindowDownstream {
		for i, p := range peers {
			if normalizeWindow(p.Window) != window || !p.Established {
				continue
			}
			_, _ = o.store.MigrateLegacyDownstreamPeerRIB(ctx, p.VRF, p.NeighborIP, p.SourceIP)
			n, _ := o.store.CountPeerRoutes(ctx, p.Window, p.VRF, p.NeighborIP, p.SourceIP)
			peers[i].RibCount = n
		}
	}
	var needIngest []PeerSnapshot
	for _, p := range peers {
		if normalizeWindow(p.Window) != window {
			continue
		}
		if !p.Established {
			continue
		}
		if p.PfxRcd == 0 {
			continue
		}
		if NeedsPeerIngest(p.Window, p.PfxRcd, p.RibCount, gapMin) {
			needIngest = append(needIngest, p)
		}
	}

	o.mu.Lock()
	j := o.newJob(JobKindRepair, window, "", "", "")
	j.Status = JobRunning
	j.StartedAt = time.Now()
	if len(needIngest) == 0 {
		j.Message = "rib aligned, scheduling fib recompute"
	} else {
		j.Message = fmt.Sprintf("scheduling ingest for %d peer(s)", len(needIngest))
	}
	repairID := j.ID
	o.mu.Unlock()

	if len(needIngest) > 0 {
		ingestIDs := make([]string, 0, len(needIngest))
		for _, p := range needIngest {
			id, _ := o.EnqueueIngest(p.Window, p.VRF, p.NeighborIP, p.SourceIP)
			if id != "" {
				ingestIDs = append(ingestIDs, id)
			}
		}
		go o.waitIngestsThenFib(repairID, window, ingestIDs)
	} else {
		fibID, _ := o.EnqueueFibRecompute(window)
		o.setJob(repairID, func(j *Job) {
			j.Message = "fib job " + fibID
		})
		go o.waitFibUntilSettled(repairID, window, fibID)
	}
	return repairID, true
}

func (o *Orchestrator) wrapRepairJob(childID, window string) string {
	o.mu.Lock()
	j := o.newJob(JobKindRepair, window, "", "", "")
	j.Status = JobRunning
	j.StartedAt = time.Now()
	j.Message = "delegated to " + childID
	id := j.ID
	o.mu.Unlock()
	go o.waitFibUntilSettled(id, window, childID)
	return id
}

func (o *Orchestrator) waitIngestsThenFib(repairID, window string, ingestJobIDs []string) {
	deadline := time.Now().Add(2 * time.Hour)
	for time.Now().Before(deadline) {
		if o.ingestJobsTerminal(ingestJobIDs) && o.windowIngestIdle(window) {
			break
		}
		time.Sleep(2 * time.Second)
	}
	if !o.ingestJobsTerminal(ingestJobIDs) || !o.windowIngestIdle(window) {
		o.setJob(repairID, func(r *Job) {
			r.Status = JobError
			r.Message = "repair timeout waiting for ingest completion"
			r.FinishedAt = time.Now()
		})
		return
	}
	fibID := o.scheduleFibRecompute(window)
	o.setJob(repairID, func(j *Job) {
		j.Message = "fib job " + fibID
	})
	o.waitFibUntilSettled(repairID, window, fibID)
}

func (o *Orchestrator) waitFibUntilSettled(repairID, window, fibJobID string) {
	deadline := time.Now().Add(4 * time.Hour)
	currentID := fibJobID
	for time.Now().Before(deadline) {
		if currentID == "" {
			o.mu.Lock()
			currentID = o.fibInflight[window]
			o.mu.Unlock()
			if currentID == "" {
				time.Sleep(time.Second)
				continue
			}
		}
		j, ok := o.GetJob(currentID)
		if !ok {
			time.Sleep(time.Second)
			continue
		}
		if j.Status == JobError {
			o.setJob(repairID, func(r *Job) {
				r.Status = JobError
				r.Message = j.Message
				r.FinishedAt = time.Now()
			})
			return
		}
		if j.Status == JobDone {
			o.mu.Lock()
			pending := o.fibPending[window]
			nextID := o.fibInflight[window]
			o.mu.Unlock()
			if pending || (nextID != "" && nextID != currentID) {
				if nextID != "" {
					currentID = nextID
				} else {
					currentID = ""
				}
				time.Sleep(time.Second)
				continue
			}
			o.setJob(repairID, func(r *Job) {
				r.Status = JobDone
				r.Message = "repair complete via " + currentID
				r.FinishedAt = time.Now()
			})
			return
		}
		time.Sleep(2 * time.Second)
	}
	o.setJob(repairID, func(r *Job) {
		r.Status = JobError
		r.Message = "repair timeout waiting for fib job"
		r.FinishedAt = time.Now()
	})
}

// ConsistencySnapshot RIB vs pfx_rcd vs FIB 快照。
func (o *Orchestrator) ConsistencySnapshot(ctx context.Context) map[string]interface{} {
	out := map[string]interface{}{
		"peers": []map[string]interface{}{},
	}
	var peers []PeerSnapshot
	if o.listPeers != nil {
		peers = o.listPeers(ctx)
	}
	peerRows := make([]map[string]interface{}, 0, len(peers))
	gapMin := ribGapMin()
	for _, p := range peers {
		drift := uint64(0)
		if uint64(p.PfxRcd) > uint64(p.RibCount) {
			drift = uint64(p.PfxRcd) - uint64(p.RibCount)
		}
		peerRows = append(peerRows, map[string]interface{}{
			"window":       p.Window,
			"vrf":          p.VRF,
			"neighbor_ip":  p.NeighborIP,
			"source_ip":    p.SourceIP,
			"pfx_rcd":      p.PfxRcd,
			"rib_count":    p.RibCount,
			"drift":        drift,
			"needs_ingest": p.Established && NeedsPeerIngest(p.Window, p.PfxRcd, p.RibCount, gapMin),
			"established":  p.Established,
		})
	}
	out["peers"] = peerRows

	fibCounts := map[string]int64{}
	for _, w := range []string{fib.WindowUpstream, fib.WindowDownstream} {
		if o.fibEng != nil {
			n, _ := o.fibEng.Store().Count(ctx, w)
			fibCounts[w] = n
		}
	}
	out["fib_count"] = fibCounts

	o.mu.Lock()
	inflight := map[string]string{}
	for k, v := range o.ingestInflight {
		inflight["ingest:"+k] = v
	}
	for k, v := range o.fibInflight {
		inflight["fib:"+k] = v
	}
	pendingFib := make([]string, 0)
	for w, v := range o.fibPending {
		if v {
			pendingFib = append(pendingFib, w)
		}
	}
	o.mu.Unlock()
	out["jobs_inflight"] = inflight
	out["fib_pending"] = pendingFib
	out["rib_gap_min"] = gapMin
	return out
}

func normalizeWindow(w string) string {
	w = strings.TrimSpace(strings.ToLower(w))
	if w == fib.WindowDownstream {
		return fib.WindowDownstream
	}
	return fib.WindowUpstream
}
