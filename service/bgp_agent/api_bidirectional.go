package main

import (
	"context"
	"encoding/json"
	"net/http"
	"strings"
	"time"

	"bgp_agent/pkg/processor"
	"bgp_agent/pkg/rx"
)

type rrRoutesReq struct {
	Routes []struct {
		Prefix  string `json:"prefix"`
		Nexthop string `json:"nexthop"`
	} `json:"routes"`
	Enable bool `json:"enable"`
}

func (s *APIServer) handleTxLearnedRoutes(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}
	vrf := strings.TrimSpace(r.URL.Query().Get("vrf"))
	if vrf == "" {
		http.Error(w, "vrf required", http.StatusBadRequest)
		return
	}
	ctx := r.Context()
	routes, err := s.txPool.ListLearnedRoutesForVrf(ctx, vrf)
	if err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	out := make([]map[string]interface{}, 0, len(routes))
	for _, rt := range routes {
		out = append(out, map[string]interface{}{
			"prefix":   rt.Prefix,
			"nexthop":  rt.Nexthop,
			"as_path":  rt.ASPath,
			"neighbor": rt.Neighbor,
			"vrf":      vrf,
		})
	}
	s.writeJSON(w, map[string]interface{}{"routes": out, "total": len(out), "vrf": vrf})
}

func (s *APIServer) handleRRRoutes(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}
	var req rrRoutesReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	ctx := r.Context()
	defaultNH := s.rxAgent.ConfigRouterID()
	ops := make([]rx.RouteOp, 0, len(req.Routes))
	for _, item := range req.Routes {
		pfx := strings.TrimSpace(item.Prefix)
		if pfx == "" {
			continue
		}
		nh := strings.TrimSpace(item.Nexthop)
		if req.Enable {
			nh = defaultNH
		}
		ops = append(ops, rx.RouteOp{Prefix: pfx, Nexthop: nh})
	}
	added, failed, errs := s.rxAgent.ApplyIPv4Batch(ctx, ops, req.Enable, defaultNH)
	s.writeJSON(w, map[string]interface{}{
		"ok":     failed == 0,
		"added":  added,
		"failed": failed,
		"errors": errs,
		"enable": req.Enable,
		"method": "gobgp_rx",
	})
}

func (s *APIServer) RunPeerWatch(ctx context.Context, interval time.Duration) {
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			s.syncPeerFreezeState(ctx)
		}
	}
}

func (s *APIServer) syncPeerFreezeState(ctx context.Context) {
	rrPeers, _ := s.rxAgent.ListRRPeers(ctx)
	anyRRUp := false
	for _, p := range rrPeers {
		up := strings.Contains(strings.ToUpper(p.State), "ESTABLISHED")
		s.processor.SetUpstreamPeerConnected(p.Address, up)
		was := s.rrWasUp[p.Address]
		if up && !was {
			s.notePeerEstablished(processor.WindowUpstream, processor.VRFGobgpRR, p.Address)
		}
		s.rrWasUp[p.Address] = up
		if up {
			anyRRUp = true
			s.maybeReconcileIngestGap(ctx, processor.WindowUpstream, processor.VRFGobgpRR, p.Address, p.PfxRcd)
		}
	}
	s.processor.SetRRConnected(anyRRUp)
	if anyRRUp {
		s.txPool.UnfreezeAll()
	} else if s.rxAgent != nil && len(rrPeers) > 0 {
		s.txPool.FreezeAll()
	}
	peers, err := s.txPool.ListAllPeers(ctx)
	if err != nil {
		return
	}
	for _, p := range peers {
		up := strings.Contains(strings.ToUpper(p.State), "ESTABLISHED")
		if p.Vrf == "" {
			continue
		}
		s.processor.SetDownstreamPeerConnected(p.Vrf, p.Address, up)
		dsKey := p.Vrf + "|" + p.Address
		was := s.dsWasUp[dsKey]
		if up && !was {
			s.notePeerEstablished(processor.WindowDownstream, p.Vrf, p.Address)
		}
		s.dsWasUp[dsKey] = up
		if up {
			s.maybeReconcileIngestGap(ctx, processor.WindowDownstream, p.Vrf, p.Address, p.PfxRcd)
		}
		s.txPool.SetVrfFrozen(p.Vrf, !up)
	}
}

func (s *APIServer) handlePeersFreezeStatus(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}
	ctx := r.Context()
	rrPeers, _ := s.rxAgent.ListRRPeers(ctx)
	var upstream []map[string]interface{}
	anyRRUp := false
	for _, p := range rrPeers {
		up := strings.Contains(strings.ToUpper(p.State), "ESTABLISHED")
		if up {
			anyRRUp = true
		}
		upstream = append(upstream, map[string]interface{}{
			"vrf":         "gobgp-rr",
			"neighbor_ip": p.Address,
			"window":      "upstream",
			"established": up,
			"frozen":      !up,
			"pfx_rcd":     p.PfxRcd,
			"state":       p.State,
		})
	}
	peers, _ := s.txPool.ListAllPeers(ctx)
	var ds []map[string]interface{}
	for _, p := range peers {
		up := strings.Contains(strings.ToUpper(p.State), "ESTABLISHED")
		ds = append(ds, map[string]interface{}{
			"vrf":         p.Vrf,
			"neighbor_ip": p.Address,
			"window":      "downstream",
			"established": up,
			"frozen":      !up,
			"pfx_rcd":     p.PfxRcd,
			"state":       p.State,
		})
	}
	s.writeJSON(w, map[string]interface{}{
		"upstream":       upstream,
		"upstream_any_up": anyRRUp,
		"downstream":     ds,
		"time":           time.Now().Format(time.RFC3339),
	})
}
