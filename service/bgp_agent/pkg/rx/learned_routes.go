package rx

import (
	"context"

	"bgp_agent/pkg/gobgp_path"

	api "github.com/osrg/gobgp/v3/api"
)

// LearnedRoute 从上游（RR）ADJ-IN 学到的路由。
type LearnedRoute struct {
	Prefix   string `json:"prefix"`
	Nexthop  string `json:"nexthop"`
	ASPath   string `json:"as_path"`
	Neighbor string `json:"neighbor"`
}

// ListAdjInRoutes 列出指定邻居在 RX ADJ-IN 中的 IPv4 单播路由（对端通告给本机）。
func (a *RxAgent) ListAdjInRoutes(ctx context.Context, neighbor string) ([]LearnedRoute, error) {
	if a.server == nil || neighbor == "" {
		return nil, nil
	}
	var out []LearnedRoute
	err := a.server.ListPath(ctx, &api.ListPathRequest{
		TableType: api.TableType_ADJ_IN,
		Family: &api.Family{
			Afi:  api.Family_AFI_IP,
			Safi: api.Family_SAFI_UNICAST,
		},
		Name: neighbor,
	}, func(dest *api.Destination) {
		if dest == nil {
			return
		}
		for _, p := range dest.Paths {
			pfx, nh, asp, ok := gobgp_path.ParseIPv4Unicast(p)
			if !ok {
				continue
			}
			out = append(out, LearnedRoute{
				Prefix:   pfx,
				Nexthop:  nh,
				ASPath:   asp,
				Neighbor: neighbor,
			})
		}
	})
	return out, err
}
