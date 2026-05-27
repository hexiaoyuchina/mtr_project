package tx

import (
	"context"
	"fmt"
	"log"
	"net"
	"strings"
	"sync"

	"bgp_agent/pkg/storage"
	api "github.com/osrg/gobgp/v3/api"
	"github.com/osrg/gobgp/v3/pkg/server"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/anypb"
)

// Config TX Agent配置
type Config struct {
	LocalAS  uint32
	RouterID string
}

// Storage 存储接口
type Storage interface {
	GetEffectiveRIB(ctx context.Context) ([]Route, error)
}

// Route 路由条目（与 storage.Route 一致）
type Route = storage.Route

// TxAgent GoBGP TX Agent，只负责向下游通告路由
type TxAgent struct {
	config        *Config
	vrf           string
	listenPort    uint16
	grpcAddr      string
	server        *server.BgpServer
	storage       Storage
	handler       PeerRouteHandler
	neighbors     map[string]*api.Peer
	neighborsMu   sync.RWMutex
	frozen        bool
	frozenMu      sync.RWMutex
	watchParent   context.Context
	watchCancels  map[string]context.CancelFunc
	watchMu       sync.Mutex
}

// NewTxAgent 创建TX Agent
func NewTxAgent(config *Config, storage Storage, listenPort uint16, vrf string, handler PeerRouteHandler) (*TxAgent, error) {
	if listenPort == 0 {
		listenPort = 1790
	}
	if vrf == "" {
		vrf = "default"
	}
	return &TxAgent{
		config:       config,
		vrf:          vrf,
		listenPort:   listenPort,
		storage:      storage,
		handler:      handler,
		neighbors:    make(map[string]*api.Peer),
		watchCancels: make(map[string]context.CancelFunc),
		frozen:       false,
	}, nil
}

// Start 启动TX Agent
func (a *TxAgent) Start(ctx context.Context) error {
	grpcPort := 50000 + int(a.listenPort)
	a.grpcAddr = fmt.Sprintf("127.0.0.1:%d", grpcPort)
	s := server.NewBgpServer(server.GrpcListenAddress(a.grpcAddr))
	go s.Serve()

	a.server = s
	a.watchParent = ctx

	// 配置全局参数
	if err := s.StartBgp(ctx, &api.StartBgpRequest{
		Global: &api.Global{
			Asn:        a.config.LocalAS,
			RouterId:   a.config.RouterID,
			ListenPort: int32(a.listenPort),
		},
	}); err != nil {
		return fmt.Errorf("启动BGP失败: %w", err)
	}

	// 从RocksDB恢复路由
	if err := a.restoreRoutes(ctx); err != nil {
		log.Printf("恢复路由失败: %v", err)
	}

	log.Printf("TX Agent已启动: LocalAS=%d RouterID=%s port=%d",
		a.config.LocalAS, a.config.RouterID, a.listenPort)

	return nil
}

// AddNeighbor 添加下游邻居（可选本端源地址，对应原 FRR update-source）
func (a *TxAgent) AddNeighbor(ctx context.Context, addr string, asn uint32, localAddress string, ebgpMultihop uint32, bindInterface string, passiveMode bool) error {
	a.neighborsMu.Lock()
	defer a.neighborsMu.Unlock()

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
	if localAddress != "" || bindInterface != "" || passiveMode {
		peer.Transport = &api.Transport{
			LocalAddress:  localAddress,
			BindInterface: bindInterface,
			PassiveMode:   passiveMode,
		}
	}
	if ebgpMultihop > 0 {
		peer.EbgpMultihop = &api.EbgpMultihop{Enabled: true, MultihopTtl: ebgpMultihop}
	}

	if err := a.server.AddPeer(ctx, &api.AddPeerRequest{Peer: peer}); err != nil {
		return fmt.Errorf("添加邻居失败: %w", err)
	}

	a.neighbors[addr] = peer
	log.Printf("TX Agent添加下游邻居: %s AS%d local=%s", addr, asn, localAddress)
	a.startPeerRouteWatch(a.watchParent, addr, asn)

	return nil
}

// SetNeighborEnabled 启停邻居（admin shutdown）。须带完整 Peer 配置，否则 GoBGP 会把 AS/Transport 清零。
func (a *TxAgent) SetNeighborEnabled(ctx context.Context, addr string, enabled bool) error {
	a.neighborsMu.RLock()
	cached, ok := a.neighbors[addr]
	a.neighborsMu.RUnlock()
	if !ok || cached == nil || cached.Conf == nil {
		return fmt.Errorf("neighbor %s not found", addr)
	}
	peer := proto.Clone(cached).(*api.Peer)
	peer.Conf.AdminDown = !enabled
	if _, err := a.server.UpdatePeer(ctx, &api.UpdatePeerRequest{Peer: peer}); err != nil {
		return err
	}
	a.neighborsMu.Lock()
	if live, ok := a.neighbors[addr]; ok && live != nil && live.Conf != nil {
		live.Conf.AdminDown = !enabled
	}
	a.neighborsMu.Unlock()
	if enabled {
		a.Unfreeze()
	}
	return nil
}

// ListPeers 列出本 TX 实例邻居及状态
func (a *TxAgent) ListPeers(ctx context.Context) ([]PeerStatus, error) {
	var out []PeerStatus
	err := a.server.ListPeer(ctx, &api.ListPeerRequest{}, func(p *api.Peer) {
		if p == nil || p.Conf == nil {
			return
		}
		st := PeerStatus{
			Address:  p.Conf.NeighborAddress,
			RemoteAS: p.Conf.PeerAsn,
			Session:  "tx",
			State:    p.State.SessionState.String(),
			Enabled:  p.State.AdminState == api.PeerState_UP,
		}
		if p.Transport != nil {
			st.LocalAddress = p.Transport.LocalAddress
		}
		if st.LocalAddress == "" || st.LocalAddress == "0.0.0.0" {
			a.neighborsMu.RLock()
			if cached, ok := a.neighbors[st.Address]; ok && cached != nil && cached.Transport != nil {
				if la := strings.TrimSpace(cached.Transport.LocalAddress); la != "" && la != "0.0.0.0" {
					st.LocalAddress = la
				}
			}
			a.neighborsMu.RUnlock()
		}
		for _, af := range p.AfiSafis {
			if af.State != nil {
				st.PfxRcd += uint32(af.State.Received)
				st.PfxAdv += uint32(af.State.Advertised)
			}
		}
		out = append(out, st)
	})
	return out, err
}

// ConfigRouterID 返回配置的 Router ID（通告缺省下一跳）。
func (a *TxAgent) ConfigRouterID() string {
	if a.config != nil && a.config.RouterID != "" {
		return a.config.RouterID
	}
	return ""
}

// RemoveNeighbor 删除下游邻居
func (a *TxAgent) RemoveNeighbor(ctx context.Context, addr string) error {
	a.neighborsMu.Lock()
	defer a.neighborsMu.Unlock()

	if err := a.server.DeletePeer(ctx, &api.DeletePeerRequest{
		Address: addr,
	}); err != nil {
		return fmt.Errorf("删除邻居失败: %w", err)
	}

	delete(a.neighbors, addr)
	a.stopPeerRouteWatch(addr)
	log.Printf("TX Agent删除下游邻居: %s", addr)

	return nil
}

// AdvertiseRoute 通告单条路由
func (a *TxAgent) AdvertiseRoute(ctx context.Context, prefix, nexthop string) error {
	// 检查freeze状态
	a.frozenMu.RLock()
	frozen := a.frozen
	a.frozenMu.RUnlock()

	if frozen {
		log.Printf("TX Agent处于frozen状态，跳过新路由通告: %s", prefix)
		return nil
	}

	// 解析前缀
	_, ipNet, err := net.ParseCIDR(prefix)
	if err != nil {
		return fmt.Errorf("无效的前缀: %w", err)
	}
	prefixLen, _ := ipNet.Mask.Size()

	// 构造BGP路径
	nlri, _ := anypb.New(&api.IPAddressPrefix{
		Prefix:    ipNet.IP.String(),
		PrefixLen: uint32(prefixLen),
	})

	originAttr, _ := anypb.New(&api.OriginAttribute{
		Origin: 0, // IGP
	})

	nhAttr, _ := anypb.New(&api.NextHopAttribute{
		NextHop: nexthop,
	})

	path := &api.Path{
		Nlri:   nlri,
		Pattrs: []*anypb.Any{originAttr, nhAttr},
		Family: &api.Family{Afi: api.Family_AFI_IP, Safi: api.Family_SAFI_UNICAST},
	}

	_, err = a.server.AddPath(ctx, &api.AddPathRequest{
		Path: path,
	})
	return err
}

// WithdrawRoute 撤销路由通告
func (a *TxAgent) WithdrawRoute(ctx context.Context, prefix string) error {
	// Freeze时不撤销
	a.frozenMu.RLock()
	frozen := a.frozen
	a.frozenMu.RUnlock()

	if frozen {
		log.Printf("TX Agent处于frozen状态，不撤销路由: %s", prefix)
		return nil
	}

	// 解析前缀
	_, ipNet, err := net.ParseCIDR(prefix)
	if err != nil {
		return fmt.Errorf("无效的前缀: %w", err)
	}
	prefixLen, _ := ipNet.Mask.Size()

	// 构造withdraw
	nlri, _ := anypb.New(&api.IPAddressPrefix{
		Prefix:    ipNet.IP.String(),
		PrefixLen: uint32(prefixLen),
	})

	path := &api.Path{
		Nlri:       nlri,
		Family:     &api.Family{Afi: api.Family_AFI_IP, Safi: api.Family_SAFI_UNICAST},
		IsWithdraw: true,
	}

	return a.server.DeletePath(ctx, &api.DeletePathRequest{
		Path: path,
	})
}

// Freeze 冻结路由通告（RR down时调用）
func (a *TxAgent) Freeze() {
	a.frozenMu.Lock()
	defer a.frozenMu.Unlock()

	a.frozen = true
	log.Println("TX Agent已冻结，继续通告现有路由，不接受新更新")
}

// Unfreeze 解冻（RR恢复时调用）
func (a *TxAgent) Unfreeze() {
	a.frozenMu.Lock()
	defer a.frozenMu.Unlock()

	a.frozen = false
	log.Println("TX Agent已解冻")
}

// IsFrozen 是否处于frozen状态
func (a *TxAgent) IsFrozen() bool {
	a.frozenMu.RLock()
	defer a.frozenMu.RUnlock()
	return a.frozen
}

// restoreRoutes 从RocksDB恢复路由
func (a *TxAgent) restoreRoutes(ctx context.Context) error {
	routes, err := a.storage.GetEffectiveRIB(ctx)
	if err != nil {
		return fmt.Errorf("获取effective RIB失败: %w", err)
	}

	log.Printf("从存储恢复 %d 条路由", len(routes))

	ops := make([]RouteOp, 0, len(routes))
	for _, route := range routes {
		ops = append(ops, RouteOp{Prefix: route.Prefix, Nexthop: route.Nexthop})
	}
	defaultNH := a.config.RouterID
	added, failed, errs := a.ApplyRoutesBatch(ctx, ops, true, defaultNH)
	log.Printf("TX 恢复通告: added=%d failed=%d sample_err=%v", added, failed, firstErr(errs))
	return nil
}

func firstErr(errs []string) string {
	if len(errs) == 0 {
		return ""
	}
	return errs[0]
}

// Stop 停止TX Agent
func (a *TxAgent) Stop() {
	a.watchMu.Lock()
	for _, cancel := range a.watchCancels {
		cancel()
	}
	a.watchCancels = make(map[string]context.CancelFunc)
	a.watchMu.Unlock()
	if a.server != nil {
		a.server.Stop()
	}
}

// GetStatus 获取TX状态
func (a *TxAgent) GetStatus(ctx context.Context) (map[string]interface{}, error) {
	a.neighborsMu.RLock()
	neighborCount := len(a.neighbors)
	a.neighborsMu.RUnlock()

	a.frozenMu.RLock()
	frozen := a.frozen
	a.frozenMu.RUnlock()

	status := map[string]interface{}{
		"local_as":       a.config.LocalAS,
		"router_id":      a.config.RouterID,
		"neighbor_count": neighborCount,
		"frozen":         frozen,
	}

	return status, nil
}
