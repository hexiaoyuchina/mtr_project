package main

import (
	"net/http"
	"strings"

	"bgp_agent/pkg/fib"
)

func (s *APIServer) handlePipelineRepair(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}
	if s.pipeline == nil {
		http.Error(w, "pipeline not ready", http.StatusServiceUnavailable)
		return
	}
	window := strings.TrimSpace(r.URL.Query().Get("window"))
	if window == "" {
		window = fib.WindowUpstream
	}
	jobID, ok := s.pipeline.RepairWindow(r.Context(), window)
	if !ok {
		http.Error(w, "repair not started", http.StatusServiceUnavailable)
		return
	}
	s.writeJSON(w, map[string]interface{}{"ok": true, "job_id": jobID, "window": window})
}

func (s *APIServer) handleKernelReconcile(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}
	if s.pipeline == nil {
		http.Error(w, "pipeline not ready", http.StatusServiceUnavailable)
		return
	}
	window := strings.TrimSpace(r.URL.Query().Get("window"))
	if window == "" {
		window = fib.WindowUpstream
	}
	jobID, started := s.pipeline.EnqueueKernelReconcile(window)
	if jobID == "" {
		http.Error(w, "kernel reconcile not started", http.StatusServiceUnavailable)
		return
	}
	s.writeJSON(w, map[string]interface{}{
		"ok": true, "window": window, "job_id": jobID, "started": started,
	})
}

func (s *APIServer) handlePipelineStatus(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}
	if s.pipeline == nil {
		http.Error(w, "pipeline not ready", http.StatusServiceUnavailable)
		return
	}
	jobID := strings.TrimSpace(r.URL.Query().Get("job_id"))
	if jobID == "" {
		http.Error(w, "job_id required", http.StatusBadRequest)
		return
	}
	j, ok := s.pipeline.GetJob(jobID)
	if !ok {
		http.Error(w, "job not found", http.StatusNotFound)
		return
	}
	s.writeJSON(w, j)
}

func (s *APIServer) handlePipelineConsistency(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}
	if s.pipeline == nil {
		http.Error(w, "pipeline not ready", http.StatusServiceUnavailable)
		return
	}
	s.writeJSON(w, s.pipeline.ConsistencySnapshot(r.Context()))
}
