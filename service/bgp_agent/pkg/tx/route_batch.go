package tx

import (
	"context"
	"strings"
)

// RouteOp 单条前缀操作（通告或撤销）。
type RouteOp struct {
	Prefix  string
	Nexthop string
}

// ApplyRoutesBatch 批量通告或撤销；返回成功/失败条数（frozen 时跳过计入 added）。
func (a *TxAgent) ApplyRoutesBatch(ctx context.Context, routes []RouteOp, enable bool, defaultNH string) (added, failed int, errs []string) {
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
