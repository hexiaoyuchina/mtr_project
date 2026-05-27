package export

import (
	"bytes"
	"context"
	"fmt"
	"log"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"sync"

	"bgp_agent/pkg/fib"
)

const defaultKernelBatch = 2000
const defaultKernelBulkMin = 64

// KernelInstaller 内核 FIB 安装（大批量走 ip -batch，流式写入策略表）。
type KernelInstaller struct {
	tableUpstream   string
	tableDownstream string
	batchSize       int
	bulkMin         int
	mu              sync.Mutex
}

func NewKernelInstaller() *KernelInstaller {
	up := strings.TrimSpace(os.Getenv("MTR_FIB_KERNEL_TABLE_UPSTREAM"))
	if up == "" {
		up = "main"
	}
	down := strings.TrimSpace(os.Getenv("MTR_FIB_KERNEL_TABLE_DOWNSTREAM"))
	if down == "" {
		down = "main"
	}
	batch := defaultKernelBatch
	if raw := strings.TrimSpace(os.Getenv("MTR_FIB_KERNEL_BATCH_SIZE")); raw != "" {
		if n, err := strconv.Atoi(raw); err == nil && n > 0 {
			batch = n
		}
	}
	bulkMin := defaultKernelBulkMin
	if raw := strings.TrimSpace(os.Getenv("MTR_FIB_KERNEL_BULK_MIN")); raw != "" {
		if n, err := strconv.Atoi(raw); err == nil && n > 0 {
			bulkMin = n
		}
	}
	return &KernelInstaller{
		tableUpstream:   up,
		tableDownstream: down,
		batchSize:       batch,
		bulkMin:         bulkMin,
	}
}

func (k *KernelInstaller) tableForWindow(window string) string {
	if window == fib.WindowDownstream {
		return k.tableDownstream
	}
	return k.tableUpstream
}

func (k *KernelInstaller) Apply(ctx context.Context, window string, diff fib.FibDiff) {
	table := k.tableForWindow(window)
	if len(diff.Withdraws) >= k.bulkMin {
		for i := 0; i < len(diff.Withdraws); i += k.batchSize {
			end := i + k.batchSize
			if end > len(diff.Withdraws) {
				end = len(diff.Withdraws)
			}
			k.runDelBatch(ctx, table, diff.Withdraws[i:end])
		}
	} else {
		for _, pfx := range diff.Withdraws {
			k.runOne(ctx, "del", table, pfx, "")
		}
	}
	if len(diff.Adds) >= k.bulkMin {
		for i := 0; i < len(diff.Adds); i += k.batchSize {
			end := i + k.batchSize
			if end > len(diff.Adds) {
				end = len(diff.Adds)
			}
			k.runReplaceBatch(ctx, table, diff.Adds[i:end])
		}
	} else {
		for _, rt := range diff.Adds {
			k.runOne(ctx, "replace", table, rt.Prefix, rt.Nexthop)
		}
	}
}

func routeReplacePrefix(table string) string {
	if table == "" || table == "main" {
		return "route replace"
	}
	return fmt.Sprintf("route replace table %s", table)
}

func routeDelPrefix(table string) string {
	if table == "" || table == "main" {
		return "route del"
	}
	return fmt.Sprintf("route del table %s", table)
}

func (k *KernelInstaller) runOne(ctx context.Context, action, table, prefix, nexthop string) {
	var line string
	switch action {
	case "del":
		line = fmt.Sprintf("%s %s", routeDelPrefix(table), prefix)
	case "replace":
		nh := strings.TrimSpace(nexthop)
		if nh == "" {
			nh = "0.0.0.0"
		}
		line = fmt.Sprintf("%s %s via %s", routeReplacePrefix(table), prefix, nh)
	default:
		return
	}
	k.execBatch(ctx, []string{line})
}

func (k *KernelInstaller) runReplaceBatch(ctx context.Context, table string, routes []fib.FibRoute) {
	if len(routes) == 0 {
		return
	}
	prefix := routeReplacePrefix(table)
	lines := make([]string, 0, len(routes))
	for _, rt := range routes {
		pfx := strings.TrimSpace(rt.Prefix)
		if pfx == "" {
			continue
		}
		nh := strings.TrimSpace(rt.Nexthop)
		if nh == "" {
			nh = "0.0.0.0"
		}
		lines = append(lines, fmt.Sprintf("%s %s via %s", prefix, pfx, nh))
	}
	k.execBatch(ctx, lines)
}

func (k *KernelInstaller) runDelBatch(ctx context.Context, table string, prefixes []string) {
	if len(prefixes) == 0 {
		return
	}
	prefix := routeDelPrefix(table)
	lines := make([]string, 0, len(prefixes))
	for _, pfx := range prefixes {
		pfx = strings.TrimSpace(pfx)
		if pfx == "" {
			continue
		}
		lines = append(lines, fmt.Sprintf("%s %s", prefix, pfx))
	}
	k.execBatch(ctx, lines)
}

func (k *KernelInstaller) execBatch(ctx context.Context, lines []string) {
	if len(lines) == 0 {
		return
	}
	k.mu.Lock()
	defer k.mu.Unlock()

	var buf bytes.Buffer
	for _, line := range lines {
		buf.WriteString(line)
		buf.WriteByte('\n')
	}
	cmd := exec.CommandContext(ctx, "ip", "-batch", "-")
	cmd.Stdin = &buf
	if out, err := cmd.CombinedOutput(); err != nil {
		msg := strings.TrimSpace(string(out))
		if len(lines) == 1 {
			log.Printf("kernel route %s: %s %v", lines[0], msg, err)
			return
		}
		log.Printf("kernel batch %d lines failed: %s %v; falling back sequential", len(lines), msg, err)
		for _, line := range lines {
			c2 := exec.CommandContext(ctx, "ip", "-batch", "-")
			c2.Stdin = strings.NewReader(line + "\n")
			if out2, err2 := c2.CombinedOutput(); err2 != nil {
				log.Printf("kernel route %s: %s %v", line, strings.TrimSpace(string(out2)), err2)
			}
		}
	}
}

// ReconcileFromFib 启动/FIB 全量重算后，流式将 FIB 装入内核（replace，不删多余项）。
func (k *KernelInstaller) ReconcileFromFib(ctx context.Context, window string, eng *fib.Engine) {
	if eng == nil {
		return
	}
	table := k.tableForWindow(window)
	var total int
	_ = eng.Store().Iterate(window, k.batchSize, func(routes []fib.FibRoute) error {
		if len(routes) == 0 {
			return nil
		}
		k.runReplaceBatch(ctx, table, routes)
		total += len(routes)
		if total%50000 == 0 {
			log.Printf("kernel reconcile progress window=%s table=%s routes=%d", window, table, total)
		}
		return nil
	})
	if total == 0 {
		return
	}
	log.Printf("kernel reconcile from fib window=%s table=%s routes=%d", window, table, total)
}
