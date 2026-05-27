package tx

import "context"

// PeerRouteHandler 下游对端 Adj-RIB-In 变更（入库开关由 Processor 检查）。
type PeerRouteHandler interface {
	HandlePeerUpdate(ctx context.Context, window, vrf, neighbor, sourceIP, prefix, nexthop, aspath string, asn uint32) error
	HandlePeerWithdraw(ctx context.Context, window, vrf, neighbor, sourceIP, prefix string) error
}
