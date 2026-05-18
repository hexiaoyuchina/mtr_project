package tx

// PeerStatus BGP 邻居摘要（供 OP 列表展示）。
type PeerStatus struct {
	Vrf           string `json:"vrf"`
	Address       string `json:"address"`
	RemoteAS      uint32 `json:"remote_as"`
	LocalAddress  string `json:"local_address"`
	Session       string `json:"session"` // tx
	State         string `json:"state"`
	PfxRcd        uint32 `json:"pfx_rcd"`
	PfxAdv        uint32 `json:"pfx_adv"`
	Enabled       bool   `json:"enabled"`
}
