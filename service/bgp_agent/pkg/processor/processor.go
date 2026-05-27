package processor

import (
	"context"
	"fmt"
	"log"
	"strings"
	"sync"
	"time"

	"bgp_agent/pkg/storage"
)

// Storage 存储接口
type Storage interface {
	// 热缓存（Redis）
	SetRoute(ctx context.Context, prefix, nexthop, aspath string, asn uint32) error
	GetRoute(ctx context.Context, prefix string) (*Route, error)
	DeleteRoute(ctx context.Context, prefix string) error
	ListRoutes(ctx context.Context) ([]Route, error)
	
	// 持久化（RocksDB）
	PersistRoute(ctx context.Context, prefix, nexthop, aspath string, asn uint32) error
	LoadPersistedRoutes(ctx context.Context) ([]Route, error)
	
	// Effective RIB
	GetEffectiveRIB(ctx context.Context) ([]Route, error)
}

// Route 路由条目（与 storage.Route 一致）
type Route = storage.Route

// Processor 路由处理器：核心业务逻辑层
type Processor struct {
	storage      Storage
	routes       map[string]*Route // 内存索引：prefix -> Route
	routesMu     sync.RWMutex
	
	// 上游 RR 连接状态：key = neighbor IP
	upstreamPeerUp map[string]bool
	upMu           sync.RWMutex

	// 任一 RR 是否 Established（legacy Effective RIB / TX freeze）
	rrConnected bool
	rrMu        sync.RWMutex

	// 下游 peer 连接状态：key = vrf+"|"+neighbor
	downstreamPeerUp map[string]bool
	dsMu             sync.RWMutex
	
	// 批量写入队列
	pendingWrites chan *Route
	batchSize     int
	flushInterval time.Duration

	ribHook RibChangeHook
}

// NewProcessor 创建处理器
func NewProcessor(storage Storage) *Processor {
	return &Processor{
		storage:          storage,
		routes:           make(map[string]*Route),
		rrConnected:      true,
		upstreamPeerUp:   make(map[string]bool),
		downstreamPeerUp: make(map[string]bool),
		pendingWrites:    make(chan *Route, 10000), // 缓冲队列
		batchSize:     1000,
		flushInterval: 5 * time.Second,
	}
}

// SetRibChangeHook 注册 RIB→FIB 变更回调。
func (p *Processor) SetRibChangeHook(h RibChangeHook) {
	p.ribHook = h
}

// Start 启动处理器
func (p *Processor) Start(ctx context.Context) error {
	// 不再从 flat RIB 全表恢复进内存（百万级由 Peer RIB/FIB 持久化承担）
	go p.batchWriteLoop(ctx)
	
	log.Printf("Route Processor已启动")
	return nil
}

// HandleUpdate 处理路由更新（从RX接收）
func (p *Processor) HandleUpdate(ctx context.Context, prefix, nexthop, aspath string, asn uint32) error {
	// 检查RR连接状态
	p.rrMu.RLock()
	connected := p.rrConnected
	p.rrMu.RUnlock()
	
	if !connected {
		// RR断连，freeze模式，不更新
		log.Printf("RR断连，忽略路由更新: %s", prefix)
		return nil
	}
	
	route := &Route{
		Prefix:    prefix,
		Nexthop:   nexthop,
		ASPath:    aspath,
		RemoteAS:  asn,
		UpdatedAt: time.Now(),
	}
	
	// 更新内存索引
	p.routesMu.Lock()
	p.routes[prefix] = route
	p.routesMu.Unlock()
	
	// 写入Redis热缓存（同步）
	if err := p.storage.SetRoute(ctx, prefix, nexthop, aspath, asn); err != nil {
		log.Printf("写入Redis失败: %v", err)
	}
	
	// 加入批量持久化队列（异步）
	select {
	case p.pendingWrites <- route:
	default:
		// 队列满，直接持久化
		if err := p.storage.PersistRoute(ctx, prefix, nexthop, aspath, asn); err != nil {
			log.Printf("持久化路由失败: %v", err)
		}
	}
	
	return nil
}

// HandleWithdraw 处理路由撤销
func (p *Processor) HandleWithdraw(ctx context.Context, prefix string) error {
	// 检查RR连接状态
	p.rrMu.RLock()
	connected := p.rrConnected
	p.rrMu.RUnlock()
	
	if !connected {
		// RR断连，freeze模式，不撤销
		log.Printf("RR断连，忽略路由撤销: %s", prefix)
		return nil
	}
	
	// 从内存删除
	p.routesMu.Lock()
	delete(p.routes, prefix)
	p.routesMu.Unlock()
	
	// 从Redis删除
	if err := p.storage.DeleteRoute(ctx, prefix); err != nil {
		log.Printf("从Redis删除路由失败: %v", err)
	}
	
	// 从RocksDB删除
	// TODO: 实现DeletePersistedRoute
	
	return nil
}

// batchWriteLoop 批量写入循环
func (p *Processor) batchWriteLoop(ctx context.Context) {
	ticker := time.NewTicker(p.flushInterval)
	defer ticker.Stop()
	
	batch := make([]*Route, 0, p.batchSize)
	
	for {
		select {
		case <-ctx.Done():
			// 刷新剩余数据
			p.flushBatch(ctx, batch)
			return
			
		case route := <-p.pendingWrites:
			batch = append(batch, route)
			if len(batch) >= p.batchSize {
				p.flushBatch(ctx, batch)
				batch = batch[:0]
			}
			
		case <-ticker.C:
			if len(batch) > 0 {
				p.flushBatch(ctx, batch)
				batch = batch[:0]
			}
		}
	}
}

// flushBatch 批量持久化
func (p *Processor) flushBatch(ctx context.Context, batch []*Route) {
	if len(batch) == 0 {
		return
	}
	
	start := time.Now()
	for _, route := range batch {
		if err := p.storage.PersistRoute(ctx, route.Prefix, route.Nexthop, route.ASPath, route.RemoteAS); err != nil {
			log.Printf("持久化路由失败 %s: %v", route.Prefix, err)
		}
	}
	
	elapsed := time.Since(start)
	log.Printf("批量持久化 %d 条路由，耗时: %v", len(batch), elapsed)
}

// restoreFromDisk 从RocksDB恢复路由
func (p *Processor) restoreFromDisk(ctx context.Context) error {
	routes, err := p.storage.LoadPersistedRoutes(ctx)
	if err != nil {
		return fmt.Errorf("加载持久化路由失败: %w", err)
	}
	
	p.routesMu.Lock()
	defer p.routesMu.Unlock()
	
	for i := range routes {
		route := &routes[i]
		p.routes[route.Prefix] = route
		
		// 同时恢复到Redis热缓存
		if err := p.storage.SetRoute(ctx, route.Prefix, route.Nexthop, route.ASPath, route.RemoteAS); err != nil {
			log.Printf("恢复路由到Redis失败 %s: %v", route.Prefix, err)
		}
	}
	
	return nil
}

func usPeerKey(neighbor string) string {
	return neighbor
}

// SetUpstreamPeerConnected 单个上游 RR 会话状态（按邻居 freeze 入库）。
func (p *Processor) SetUpstreamPeerConnected(neighbor string, connected bool) {
	neighbor = strings.TrimSpace(neighbor)
	if neighbor == "" {
		return
	}
	p.upMu.Lock()
	old, ok := p.upstreamPeerUp[usPeerKey(neighbor)]
	p.upstreamPeerUp[usPeerKey(neighbor)] = connected
	p.upMu.Unlock()
	if !ok || old != connected {
		if connected {
			log.Printf("上游 RR 恢复 neighbor=%s", neighbor)
		} else {
			log.Printf("上游 RR 断链 freeze neighbor=%s", neighbor)
		}
	}
}

// IsUpstreamPeerConnected 该 RR 是否 Established。
func (p *Processor) IsUpstreamPeerConnected(neighbor string) bool {
	p.upMu.RLock()
	defer p.upMu.RUnlock()
	return p.upstreamPeerUp[usPeerKey(neighbor)]
}

// SetRRConnected 设置「任一 RR」连接状态（TX 池 freeze / legacy RIB）
func (p *Processor) SetRRConnected(connected bool) {
	p.rrMu.Lock()
	defer p.rrMu.Unlock()
	
	oldState := p.rrConnected
	p.rrConnected = connected
	
	if oldState != connected {
		if connected {
			log.Println("RR连接恢复，解除freeze")
		} else {
			log.Println("RR连接断开，进入freeze模式（保持当前RIB）")
		}
	}
}

// IsRRConnected 获取RR连接状态
func (p *Processor) IsRRConnected() bool {
	p.rrMu.RLock()
	defer p.rrMu.RUnlock()
	return p.rrConnected
}

func dsPeerKey(vrf, neighbor string) string {
	return vrf + "|" + neighbor
}

// SetDownstreamPeerConnected 下游会话状态（断链时 freeze 该 peer 的入库更新，由 OP 定时快照保留库内路由）。
func (p *Processor) SetDownstreamPeerConnected(vrf, neighbor string, connected bool) {
	if vrf == "" || neighbor == "" {
		return
	}
	p.dsMu.Lock()
	defer p.dsMu.Unlock()
	old, ok := p.downstreamPeerUp[dsPeerKey(vrf, neighbor)]
	p.downstreamPeerUp[dsPeerKey(vrf, neighbor)] = connected
	if !ok || old != connected {
		if connected {
			log.Printf("下游 peer 恢复 vrf=%s neighbor=%s", vrf, neighbor)
		} else {
			log.Printf("下游 peer 断链 freeze vrf=%s neighbor=%s", vrf, neighbor)
		}
	}
}

// IsDownstreamPeerConnected 下游 peer 是否 Established。
// 未写入状态前（Agent 刚启动、peer_watch 尚未刷态）按可达处理，避免 FIB 全空。
func (p *Processor) IsDownstreamPeerConnected(vrf, neighbor string) bool {
	p.dsMu.RLock()
	defer p.dsMu.RUnlock()
	connected, ok := p.downstreamPeerUp[dsPeerKey(vrf, neighbor)]
	if !ok {
		return true
	}
	return connected
}

// GetRouteCount 获取路由数量
func (p *Processor) GetRouteCount() int {
	p.routesMu.RLock()
	defer p.routesMu.RUnlock()
	return len(p.routes)
}

// GetStatus 获取处理器状态
func (p *Processor) GetStatus() map[string]interface{} {
	p.routesMu.RLock()
	routeCount := len(p.routes)
	p.routesMu.RUnlock()
	
	p.rrMu.RLock()
	rrConnected := p.rrConnected
	p.rrMu.RUnlock()
	
	return map[string]interface{}{
		"route_count":   routeCount,
		"rr_connected":  rrConnected,
		"pending_writes": len(p.pendingWrites),
		"frozen":        !rrConnected,
	}
}
