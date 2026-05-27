package rx

import (
	"context"
	"strings"
	"time"

	"bgp_agent/pkg/gobgp_path"
	api "github.com/osrg/gobgp/v3/api"
)

// RouteOp 单条前缀操作。
type RouteOp struct {
	Prefix  string
	Nexthop string
}

const bulkAdvertiseMin = 64

// ApplyIPv4Batch 批量向 RR 通告或撤销 IPv4 前缀。
func (a *RxAgent) ApplyIPv4Batch(ctx context.Context, routes []RouteOp, enable bool, defaultNH string) (added, failed int, errs []string) {
	if len(routes) == 0 {
		return 0, 0, nil
	}
	if enable && len(routes) >= bulkAdvertiseMin && a.grpcAddr != "" {
		paths := make([]*api.Path, 0, len(routes))
		for _, item := range routes {
			pfx := strings.TrimSpace(item.Prefix)
			if pfx == "" {
				failed++
				continue
			}
			nh := strings.TrimSpace(item.Nexthop)
			if nh == "" {
				nh = defaultNH
			}
			p, err := gobgp_path.BuildIPv4UnicastPath(pfx, nh, false)
			if err != nil {
				failed++
				if len(errs) < 20 {
					errs = append(errs, pfx+": "+err.Error())
				}
				continue
			}
			paths = append(paths, p)
		}
		if len(paths) > 0 {
			var lastErr error
			for attempt := 0; attempt < 8; attempt++ {
				lastErr = gobgp_path.AddPathStreamBatch(ctx, a.grpcAddr, paths, 0)
				if lastErr == nil {
					return len(paths), failed, errs
				}
				time.Sleep(50 * time.Millisecond)
			}
			if lastErr != nil && len(errs) < 20 {
				errs = append(errs, lastErr.Error())
			}
			return 0, failed + len(paths), errs
		}
	}
	for _, item := range routes {
		pfx := strings.TrimSpace(item.Prefix)
		if pfx == "" {
			continue
		}
		var err error
		if enable {
			nh := strings.TrimSpace(item.Nexthop)
			if nh == "" {
				nh = defaultNH
			}
			err = a.AdvertiseIPv4(ctx, pfx, nh)
		} else {
			err = a.WithdrawIPv4(ctx, pfx)
		}
		if err != nil {
			failed++
			if len(errs) < 20 {
				errs = append(errs, pfx+": "+err.Error())
			}
			continue
		}
		added++
	}
	return added, failed, errs
}
