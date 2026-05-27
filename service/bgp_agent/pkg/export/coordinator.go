package export

import (
	"context"
	"log"
	"strings"
	"sync"

	"bgp_agent/pkg/fib"
	"bgp_agent/pkg/processor"
	"bgp_agent/pkg/rx"
	"bgp_agent/pkg/storage"
	"bgp_agent/pkg/tx"
)

const batchSize = 5000
const bulkAdvertiseMin = 64

// Coordinator 订阅 FIB 变更，向 enabled 会话 diff 通告 + 内核安装。
type Coordinator struct {
	fibEngine *fib.Engine
	state     *State
	kernel    *KernelInstaller
	rxAgent   *rx.RxAgent
	txPool    *tx.Pool
	storage   *storage.Storage
	mu        sync.Mutex
}

func NewCoordinator(fibEngine *fib.Engine, store *storage.Storage, rxAgent *rx.RxAgent, txPool *tx.Pool) *Coordinator {
	c := &Coordinator{
		fibEngine: fibEngine,
		state:     NewState(store),
		kernel:    NewKernelInstaller(),
		rxAgent:   rxAgent,
		txPool:    txPool,
		storage:   store,
	}
	fibEngine.AddListener(c)
	return c
}

func (c *Coordinator) OnFibChange(window string, diff fib.FibDiff) {
	if len(diff.Adds) == 0 && len(diff.Withdraws) == 0 {
		return
	}
	ctx := context.Background()
	c.kernel.Apply(ctx, window, diff)
	switch window {
	case fib.WindowUpstream:
		c.applyUpstreamToDownstream(ctx, diff)
	case fib.WindowDownstream:
		c.applyDownstreamToRR(ctx, diff)
	}
}

func (c *Coordinator) applyUpstreamToDownstream(ctx context.Context, diff fib.FibDiff) {
	peers, err := c.txPool.ListAllPeers(ctx)
	if err != nil {
		return
	}
	seenVRF := make(map[string]struct{})
	for _, p := range peers {
		if !p.Enabled || !strings.Contains(strings.ToUpper(p.State), "ESTABLISHED") {
			continue
		}
		if !c.storage.ShouldStoreRoutes(ctx, p.Vrf, p.Address) {
			continue
		}
		// 同一 VRF 的 TxAgent 共用 LOCAL-RIB，AddPath 一次即可（避免重复通告）
		if _, ok := seenVRF[p.Vrf]; ok {
			targetID := c.state.TargetID(p.Vrf, p.Address, p.LocalAddress)
			c.markDiffStateOnly(ctx, "tx", targetID, diff)
			continue
		}
		seenVRF[p.Vrf] = struct{}{}
		targetID := c.state.TargetID(p.Vrf, p.Address, p.LocalAddress)
		c.applyToTX(ctx, p.Vrf, p.Address, targetID, diff)
	}
}

// markDiffStateOnly 同 VRF/RX 已 AddPath 后，仅同步各会话 export state（避免重复通告）。
func (c *Coordinator) markDiffStateOnly(ctx context.Context, targetType, targetID string, diff fib.FibDiff) {
	for _, pfx := range diff.Withdraws {
		_ = c.state.MarkWithdrawn(ctx, targetType, targetID, pfx)
	}
	for _, rt := range diff.Adds {
		_ = c.state.MarkAdvertised(ctx, targetType, targetID, rt.Prefix)
	}
}

func (c *Coordinator) applyDownstreamToRR(ctx context.Context, diff fib.FibDiff) {
	rrPeers, err := c.rxAgent.ListRRPeers(ctx)
	if err != nil {
		return
	}
	defaultNH := c.rxAgent.ConfigRouterID()
	var applied bool
	for _, p := range rrPeers {
		if !p.Enabled || !strings.Contains(strings.ToUpper(p.State), "ESTABLISHED") {
			continue
		}
		targetID := c.state.TargetID(processor.VRFGobgpRR, p.Address, "")
		if !applied {
			c.applyToRR(ctx, targetID, diff, defaultNH)
			applied = true
			continue
		}
		c.markDiffStateOnly(ctx, "rr", targetID, diff)
	}
}

func (c *Coordinator) applyToTX(ctx context.Context, vrf, neighbor, targetID string, diff fib.FibDiff) {
	agent, err := c.txPool.GetAgent(vrf)
	if err != nil || agent == nil {
		return
	}
	defaultNH := agent.ConfigRouterID()
	var ops []tx.RouteOp
	for _, pfx := range diff.Withdraws {
		if c.state.IsAdvertised(ctx, "tx", targetID, pfx) {
			ops = append(ops, tx.RouteOp{Prefix: pfx})
		}
	}
	for i := 0; i < len(ops); i += batchSize {
		end := i + batchSize
		if end > len(ops) {
			end = len(ops)
		}
		batch := ops[i:end]
		agent.ApplyRoutesBatch(ctx, batch, false, defaultNH)
		for _, op := range batch {
			_ = c.state.MarkWithdrawn(ctx, "tx", targetID, op.Prefix)
		}
	}
	ops = ops[:0]
	for _, rt := range diff.Adds {
		if len(diff.Adds) < bulkAdvertiseMin && c.state.IsAdvertised(ctx, "tx", targetID, rt.Prefix) {
			continue
		}
		ops = append(ops, tx.RouteOp{Prefix: rt.Prefix, Nexthop: rt.Nexthop})
	}
	for i := 0; i < len(ops); i += batchSize {
		end := i + batchSize
		if end > len(ops) {
			end = len(ops)
		}
		batch := ops[i:end]
		agent.ApplyRoutesBatch(ctx, batch, true, defaultNH)
		if len(diff.Adds) >= bulkAdvertiseMin {
			pfxs := make([]string, len(batch))
			for j, op := range batch {
				pfxs[j] = op.Prefix
			}
			_ = c.state.MarkAdvertisedBatch(ctx, "tx", targetID, pfxs)
		} else {
			for _, op := range batch {
				_ = c.state.MarkAdvertised(ctx, "tx", targetID, op.Prefix)
			}
		}
	}
}

func (c *Coordinator) applyToRR(ctx context.Context, targetID string, diff fib.FibDiff, defaultNH string) {
	var ops []rx.RouteOp
	for _, pfx := range diff.Withdraws {
		if c.state.IsAdvertised(ctx, "rr", targetID, pfx) {
			ops = append(ops, rx.RouteOp{Prefix: pfx})
		}
	}
	for i := 0; i < len(ops); i += batchSize {
		end := i + batchSize
		if end > len(ops) {
			end = len(ops)
		}
		batch := ops[i:end]
		c.rxAgent.ApplyIPv4Batch(ctx, batch, false, defaultNH)
		for _, op := range batch {
			_ = c.state.MarkWithdrawn(ctx, "rr", targetID, op.Prefix)
		}
	}
	ops = ops[:0]
	for _, rt := range diff.Adds {
		if c.state.IsAdvertised(ctx, "rr", targetID, rt.Prefix) {
			continue
		}
		// downstream FIB → RR：next-hop-self（本机 RouterID/uplink），不用下游原始 nexthop
		ops = append(ops, rx.RouteOp{Prefix: rt.Prefix, Nexthop: defaultNH})
	}
	for i := 0; i < len(ops); i += batchSize {
		end := i + batchSize
		if end > len(ops) {
			end = len(ops)
		}
		batch := ops[i:end]
		c.rxAgent.ApplyIPv4Batch(ctx, batch, true, defaultNH)
		for _, op := range batch {
			_ = c.state.MarkAdvertised(ctx, "rr", targetID, op.Prefix)
		}
	}
}

// Reconcile 启动/部署后对 enabled 会话做 FIB ↔ export state 双向 diff（含 withdraw 陈旧前缀）。
func (c *Coordinator) Reconcile(ctx context.Context) {
	c.reconcileAll(ctx, false)
}

// ReconcileForce 清空 TX export state 后全量重通告（会话重建/export state 漂移时用）。
func (c *Coordinator) ReconcileForce(ctx context.Context) {
	c.reconcileAll(ctx, true)
}

func (c *Coordinator) reconcileAll(ctx context.Context, force bool) {
	if force {
		log.Printf("export reconcile force starting")
	} else {
		log.Printf("export reconcile starting")
	}
	c.reconcileUpstreamToDownstreamStream(ctx, force)
	fibMap := c.loadFibMap(fib.WindowDownstream)
	c.reconcileDownstreamToRR(ctx, fibMap)
	log.Printf("export reconcile done")
}

func (c *Coordinator) reconcileUpstreamToDownstreamStream(ctx context.Context, force bool) {
	peers, err := c.txPool.ListAllPeers(ctx)
	if err != nil {
		return
	}
	seenVRF := make(map[string]struct{})
	var primaryVRF, primaryNeighbor, primaryTarget string
	var primaryAgent *tx.TxAgent
	var primaryNH string
	for _, p := range peers {
		if !p.Enabled || !strings.Contains(strings.ToUpper(p.State), "ESTABLISHED") {
			continue
		}
		if !c.storage.ShouldStoreRoutes(ctx, p.Vrf, p.Address) {
			continue
		}
		if _, ok := seenVRF[p.Vrf]; ok {
			continue
		}
		agent, err := c.txPool.GetAgent(p.Vrf)
		if err != nil || agent == nil {
			continue
		}
		seenVRF[p.Vrf] = struct{}{}
		primaryVRF = p.Vrf
		primaryNeighbor = p.Address
		primaryTarget = c.state.TargetID(p.Vrf, p.Address, p.LocalAddress)
		primaryAgent = agent
		primaryNH = agent.ConfigRouterID()
		break
	}
	if primaryAgent == nil {
		return
	}
	if force && primaryAgent.IsFrozen() {
		log.Printf("export reconcile force deferred: TX frozen vrf=%s neighbor=%s", primaryVRF, primaryNeighbor)
		return
	}

	var adv map[string]struct{}
	if force {
		go func() { _ = c.state.ClearTarget(context.Background(), "tx", primaryTarget) }()
		adv = make(map[string]struct{})
	} else {
		adv, _ = c.state.ListAdvertisedPrefixes(ctx, "tx", primaryTarget)
	}
	fibSet := make(map[string]struct{})
	var addedTotal int

	_ = c.fibEngine.Store().Iterate(fib.WindowUpstream, batchSize, func(batch []fib.FibRoute) error {
		var ops []tx.RouteOp
		var pfxs []string
		for _, rt := range batch {
			fibSet[rt.Prefix] = struct{}{}
			if _, ok := adv[rt.Prefix]; ok {
				continue
			}
			ops = append(ops, tx.RouteOp{Prefix: rt.Prefix, Nexthop: rt.Nexthop})
			pfxs = append(pfxs, rt.Prefix)
		}
		for i := 0; i < len(ops); i += batchSize {
			end := i + batchSize
			if end > len(ops) {
				end = len(ops)
			}
			sub := ops[i:end]
			subPfxs := pfxs[i:end]
			n, _, _ := primaryAgent.ApplyRoutesBatch(ctx, sub, true, primaryNH)
			if n <= 0 {
				continue
			}
			addedTotal += n
			mark := subPfxs
			if n < len(subPfxs) {
				mark = subPfxs[:n]
			}
			for _, pfx := range mark {
				adv[pfx] = struct{}{}
			}
			_ = c.state.MarkAdvertisedBatch(ctx, "tx", primaryTarget, mark)
		}
		return nil
	})

	var withdraws []string
	for pfx := range adv {
		if _, ok := fibSet[pfx]; !ok {
			withdraws = append(withdraws, pfx)
		}
	}
	if len(withdraws) > 0 {
		diff := fib.FibDiff{Withdraws: withdraws}
		c.applyToTX(ctx, primaryVRF, primaryNeighbor, primaryTarget, diff)
	}

	for _, p := range peers {
		if !p.Enabled || !strings.Contains(strings.ToUpper(p.State), "ESTABLISHED") {
			continue
		}
		if !c.storage.ShouldStoreRoutes(ctx, p.Vrf, p.Address) {
			continue
		}
		if p.Vrf != primaryVRF || p.Address == primaryNeighbor {
			continue
		}
		targetID := c.state.TargetID(p.Vrf, p.Address, p.LocalAddress)
		for pfx := range adv {
			_ = c.state.MarkAdvertised(ctx, "tx", targetID, pfx)
		}
	}
	if addedTotal > 0 {
		log.Printf("export upstream stream added=%d target=%s", addedTotal, primaryTarget)
	} else if force && len(fibSet) > 0 {
		log.Printf("export reconcile force warning: 0 added fib=%d frozen=%v target=%s",
			len(fibSet), primaryAgent.IsFrozen(), primaryTarget)
	}
	_ = c.state.SetAdvertisedCount(ctx, "tx", primaryTarget, len(adv))
}

func (c *Coordinator) loadFibMap(window string) map[string]fib.FibRoute {
	fibMap := make(map[string]fib.FibRoute)
	_ = c.fibEngine.Store().Iterate(window, 5000, func(batch []fib.FibRoute) error {
		for _, rt := range batch {
			fibMap[rt.Prefix] = rt
		}
		return nil
	})
	return fibMap
}

func (c *Coordinator) diffFibVsAdvertised(adv map[string]struct{}, fibMap map[string]fib.FibRoute) fib.FibDiff {
	var diff fib.FibDiff
	for pfx := range adv {
		if _, ok := fibMap[pfx]; !ok {
			diff.Withdraws = append(diff.Withdraws, pfx)
		}
	}
	for pfx, rt := range fibMap {
		if _, ok := adv[pfx]; !ok {
			diff.Adds = append(diff.Adds, rt)
		}
	}
	return diff
}

func (c *Coordinator) reconcileUpstreamToDownstream(ctx context.Context, fibMap map[string]fib.FibRoute) {
	peers, err := c.txPool.ListAllPeers(ctx)
	if err != nil {
		return
	}
	seenVRF := make(map[string]struct{})
	for _, p := range peers {
		if !p.Enabled || !strings.Contains(strings.ToUpper(p.State), "ESTABLISHED") {
			continue
		}
		if !c.storage.ShouldStoreRoutes(ctx, p.Vrf, p.Address) {
			continue
		}
		targetID := c.state.TargetID(p.Vrf, p.Address, p.LocalAddress)
		adv, _ := c.state.ListAdvertisedPrefixes(ctx, "tx", targetID)
		diff := c.diffFibVsAdvertised(adv, fibMap)
		if len(diff.Adds) == 0 && len(diff.Withdraws) == 0 {
			continue
		}
		if _, ok := seenVRF[p.Vrf]; ok {
			c.markDiffStateOnly(ctx, "tx", targetID, diff)
			continue
		}
		seenVRF[p.Vrf] = struct{}{}
		c.applyToTX(ctx, p.Vrf, p.Address, targetID, diff)
	}
}

func (c *Coordinator) reconcileDownstreamToRR(ctx context.Context, fibMap map[string]fib.FibRoute) {
	rrPeers, err := c.rxAgent.ListRRPeers(ctx)
	if err != nil {
		return
	}
	defaultNH := c.rxAgent.ConfigRouterID()
	var applied bool
	for _, p := range rrPeers {
		if !p.Enabled || !strings.Contains(strings.ToUpper(p.State), "ESTABLISHED") {
			continue
		}
		targetID := c.state.TargetID(processor.VRFGobgpRR, p.Address, "")
		adv, _ := c.state.ListAdvertisedPrefixes(ctx, "rr", targetID)
		diff := c.diffFibVsAdvertised(adv, fibMap)
		if len(diff.Adds) == 0 && len(diff.Withdraws) == 0 {
			continue
		}
		if !applied {
			c.applyToRR(ctx, targetID, diff, defaultNH)
			applied = true
			continue
		}
		c.markDiffStateOnly(ctx, "rr", targetID, diff)
	}
}

// WithdrawSession 停用邻居时撤销该会话已通告集。
func (c *Coordinator) WithdrawSession(ctx context.Context, targetType, targetID string, vrf, neighbor string) {
	adv, _ := c.state.ListAdvertisedPrefixes(ctx, targetType, targetID)
	if len(adv) == 0 {
		return
	}
	var withdraws []string
	for pfx := range adv {
		withdraws = append(withdraws, pfx)
	}
	diff := fib.FibDiff{Withdraws: withdraws}
	if targetType == "tx" {
		c.applyToTX(ctx, vrf, neighbor, targetID, diff)
	} else {
		c.applyToRR(ctx, targetID, diff, c.rxAgent.ConfigRouterID())
	}
}

// WithdrawTXPeer 撤销下游 TX 会话已通告前缀。
func (c *Coordinator) WithdrawTXPeer(ctx context.Context, vrf, neighbor, localAddress string) {
	targetID := c.state.TargetID(vrf, neighbor, localAddress)
	c.WithdrawSession(ctx, "tx", targetID, vrf, neighbor)
}

// WithdrawRRPeer 撤销向指定 RR 会话已通告的 downstream_fib 前缀。
func (c *Coordinator) WithdrawRRPeer(ctx context.Context, rrAddress string) {
	targetID := c.state.TargetID(processor.VRFGobgpRR, rrAddress, "")
	c.WithdrawSession(ctx, "rr", targetID, processor.VRFGobgpRR, rrAddress)
}
