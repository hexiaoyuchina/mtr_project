package tx

import "context"

// ListLearnedRoutesForVrf 汇总某 VRF 下所有下游邻居 ADJ-IN 路由。
func (p *Pool) ListLearnedRoutesForVrf(ctx context.Context, vrf string) ([]LearnedRoute, error) {
	key := vrfKey(vrf)
	p.mu.RLock()
	agent, ok := p.agents[key]
	p.mu.RUnlock()
	if !ok || agent == nil {
		return nil, nil
	}
	peers, err := agent.ListPeers(ctx)
	if err != nil {
		return nil, err
	}
	var out []LearnedRoute
	for _, peer := range peers {
		if peer.Address == "" {
			continue
		}
		rs, err := agent.ListAdjInRoutes(ctx, peer.Address)
		if err != nil {
			return nil, err
		}
		out = append(out, rs...)
	}
	return out, nil
}

// SetVrfFrozen 按 VRF 冻结/解冻 TX（下游断链时保持已通告路由）。
func (p *Pool) SetVrfFrozen(vrf string, frozen bool) {
	key := vrfKey(vrf)
	p.mu.RLock()
	agent, ok := p.agents[key]
	p.mu.RUnlock()
	if !ok || agent == nil {
		return
	}
	if frozen {
		agent.Freeze()
	} else {
		agent.Unfreeze()
	}
}
