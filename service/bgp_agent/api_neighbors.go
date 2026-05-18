package main

import (
	"encoding/json"
	"net/http"
	"strings"

)

type neighborAddReq struct {
	Address        string `json:"address"`
	RemoteAS       uint32 `json:"remote_as"`
	Role           string `json:"role"`
	Vrf            string `json:"vrf"`
	LocalAddress   string `json:"local_address"`
	EbgpMultihop   uint32 `json:"ebgp_multihop"`
	BindInterface  string `json:"bind_interface"`
	PassiveMode    bool   `json:"passive_mode"`
}

type neighborRemoveReq struct {
	Address string `json:"address"`
	Vrf     string `json:"vrf"`
}

type neighborToggleReq struct {
	Address string `json:"address"`
	Vrf     string `json:"vrf"`
	Enabled bool   `json:"enabled"`
}

type rrConfigReq struct {
	Address       string `json:"address"`
	RemoteAS      uint32 `json:"remote_as"`
	LocalAddress  string `json:"local_address"`
}

func (s *APIServer) handleNeighbors(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}
	ctx := r.Context()
	var list []map[string]interface{}

	rrPeers, err := s.rxAgent.ListRRPeers(ctx)
	if err == nil {
		for _, p := range rrPeers {
			if strings.TrimSpace(p.Address) == "" {
				continue
			}
			list = append(list, map[string]interface{}{
				"vrf":           "gobgp-rr",
				"address":       p.Address,
				"remote_as":     p.RemoteAS,
				"session":       "rx",
				"state":         p.State,
				"local_address": p.LocalAddress,
				"enabled":       p.Enabled,
				"pfx_rcd":       p.PfxRcd,
				"pfx_adv":       p.PfxAdv,
				"role":          "rr",
			})
		}
	}

	peers, err := s.txPool.ListAllPeers(ctx)
	if err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	for _, p := range peers {
		list = append(list, map[string]interface{}{
			"vrf":           p.Vrf,
			"address":       p.Address,
			"remote_as":     p.RemoteAS,
			"session":       p.Session,
			"state":         p.State,
			"local_address": p.LocalAddress,
			"enabled":       p.Enabled,
			"pfx_rcd":       p.PfxRcd,
			"pfx_adv":       p.PfxAdv,
			"role":          "downstream",
		})
	}
	s.writeJSON(w, map[string]interface{}{"neighbors": list})
}

func (s *APIServer) handleAddNeighbor(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}
	var req neighborAddReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	if req.Address == "" || req.RemoteAS == 0 {
		http.Error(w, "address and remote_as required", http.StatusBadRequest)
		return
	}
	ctx := r.Context()
	role := strings.ToLower(strings.TrimSpace(req.Role))
	if role == "rr" || role == "upstream" {
		if err := s.rxAgent.AddRRPeer(ctx, req.Address, req.RemoteAS, req.LocalAddress); err != nil {
			http.Error(w, err.Error(), http.StatusInternalServerError)
			return
		}
		s.syncPeerFreezeState(ctx)
		s.writeJSON(w, map[string]interface{}{"ok": true, "session": "rx", "address": req.Address, "local_address": s.rxAgent.LocalBGPAddress()})
		return
	}
	vrf := req.Vrf
	if vrf == "" {
		vrf = "default"
	}
	if vrf == "gobgp-rr" {
		http.Error(w, "gobgp-rr is reserved for RX RR; set role=rr", http.StatusBadRequest)
		return
	}
	if err := s.txPool.AddNeighbor(ctx, vrf, req.Address, req.RemoteAS, req.LocalAddress, req.EbgpMultihop, req.BindInterface, req.PassiveMode); err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	s.writeJSON(w, map[string]interface{}{
		"ok":      true,
		"session": "tx",
		"vrf":     vrf,
		"address": req.Address,
	})
}

func (s *APIServer) handleRemoveNeighbor(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}
	var req neighborRemoveReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	ctx := r.Context()
	vrf := strings.TrimSpace(req.Vrf)
	if vrf == "gobgp-rr" {
		addr := strings.TrimSpace(req.Address)
		if addr == "" {
			http.Error(w, "address required for gobgp-rr remove", http.StatusBadRequest)
			return
		}
		if err := s.rxAgent.RemoveRR(ctx, addr); err != nil {
			http.Error(w, err.Error(), http.StatusInternalServerError)
			return
		}
		s.syncPeerFreezeState(ctx)
		s.writeJSON(w, map[string]interface{}{"ok": true, "session": "rx", "address": addr})
		return
	}
	if vrf == "" {
		vrf = "default"
	}
	if err := s.txPool.RemoveNeighbor(ctx, vrf, req.Address); err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	s.writeJSON(w, map[string]interface{}{"ok": true, "address": req.Address})
}

func (s *APIServer) handleNeighborToggle(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}
	var req neighborToggleReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	ctx := r.Context()
	vrf := req.Vrf
	if vrf == "" {
		vrf = "default"
	}
	if err := s.txPool.SetNeighborEnabled(ctx, vrf, req.Address, req.Enabled); err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	s.writeJSON(w, map[string]interface{}{"ok": true, "enabled": req.Enabled})
}

func (s *APIServer) handleRRConfig(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}
	var req rrConfigReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	ctx := r.Context()
	if err := s.rxAgent.AddRRPeer(ctx, req.Address, req.RemoteAS, req.LocalAddress); err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	s.syncPeerFreezeState(ctx)
	s.writeJSON(w, map[string]interface{}{
		"ok":            true,
		"rr_addr":       req.Address,
		"rr_as":         req.RemoteAS,
		"local_address": s.rxAgent.LocalBGPAddress(),
	})
}

func (s *APIServer) handleRRToggle(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}
	var req struct {
		Address string `json:"address"`
		Enabled bool   `json:"enabled"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	ctx := r.Context()
	addr := strings.TrimSpace(req.Address)
	if addr == "" {
		http.Error(w, "address required", http.StatusBadRequest)
		return
	}
	if err := s.rxAgent.SetRRAdminState(ctx, addr, req.Enabled); err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	s.syncPeerFreezeState(ctx)
	s.writeJSON(w, map[string]interface{}{"ok": true, "enabled": req.Enabled, "address": addr})
}

func (s *APIServer) handleRRRemove(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}
	var req struct {
		Address string `json:"address"`
	}
	_ = json.NewDecoder(r.Body).Decode(&req)
	ctx := r.Context()
	if err := s.rxAgent.RemoveRR(ctx, strings.TrimSpace(req.Address)); err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	s.syncPeerFreezeState(ctx)
	s.writeJSON(w, map[string]interface{}{"ok": true, "address": req.Address})
}
