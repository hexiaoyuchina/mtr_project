package rx

import (
	"context"

	"bgp_agent/pkg/gobgp_path"
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
	var out []LearnedRoute
	err := a.WalkAdjInRoutes(ctx, neighbor, func(lr LearnedRoute) error {
		out = append(out, lr)
		return nil
	})
	return out, err
}

// WalkAdjInRoutes 流式遍历 ADJ-IN（每个前缀一条，供 ingest 使用）。
func (a *RxAgent) WalkAdjInRoutes(ctx context.Context, neighbor string, fn func(LearnedRoute) error) error {
	if a.server == nil || neighbor == "" {
		return nil
	}
	return gobgp_path.WalkAdjInIPv4(ctx, a.server, neighbor, func(r gobgp_path.AdjInRoute) error {
		return fn(LearnedRoute{
			Prefix:   r.Prefix,
			Nexthop:  r.Nexthop,
			ASPath:   r.ASPath,
			Neighbor: neighbor,
		})
	})
}
