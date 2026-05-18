package gobgp_path

import api "github.com/osrg/gobgp/v3/api"

// NeighborIP 从 Path 解析通告来源邻居（GoBGP v3 neighbor_ip 字段）。
func NeighborIP(path *api.Path) string {
	if path == nil {
		return ""
	}
	return path.GetNeighborIp()
}
