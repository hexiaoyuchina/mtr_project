package processor

import (
	"context"
	"log"
	"strings"
	"time"

	"bgp_agent/pkg/storage"
)

// RibChangeHook RIB 变更后通知 FIB 引擎（可选）。
type RibChangeHook interface {
	OnPeerRouteUpsert(window, vrf, neighbor, sourceIP, prefix string)
	OnPeerRouteDelete(window, vrf, neighbor, sourceIP, prefix string)
	OnPeerPurge(window, vrf, neighbor, sourceIP string)
}

// HandlePeerUpdate 按 peer 写入 RIB（enabled 邻居默认入库）。
func (p *Processor) HandlePeerUpdate(
	ctx context.Context,
	window, vrf, neighbor, sourceIP, prefix, nexthop, aspath string,
	asn uint32,
) error {
	if window == WindowUpstream {
		if !p.IsUpstreamPeerConnected(neighbor) {
			return nil
		}
	} else if window == WindowDownstream {
		if !p.IsDownstreamPeerConnected(vrf, neighbor) {
			return nil
		}
	}

	store, ok := p.storage.(*storage.Storage)
	if !ok {
		return p.HandleUpdate(ctx, prefix, nexthop, aspath, asn)
	}
	if !store.ShouldStoreRoutes(ctx, vrf, neighbor) {
		return nil
	}

	rt := storage.PeerRoute{
		Window:     window,
		VRF:        vrf,
		NeighborIP: neighbor,
		SourceIP:   sourceIP,
		Prefix:     prefix,
		Nexthop:    nexthop,
		ASPath:     aspath,
		RemoteAS:   asn,
		UpdatedAt:  time.Now(),
	}
	_, err := store.UpsertPeerRoute(ctx, rt)
	if err == nil && p.ribHook != nil {
		p.ribHook.OnPeerRouteUpsert(window, vrf, neighbor, sourceIP, prefix)
	}
	return err
}

// HandlePeerWithdraw 撤销单条；断链/teardown 期间忽略 withdraw（保库）。
func (p *Processor) HandlePeerWithdraw(ctx context.Context, window, vrf, neighbor, sourceIP, prefix string) error {
	if window == WindowUpstream {
		if !p.IsUpstreamPeerConnected(neighbor) {
			return nil
		}
	} else if window == WindowDownstream {
		if !p.IsDownstreamPeerConnected(vrf, neighbor) {
			return nil
		}
	}

	store, ok := p.storage.(*storage.Storage)
	if !ok {
		return p.HandleWithdraw(ctx, prefix)
	}
	if !store.ShouldStoreRoutes(ctx, vrf, neighbor) {
		return nil
	}
	if err := store.DeletePeerRoute(ctx, window, vrf, neighbor, sourceIP, prefix); err != nil {
		return err
	}
	if p.ribHook != nil {
		p.ribHook.OnPeerRouteDelete(window, vrf, neighbor, sourceIP, prefix)
	}
	return nil
}

// CountPeerRoutes 返回 Agent 持久库中该 peer 路由条数。
func (p *Processor) CountPeerRoutes(ctx context.Context, window, vrf, neighbor, sourceIP string) int64 {
	store, ok := p.storage.(*storage.Storage)
	if !ok {
		return int64(p.GetRouteCount())
	}
	n, err := store.CountPeerRoutes(ctx, window, vrf, neighbor, sourceIP)
	if err != nil {
		return 0
	}
	return n
}

// SyncPeerPolicy 由 OP 下发入库策略。
func (p *Processor) SyncPeerPolicy(ctx context.Context, pol storage.PeerPolicy) error {
	store, ok := p.storage.(*storage.Storage)
	if !ok {
		return nil
	}
	if pol.StoreRoutes || pol.Enabled {
		pol.StoreRoutes = true
	}
	storage.NormalizePeerPolicy(&pol, pol.VRF, pol.NeighborIP)
	if err := store.SetPeerPolicy(ctx, pol); err != nil {
		return err
	}
	log.Printf("peer policy vrf=%s neighbor=%s store=%v enabled=%v source=%s",
		pol.VRF, pol.NeighborIP, pol.StoreRoutes, pol.Enabled, pol.SourceIP)
	return nil
}

// EnsurePeerPolicyEnabled 建邻/启用时默认开启入库。
func (p *Processor) EnsurePeerPolicyEnabled(ctx context.Context, window, vrf, neighbor, sourceIP string) error {
	store, ok := p.storage.(*storage.Storage)
	if !ok {
		return nil
	}
	pol, err := store.GetPeerPolicy(ctx, vrf, neighbor)
	if err != nil {
		return err
	}
	pol.Window = window
	pol.VRF = vrf
	pol.NeighborIP = neighbor
	if sip := strings.TrimSpace(sourceIP); sip != "" {
		pol.SourceIP = sip
	}
	pol.StoreRoutes = true
	pol.Enabled = true
	return store.SetPeerPolicy(ctx, pol)
}

// SetPeerEnabled 启停邻居时同步 Agent 入库/参与策略（不断链 purge RIB）。
func (p *Processor) SetPeerEnabled(ctx context.Context, window, vrf, neighbor, sourceIP string, enabled bool) error {
	store, ok := p.storage.(*storage.Storage)
	if !ok {
		return nil
	}
	pol, err := store.GetPeerPolicy(ctx, vrf, neighbor)
	if err != nil {
		return err
	}
	pol.Window = window
	pol.VRF = vrf
	pol.NeighborIP = neighbor
	if sip := strings.TrimSpace(sourceIP); sip != "" {
		pol.SourceIP = sip
	}
	pol.Enabled = enabled
	pol.StoreRoutes = enabled
	if !enabled && window == WindowUpstream {
		t := true
		pol.ParticipateFib = &t
	}
	return store.SetPeerPolicy(ctx, pol)
}

// PurgePeerRIB 管理删除邻居时清空该 peer RIB。
func (p *Processor) PurgePeerRIB(ctx context.Context, window, vrf, neighbor, sourceIP string) (int, error) {
	store, ok := p.storage.(*storage.Storage)
	if !ok {
		return 0, nil
	}
	n, err := store.PurgePeerRoutes(ctx, window, vrf, neighbor, sourceIP)
	if err == nil && p.ribHook != nil {
		p.ribHook.OnPeerPurge(window, vrf, neighbor, sourceIP)
	}
	return n, err
}
