package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"strings"
	"sync"
	"time"

	"bgp_agent/pkg/rx"
	"bgp_agent/pkg/storage"
	"bgp_agent/pkg/tx"
)

type ribAdvertiseReq struct {
	TaskID          string `json:"task_id"`
	SrcWindow       string `json:"src_window"`
	SrcVRF          string `json:"src_vrf"`
	SrcNeighborIP   string `json:"src_neighbor_ip"`
	Target          string `json:"target"` // tx | rr
	TargetVRF       string `json:"target_vrf"`
	TargetNeighbor  string `json:"target_neighbor_ip"`
	Enable          bool   `json:"enable"`
	BatchSize       int    `json:"batch_size"`
}

type ribAdvertiseJob struct {
	TaskID      string
	Status      string
	Progress    int
	TotalRoutes int64
	Processed   int64
	Added       int64
	Failed      int64
	Message     string
	mu          sync.Mutex
}

func (s *APIServer) initRibJobs() {
	if s.ribJobs == nil {
		s.ribJobs = make(map[string]*ribAdvertiseJob)
	}
}

func (s *APIServer) handleRibAdvertise(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}
	var req ribAdvertiseReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	req.Enable = true
	s.startRibAdvertiseJob(w, r, req)
}

func (s *APIServer) handleRibWithdraw(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}
	var req ribAdvertiseReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	req.Enable = false
	s.startRibAdvertiseJob(w, r, req)
}

func (s *APIServer) handleRibAdvertiseStatus(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}
	taskID := strings.TrimSpace(r.URL.Query().Get("task_id"))
	if taskID == "" {
		http.Error(w, "task_id required", http.StatusBadRequest)
		return
	}
	s.ribJobsMu.Lock()
	job, ok := s.ribJobs[taskID]
	s.ribJobsMu.Unlock()
	if !ok {
		http.Error(w, "task not found", http.StatusNotFound)
		return
	}
	s.writeJSON(w, job.snapshot())
}

func (s *APIServer) startRibAdvertiseJob(w http.ResponseWriter, r *http.Request, req ribAdvertiseReq) {
	s.initRibJobs()
	taskID := strings.TrimSpace(req.TaskID)
	if taskID == "" {
		http.Error(w, "task_id required", http.StatusBadRequest)
		return
	}
	srcWindow := strings.TrimSpace(req.SrcWindow)
	srcVRF := strings.TrimSpace(req.SrcVRF)
	srcNIP := strings.TrimSpace(req.SrcNeighborIP)
	if srcWindow == "" || srcVRF == "" || srcNIP == "" {
		http.Error(w, "src_window, src_vrf, src_neighbor_ip required", http.StatusBadRequest)
		return
	}
	target := strings.ToLower(strings.TrimSpace(req.Target))
	if target == "" {
		target = "tx"
	}
	if target != "tx" && target != "rr" {
		http.Error(w, "target must be tx or rr", http.StatusBadRequest)
		return
	}
	targetVRF := strings.TrimSpace(req.TargetVRF)
	if target == "tx" && targetVRF == "" {
		http.Error(w, "target_vrf required for tx", http.StatusBadRequest)
		return
	}
	batchSize := req.BatchSize
	if batchSize <= 0 {
		batchSize = 5000
	}
	if batchSize > 10000 {
		batchSize = 10000
	}

	s.ribJobsMu.Lock()
	if existing, ok := s.ribJobs[taskID]; ok && existing.Status == "running" {
		s.ribJobsMu.Unlock()
		http.Error(w, "task already running", http.StatusConflict)
		return
	}
	job := &ribAdvertiseJob{
		TaskID:  taskID,
		Status:  "running",
		Message: "starting",
	}
	s.ribJobs[taskID] = job
	s.ribJobsMu.Unlock()

	go s.runRibAdvertiseJob(context.Background(), job, srcWindow, srcVRF, srcNIP, target, targetVRF, req.Enable, batchSize)

	s.writeJSON(w, map[string]interface{}{
		"ok":      true,
		"task_id": taskID,
		"status":  "running",
	})
}

func (j *ribAdvertiseJob) snapshot() map[string]interface{} {
	j.mu.Lock()
	defer j.mu.Unlock()
	return map[string]interface{}{
		"task_id":      j.TaskID,
		"status":       j.Status,
		"progress":     j.Progress,
		"total_routes": j.TotalRoutes,
		"processed":    j.Processed,
		"added":        j.Added,
		"failed":       j.Failed,
		"message":      j.Message,
	}
}

func (j *ribAdvertiseJob) setProgress(processed, total int64, added, failed int64, msg string) {
	j.mu.Lock()
	defer j.mu.Unlock()
	j.Processed = processed
	j.TotalRoutes = total
	j.Added = added
	j.Failed = failed
	j.Message = msg
	if total > 0 {
		p := int(processed * 100 / total)
		if p > 99 && j.Status == "running" {
			p = 99
		}
		j.Progress = p
	}
}

func (j *ribAdvertiseJob) finish(status string, msg string) {
	j.mu.Lock()
	defer j.mu.Unlock()
	j.Status = status
	j.Message = msg
	if status == "completed" {
		j.Progress = 100
	}
}

func (s *APIServer) runRibAdvertiseJob(
	ctx context.Context,
	job *ribAdvertiseJob,
	srcWindow, srcVRF, srcNIP, target, targetVRF string,
	enable bool,
	batchSize int,
) {
	defer func() {
		if rec := recover(); rec != nil {
			log.Printf("rib advertise job panic: %v", rec)
			job.finish("error", fmt.Sprintf("panic: %v", rec))
		}
	}()

	total, err := s.storage.CountPeerRoutes(ctx, srcWindow, srcVRF, srcNIP)
	if err != nil {
		job.finish("error", err.Error())
		return
	}
	if total == 0 {
		job.finish("completed", "no routes in peer rib")
		return
	}

	defaultNH := s.rxAgent.ConfigRouterID()
	var processed, addedTotal, failedTotal int64
	action := "advertise"
	if !enable {
		action = "withdraw"
	}
	job.setProgress(0, total, 0, 0, fmt.Sprintf("%s 0/%d", action, total))

	err = s.storage.IteratePeerRoutes(srcWindow, srcVRF, srcNIP, batchSize, func(batch []storage.PeerRoute) error {
		if len(batch) == 0 {
			return nil
		}
		ops := make([]tx.RouteOp, 0, len(batch))
		rxOps := make([]rx.RouteOp, 0, len(batch))
		for _, rt := range batch {
			if strings.TrimSpace(rt.Prefix) == "" {
				continue
			}
			ops = append(ops, tx.RouteOp{Prefix: rt.Prefix, Nexthop: rt.Nexthop})
			rxOps = append(rxOps, rx.RouteOp{Prefix: rt.Prefix, Nexthop: rt.Nexthop})
		}
		var added, failed int
		var errs []string
		switch target {
		case "rr":
			added, failed, errs = s.rxAgent.ApplyIPv4Batch(ctx, rxOps, enable, defaultNH)
		default:
			added, failed, errs = s.txPool.ApplyRoutesBatch(ctx, targetVRF, ops, enable, defaultNH)
		}
		_ = errs
		processed += int64(len(ops))
		addedTotal += int64(added)
		failedTotal += int64(failed)
		job.setProgress(processed, total, addedTotal, failedTotal,
			fmt.Sprintf("%s %d/%d", action, processed, total))
		return nil
	})
	if err != nil {
		job.finish("error", err.Error())
		return
	}
	job.finish("completed", fmt.Sprintf("done %s added=%d failed=%d total=%d",
		action, addedTotal, failedTotal, total))
	log.Printf("rib job %s %s src=%s/%s target=%s/%s added=%d failed=%d total=%d",
		job.TaskID, action, srcVRF, srcNIP, target, targetVRF, addedTotal, failedTotal, total)

	// 保留已完成任务一段时间供轮询，由后续任务覆盖同 task_id
	time.AfterFunc(30*time.Minute, func() {
		s.ribJobsMu.Lock()
		if cur, ok := s.ribJobs[job.TaskID]; ok && cur == job && cur.Status != "running" {
			delete(s.ribJobs, job.TaskID)
		}
		s.ribJobsMu.Unlock()
	})
}
