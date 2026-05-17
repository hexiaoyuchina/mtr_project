package main

import (
	"encoding/json"
	"net/http"
	"strings"
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
	added, failed := 0, 0
	var errs []string
	defaultNH := s.rxAgent.ConfigRouterID()
	for _, item := range req.Routes {
		pfx := strings.TrimSpace(item.Prefix)
		if pfx == "" {
			continue
		}
		nh := strings.TrimSpace(item.Nexthop)
		if nh == "" {
			nh = defaultNH
		}
		var err error
		if req.Enable {
			err = s.txPool.AdvertiseRoute(ctx, vrf, pfx, nh)
		} else {
			err = s.txPool.WithdrawRoute(ctx, vrf, pfx)
		}
		if err != nil {
			failed++
			if len(errs) < 20 {
				errs = append(errs, pfx+": "+err.Error())
			}
			continue
		}
		added++
	}
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
