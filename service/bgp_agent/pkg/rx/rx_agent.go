package rx

import (
	"context"
	"fmt"
	"log"
	"strings"
	"sync"

	"bgp_agent/pkg/gobgp_path"
	"bgp_agent/pkg/processor"

	api "github.com/osrg/gobgp/v3/api"
	"github.com/osrg/gobgp/v3/pkg/server"
	"google.golang.org/protobuf/proto"
)

// Config RX Agent配置
type Config struct {
	LocalAS      uint32
	RouterID     string
	LocalAddress string // 与 RR 建连的 TCP 源（update-source），默认同 RouterID
	RRAddr       string // 启动参数预置 RR（可选）
	RRAS         uint32
}

// RouteHandler 路由处理接口
type RouteHandler interface {
	HandleUpdate(ctx context.Context, prefix, nexthop, aspath string, asn uint32) error
	HandleWithdraw(ctx context.Context, prefix string) error
	HandlePeerUpdate(ctx context.Context, window, vrf, neighbor, prefix, nexthop, aspath string, asn uint32) error
	HandlePeerWithdraw(ctx context.Context, window, vrf, neighbor, prefix string) error
}

// RxAgent GoBGP RX Agent：从多个 RR 接收路由（多活全量）
type RxAgent struct {
	config  *Config
	server  *server.BgpServer
	handler RouteHandler
	rrPeers map[string]*rrPeerEntry
	rrMu    sync.RWMutex
}

// NewRxAgent 创建RX Agent
func NewRxAgent(config *Config, handler RouteHandler) (*RxAgent, error) {
	return &RxAgent{
		config:  config,
		rrPeers: make(map[string]*rrPeerEntry),
		handler: handler,
	}, nil
}

// Start 启动RX Agent
func (a *RxAgent) Start(ctx context.Context) error {
	s := server.NewBgpServer()
	go s.Serve()
	a.server = s

	if err := s.StartBgp(ctx, &api.StartBgpRequest{
		Global: &api.Global{
			Asn:        a.config.LocalAS,
			RouterId:   a.config.RouterID,
			ListenPort: 179,
		},
	}); err != nil {
		return fmt.Errorf("启动BGP失败: %w", err)
	}

	if a.config.RRAddr != "" {
		asn := a.config.RRAS
		if asn == 0 {
			asn = a.config.LocalAS
		}
		if err := a.AddRRPeer(ctx, a.config.RRAddr, asn, a.config.LocalAddress); err != nil {
			return fmt.Errorf("添加RR邻居失败: %w", err)
		}
	}

	go a.watchRoutes(ctx)

	log.Printf("RX Agent已启动: LocalAS=%d RouterID=%s uplink=%s",
		a.config.LocalAS, a.config.RouterID, a.rrLocalAddress())
	return nil
}

func (a *RxAgent) rrLocalAddress() string {
	if a.config.LocalAddress != "" {
		return a.config.LocalAddress
	}
	return a.config.RouterID
}

// LocalBGPAddress 供 API 展示：与 RR 建连的本端地址
func (a *RxAgent) LocalBGPAddress() string {
	return a.rrLocalAddress()
}

func (a *RxAgent) setUplinkLocalAddress(la string) {
	la = strings.TrimSpace(la)
	if la != "" {
		a.config.LocalAddress = la
	}
}

func (a *RxAgent) buildRRPeer(addr string, asn uint32) *api.Peer {
	peer := &api.Peer{
		Conf: &api.PeerConf{
			NeighborAddress: addr,
			PeerAsn:         asn,
		},
		AfiSafis: []*api.AfiSafi{
			{
				Config: &api.AfiSafiConfig{
					Family: &api.Family{
						Afi:  api.Family_AFI_IP,
						Safi: api.Family_SAFI_UNICAST,
					},
					Enabled: true,
				},
			},
		},
	}
	if la := a.rrLocalAddress(); la != "" {
		peer.Transport = &api.Transport{LocalAddress: la}
	}
	return peer
}

// AddRRPeer 添加上游 RR（不删除其它 RR；本端源地址统一为 uplink local_address）
func (a *RxAgent) AddRRPeer(ctx context.Context, addr string, asn uint32, localAddress string) error {
	addr = strings.TrimSpace(addr)
	if addr == "" {
		return fmt.Errorf("rr address required")
	}
	if asn == 0 {
		asn = a.config.LocalAS
	}
	if localAddress != "" {
		a.setUplinkLocalAddress(localAddress)
	}

	a.rrMu.Lock()
	if _, ok := a.rrPeers[addr]; ok {
		a.rrMu.Unlock()
		return nil
	}
	a.rrMu.Unlock()

	peer := a.buildRRPeer(addr, asn)
	if err := a.server.AddPeer(ctx, &api.AddPeerRequest{Peer: peer}); err != nil {
		return err
	}
	a.rrMu.Lock()
	a.rrPeers[addr] = &rrPeerEntry{addr: addr, asn: asn, cached: proto.Clone(peer).(*api.Peer)}
	a.rrMu.Unlock()
	log.Printf("RX 添加上游 RR: %s AS%d local=%s", addr, asn, a.rrLocalAddress())
	return nil
}

// ReconfigureRR 兼容旧 API：等同 AddRRPeer
func (a *RxAgent) ReconfigureRR(ctx context.Context, addr string, asn uint32, localAddress string) error {
	return a.AddRRPeer(ctx, addr, asn, localAddress)
}

// RemoveRRPeer 删除指定 RR
func (a *RxAgent) RemoveRRPeer(ctx context.Context, addr string) error {
	addr = strings.TrimSpace(addr)
	if addr == "" {
		return fmt.Errorf("rr address required")
	}
	a.rrMu.Lock()
	_, ok := a.rrPeers[addr]
	a.rrMu.Unlock()
	if !ok {
		return nil
	}
	if err := a.server.DeletePeer(ctx, &api.DeletePeerRequest{Address: addr}); err != nil {
		return err
	}
	a.rrMu.Lock()
	delete(a.rrPeers, addr)
	a.rrMu.Unlock()
	log.Printf("RX 删除上游 RR: %s", addr)
	return nil
}

// RemoveRR 删除 RR；addr 为空时删除全部
func (a *RxAgent) RemoveRR(ctx context.Context, addr string) error {
	addr = strings.TrimSpace(addr)
	if addr != "" {
		return a.RemoveRRPeer(ctx, addr)
	}
	a.rrMu.RLock()
	addrs := make([]string, 0, len(a.rrPeers))
	for k := range a.rrPeers {
		addrs = append(addrs, k)
	}
	a.rrMu.RUnlock()
	for _, ip := range addrs {
		if err := a.RemoveRRPeer(ctx, ip); err != nil {
			return err
		}
	}
	return nil
}

// SetRRAdminState 启停指定 RR
func (a *RxAgent) SetRRAdminState(ctx context.Context, addr string, enabled bool) error {
	addr = strings.TrimSpace(addr)
	if addr == "" {
		return fmt.Errorf("rr address required")
	}
	a.rrMu.RLock()
	entry := a.rrPeers[addr]
	a.rrMu.RUnlock()
	if entry == nil || entry.cached == nil || entry.cached.Conf == nil {
		return fmt.Errorf("rr peer %s not found", addr)
	}
	peer := proto.Clone(entry.cached).(*api.Peer)
	peer.Conf.AdminDown = !enabled
	if _, err := a.server.UpdatePeer(ctx, &api.UpdatePeerRequest{Peer: peer}); err != nil {
		return err
	}
	a.rrMu.Lock()
	if e := a.rrPeers[addr]; e != nil && e.cached != nil && e.cached.Conf != nil {
		e.cached.Conf.AdminDown = !enabled
	}
	a.rrMu.Unlock()
	return nil
}

func (a *RxAgent) lookupRRPeer(addr string) (asn uint32, ok bool) {
	a.rrMu.RLock()
	defer a.rrMu.RUnlock()
	e, ok := a.rrPeers[addr]
	if !ok || e == nil {
		return 0, false
	}
	return e.asn, true
}

func (a *RxAgent) resolvePathNeighbor(path *api.Path) (string, uint32, bool) {
	neighbor := strings.TrimSpace(gobgp_path.NeighborIP(path))
	if neighbor != "" {
		if asn, ok := a.lookupRRPeer(neighbor); ok {
			return neighbor, asn, true
		}
		return "", 0, false
	}
	a.rrMu.RLock()
	defer a.rrMu.RUnlock()
	if len(a.rrPeers) == 1 {
		for addr, e := range a.rrPeers {
			return addr, e.asn, true
		}
	}
	return "", 0, false
}

// ListRRPeers 返回所有已配置的 RR 状态
func (a *RxAgent) ListRRPeers(ctx context.Context) ([]RRPeerStatus, error) {
	a.rrMu.RLock()
	want := make(map[string]*rrPeerEntry, len(a.rrPeers))
	for k, v := range a.rrPeers {
		want[k] = v
	}
	a.rrMu.RUnlock()
	if len(want) == 0 {
		return nil, nil
	}
	byAddr := make(map[string]RRPeerStatus, len(want))
	for addr := range want {
		byAddr[addr] = RRPeerStatus{
			Address:      addr,
			RemoteAS:     want[addr].asn,
			LocalAddress: a.rrLocalAddress(),
			State:        "IDLE",
			Enabled:      true,
		}
	}
	err := a.server.ListPeer(ctx, &api.ListPeerRequest{}, func(p *api.Peer) {
		if p == nil || p.Conf == nil {
			return
		}
		addr := p.Conf.NeighborAddress
		if _, ok := want[addr]; !ok {
			return
		}
		st := byAddr[addr]
		st.Address = addr
		st.RemoteAS = p.Conf.PeerAsn
		st.LocalAddress = a.rrLocalAddress()
		if p.State != nil {
			st.State = p.State.SessionState.String()
			st.Enabled = p.State.AdminState == api.PeerState_UP
		}
		for _, af := range p.AfiSafis {
			if af.State != nil {
				st.PfxRcd += uint32(af.State.Received)
				st.PfxAdv += uint32(af.State.Advertised)
			}
		}
		byAddr[addr] = st
	})
	out := make([]RRPeerStatus, 0, len(byAddr))
	for _, st := range byAddr {
		out = append(out, st)
	}
	return out, err
}

// ListRRPeer 兼容：返回第一个 RR 的状态
func (a *RxAgent) ListRRPeer(ctx context.Context) (address string, remoteAS uint32, state string, pfxRcd, pfxAdv uint32, enabled bool, err error) {
	peers, err := a.ListRRPeers(ctx)
	if err != nil || len(peers) == 0 {
		return
	}
	p := peers[0]
	return p.Address, p.RemoteAS, p.State, p.PfxRcd, p.PfxAdv, p.Enabled, nil
}

func (a *RxAgent) watchRoutes(ctx context.Context) {
	err := a.server.WatchEvent(ctx, &api.WatchEventRequest{
		Table: &api.WatchEventRequest_Table{
			Filters: []*api.WatchEventRequest_Table_Filter{
				{Type: api.WatchEventRequest_Table_Filter_BEST},
			},
		},
	}, func(response *api.WatchEventResponse) {
		if response.GetTable() == nil {
			return
		}
		for _, path := range response.GetTable().Paths {
			a.handlePath(ctx, path)
		}
	})
	if err != nil {
		log.Printf("监听路由失败: %v", err)
	}
}

func (a *RxAgent) handlePath(ctx context.Context, path *api.Path) {
	neighbor, asn, ok := a.resolvePathNeighbor(path)
	if !ok {
		return
	}
	vrf := processor.VRFGobgpRR
	window := processor.WindowUpstream

	if path.IsWithdraw {
		pfx, ok := gobgp_path.ParseWithdrawPrefix(path)
		if !ok || pfx == "" {
			return
		}
		if err := a.handler.HandlePeerWithdraw(ctx, window, vrf, neighbor, pfx); err != nil {
			log.Printf("peer withdraw失败: %v", err)
		}
		_ = a.handler.HandleWithdraw(ctx, pfx)
		return
	}

	prefix, nexthop, aspath, ok := gobgp_path.ParseIPv4Unicast(path)
	if !ok || prefix == "" {
		return
	}
	if err := a.handler.HandlePeerUpdate(ctx, window, vrf, neighbor, prefix, nexthop, aspath, asn); err != nil {
		log.Printf("peer update失败: %v", err)
	}
	_ = a.handler.HandleUpdate(ctx, prefix, nexthop, aspath, asn)
}

// ConfigRouterID 返回配置的 Router ID
func (a *RxAgent) ConfigRouterID() string {
	return a.config.RouterID
}

// Stop 停止RX Agent
func (a *RxAgent) Stop() {
	if a.server != nil {
		a.server.Stop()
	}
}

// GetStatus 获取RX状态
func (a *RxAgent) GetStatus(ctx context.Context) (map[string]interface{}, error) {
	peers, err := a.ListRRPeers(ctx)
	if err != nil {
		return nil, err
	}
	var rrList []map[string]interface{}
	anyUp := false
	for _, p := range peers {
		up := strings.Contains(strings.ToUpper(p.State), "ESTABLISHED")
		if up {
			anyUp = true
		}
		rrList = append(rrList, map[string]interface{}{
			"address":       p.Address,
			"remote_as":     p.RemoteAS,
			"state":         p.State,
			"established":   up,
			"pfx_rcd":       p.PfxRcd,
			"local_address": p.LocalAddress,
		})
	}
	status := map[string]interface{}{
		"local_as":      a.config.LocalAS,
		"router_id":     a.config.RouterID,
		"local_address": a.rrLocalAddress(),
		"rr_peers":      rrList,
		"rr_connected":  anyUp,
	}
	if len(peers) == 1 {
		status["rr_addr"] = peers[0].Address
		status["rr_as"] = peers[0].RemoteAS
		status["rr_state"] = peers[0].State
	}
	return status, nil
}
