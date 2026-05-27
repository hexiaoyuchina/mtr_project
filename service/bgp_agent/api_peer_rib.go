package main

import (
	"context"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"net/url"
	"strconv"
	"strings"

	"bgp_agent/pkg/processor"
	"bgp_agent/pkg/rx"
	"bgp_agent/pkg/storage"
	"bgp_agent/pkg/tx"
)

func querySourceIP(q url.Values, window string) string {
	if window != processor.WindowDownstream {
		return ""
	}
	return strings.TrimSpace(q.Get("source_ip"))
}

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
	sourceIP := querySourceIP(q, window)
	if window == "" || vrf == "" || neighbor == "" {
		http.Error(w, "window, vrf, neighbor_ip required", http.StatusBadRequest)
		return
	}
	ctx := r.Context()
	if pfxRaw := strings.TrimSpace(q.Get("prefix")); pfxRaw != "" {
		pfx, err := normalizeIPv4PrefixExact(pfxRaw)
		if err != nil {
			http.Error(w, "invalid prefix: "+err.Error(), http.StatusBadRequest)
			return
		}
		rt, err := s.storage.GetPeerRoute(ctx, window, vrf, neighbor, sourceIP, pfx)
		if err != nil {
			http.Error(w, err.Error(), http.StatusInternalServerError)
			return
		}
		out := make([]map[string]interface{}, 0, 1)
		if rt != nil {
			out = append(out, peerRouteJSON(rt))
		}
		s.writeJSON(w, map[string]interface{}{
			"routes":     out,
			"total":      len(out),
			"page":       1,
			"page_size":  len(out),
			"prefix":     pfx,
			"source_ip":  sourceIP,
			"data_store": "redis+rocksdb",
		})
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
	routes, total, err := s.storage.ListPeerRoutesPage(window, vrf, neighbor, sourceIP, offset, pageSize)
	if err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	out := make([]map[string]interface{}, 0, len(routes))
	for _, rt := range routes {
		out = append(out, peerRouteJSON(&rt))
	}
	s.writeJSON(w, map[string]interface{}{
		"routes":     out,
		"total":      total,
		"page":       page,
		"page_size":  pageSize,
		"source_ip":  sourceIP,
		"data_store": "redis+rocksdb",
	})
}

func peerRouteJSON(rt *storage.PeerRoute) map[string]interface{} {
	return map[string]interface{}{
		"window":      rt.Window,
		"vrf":         rt.VRF,
		"neighbor_ip": rt.NeighborIP,
		"source_ip":   rt.SourceIP,
		"prefix":      rt.Prefix,
		"nexthop":     rt.Nexthop,
		"as_path":     rt.ASPath,
		"remote_as":   rt.RemoteAS,
		"updated_at":  rt.UpdatedAt.UTC().Format("2006-01-02T15:04:05Z"),
	}
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
	sourceIP := querySourceIP(q, window)
	if window == "" || vrf == "" || neighbor == "" {
		http.Error(w, "window, vrf, neighbor_ip required", http.StatusBadRequest)
		return
	}
	ctx := r.Context()
	n, err := s.storage.CountPeerRoutes(ctx, window, vrf, neighbor, sourceIP)
	if err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	s.writeJSON(w, map[string]interface{}{
		"count": n, "window": window, "vrf": vrf, "neighbor_ip": neighbor, "source_ip": sourceIP,
	})
}

func (s *APIServer) enrichPeerPolicy(ctx context.Context, p *storage.PeerPolicy) {
	storage.NormalizePeerPolicy(p, p.VRF, p.NeighborIP)
	if p.Window == processor.WindowDownstream && strings.TrimSpace(p.SourceIP) == "" {
		if sip := s.resolveDownstreamSourceIP(ctx, p.VRF, p.NeighborIP); sip != "" {
			p.SourceIP = sip
		}
	}
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
		s.enrichPeerPolicy(r.Context(), &p)
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
		if p.StoreRoutes || p.Enabled {
			p.StoreRoutes = true
		}
		s.enrichPeerPolicy(r.Context(), &p)
		if err := s.processor.SyncPeerPolicy(r.Context(), p); err != nil {
			http.Error(w, err.Error(), http.StatusInternalServerError)
			return
		}
		win := strings.TrimSpace(p.Window)
		if win == "" {
			if p.VRF == processor.VRFGobgpRR {
				win = processor.WindowUpstream
			} else {
				win = processor.WindowDownstream
			}
		}
		s.maybeBackgroundIngestPeer(win, p.VRF, p.NeighborIP, p.SourceIP)
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
	q := r.URL.Query()
	vrf := strings.TrimSpace(q.Get("vrf"))
	neighbor := strings.TrimSpace(q.Get("neighbor_ip"))
	window := strings.TrimSpace(q.Get("window"))
	sourceIP := strings.TrimSpace(q.Get("source_ip"))
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
	if window == processor.WindowDownstream && sourceIP == "" {
		sourceIP = s.resolveDownstreamSourceIP(r.Context(), vrf, neighbor)
	}
	if s.pipeline != nil {
		if ingestJobID, busy := s.pipeline.IngestJobForPeer(window, vrf, neighbor, sourceIP); busy {
			w.WriteHeader(http.StatusConflict)
			s.writeJSON(w, map[string]interface{}{
				"ok": false, "error": "background ingest already running", "ingest_job_id": ingestJobID,
			})
			return
		}
	}
	ctx := r.Context()
	ingested, removed, err := s.ingestPeerRoutesCore(ctx, window, vrf, neighbor, sourceIP)
	if err != nil {
		if err.Error() == "store_routes disabled" {
			s.writeJSON(w, map[string]interface{}{"ok": false, "ingested": 0, "removed": 0, "message": err.Error()})
			return
		}
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	var fibJobID string
	if s.pipeline != nil {
		win := window
		if win != processor.WindowUpstream {
			win = processor.WindowDownstream
		}
		fibJobID, _ = s.pipeline.EnqueueFibRecompute(win)
	}
	s.writeJSON(w, map[string]interface{}{
		"ok":          true,
		"ingested":    ingested,
		"removed":     removed,
		"window":      window,
		"vrf":         vrf,
		"neighbor_ip": neighbor,
		"source_ip":   sourceIP,
		"fib_job_id":  fibJobID,
	})
}

func (s *APIServer) handleRibPurgePeer(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost && r.Method != http.MethodDelete {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}
	q := r.URL.Query()
	window := strings.TrimSpace(q.Get("window"))
	vrf := strings.TrimSpace(q.Get("vrf"))
	neighbor := strings.TrimSpace(q.Get("neighbor_ip"))
	sourceIP := strings.TrimSpace(q.Get("source_ip"))
	if vrf == "" || neighbor == "" {
		http.Error(w, "vrf and neighbor_ip required", http.StatusBadRequest)
		return
	}
	if window == "" {
		if vrf == processor.VRFGobgpRR {
			window = processor.WindowUpstream
		} else {
			window = processor.WindowDownstream
		}
	}
	if window == processor.WindowDownstream && sourceIP == "" {
		sourceIP = s.resolveDownstreamSourceIP(r.Context(), vrf, neighbor)
	}
	ctx := r.Context()
	removed, err := s.processor.PurgePeerRIB(ctx, window, vrf, neighbor, sourceIP)
	if err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	var fibJobID string
	if s.pipeline != nil {
		fibJobID, _ = s.pipeline.EnqueueFibRecompute(window)
	}
	s.writeJSON(w, map[string]interface{}{
		"ok": true, "removed": removed, "window": window, "vrf": vrf,
		"neighbor_ip": neighbor, "source_ip": sourceIP, "fib_job_id": fibJobID,
	})
}

func (s *APIServer) resolveDownstreamSourceIP(ctx context.Context, vrf, neighbor string) string {
	peers, err := s.txPool.ListAllPeers(ctx)
	if err != nil {
		return ""
	}
	for _, p := range peers {
		if p.Vrf == vrf && p.Address == neighbor {
			return p.LocalAddress
		}
	}
	pol, _ := s.storage.GetPeerPolicy(ctx, vrf, neighbor)
	return pol.SourceIP
}

func (s *APIServer) handleRibIngestDownstream(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	if strings.TrimSpace(q.Get("window")) == "" {
		q.Set("window", processor.WindowDownstream)
		r.URL.RawQuery = q.Encode()
	}
	s.handleRibIngestPeer(w, r)
}

func (s *APIServer) ingestPeerRoutesCore(ctx context.Context, window, vrf, neighbor, sourceIP string) (ingested int, removed int, err error) {
	if !s.storage.ShouldStoreRoutes(ctx, vrf, neighbor) {
		return 0, 0, fmt.Errorf("store_routes disabled")
	}
	win := window
	if win != processor.WindowUpstream {
		win = processor.WindowDownstream
		_, _ = s.storage.MigrateLegacyDownstreamPeerRIB(ctx, vrf, neighbor, sourceIP)
	}
	builder := s.storage.NewPeerIngestBuilder(win, vrf, neighbor, sourceIP)
	switch win {
	case processor.WindowUpstream:
		err = s.rxAgent.WalkAdjInRoutes(ctx, neighbor, func(lr rx.LearnedRoute) error {
			return builder.Add(ctx, lr.Prefix, lr.Nexthop, lr.ASPath, 0)
		})
	default:
		agent, gerr := s.txPool.GetAgent(vrf)
		if gerr != nil || agent == nil {
			return 0, 0, fmt.Errorf("tx agent not found for vrf %s", vrf)
		}
		err = agent.WalkAdjInRoutes(ctx, neighbor, func(lr tx.LearnedRoute) error {
			return builder.Add(ctx, lr.Prefix, lr.Nexthop, lr.ASPath, 0)
		})
	}
	if err != nil {
		return 0, 0, err
	}
	return builder.Finish(ctx)
}

func normalizeIPv4PrefixExact(raw string) (string, error) {
	s := strings.TrimSpace(raw)
	if s == "" {
		return "", fmt.Errorf("empty")
	}
	if !strings.Contains(s, "/") {
		s += "/32"
	}
	_, ipNet, err := net.ParseCIDR(s)
	if err != nil {
		return "", err
	}
	if ipNet.IP.To4() == nil {
		return "", fmt.Errorf("ipv4 only")
	}
	ones, _ := ipNet.Mask.Size()
	return ipNet.IP.String() + "/" + strconv.Itoa(ones), nil
}
