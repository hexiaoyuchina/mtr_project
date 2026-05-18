package main

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"strconv"
	"strings"

	"bgp_agent/pkg/processor"
	"bgp_agent/pkg/storage"
)

func (s *APIServer) handleRibRoutes(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}
	q := r.URL.Query()
	window := strings.TrimSpace(q.Get("window"))
	vrf := strings.TrimSpace(q.Get("vrf"))
	neighbor := strings.TrimSpace(q.Get("neighbor_ip"))
	if neighbor == "" {
		neighbor = strings.TrimSpace(q.Get("neighbor"))
	}
	if window == "" || vrf == "" || neighbor == "" {
		http.Error(w, "window, vrf, neighbor_ip required", http.StatusBadRequest)
		return
	}
	page, _ := strconv.Atoi(q.Get("page"))
	if page < 1 {
		page = 1
	}
	pageSize, _ := strconv.Atoi(q.Get("page_size"))
	if pageSize < 1 {
		pageSize = 100
	}
	offset := (page - 1) * pageSize
	routes, total, err := s.storage.ListPeerRoutesPage(window, vrf, neighbor, offset, pageSize)
	if err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	out := make([]map[string]interface{}, 0, len(routes))
	for _, rt := range routes {
		out = append(out, map[string]interface{}{
			"window":      rt.Window,
			"vrf":         rt.VRF,
			"neighbor_ip": rt.NeighborIP,
			"prefix":      rt.Prefix,
			"nexthop":     rt.Nexthop,
			"as_path":     rt.ASPath,
			"remote_as":   rt.RemoteAS,
			"updated_at":  rt.UpdatedAt.UTC().Format("2006-01-02T15:04:05Z"),
		})
	}
	s.writeJSON(w, map[string]interface{}{
		"routes":     out,
		"total":      total,
		"page":       page,
		"page_size":  pageSize,
		"data_store": "redis+rocksdb",
	})
}

func (s *APIServer) handleRibRoutesCount(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}
	q := r.URL.Query()
	window := strings.TrimSpace(q.Get("window"))
	vrf := strings.TrimSpace(q.Get("vrf"))
	neighbor := strings.TrimSpace(q.Get("neighbor_ip"))
	if window == "" || vrf == "" || neighbor == "" {
		http.Error(w, "window, vrf, neighbor_ip required", http.StatusBadRequest)
		return
	}
	ctx := r.Context()
	n, err := s.storage.CountPeerRoutes(ctx, window, vrf, neighbor)
	if err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	s.writeJSON(w, map[string]interface{}{"count": n, "window": window, "vrf": vrf, "neighbor_ip": neighbor})
}

func (s *APIServer) handleRibPolicy(w http.ResponseWriter, r *http.Request) {
	switch r.Method {
	case http.MethodGet:
		vrf := strings.TrimSpace(r.URL.Query().Get("vrf"))
		neighbor := strings.TrimSpace(r.URL.Query().Get("neighbor_ip"))
		if vrf == "" || neighbor == "" {
			http.Error(w, "vrf and neighbor_ip required", http.StatusBadRequest)
			return
		}
		p, err := s.storage.GetPeerPolicy(r.Context(), vrf, neighbor)
		if err != nil {
			http.Error(w, err.Error(), http.StatusInternalServerError)
			return
		}
		s.writeJSON(w, p)
	case http.MethodPost:
		var p storage.PeerPolicy
		if err := json.NewDecoder(r.Body).Decode(&p); err != nil {
			http.Error(w, err.Error(), http.StatusBadRequest)
			return
		}
		if p.VRF == "" || p.NeighborIP == "" {
			http.Error(w, "vrf and neighbor_ip required", http.StatusBadRequest)
			return
		}
		if err := s.processor.SyncPeerPolicy(r.Context(), p); err != nil {
			http.Error(w, err.Error(), http.StatusInternalServerError)
			return
		}
		s.writeJSON(w, map[string]interface{}{"ok": true})
	default:
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
	}
}

func (s *APIServer) handleRibIngestPeer(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}
	vrf := strings.TrimSpace(r.URL.Query().Get("vrf"))
	neighbor := strings.TrimSpace(r.URL.Query().Get("neighbor_ip"))
	window := strings.TrimSpace(r.URL.Query().Get("window"))
	if vrf == "" || neighbor == "" {
		http.Error(w, "vrf and neighbor_ip required", http.StatusBadRequest)
		return
	}
	if window == "" {
		if vrf == processor.VRFGobgpRR || strings.EqualFold(vrf, "rr") {
			window = processor.WindowUpstream
		} else {
			window = processor.WindowDownstream
		}
	}
	ctx := r.Context()
	ingested, removed, err := s.ingestPeerRoutesFromAdjIn(ctx, window, vrf, neighbor)
	if err != nil {
		if err.Error() == "store_routes disabled" {
			s.writeJSON(w, map[string]interface{}{"ok": false, "ingested": 0, "removed": 0, "message": err.Error()})
			return
		}
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	s.writeJSON(w, map[string]interface{}{
		"ok":          true,
		"ingested":    ingested,
		"removed":     removed,
		"window":      window,
		"vrf":         vrf,
		"neighbor_ip": neighbor,
	})
}

// handleRibIngestDownstream 兼容旧路径，等同 ingest-peer（window=downstream）。
func (s *APIServer) handleRibIngestDownstream(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	if strings.TrimSpace(q.Get("window")) == "" {
		q.Set("window", processor.WindowDownstream)
		r.URL.RawQuery = q.Encode()
	}
	s.handleRibIngestPeer(w, r)
}

func (s *APIServer) ingestPeerRoutesFromAdjIn(ctx context.Context, window, vrf, neighbor string) (ingested int, removed int, err error) {
	if !s.storage.ShouldStoreRoutes(ctx, vrf, neighbor) {
		return 0, 0, fmt.Errorf("store_routes disabled")
	}
	batch := make([]storage.PeerRoute, 0, 4096)
	switch window {
	case processor.WindowUpstream:
		learned, err := s.rxAgent.ListAdjInRoutes(ctx, neighbor)
		if err != nil {
			return 0, 0, err
		}
		for _, lr := range learned {
			batch = append(batch, storage.PeerRoute{
				Window:     window,
				VRF:        vrf,
				NeighborIP: neighbor,
				Prefix:     lr.Prefix,
				Nexthop:    lr.Nexthop,
				ASPath:     lr.ASPath,
			})
		}
	default:
		agent, err := s.txPool.GetAgent(vrf)
		if err != nil || agent == nil {
			return 0, 0, fmt.Errorf("tx agent not found for vrf %s", vrf)
		}
		learned, err := agent.ListAdjInRoutes(ctx, neighbor)
		if err != nil {
			return 0, 0, err
		}
		for _, lr := range learned {
			batch = append(batch, storage.PeerRoute{
				Window:     processor.WindowDownstream,
				VRF:        vrf,
				NeighborIP: neighbor,
				Prefix:     lr.Prefix,
				Nexthop:    lr.Nexthop,
				ASPath:     lr.ASPath,
			})
		}
		window = processor.WindowDownstream
	}
	return s.storage.IngestPeerRoutes(ctx, window, vrf, neighbor, batch)
}
