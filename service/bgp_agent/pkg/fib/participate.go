package fib

import (
	"strings"

	"bgp_agent/pkg/processor"
)

// ParticipateCtx 选路参与上下文（由 Processor 连接状态驱动）。
type ParticipateCtx struct {
	proc *processor.Processor
}

func NewParticipateCtx(proc *processor.Processor) *ParticipateCtx {
	return &ParticipateCtx{proc: proc}
}

// UpstreamParticipates RR 断链仍参与 upstream_fib。
func (c *ParticipateCtx) UpstreamParticipates(neighbor string) bool {
	return strings.TrimSpace(neighbor) != ""
}

// DownstreamParticipates 下游断链不参与 downstream_fib。
func (c *ParticipateCtx) DownstreamParticipates(vrf, neighbor string) bool {
	if c.proc == nil {
		return true
	}
	return c.proc.IsDownstreamPeerConnected(vrf, neighbor)
}
