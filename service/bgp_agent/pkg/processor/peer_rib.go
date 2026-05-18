package processor

import (
	"context"
	"log"
	"time"

	"bgp_agent/pkg/storage"
)

const (
	WindowUpstream   = "upstream"
	WindowDownstream = "downstream"
	VRFGobgpRR       = "gobgp-rr"
)

// HandlePeerUpdate 按 peer 写入 RIB（受 store_routes 策略控制）。
func (p *Processor) HandlePeerUpdate(
	ctx context.Context,
	window, vrf, neighbor string,
	prefix, nexthop, aspath string,
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
		return p.handleLegacyUpdate(ctx, prefix, nexthop, aspath, asn)
	}
	if !store.ShouldStoreRoutes(ctx, vrf, neighbor) {
		return nil
	}

	rt := storage.PeerRoute{
		Window:     window,
		VRF:        vrf,
		NeighborIP: neighbor,
		Prefix:     prefix,
		Nexthop:    nexthop,
		ASPath:     aspath,
		RemoteAS:   asn,
		UpdatedAt:  time.Now(),
	}
	_, err := store.UpsertPeerRoute(ctx, rt)
	return err
}

// HandlePeerWithdraw 撤销单条；对端 withdraw 时始终删库（不受 freeze/断连门控）。
func (p *Processor) HandlePeerWithdraw(ctx context.Context, window, vrf, neighbor, prefix string) error {
	store, ok := p.storage.(*storage.Storage)
	if !ok {
		return p.HandleWithdraw(ctx, prefix)
	}
	if !store.ShouldStoreRoutes(ctx, vrf, neighbor) {
		return nil
	}
	return store.DeletePeerRoute(ctx, window, vrf, neighbor, prefix)
}

func (p *Processor) handleLegacyUpdate(ctx context.Context, prefix, nexthop, aspath string, asn uint32) error {
	return p.HandleUpdate(ctx, prefix, nexthop, aspath, asn)
}

// CountPeerRoutes 返回 Agent 持久库中该 peer 路由条数。
func (p *Processor) CountPeerRoutes(ctx context.Context, window, vrf, neighbor string) int64 {
	store, ok := p.storage.(*storage.Storage)
	if !ok {
		return int64(p.GetRouteCount())
	}
	n, err := store.CountPeerRoutes(ctx, window, vrf, neighbor)
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
	if err := store.SetPeerPolicy(ctx, pol); err != nil {
		return err
	}
	log.Printf("peer policy vrf=%s neighbor=%s store=%v", pol.VRF, pol.NeighborIP, pol.StoreRoutes)
	return nil
}
