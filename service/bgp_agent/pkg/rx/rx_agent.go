package rx

import (
	"context"
	"fmt"
	"log"

	api "github.com/osrg/gobgp/v3/api"
	"github.com/osrg/gobgp/v3/pkg/server"
	"google.golang.org/protobuf/types/known/anypb"
)

// Config RX Agent配置
type Config struct {
	LocalAS       uint32
	RouterID      string
	LocalAddress  string // 与 RR 建连的 TCP 源（update-source），默认同 RouterID
	RRAddr        string
	RRAS          uint32
}

// RouteHandler 路由处理接口
type RouteHandler interface {
	HandleUpdate(ctx context.Context, prefix, nexthop, aspath string, asn uint32) error
	HandleWithdraw(ctx context.Context, prefix string) error
}

// RxAgent GoBGP RX Agent，只负责从RR接收路由
type RxAgent struct {
	config  *Config
	server  *server.BgpServer
	handler RouteHandler
}

// NewRxAgent 创建RX Agent
func NewRxAgent(config *Config, handler RouteHandler) (*RxAgent, error) {
	return &RxAgent{
		config:  config,
		handler: handler,
	}, nil
}

// Start 启动RX Agent
func (a *RxAgent) Start(ctx context.Context) error {
	// 创建GoBGP Server（只接收路由）
	s := server.NewBgpServer()
	go s.Serve()

	a.server = s

	// 配置全局参数
	if err := s.StartBgp(ctx, &api.StartBgpRequest{
		Global: &api.Global{
			Asn:        a.config.LocalAS,
			RouterId:   a.config.RouterID,
			ListenPort: 179,
		},
	}); err != nil {
		return fmt.Errorf("启动BGP失败: %w", err)
	}

	// RR 由 OP 下发；启动参数 -rr 非空时预建会话
	if a.config.RRAddr != "" {
		if a.config.RRAS == 0 {
			a.config.RRAS = a.config.LocalAS
		}
		if err := a.addRRNeighbor(ctx); err != nil {
			return fmt.Errorf("添加RR邻居失败: %w", err)
		}
	}

	// 启动路由监听
	go a.watchRoutes(ctx)

	log.Printf("RX Agent已启动: LocalAS=%d RouterID=%s RR=%s",
		a.config.LocalAS, a.config.RouterID, a.config.RRAddr)

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

// addRRNeighbor 添加RR作为BGP邻居
func (a *RxAgent) addRRNeighbor(ctx context.Context) error {
	peer := &api.Peer{
		Conf: &api.PeerConf{
			NeighborAddress: a.config.RRAddr,
			PeerAsn:         a.config.RRAS,
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

	return a.server.AddPeer(ctx, &api.AddPeerRequest{Peer: peer})
}

// watchRoutes 监听路由更新
func (a *RxAgent) watchRoutes(ctx context.Context) {
	// 监听路由表变化
	err := a.server.WatchEvent(ctx, &api.WatchEventRequest{
		Table: &api.WatchEventRequest_Table{
			Filters: []*api.WatchEventRequest_Table_Filter{
				{
					Type: api.WatchEventRequest_Table_Filter_BEST,
				},
			},
		},
	}, func(response *api.WatchEventResponse) {
		if response.GetTable() != nil {
			for _, path := range response.GetTable().Paths {
				a.handlePath(ctx, path)
			}
		}
	})

	if err != nil {
		log.Printf("监听路由失败: %v", err)
	}
}

// handlePath 处理单条路径
func (a *RxAgent) handlePath(ctx context.Context, path *api.Path) {
	// 解析前缀
	prefix := ""
	nexthop := ""
	aspath := ""

	for _, attr := range path.Pattrs {
		any := &anypb.Any{
			TypeUrl: attr.TypeUrl,
			Value:   attr.Value,
		}

		switch attr.TypeUrl {
		case "type.googleapis.com/apipb.IPAddressPrefix":
			// 提取前缀
			if path.GetNlri() != nil {
				nlri := path.GetNlri()
				if nlri.TypeUrl == "type.googleapis.com/apipb.IPAddressPrefix" {
					// 简化解析，实际需要完整解析
					prefix = path.GetFamily().String()
				}
			}
		case "type.googleapis.com/apipb.NextHopAttribute":
			// 提取下一跳（简化）
			nexthop = "0.0.0.0"
		case "type.googleapis.com/apipb.AsPathAttribute":
			// 提取AS路径
			aspath = ""
		}
		_ = any
	}

	if prefix == "" {
		return
	}

	// 判断是announce还是withdraw
	if path.IsWithdraw {
		if err := a.handler.HandleWithdraw(ctx, prefix); err != nil {
			log.Printf("处理withdraw失败: %v", err)
		}
	} else {
		// 获取AS号
		asn := a.config.RRAS
		if err := a.handler.HandleUpdate(ctx, prefix, nexthop, aspath, asn); err != nil {
			log.Printf("处理update失败: %v", err)
		}
	}
}

// ReconfigureRR 由 OP 创建/更新 RR 会话（单 peer，本端 local_address 为与 RR 直连地址）
func (a *RxAgent) ReconfigureRR(ctx context.Context, addr string, asn uint32, localAddress string) error {
	if addr == "" {
		return fmt.Errorf("rr address required")
	}
	if asn == 0 {
		asn = a.config.LocalAS
	}
	la := localAddress
	if la == "" {
		la = a.rrLocalAddress()
	}
	if addr == a.config.RRAddr && asn == a.config.RRAS && la == a.config.LocalAddress && a.config.RRAddr != "" {
		return nil
	}
	if a.config.RRAddr != "" {
		_ = a.server.DeletePeer(ctx, &api.DeletePeerRequest{Address: a.config.RRAddr})
	}
	a.config.RRAddr = addr
	a.config.RRAS = asn
	a.config.LocalAddress = la
	return a.addRRNeighbor(ctx)
}

// RemoveRR 删除 RR 会话（OP 删除邻居时）
func (a *RxAgent) RemoveRR(ctx context.Context) error {
	if a.config.RRAddr == "" {
		return nil
	}
	err := a.server.DeletePeer(ctx, &api.DeletePeerRequest{Address: a.config.RRAddr})
	a.config.RRAddr = ""
	a.config.RRAS = 0
	return err
}

// ListRRPeer 返回 RR 邻居状态
func (a *RxAgent) ListRRPeer(ctx context.Context) (address string, remoteAS uint32, state string, pfxRcd uint32, enabled bool, err error) {
	err = a.server.ListPeer(ctx, &api.ListPeerRequest{}, func(p *api.Peer) {
		if p == nil || p.Conf == nil {
			return
		}
		if p.Conf.NeighborAddress != a.config.RRAddr {
			return
		}
		address = p.Conf.NeighborAddress
		remoteAS = p.Conf.PeerAsn
		if p.State != nil {
			state = p.State.SessionState.String()
			enabled = p.State.AdminState == api.PeerState_UP
		}
		for _, af := range p.AfiSafis {
			if af.State != nil {
				pfxRcd += uint32(af.State.Received)
			}
		}
	})
	return
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
	status := map[string]interface{}{
		"rr_addr":   a.config.RRAddr,
		"rr_as":     a.config.RRAS,
		"local_as":  a.config.LocalAS,
		"router_id": a.config.RouterID,
	}

	// 获取邻居状态
	var peers []*api.Peer
	err := a.server.ListPeer(ctx, &api.ListPeerRequest{}, func(p *api.Peer) {
		peers = append(peers, p)
	})
	if err != nil {
		return status, err
	}

	var rrConnected bool
	for _, peer := range peers {
		if peer.GetConf().GetNeighborAddress() == a.config.RRAddr {
			state := peer.GetState()
			status["rr_state"] = state.GetSessionState().String()
			rrConnected = (state.GetSessionState() == api.PeerState_ESTABLISHED)
			break
		}
	}
	status["rr_connected"] = rrConnected

	return status, nil
}
