package main

import (
	"encoding/json"
	"fmt"
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
	txAgent   *tx.TxAgent
	storage   *storage.Storage
	mux       *http.ServeMux
}

// NewAPIServer 创建API服务器
func NewAPIServer(
	addr string,
	proc *processor.Processor,
	rxAgent *rx.RxAgent,
	txAgent *tx.TxAgent,
	store *storage.Storage,
) *APIServer {
	s := &APIServer{
		addr:      addr,
		processor: proc,
		rxAgent:   rxAgent,
		txAgent:   txAgent,
		storage:   store,
		mux:       http.NewServeMux(),
	}
	
	s.registerRoutes()
	return s
}

// registerRoutes 注册路由
func (s *APIServer) registerRoutes() {
	// 健康检查
	s.mux.HandleFunc("/health", s.handleHealth)
	
	// 状态查询
	s.mux.HandleFunc("/api/status", s.handleStatus)
	
	// BGP邻居管理（TX）
	s.mux.HandleFunc("/api/neighbors", s.handleNeighbors)
	s.mux.HandleFunc("/api/neighbors/add", s.handleAddNeighbor)
	s.mux.HandleFunc("/api/neighbors/remove", s.handleRemoveNeighbor)
	
	// 路由查询
	s.mux.HandleFunc("/api/routes", s.handleRoutes)
	s.mux.HandleFunc("/api/routes/count", s.handleRouteCount)
	
	// RR连接管理
	s.mux.HandleFunc("/api/rr/status", s.handleRRStatus)
	s.mux.HandleFunc("/api/rr/freeze", s.handleFreeze)
	s.mux.HandleFunc("/api/rr/unfreeze", s.handleUnfreeze)
	
	// 存储统计
	s.mux.HandleFunc("/api/storage/stats", s.handleStorageStats)
}

// Start 启动API服务
func (s *APIServer) Start() error {
	log.Printf("管理API启动: %s", s.addr)
	return http.ListenAndServe(s.addr, s.corsMiddleware(s.mux))
}

// corsMiddleware CORS中间件
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

// handleHealth 健康检查
func (s *APIServer) handleHealth(w http.ResponseWriter, r *http.Request) {
	s.writeJSON(w, map[string]interface{}{
		"status": "ok",
		"time":   time.Now().Format(time.RFC3339),
	})
}

// handleStatus 获取系统状态
func (s *APIServer) handleStatus(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	
	rxStatus, _ := s.rxAgent.GetStatus(ctx)
	txStatus, _ := s.txAgent.GetStatus(ctx)
	procStatus := s.processor.GetStatus()
	
	s.writeJSON(w, map[string]interface{}{
		"rx":        rxStatus,
		"tx":        txStatus,
		"processor": procStatus,
		"timestamp": time.Now().Format(time.RFC3339),
	})
}

// handleNeighbors 获取所有BGP邻居
func (s *APIServer) handleNeighbors(w http.ResponseWriter, r *http.Request) {
	// TODO: 实现邻居列表
	s.writeJSON(w, map[string]interface{}{
		"neighbors": []interface{}{},
	})
}

// handleAddNeighbor 添加BGP邻居
func (s *APIServer) handleAddNeighbor(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}
	
	var req struct {
		Address  string `json:"address"`
		RemoteAS uint32 `json:"remote_as"`
	}
	
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	
	ctx := r.Context()
	if err := s.txAgent.AddNeighbor(ctx, req.Address, req.RemoteAS); err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	
	s.writeJSON(w, map[string]interface{}{
		"ok":      true,
		"message": fmt.Sprintf("Added neighbor %s AS%d", req.Address, req.RemoteAS),
	})
}

// handleRemoveNeighbor 删除BGP邻居
func (s *APIServer) handleRemoveNeighbor(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}
	
	var req struct {
		Address string `json:"address"`
	}
	
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	
	ctx := r.Context()
	if err := s.txAgent.RemoveNeighbor(ctx, req.Address); err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	
	s.writeJSON(w, map[string]interface{}{
		"ok":      true,
		"message": fmt.Sprintf("Removed neighbor %s", req.Address),
	})
}

// handleRoutes 获取路由列表
func (s *APIServer) handleRoutes(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	routes, err := s.storage.ListRoutes(ctx)
	if err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	
	s.writeJSON(w, map[string]interface{}{
		"routes": routes,
		"total":  len(routes),
	})
}

// handleRouteCount 获取路由数量
func (s *APIServer) handleRouteCount(w http.ResponseWriter, r *http.Request) {
	count := s.processor.GetRouteCount()
	s.writeJSON(w, map[string]interface{}{
		"count": count,
	})
}

// handleRRStatus 获取RR连接状态
func (s *APIServer) handleRRStatus(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	rxStatus, _ := s.rxAgent.GetStatus(ctx)
	
	s.writeJSON(w, map[string]interface{}{
		"connected": s.processor.IsRRConnected(),
		"frozen":    s.txAgent.IsFrozen(),
		"rx_status": rxStatus,
	})
}

// handleFreeze 冻结（测试用）
func (s *APIServer) handleFreeze(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}
	
	s.processor.SetRRConnected(false)
	s.txAgent.Freeze()
	
	s.writeJSON(w, map[string]interface{}{
		"ok":      true,
		"message": "System frozen, keeping current RIB",
	})
}

// handleUnfreeze 解冻（测试用）
func (s *APIServer) handleUnfreeze(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}
	
	s.processor.SetRRConnected(true)
	s.txAgent.Unfreeze()
	
	s.writeJSON(w, map[string]interface{}{
		"ok":      true,
		"message": "System unfrozen, accepting updates",
	})
}

// handleStorageStats 获取存储统计
func (s *APIServer) handleStorageStats(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	stats, err := s.storage.GetStats(ctx)
	if err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	
	s.writeJSON(w, stats)
}

// writeJSON 写入JSON响应
func (s *APIServer) writeJSON(w http.ResponseWriter, data interface{}) {
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(data)
}
