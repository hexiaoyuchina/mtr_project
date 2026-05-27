package pipeline

import (
	"testing"

	"bgp_agent/pkg/processor"
)

func TestNeedsPeerIngest_NoThreshold(t *testing.T) {
	const gap = uint64(5000)
	for _, win := range []string{processor.WindowDownstream, processor.WindowUpstream} {
		if NeedsPeerIngest(win, 0, 0, gap) {
			t.Fatalf("%s: both zero", win)
		}
		if !NeedsPeerIngest(win, 2, 0, gap) {
			t.Fatalf("%s: empty rib", win)
		}
		if !NeedsPeerIngest(win, 2, 1, gap) {
			t.Fatalf("%s: drift +1", win)
		}
		if !NeedsPeerIngest(win, 1, 2, gap) {
			t.Fatalf("%s: rib stale", win)
		}
		if NeedsPeerIngest(win, 2, 2, gap) {
			t.Fatalf("%s: aligned", win)
		}
		// 原上游 gap 逻辑：差 50 也应灌
		if !NeedsPeerIngest(win, 1_000_100, 1_000_050, gap) {
			t.Fatalf("%s: upstream small drift", win)
		}
	}
}
