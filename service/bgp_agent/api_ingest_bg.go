package main

import (
	"context"
	"log"
	"strings"
	"time"
)

func ingestPeerKey(window, vrf, neighbor string) string {
	return window + "|" + vrf + "|" + neighbor
}

// maybeBackgroundIngestPeer RR/下游 Established 且开启入库时，后台全量灌库（去重并发）。
func (s *APIServer) maybeBackgroundIngestPeer(window, vrf, neighbor string) {
	vrf = strings.TrimSpace(vrf)
	neighbor = strings.TrimSpace(neighbor)
	if vrf == "" || neighbor == "" {
		return
	}
	ctx := context.Background()
	if !s.storage.ShouldStoreRoutes(ctx, vrf, neighbor) {
		return
	}
	key := ingestPeerKey(window, vrf, neighbor)
	s.ingestMu.Lock()
	if s.ingestInflight == nil {
		s.ingestInflight = make(map[string]bool)
	}
	if s.ingestInflight[key] {
		s.ingestMu.Unlock()
		return
	}
	s.ingestInflight[key] = true
	s.ingestMu.Unlock()

	go func() {
		defer func() {
			s.ingestMu.Lock()
			delete(s.ingestInflight, key)
			s.ingestMu.Unlock()
		}()
		runCtx, cancel := context.WithTimeout(context.Background(), 2*time.Hour)
		defer cancel()
		ingested, removed, err := s.ingestPeerRoutesFromAdjIn(runCtx, window, vrf, neighbor)
		if err != nil {
			log.Printf("background ingest %s %s/%s: %v", window, vrf, neighbor, err)
			return
		}
		log.Printf("background ingest %s %s/%s: ingested=%d removed=%d", window, vrf, neighbor, ingested, removed)
	}()
}

func (s *APIServer) notePeerEstablished(window, vrf, neighbor string) {
	s.maybeBackgroundIngestPeer(window, vrf, neighbor)
}

// maybeReconcileIngestGap 会话已 Up 但持久库明显少于 Received 时补一次全量灌库。
func (s *APIServer) maybeReconcileIngestGap(ctx context.Context, window, vrf, neighbor string, pfxRcd uint32) {
	if pfxRcd == 0 {
		return
	}
	if !s.storage.ShouldStoreRoutes(ctx, vrf, neighbor) {
		return
	}
	n, err := s.storage.CountPeerRoutes(ctx, window, vrf, neighbor)
	if err != nil {
		return
	}
	cached := uint64(n)
	const minGap uint64 = 5000
	if uint64(pfxRcd) <= cached || uint64(pfxRcd)-cached < minGap {
		return
	}
	log.Printf("ingest gap %s %s/%s: pfx_rcd=%d cached=%d, scheduling ingest", window, vrf, neighbor, pfxRcd, n)
	s.maybeBackgroundIngestPeer(window, vrf, neighbor)
}
