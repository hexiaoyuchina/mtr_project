package main

import (
	"encoding/json"
	"log"
	"net/http"
	"time"

	"bgp_agent/pkg/processor"
	"bgp_agent/pkg/rx"
	"bgp_agent/pkg/storage"
	"bgp_agent/pkg/tx"
)

// APIServer 管理API服务器
type APIServer struct {
	addr      string
	processor *processor.Processor
	rxAgent   *rx.RxAgent
	txPool    *tx.Pool
	storage   *storage.Storage
	mux       *http.ServeMux
}

// NewAPIServer 创建API服务器
func NewAPIServer(
	addr string,
	proc *processor.Processor,
	rxAgent *rx.RxAgent,
	txPool *tx.Pool,
	store *storage.Storage,
) *APIServer {
	s := &APIServer{
		addr:      addr,
		processor: proc,
		rxAgent:   rxAgent,
		txPool:    txPool,
		storage:   store,
		mux:       http.NewServeMux(),
	}
	s.registerRoutes()
	return s
}

func (s *APIServer) registerRoutes() {
	s.mux.HandleFunc("/health", s.handleHealth)
	s.mux.HandleFunc("/api/status", s.handleStatus)
	s.mux.HandleFunc("/api/neighbors", s.handleNeighbors)
	s.mux.HandleFunc("/api/neighbors/add", s.handleAddNeighbor)
	s.mux.HandleFunc("/api/neighbors/remove", s.handleRemoveNeighbor)
	s.mux.HandleFunc("/api/neighbors/toggle", s.handleNeighborToggle)
	s.mux.HandleFunc("/api/rr/config", s.handleRRConfig)
	s.mux.HandleFunc("/api/rr/remove", s.handleRRRemove)
	s.mux.HandleFunc("/api/routes", s.handleRoutes)
	s.mux.HandleFunc("/api/routes/count", s.handleRouteCount)
	s.mux.HandleFunc("/api/rr/status", s.handleRRStatus)
	s.mux.HandleFunc("/api/rr/freeze", s.handleFreeze)
	s.mux.HandleFunc("/api/rr/unfreeze", s.handleUnfreeze)
	s.mux.HandleFunc("/api/storage/stats", s.handleStorageStats)
	s.mux.HandleFunc("/api/tx/routes", s.handleTxRoutes)
	s.mux.HandleFunc("/api/tx/learned-routes", s.handleTxLearnedRoutes)
	s.mux.HandleFunc("/api/rr/routes", s.handleRRRoutes)
	s.mux.HandleFunc("/api/peers/freeze-status", s.handlePeersFreezeStatus)
}

func (s *APIServer) Start() error {
	log.Printf("管理API启动: %s", s.addr)
	return http.ListenAndServe(s.addr, s.corsMiddleware(s.mux))
}

func (s *APIServer) corsMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Access-Control-Allow-Origin", "*")
		w.Header().Set("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
		w.Header().Set("Access-Control-Allow-Headers", "Content-Type")
		if r.Method == "OPTIONS" {
			w.WriteHeader(http.StatusOK)
			return
		}
		next.ServeHTTP(w, r)
	})
}

func (s *APIServer) handleHealth(w http.ResponseWriter, r *http.Request) {
	s.writeJSON(w, map[string]interface{}{
		"status": "ok",
		"time":   time.Now().Format(time.RFC3339),
	})
}

func (s *APIServer) handleStatus(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	rxStatus, _ := s.rxAgent.GetStatus(ctx)
	procStatus := s.processor.GetStatus()
	s.writeJSON(w, map[string]interface{}{
		"rx":        rxStatus,
		"processor": procStatus,
		"timestamp": time.Now().Format(time.RFC3339),
	})
}

func (s *APIServer) handleRoutes(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	routes, err := s.storage.ListRoutes(ctx)
	if err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	s.writeJSON(w, map[string]interface{}{"routes": routes, "total": len(routes)})
}

func (s *APIServer) handleRouteCount(w http.ResponseWriter, r *http.Request) {
	s.writeJSON(w, map[string]interface{}{"count": s.processor.GetRouteCount()})
}

func (s *APIServer) handleRRStatus(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	rxStatus, _ := s.rxAgent.GetStatus(ctx)
	s.writeJSON(w, map[string]interface{}{
		"connected": s.processor.IsRRConnected(),
		"frozen":    s.txPool != nil,
		"rx_status": rxStatus,
	})
}

func (s *APIServer) handleFreeze(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}
	s.syncPeerFreezeState(r.Context())
	s.processor.SetRRConnected(false)
	s.txPool.FreezeAll()
	s.writeJSON(w, map[string]interface{}{"ok": true, "message": "System frozen"})
}

func (s *APIServer) handleUnfreeze(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}
	s.processor.SetRRConnected(true)
	s.txPool.UnfreezeAll()
	s.syncPeerFreezeState(r.Context())
	s.writeJSON(w, map[string]interface{}{"ok": true, "message": "System unfrozen"})
}

func (s *APIServer) handleStorageStats(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	stats, err := s.storage.GetStats(ctx)
	if err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	s.writeJSON(w, stats)
}

func (s *APIServer) writeJSON(w http.ResponseWriter, data interface{}) {
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(data)
}
