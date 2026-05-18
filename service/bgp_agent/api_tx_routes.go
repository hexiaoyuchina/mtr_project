package main

import (
	"encoding/json"
	"net/http"
	"strings"

	"bgp_agent/pkg/tx"
)

type txRouteItem struct {
	Prefix  string `json:"prefix"`
	Nexthop string `json:"nexthop"`
}

type txRoutesReq struct {
	Vrf     string        `json:"vrf"`
	Routes  []txRouteItem `json:"routes"`
	Enable  bool          `json:"enable"`
}

func (s *APIServer) handleTxRoutes(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}
	var req txRoutesReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	vrf := strings.TrimSpace(req.Vrf)
	if vrf == "" {
		vrf = "default"
	}
	ctx := r.Context()
	defaultNH := s.rxAgent.ConfigRouterID()
	ops := make([]tx.RouteOp, 0, len(req.Routes))
	for _, item := range req.Routes {
		pfx := strings.TrimSpace(item.Prefix)
		if pfx == "" {
			continue
		}
		ops = append(ops, tx.RouteOp{Prefix: pfx, Nexthop: strings.TrimSpace(item.Nexthop)})
	}
	added, failed, errs := s.txPool.ApplyRoutesBatch(ctx, vrf, ops, req.Enable, defaultNH)
	s.writeJSON(w, map[string]interface{}{
		"ok":      failed == 0,
		"added":   added,
		"failed":  failed,
		"errors":  errs,
		"vrf":     vrf,
		"enable":  req.Enable,
		"method":  "gobgp_tx",
	})
}
