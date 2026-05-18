package rx

import (
	"context"
	"strings"
)

// RouteOp 单条前缀操作。
type RouteOp struct {
	Prefix  string
	Nexthop string
}

// ApplyIPv4Batch 批量向 RR 通告或撤销 IPv4 前缀。
func (a *RxAgent) ApplyIPv4Batch(ctx context.Context, routes []RouteOp, enable bool, defaultNH string) (added, failed int, errs []string) {
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
