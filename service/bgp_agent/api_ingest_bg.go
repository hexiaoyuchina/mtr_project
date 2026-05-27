package main

import (
	"context"
	"strings"

	"bgp_agent/pkg/pipeline"
	"bgp_agent/pkg/processor"
)

func (s *APIServer) maybeBackgroundIngestPeer(window, vrf, neighbor, sourceIP string) {
	if s.pipeline == nil {
		return
	}
	vrf = strings.TrimSpace(vrf)
	neighbor = strings.TrimSpace(neighbor)
	if vrf == "" || neighbor == "" {
		return
	}
	ctx := context.Background()
	if !s.storage.ShouldStoreRoutes(ctx, vrf, neighbor) {
		return
	}
	s.pipeline.EnqueueIngest(window, vrf, neighbor, sourceIP)
}

func (s *APIServer) notePeerEstablished(window, vrf, neighbor, sourceIP string) {
	s.maybeBackgroundIngestPeer(window, vrf, neighbor, sourceIP)
}

func (s *APIServer) maybeReconcileIngestGap(ctx context.Context, window, vrf, neighbor, sourceIP string, pfxRcd uint32) {
	if pfxRcd == 0 || s.pipeline == nil {
		return
	}
	if !s.storage.ShouldStoreRoutes(ctx, vrf, neighbor) {
		return
	}
	if window == processor.WindowDownstream {
		_, _ = s.storage.MigrateLegacyDownstreamPeerRIB(ctx, vrf, neighbor, sourceIP)
	}
	n, err := s.storage.CountPeerRoutes(ctx, window, vrf, neighbor, sourceIP)
	if err != nil {
		return
	}
	if !pipeline.NeedsPeerIngest(window, pfxRcd, n, pipeline.RibGapMin()) {
		return
	}
	s.pipeline.EnqueueIngest(window, vrf, neighbor, sourceIP)
}

func (s *APIServer) listPeerSnapshots(ctx context.Context) []pipeline.PeerSnapshot {
	var out []pipeline.PeerSnapshot
	rrPeers, _ := s.rxAgent.ListRRPeers(ctx)
	for _, p := range rrPeers {
		if strings.TrimSpace(p.Address) == "" {
			continue
		}
		n, _ := s.storage.CountPeerRoutes(ctx, processor.WindowUpstream, processor.VRFGobgpRR, p.Address, p.LocalAddress)
		out = append(out, pipeline.PeerSnapshot{
			Window:      processor.WindowUpstream,
			VRF:         processor.VRFGobgpRR,
			NeighborIP:  p.Address,
			SourceIP:    p.LocalAddress,
			PfxRcd:      p.PfxRcd,
			RibCount:    n,
			Established: strings.Contains(strings.ToUpper(p.State), "ESTABLISHED"),
		})
	}
	txPeers, _ := s.txPool.ListAllPeers(ctx)
	for _, p := range txPeers {
		n, _ := s.storage.CountPeerRoutes(ctx, processor.WindowDownstream, p.Vrf, p.Address, p.LocalAddress)
		out = append(out, pipeline.PeerSnapshot{
			Window:      processor.WindowDownstream,
			VRF:         p.Vrf,
			NeighborIP:  p.Address,
			SourceIP:    p.LocalAddress,
			PfxRcd:      p.PfxRcd,
			RibCount:    n,
			Established: strings.Contains(strings.ToUpper(p.State), "ESTABLISHED"),
		})
	}
	return out
}
