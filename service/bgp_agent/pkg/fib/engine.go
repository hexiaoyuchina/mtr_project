package fib

import (
	"context"
	"log"
	"strings"
	"sync"
	"time"

	"bgp_agent/pkg/processor"
	"bgp_agent/pkg/storage"
)

// ChangeListener FIB 变更订阅者（export 模块）。
type ChangeListener interface {
	OnFibChange(window string, diff FibDiff)
}

// SourceIPResolver 下游 policy 缺 source_ip 时从 BGP 会话补全（SQLite 经 OP 下发前的兜底）。
type SourceIPResolver func(window, vrf, neighbor string) string

// ProgressFunc 批量 FIB 重算进度（processed/total prefix）。
type ProgressFunc func(processed, total int64)

// Engine prefix 级 debounce FIB 重算引擎。
type Engine struct {
	store            *Store
	raw              *storage.Storage
	participate      *ParticipateCtx
	resolveSourceIP  SourceIPResolver
	mu               sync.Mutex
	pending          map[string]map[string]struct{} // window -> prefixes
	debounce         time.Duration
	listeners        []ChangeListener
	bulkDepth        int // >0 时跳过 emit（全量重算末尾由 pipeline Reconcile）
}

func NewEngine(s *storage.Storage, proc *processor.Processor) *Engine {
	return &Engine{
		store:       NewStore(s),
		raw:         s,
		participate: NewParticipateCtx(proc),
		pending:     make(map[string]map[string]struct{}),
		debounce:    100 * time.Millisecond,
	}
}

func (e *Engine) AddListener(l ChangeListener) {
	e.mu.Lock()
	defer e.mu.Unlock()
	e.listeners = append(e.listeners, l)
}

func (e *Engine) SetSourceIPResolver(fn SourceIPResolver) {
	e.resolveSourceIP = fn
}

func (e *Engine) effectivePolicySourceIP(window string, p storage.PeerPolicy) string {
	sip := strings.TrimSpace(p.SourceIP)
	if window != WindowDownstream {
		return sip
	}
	if sip != "" {
		return sip
	}
	if e.resolveSourceIP != nil {
		return strings.TrimSpace(e.resolveSourceIP(window, p.VRF, p.NeighborIP))
	}
	return sip
}

func (e *Engine) NotifyPrefix(window, prefix string) {
	window = normalizeWindow(window)
	prefix = strings.TrimSpace(prefix)
	if window == "" || prefix == "" {
		return
	}
	e.mu.Lock()
	if e.pending[window] == nil {
		e.pending[window] = make(map[string]struct{})
	}
	e.pending[window][prefix] = struct{}{}
	e.mu.Unlock()
}

func (e *Engine) Start(ctx context.Context) {
	go e.debounceLoop(ctx)
}

func (e *Engine) debounceLoop(ctx context.Context) {
	ticker := time.NewTicker(e.debounce)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			e.flushPending(ctx)
		}
	}
}

func (e *Engine) flushPending(ctx context.Context) {
	e.mu.Lock()
	batch := e.pending
	e.pending = make(map[string]map[string]struct{})
	e.mu.Unlock()
	for window, prefixes := range batch {
		pols, _ := e.activePolicies(ctx, window)
		for prefix := range prefixes {
			if err := e.recomputePrefixWithPolicies(ctx, window, prefix, pols); err != nil {
				log.Printf("fib recompute %s %s: %v", window, prefix, err)
			}
		}
	}
}

func (e *Engine) RecomputeForPeer(ctx context.Context, window, vrf, neighbor, sourceIP string) error {
	window = normalizeWindow(window)
	// 批量 peer 重算必须同步 recomputePrefix；仅用 NotifyPrefix+debounce 会在
	// 首次 flush 时 peer 未 Established / policy 未齐导致空候选，且 prefix 不会再次入队。
	return e.raw.IteratePeerRoutes(window, vrf, neighbor, sourceIP, 5000, func(batch []storage.PeerRoute) error {
		for _, rt := range batch {
			if err := e.recomputePrefix(ctx, window, rt.Prefix); err != nil {
				return err
			}
		}
		return nil
	})
}

func (e *Engine) RecomputeAll(ctx context.Context, window string, onProgress ProgressFunc) error {
	window = normalizeWindow(window)
	e.beginBulk()
	defer e.endBulk()

	pols, err := e.activePolicies(ctx, window)
	if err != nil {
		return err
	}

	candidates := make(map[string][]Candidate)
	var scanned int64
	for _, p := range pols {
		sip := e.effectivePolicySourceIP(window, p)
		err := e.raw.IteratePeerRoutes(window, p.VRF, p.NeighborIP, sip, 5000, func(batch []storage.PeerRoute) error {
			for _, rt := range batch {
				candidates[rt.Prefix] = append(candidates[rt.Prefix], candidateFromPeerRoute(rt))
			}
			scanned += int64(len(batch))
			if onProgress != nil && scanned%50000 == 0 {
				onProgress(0, int64(len(candidates)))
			}
			return nil
		})
		if err != nil {
			return err
		}
	}

	// 旧 FIB 里需 withdraw 的前缀也纳入（RIB 已删但 FIB 仍在）
	_ = e.store.Iterate(window, 5000, func(batch []FibRoute) error {
		for _, rt := range batch {
			if _, ok := candidates[rt.Prefix]; !ok {
				candidates[rt.Prefix] = nil
			}
		}
		return nil
	})

	total := int64(len(candidates))
	if onProgress != nil {
		onProgress(0, total)
	}

	var processed int64
	for pfx, cands := range candidates {
		if err := e.applyPrefixCandidates(ctx, window, pfx, cands); err != nil {
			return err
		}
		processed++
		if onProgress != nil && (processed%5000 == 0 || processed == total) {
			onProgress(processed, total)
		}
	}
	if onProgress != nil && total == 0 {
		onProgress(0, 0)
	}
	return nil
}

func (e *Engine) beginBulk() {
	e.mu.Lock()
	e.bulkDepth++
	e.mu.Unlock()
}

func (e *Engine) endBulk() {
	e.mu.Lock()
	if e.bulkDepth > 0 {
		e.bulkDepth--
	}
	e.mu.Unlock()
}

func (e *Engine) activePolicies(ctx context.Context, window string) ([]storage.PeerPolicy, error) {
	pols, err := e.raw.ListPeerPolicies(ctx)
	if err != nil {
		return nil, err
	}
	var active []storage.PeerPolicy
	for _, p := range pols {
		if !windowMatchesPolicy(window, p) || !storage.ParticipatesInFib(p) {
			continue
		}
		if window == WindowUpstream {
			if !e.participate.UpstreamParticipates(p.NeighborIP) {
				continue
			}
		} else if !e.participate.DownstreamParticipates(p.VRF, p.NeighborIP) {
			continue
		}
		active = append(active, p)
	}
	return active, nil
}

func candidateFromPeerRoute(rt storage.PeerRoute) Candidate {
	return Candidate{
		Prefix:     rt.Prefix,
		Nexthop:    rt.Nexthop,
		ASPath:     rt.ASPath,
		RemoteAS:   rt.RemoteAS,
		NeighborIP: rt.NeighborIP,
		SourceIP:   rt.SourceIP,
		VRF:        rt.VRF,
		ASPathLen:  ASPathLength(rt.ASPath),
	}
}

func (e *Engine) applyPrefixCandidates(ctx context.Context, window, prefix string, cands []Candidate) error {
	var best *Candidate
	if len(cands) > 0 {
		best = SelectBest(cands)
	}
	old, _ := e.store.Get(ctx, window, prefix)
	if best == nil {
		if old != nil {
			if err := e.store.Delete(ctx, window, prefix); err != nil {
				return err
			}
			e.emit(window, DiffSingle(old, nil))
		}
		return nil
	}
	newRt := FibRoute{
		Window:     window,
		Prefix:     prefix,
		Nexthop:    best.Nexthop,
		ASPath:     best.ASPath,
		RemoteAS:   best.RemoteAS,
		NeighborIP: best.NeighborIP,
		SourceIP:   best.SourceIP,
		VRF:        best.VRF,
		LocalPref:  best.LocalPref,
		Origin:     best.Origin,
		UpdatedAt:  time.Now(),
	}
	diff := DiffSingle(old, &newRt)
	if len(diff.Adds) == 0 && len(diff.Withdraws) == 0 {
		return nil
	}
	if err := e.store.Put(ctx, newRt); err != nil {
		return err
	}
	e.emit(window, diff)
	return nil
}

func (e *Engine) recomputePrefix(ctx context.Context, window, prefix string) error {
	pols, _ := e.activePolicies(ctx, window)
	return e.recomputePrefixWithPolicies(ctx, window, prefix, pols)
}

func (e *Engine) recomputePrefixWithPolicies(ctx context.Context, window, prefix string, pols []storage.PeerPolicy) error {
	cands := e.collectCandidatesFromPolicies(ctx, window, prefix, pols)
	return e.applyPrefixCandidates(ctx, window, prefix, cands)
}

func (e *Engine) collectCandidates(ctx context.Context, window, prefix string) []Candidate {
	pols, err := e.raw.ListPeerPolicies(ctx)
	if err != nil {
		return nil
	}
	return e.collectCandidatesFromPolicies(ctx, window, prefix, pols)
}

func (e *Engine) collectCandidatesFromPolicies(ctx context.Context, window, prefix string, pols []storage.PeerPolicy) []Candidate {
	var cands []Candidate
	for _, p := range pols {
		if !windowMatchesPolicy(window, p) || !storage.ParticipatesInFib(p) {
			continue
		}
		if window == WindowUpstream {
			if !e.participate.UpstreamParticipates(p.NeighborIP) {
				continue
			}
		} else {
			if !e.participate.DownstreamParticipates(p.VRF, p.NeighborIP) {
				continue
			}
		}
		rt, err := e.raw.GetPeerRoute(ctx, window, p.VRF, p.NeighborIP, e.effectivePolicySourceIP(window, p), prefix)
		if err != nil || rt == nil {
			continue
		}
		cands = append(cands, candidateFromPeerRoute(*rt))
	}
	return cands
}

func (e *Engine) emit(window string, diff FibDiff) {
	if len(diff.Adds) == 0 && len(diff.Withdraws) == 0 {
		return
	}
	e.mu.Lock()
	if e.bulkDepth > 0 {
		e.mu.Unlock()
		return
	}
	listeners := append([]ChangeListener(nil), e.listeners...)
	e.mu.Unlock()
	for _, l := range listeners {
		l.OnFibChange(window, diff)
	}
}

func (e *Engine) Store() *Store { return e.store }

func normalizeWindow(w string) string {
	w = strings.TrimSpace(strings.ToLower(w))
	if w == WindowDownstream {
		return WindowDownstream
	}
	return WindowUpstream
}

func windowMatchesPolicy(window string, p storage.PeerPolicy) bool {
	pw := strings.TrimSpace(strings.ToLower(p.Window))
	if pw == "" {
		if p.VRF == processor.VRFGobgpRR || strings.EqualFold(p.VRF, "rr") {
			pw = WindowUpstream
		} else {
			pw = WindowDownstream
		}
	}
	return pw == window
}

// Hook 实现 processor.RibChangeHook。
type Hook struct {
	e        *Engine
	onPurge  func(window string)
}

func NewHook(e *Engine) *Hook { return &Hook{e: e} }

// SetOnPurgeWindow purge peer RIB 后按 window 触发 FIB 全量重算（由 pipeline 注册）。
func (h *Hook) SetOnPurgeWindow(fn func(window string)) { h.onPurge = fn }

func (h *Hook) OnPeerRouteUpsert(window, vrf, neighbor, sourceIP, prefix string) {
	h.e.NotifyPrefix(window, prefix)
}

func (h *Hook) OnPeerRouteDelete(window, vrf, neighbor, sourceIP, prefix string) {
	h.e.NotifyPrefix(window, prefix)
}

func (h *Hook) OnPeerPurge(window, vrf, neighbor, sourceIP string) {
	if h.onPurge != nil {
		h.onPurge(normalizeWindow(window))
	}
}
