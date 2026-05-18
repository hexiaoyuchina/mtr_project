package rx

import (
	api "github.com/osrg/gobgp/v3/api"
)

// RRPeerStatus 单个上游 RR 邻居状态。
type RRPeerStatus struct {
	Address      string
	RemoteAS     uint32
	State        string
	PfxRcd       uint32
	PfxAdv       uint32
	Enabled      bool
	LocalAddress string
}

type rrPeerEntry struct {
	addr   string
	asn    uint32
	cached *api.Peer
}
