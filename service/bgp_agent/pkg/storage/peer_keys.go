package storage

import (
	"fmt"
	"strings"
)

const WindowUpstream = "upstream"
const WindowDownstream = "downstream"

// NormalizeSourceIP downstream RIB 键中的本端源地址；空则用占位符。
func NormalizeSourceIP(window, sourceIP string) string {
	if window != WindowDownstream {
		return ""
	}
	s := strings.TrimSpace(sourceIP)
	if s == "" || s == "0.0.0.0" {
		return "_default_"
	}
	return s
}

func peerRouteRedisKey(window, vrf, neighbor, sourceIP, prefix string) string {
	if window == WindowDownstream {
		sip := NormalizeSourceIP(window, sourceIP)
		return fmt.Sprintf("rib:downstream:%s:%s:%s:%s", vrf, neighbor, sip, prefix)
	}
	return fmt.Sprintf("rib:%s:%s:%s:%s", window, vrf, neighbor, prefix)
}

func peerRouteRocksKey(window, vrf, neighbor, sourceIP, prefix string) []byte {
	if window == WindowDownstream {
		sip := NormalizeSourceIP(window, sourceIP)
		return []byte(fmt.Sprintf("r:downstream:%s:%s:%s:%s", vrf, neighbor, sip, prefix))
	}
	return []byte(fmt.Sprintf("r:%s:%s:%s:%s", window, vrf, neighbor, prefix))
}

func peerCountRedisKey(window, vrf, neighbor, sourceIP string) string {
	if window == WindowDownstream {
		sip := NormalizeSourceIP(window, sourceIP)
		return fmt.Sprintf("rib:cnt:downstream:%s:%s:%s", vrf, neighbor, sip)
	}
	return fmt.Sprintf("rib:cnt:%s:%s:%s", window, vrf, neighbor)
}

func peerScanPrefix(window, vrf, neighbor, sourceIP string) string {
	if window == WindowDownstream {
		sip := NormalizeSourceIP(window, sourceIP)
		return fmt.Sprintf("rib:downstream:%s:%s:%s:", vrf, neighbor, sip)
	}
	return fmt.Sprintf("rib:%s:%s:%s:", window, vrf, neighbor)
}

func rocksScanPrefix(window, vrf, neighbor, sourceIP string) []byte {
	if window == WindowDownstream {
		sip := NormalizeSourceIP(window, sourceIP)
		return []byte(fmt.Sprintf("r:downstream:%s:%s:%s:", vrf, neighbor, sip))
	}
	return []byte(fmt.Sprintf("r:%s:%s:%s:", window, vrf, neighbor))
}
