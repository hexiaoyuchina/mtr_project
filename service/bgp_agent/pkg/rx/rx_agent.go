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
	LocalAS  uint32
	RouterID string
	RRAddr   string
	RRAS     uint32
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
			ListenPort: 179, // BGP监听端口
		},
	}); err != nil {
		return fmt.Errorf("启动BGP失败: %w", err)
	}

	// 添加RR作为邻居
	if err := a.addRRNeighbor(ctx); err != nil {
		return fmt.Errorf("添加RR邻居失败: %w", err)
	}

	// 启动路由监听
	go a.watchRoutes(ctx)

	log.Printf("RX Agent已启动: LocalAS=%d RouterID=%s RR=%s",
		a.config.LocalAS, a.config.RouterID, a.config.RRAddr)

	return nil
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
