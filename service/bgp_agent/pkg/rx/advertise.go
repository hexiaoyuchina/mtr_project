package rx

import (
	"context"
	"fmt"
	"net"

	api "github.com/osrg/gobgp/v3/api"
	"google.golang.org/protobuf/types/known/anypb"
)

// AdvertiseIPv4 向 RR 邻居通告单条 IPv4 前缀（RX 实例通常仅 RR 一个 peer）。
func (a *RxAgent) AdvertiseIPv4(ctx context.Context, prefix, nexthop string) error {
	if a.server == nil {
		return fmt.Errorf("rx server not started")
	}
	_, ipNet, err := net.ParseCIDR(prefix)
	if err != nil {
		return fmt.Errorf("invalid prefix: %w", err)
	}
	pl, _ := ipNet.Mask.Size()
	nlri, err := anypb.New(&api.IPAddressPrefix{
		Prefix:    ipNet.IP.String(),
		PrefixLen: uint32(pl),
	})
	if err != nil {
		return err
	}
	nh := nexthop
	if nh == "" {
		nh = a.rrLocalAddress()
	}
	originAttr, _ := anypb.New(&api.OriginAttribute{Origin: 0})
	nhAttr, _ := anypb.New(&api.NextHopAttribute{NextHop: nh})
	path := &api.Path{
		Nlri:   nlri,
		Pattrs: []*anypb.Any{originAttr, nhAttr},
		Family: &api.Family{Afi: api.Family_AFI_IP, Safi: api.Family_SAFI_UNICAST},
	}
	_, err = a.server.AddPath(ctx, &api.AddPathRequest{Path: path})
	return err
}

// WithdrawIPv4 向 RR 撤销前缀。
func (a *RxAgent) WithdrawIPv4(ctx context.Context, prefix string) error {
	if a.server == nil {
		return fmt.Errorf("rx server not started")
	}
	_, ipNet, err := net.ParseCIDR(prefix)
	if err != nil {
		return err
	}
	pl, _ := ipNet.Mask.Size()
	nlri, _ := anypb.New(&api.IPAddressPrefix{
		Prefix:    ipNet.IP.String(),
		PrefixLen: uint32(pl),
	})
	path := &api.Path{
		Nlri:       nlri,
		Family:     &api.Family{Afi: api.Family_AFI_IP, Safi: api.Family_SAFI_UNICAST},
		IsWithdraw: true,
	}
	return a.server.DeletePath(ctx, &api.DeletePathRequest{Path: path})
}
