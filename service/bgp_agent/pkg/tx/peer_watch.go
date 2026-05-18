package tx

import (
	"context"
	"log"

	"bgp_agent/pkg/gobgp_path"
	"bgp_agent/pkg/processor"

	api "github.com/osrg/gobgp/v3/api"
)

func (a *TxAgent) startPeerRouteWatch(parent context.Context, neighbor string, remoteAS uint32) {
	if a.handler == nil || a.server == nil {
		return
	}
	a.watchMu.Lock()
	if a.watchCancels == nil {
		a.watchCancels = make(map[string]context.CancelFunc)
	}
	if cancel, ok := a.watchCancels[neighbor]; ok {
		cancel()
	}
	ctx, cancel := context.WithCancel(parent)
	a.watchCancels[neighbor] = cancel
	a.watchMu.Unlock()

	go a.watchPeerAdjIn(ctx, neighbor, remoteAS)
}

func (a *TxAgent) stopPeerRouteWatch(neighbor string) {
	a.watchMu.Lock()
	defer a.watchMu.Unlock()
	if cancel, ok := a.watchCancels[neighbor]; ok {
		cancel()
		delete(a.watchCancels, neighbor)
	}
}

func (a *TxAgent) watchPeerAdjIn(ctx context.Context, neighbor string, remoteAS uint32) {
	window := processor.WindowDownstream
	vrf := a.vrf
	err := a.server.WatchEvent(ctx, &api.WatchEventRequest{
		Table: &api.WatchEventRequest_Table{
			Filters: []*api.WatchEventRequest_Table_Filter{
				{Type: api.WatchEventRequest_Table_Filter_ADJIN},
			},
		},
	}, func(response *api.WatchEventResponse) {
		if response.GetTable() == nil {
			return
		}
		for _, path := range response.GetTable().Paths {
			a.handleLearnedPath(ctx, window, vrf, neighbor, remoteAS, path)
		}
	})
	if err != nil && ctx.Err() == nil {
		log.Printf("TX 监听下游路由失败 vrf=%s neighbor=%s: %v", vrf, neighbor, err)
	}
}

func (a *TxAgent) handleLearnedPath(ctx context.Context, window, vrf, neighbor string, remoteAS uint32, path *api.Path) {
	if path == nil {
		return
	}
	if path.IsWithdraw {
		pfx, ok := gobgp_path.ParseWithdrawPrefix(path)
		if !ok || pfx == "" {
			return
		}
		if err := a.handler.HandlePeerWithdraw(ctx, window, vrf, neighbor, pfx); err != nil {
			log.Printf("TX peer withdraw %s %s: %v", neighbor, pfx, err)
		}
		return
	}
	prefix, nexthop, aspath, ok := gobgp_path.ParseIPv4Unicast(path)
	if !ok {
		return
	}
	if err := a.handler.HandlePeerUpdate(ctx, window, vrf, neighbor, prefix, nexthop, aspath, remoteAS); err != nil {
		log.Printf("TX peer update %s %s: %v", neighbor, prefix, err)
	}
}
