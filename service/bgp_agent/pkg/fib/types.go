package fib

import "time"

const (
	WindowUpstream   = "upstream"
	WindowDownstream = "downstream"
)

// FibRoute FIB 中每个 prefix 的最佳路由。
type FibRoute struct {
	Window     string    `json:"window"`
	Prefix     string    `json:"prefix"`
	Nexthop    string    `json:"nexthop"`
	ASPath     string    `json:"as_path"`
	RemoteAS   uint32    `json:"remote_as"`
	NeighborIP string    `json:"neighbor_ip"`
	SourceIP   string    `json:"source_ip,omitempty"`
	VRF        string    `json:"vrf,omitempty"`
	LocalPref  uint32    `json:"local_pref"`
	Origin     uint32    `json:"origin"`
	UpdatedAt  time.Time `json:"updated_at"`
}

// Candidate 选路候选。
type Candidate struct {
	Prefix     string
	Nexthop    string
	ASPath     string
	RemoteAS   uint32
	NeighborIP string
	SourceIP   string
	VRF        string
	LocalPref  uint32
	Origin     uint32
	ASPathLen  int
}

// FibDiff FIB 变更 diff。
type FibDiff struct {
	Adds      []FibRoute
	Withdraws []string
}
