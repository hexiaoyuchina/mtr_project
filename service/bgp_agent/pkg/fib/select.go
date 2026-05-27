package fib

import (
	"strings"
)

// SelectBest BGP 选路：local_pref → as_path len → origin → neighbor tie-break。
func SelectBest(cands []Candidate) *Candidate {
	if len(cands) == 0 {
		return nil
	}
	best := &cands[0]
	for i := 1; i < len(cands); i++ {
		c := &cands[i]
		if c.LocalPref > best.LocalPref {
			best = c
			continue
		}
		if c.LocalPref < best.LocalPref {
			continue
		}
		if c.ASPathLen < best.ASPathLen {
			best = c
			continue
		}
		if c.ASPathLen > best.ASPathLen {
			continue
		}
		if c.Origin < best.Origin {
			best = c
			continue
		}
		if c.Origin > best.Origin {
			continue
		}
		if strings.Compare(c.NeighborIP, best.NeighborIP) < 0 {
			best = c
		}
	}
	return best
}

func ASPathLength(aspath string) int {
	aspath = strings.TrimSpace(aspath)
	if aspath == "" {
		return 0
	}
	n := 0
	for _, seg := range strings.Fields(aspath) {
		if seg != "" {
			n++
		}
	}
	return n
}
