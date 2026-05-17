package gobgp_path

import (
	"strconv"

	api "github.com/osrg/gobgp/v3/api"
)

// ParseIPv4Unicast 从 GoBGP Path 解析 IPv4 前缀与下一跳。
func ParseIPv4Unicast(path *api.Path) (prefix string, nexthop string, asPath string, ok bool) {
	if path == nil || path.IsWithdraw {
		return "", "", "", false
	}
	if path.Family != nil && (path.Family.Afi != api.Family_AFI_IP || path.Family.Safi != api.Family_SAFI_UNICAST) {
		return "", "", "", false
	}
	nlri := &api.IPAddressPrefix{}
	if path.Nlri != nil {
		if err := path.Nlri.UnmarshalTo(nlri); err != nil {
			return "", "", "", false
		}
	}
	if nlri.Prefix == "" {
		return "", "", "", false
	}
	prefix = nlri.Prefix + "/" + strconv.FormatUint(uint64(nlri.PrefixLen), 10)
	nexthop = "0.0.0.0"
	asPath = ""
	for _, attr := range path.Pattrs {
		if attr == nil {
			continue
		}
		switch attr.GetTypeUrl() {
		case "type.googleapis.com/apipb.NextHopAttribute":
			nh := &api.NextHopAttribute{}
			if err := attr.UnmarshalTo(nh); err == nil && nh.NextHop != "" {
				nexthop = nh.NextHop
			}
		case "type.googleapis.com/apipb.AsPathAttribute":
			asp := &api.AsPathAttribute{}
			if err := attr.UnmarshalTo(asp); err == nil {
				asPath = asp.String()
			}
		}
	}
	return prefix, nexthop, asPath, true
}
