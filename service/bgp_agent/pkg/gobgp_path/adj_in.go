package gobgp_path

import (
	"context"

	api "github.com/osrg/gobgp/v3/api"
	"github.com/osrg/gobgp/v3/pkg/server"
)

// AdjInRoute ADJ-IN 中单条 IPv4 单播路由（每个 NLRI 取第一条可解析路径）。
type AdjInRoute struct {
	Prefix  string
	Nexthop string
	ASPath  string
}

// WalkAdjInIPv4 流式遍历邻居 ADJ-IN，避免一次性加载百万路由进内存。
func WalkAdjInIPv4(ctx context.Context, srv *server.BgpServer, neighbor string, fn func(AdjInRoute) error) error {
	if srv == nil || neighbor == "" {
		return nil
	}
	var walkErr error
	err := srv.ListPath(ctx, &api.ListPathRequest{
		TableType: api.TableType_ADJ_IN,
		Family: &api.Family{
			Afi:  api.Family_AFI_IP,
			Safi: api.Family_SAFI_UNICAST,
		},
		Name: neighbor,
	}, func(dest *api.Destination) {
		if walkErr != nil || dest == nil {
			return
		}
		for _, p := range dest.Paths {
			pfx, nh, asp, ok := ParseIPv4Unicast(p)
			if !ok || pfx == "" {
				continue
			}
			if err := fn(AdjInRoute{Prefix: pfx, Nexthop: nh, ASPath: asp}); err != nil {
				walkErr = err
			}
			break
		}
	})
	if walkErr != nil {
		return walkErr
	}
	return err
}
