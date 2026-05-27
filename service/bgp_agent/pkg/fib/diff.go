package fib

// DiffRoutes 比较新旧 FIB 路由集，返回 add/withdraw。
func DiffRoutes(oldMap, newMap map[string]FibRoute) FibDiff {
	var d FibDiff
	for pfx, rt := range newMap {
		old, ok := oldMap[pfx]
		if !ok || !routesEqual(old, rt) {
			d.Adds = append(d.Adds, rt)
		}
	}
	for pfx := range oldMap {
		if _, ok := newMap[pfx]; !ok {
			d.Withdraws = append(d.Withdraws, pfx)
		}
	}
	return d
}

func DiffSingle(old *FibRoute, new *FibRoute) FibDiff {
	var d FibDiff
	if old == nil && new == nil {
		return d
	}
	if new == nil {
		if old != nil {
			d.Withdraws = append(d.Withdraws, old.Prefix)
		}
		return d
	}
	if old == nil || !routesEqual(*old, *new) {
		d.Adds = append(d.Adds, *new)
	}
	return d
}

func routesEqual(a, b FibRoute) bool {
	return a.Prefix == b.Prefix && a.Nexthop == b.Nexthop && a.ASPath == b.ASPath &&
		a.NeighborIP == b.NeighborIP && a.SourceIP == b.SourceIP && a.VRF == b.VRF
}
