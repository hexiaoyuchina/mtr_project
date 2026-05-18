package tx

import (
	"context"
	"fmt"
	"sync"
)

// Pool 按 VRF 名维护多个 TX Agent（对应原 FRR 多 ``router bgp vrf``）。
type Pool struct {
	config   *Config
	storage  Storage
	handler  PeerRouteHandler
	basePort uint16
	mu       sync.RWMutex
	agents   map[string]*TxAgent
}

// GetOrCreateDefault 预创建默认 TX（加载 RIB 并向默认下游通告）
func (p *Pool) GetOrCreateDefault(ctx context.Context) (*TxAgent, error) {
	return p.getOrCreate(ctx, "default")
}

// GetAgent 返回已存在的 TX Agent（不创建）。
func (p *Pool) GetAgent(vrf string) (*TxAgent, error) {
	key := vrfKey(vrf)
	p.mu.RLock()
	a, ok := p.agents[key]
	p.mu.RUnlock()
	if !ok || a == nil {
		return nil, fmt.Errorf("tx agent vrf %s not found", vrf)
	}
	return a, nil
}

func NewPool(config *Config, store Storage, basePort uint16, handler PeerRouteHandler) *Pool {
	if basePort == 0 {
		basePort = 1790
	}
	return &Pool{
		config:   config,
		storage:  store,
		handler:  handler,
		basePort: basePort,
		agents:   make(map[string]*TxAgent),
	}
}

func vrfKey(vrf string) string {
	if vrf == "" || vrf == "default" {
		return "default"
	}
	return vrf
}

func (p *Pool) portFor(vrf string) uint16 {
	key := vrfKey(vrf)
	if key == "default" {
		return p.basePort
	}
	// 卫星 VRF 使用独立端口，避免单实例无法对同一对端 IP 建多条会话
	h := uint16(0)
	for i := 0; i < len(key); i++ {
		h = h*31 + uint16(key[i])
	}
	return p.basePort + 1 + (h % 50)
}

func (p *Pool) getOrCreate(ctx context.Context, vrf string) (*TxAgent, error) {
	key := vrfKey(vrf)
	p.mu.RLock()
	a, ok := p.agents[key]
	p.mu.RUnlock()
	if ok {
		return a, nil
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	if a, ok = p.agents[key]; ok {
		return a, nil
	}
	cfg := *p.config
	agent, err := NewTxAgent(&cfg, p.storage, p.portFor(vrf), key, p.handler)
	if err != nil {
		return nil, err
	}
	if err := agent.Start(ctx); err != nil {
		return nil, fmt.Errorf("start tx vrf %s: %w", key, err)
	}
	p.agents[key] = agent
	return agent, nil
}

func (p *Pool) AddNeighbor(ctx context.Context, vrf, addr string, asn uint32, localAddress string, ebgpMultihop uint32, bindInterface string, passiveMode bool) error {
	agent, err := p.getOrCreate(ctx, vrf)
	if err != nil {
		return err
	}
	return agent.AddNeighbor(ctx, addr, asn, localAddress, ebgpMultihop, bindInterface, passiveMode)
}

func (p *Pool) RemoveNeighbor(ctx context.Context, vrf, addr string) error {
	key := vrfKey(vrf)
	p.mu.RLock()
	agent, ok := p.agents[key]
	p.mu.RUnlock()
	if !ok {
		return nil
	}
	return agent.RemoveNeighbor(ctx, addr)
}

func (p *Pool) SetNeighborEnabled(ctx context.Context, vrf, addr string, enabled bool) error {
	agent, err := p.getOrCreate(ctx, vrf)
	if err != nil {
		return err
	}
	return agent.SetNeighborEnabled(ctx, addr, enabled)
}

func (p *Pool) AdvertiseRoute(ctx context.Context, vrf, prefix, nexthop string) error {
	agent, err := p.getOrCreate(ctx, vrf)
	if err != nil {
		return err
	}
	return agent.AdvertiseRoute(ctx, prefix, nexthop)
}

func (p *Pool) WithdrawRoute(ctx context.Context, vrf, prefix string) error {
	key := vrfKey(vrf)
	p.mu.RLock()
	agent, ok := p.agents[key]
	p.mu.RUnlock()
	if !ok {
		return nil
	}
	return agent.WithdrawRoute(ctx, prefix)
}

// ApplyRoutesBatch 在指定 VRF 的 TX 上批量通告或撤销。
func (p *Pool) ApplyRoutesBatch(ctx context.Context, vrf string, routes []RouteOp, enable bool, defaultNH string) (added, failed int, errs []string) {
	agent, err := p.getOrCreate(ctx, vrf)
	if err != nil {
		return 0, len(routes), []string{err.Error()}
	}
	return agent.ApplyRoutesBatch(ctx, routes, enable, defaultNH)
}

func (p *Pool) ListAllPeers(ctx context.Context) ([]PeerStatus, error) {
	p.mu.RLock()
	keys := make([]string, 0, len(p.agents))
	for k := range p.agents {
		keys = append(keys, k)
	}
	p.mu.RUnlock()
	var out []PeerStatus
	for _, k := range keys {
		p.mu.RLock()
		agent := p.agents[k]
		p.mu.RUnlock()
		if agent == nil {
			continue
		}
		peers, err := agent.ListPeers(ctx)
		if err != nil {
			return nil, err
		}
		for i := range peers {
			peers[i].Vrf = k
			out = append(out, peers[i])
		}
	}
	return out, nil
}

func (p *Pool) FreezeAll() {
	p.mu.RLock()
	defer p.mu.RUnlock()
	for _, a := range p.agents {
		a.Freeze()
	}
}

func (p *Pool) UnfreezeAll() {
	p.mu.RLock()
	defer p.mu.RUnlock()
	for _, a := range p.agents {
		a.Unfreeze()
	}
}

func (p *Pool) StopAll() {
	p.mu.Lock()
	defer p.mu.Unlock()
	for _, a := range p.agents {
		a.Stop()
	}
	p.agents = make(map[string]*TxAgent)
}
