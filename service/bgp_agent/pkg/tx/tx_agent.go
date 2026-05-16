package tx

import (
	"context"
	"fmt"
	"log"
	"net"
	"sync"

	"bgp_agent/pkg/storage"
	api "github.com/osrg/gobgp/v3/api"
	"github.com/osrg/gobgp/v3/pkg/server"
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
	config      *Config
	server      *server.BgpServer
	storage     Storage
	neighbors   map[string]*api.Peer
	neighborsMu sync.RWMutex
	frozen      bool
	frozenMu    sync.RWMutex
}

// NewTxAgent 创建TX Agent
func NewTxAgent(config *Config, storage Storage) (*TxAgent, error) {
	return &TxAgent{
		config:    config,
		storage:   storage,
		neighbors: make(map[string]*api.Peer),
		frozen:    false,
	}, nil
}

// Start 启动TX Agent
func (a *TxAgent) Start(ctx context.Context) error {
	// 创建GoBGP Server（只通告路由）
	s := server.NewBgpServer()
	go s.Serve()

	a.server = s

	// 配置全局参数
	if err := s.StartBgp(ctx, &api.StartBgpRequest{
		Global: &api.Global{
			Asn:        a.config.LocalAS,
			RouterId:   a.config.RouterID,
			ListenPort: 1790, // 使用不同端口避免冲突
		},
	}); err != nil {
		return fmt.Errorf("启动BGP失败: %w", err)
	}

	// 从RocksDB恢复路由
	if err := a.restoreRoutes(ctx); err != nil {
		log.Printf("恢复路由失败: %v", err)
	}

	log.Printf("TX Agent已启动: LocalAS=%d RouterID=%s",
		a.config.LocalAS, a.config.RouterID)

	return nil
}

// AddNeighbor 添加下游邻居
func (a *TxAgent) AddNeighbor(ctx context.Context, addr string, asn uint32) error {
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

	if err := a.server.AddPeer(ctx, &api.AddPeerRequest{Peer: peer}); err != nil {
		return fmt.Errorf("添加邻居失败: %w", err)
	}

	a.neighbors[addr] = peer
	log.Printf("TX Agent添加下游邻居: %s AS%d", addr, asn)

	return nil
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

	for _, route := range routes {
		if err := a.AdvertiseRoute(ctx, route.Prefix, route.Nexthop); err != nil {
			log.Printf("恢复路由失败 %s: %v", route.Prefix, err)
		}
	}

	return nil
}

// Stop 停止TX Agent
func (a *TxAgent) Stop() {
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
