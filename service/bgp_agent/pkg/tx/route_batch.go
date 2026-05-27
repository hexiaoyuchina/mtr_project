package tx

import (
	"context"
	"strings"
	"time"

	"bgp_agent/pkg/gobgp_path"
	api "github.com/osrg/gobgp/v3/api"
)

// RouteOp 单条前缀操作（通告或撤销）。
type RouteOp struct {
	Prefix  string
	Nexthop string
}

const bulkAdvertiseMin = 64

// ApplyRoutesBatch 批量通告或撤销；大批量走 AddPathStream（单次 propagateUpdate）。
func (a *TxAgent) ApplyRoutesBatch(ctx context.Context, routes []RouteOp, enable bool, defaultNH string) (added, failed int, errs []string) {
	if len(routes) == 0 {
		return 0, 0, nil
	}
	if !enable || len(routes) < bulkAdvertiseMin || a.grpcAddr == "" {
		return a.applyRoutesBatchSequential(ctx, routes, enable, defaultNH)
	}
	a.frozenMu.RLock()
	frozen := a.frozen
	a.frozenMu.RUnlock()
	if frozen {
		return 0, len(routes), nil
	}

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
	if len(paths) == 0 {
		return 0, failed, errs
	}
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

func (a *TxAgent) applyRoutesBatchSequential(ctx context.Context, routes []RouteOp, enable bool, defaultNH string) (added, failed int, errs []string) {
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
			err = a.AdvertiseRoute(ctx, pfx, nh)
		} else {
			err = a.WithdrawRoute(ctx, pfx)
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
