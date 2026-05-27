package pipeline

import (
	"os"
	"strconv"
	"strings"
)

// RibGapMin 仅用于 consistency API 展示/运维参考；灌库判定不再依赖该阈值。
func RibGapMin() uint64 {
	raw := strings.TrimSpace(os.Getenv("MTR_BGP_RIB_GAP_MIN"))
	if raw == "" {
		return 5000
	}
	n, err := strconv.ParseUint(raw, 10, 64)
	if err != nil || n == 0 {
		return 5000
	}
	return n
}

// NeedsPeerIngest 是否应对该 peer 触发 ADJ-IN 灌库。
// 上游/下游统一：BGP 会话 pfx_rcd 与持久 RIB 条数不一致即灌（无 drift 阈值；含增删）。
// gapMin 参数保留兼容调用方，不参与判定。
func NeedsPeerIngest(window string, pfxRcd uint32, cached int64, gapMin uint64) bool {
	_ = window
	_ = gapMin
	if pfxRcd == 0 && cached == 0 {
		return false
	}
	return uint64(pfxRcd) != uint64(cached)
}
