package main

import (
	"context"
	"net/http"
	"strconv"
	"strings"

	"bgp_agent/pkg/fib"
)

func (s *APIServer) handleFibRecompute(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}
	if s.fibEngine == nil {
		http.Error(w, "fib engine not ready", http.StatusServiceUnavailable)
		return
	}
	window := strings.TrimSpace(r.URL.Query().Get("window"))
	if window == "" {
		window = fib.WindowUpstream
	}
	if strings.TrimSpace(r.URL.Query().Get("sync")) == "1" {
		if err := s.fibEngine.RecomputeAll(r.Context(), window, nil); err != nil {
			http.Error(w, err.Error(), http.StatusInternalServerError)
			return
		}
		if s.exportCoord != nil {
			go s.exportCoord.Reconcile(r.Context())
		}
		s.writeJSON(w, map[string]interface{}{"ok": true, "window": window, "sync": true})
		return
	}
	if s.pipeline == nil {
		http.Error(w, "pipeline not ready", http.StatusServiceUnavailable)
		return
	}
	jobID, started := s.pipeline.EnqueueFibRecompute(window)
	if jobID == "" {
		http.Error(w, "fib job not started", http.StatusServiceUnavailable)
		return
	}
	s.writeJSON(w, map[string]interface{}{
		"ok": true, "window": window, "job_id": jobID, "started": started,
	})
}

func (s *APIServer) handleFibRoutes(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}
	if s.fibEngine == nil {
		http.Error(w, "fib engine not ready", http.StatusServiceUnavailable)
		return
	}
	q := r.URL.Query()
	window := strings.TrimSpace(q.Get("window"))
	if window == "" {
		window = fib.WindowUpstream
	}
	ctx := r.Context()
	if pfxRaw := strings.TrimSpace(q.Get("prefix")); pfxRaw != "" {
		pfx, err := normalizeIPv4PrefixExact(pfxRaw)
		if err != nil {
			http.Error(w, "invalid prefix: "+err.Error(), http.StatusBadRequest)
			return
		}
		rt, err := s.fibEngine.Store().Get(ctx, window, pfx)
		if err != nil {
			http.Error(w, err.Error(), http.StatusInternalServerError)
			return
		}
		out := make([]map[string]interface{}, 0, 1)
		if rt != nil {
			out = append(out, map[string]interface{}{
				"window":      rt.Window,
				"prefix":      rt.Prefix,
				"nexthop":     rt.Nexthop,
				"as_path":     rt.ASPath,
				"neighbor_ip": rt.NeighborIP,
				"source_ip":   rt.SourceIP,
				"vrf":         rt.VRF,
				"updated_at":  rt.UpdatedAt.UTC().Format("2006-01-02T15:04:05Z"),
			})
		}
		s.writeJSON(w, map[string]interface{}{
			"routes":     out,
			"total":      len(out),
			"page":       1,
			"page_size":  len(out),
			"prefix":     pfx,
			"window":     window,
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
	routes, total, err := s.fibEngine.Store().ListPage(window, offset, pageSize)
	if err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	out := make([]map[string]interface{}, 0, len(routes))
	for _, rt := range routes {
		out = append(out, map[string]interface{}{
			"window":      rt.Window,
			"prefix":      rt.Prefix,
			"nexthop":     rt.Nexthop,
			"as_path":     rt.ASPath,
			"neighbor_ip": rt.NeighborIP,
			"source_ip":   rt.SourceIP,
			"vrf":         rt.VRF,
			"updated_at":  rt.UpdatedAt.UTC().Format("2006-01-02T15:04:05Z"),
		})
	}
	s.writeJSON(w, map[string]interface{}{
		"routes":     out,
		"total":      total,
		"page":       page,
		"page_size":  pageSize,
		"window":     window,
		"data_store": "redis+rocksdb",
	})
}

func (s *APIServer) handleFibRoutesCount(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}
	if s.fibEngine == nil {
		http.Error(w, "fib engine not ready", http.StatusServiceUnavailable)
		return
	}
	window := strings.TrimSpace(r.URL.Query().Get("window"))
	if window == "" {
		window = fib.WindowUpstream
	}
	n, err := s.fibEngine.Store().Count(r.Context(), window)
	if err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	s.writeJSON(w, map[string]interface{}{"count": n, "window": window})
}

func (s *APIServer) handleExportReconcile(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}
	if s.exportCoord == nil {
		http.Error(w, "export not ready", http.StatusServiceUnavailable)
		return
	}
	force := strings.TrimSpace(r.URL.Query().Get("force")) == "1"
	if force {
		go s.exportCoord.ReconcileForce(r.Context())
		s.writeJSON(w, map[string]interface{}{"ok": true, "message": "force reconcile started", "force": true})
		return
	}
	go s.exportCoord.Reconcile(r.Context())
	s.writeJSON(w, map[string]interface{}{"ok": true, "message": "reconcile started"})
}

// RunStartupRepair Agent 启动后检测 RIB/FIB 漂移并排队 repair（替代盲目 export reconcile）。
func (s *APIServer) RunStartupRepair(ctx context.Context) {
	if s.pipeline != nil {
		for _, w := range []string{fib.WindowDownstream, fib.WindowUpstream} {
			s.pipeline.RepairWindow(ctx, w)
		}
		return
	}
	if s.exportCoord != nil {
		s.exportCoord.Reconcile(ctx)
	}
}
