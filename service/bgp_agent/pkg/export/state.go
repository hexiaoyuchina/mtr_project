package export

import (
	"context"
	"fmt"
	"strings"

	"bgp_agent/pkg/storage"

	"github.com/go-redis/redis/v8"
)

func exportStateKey(targetType, targetID, prefix string) string {
	return fmt.Sprintf("export:%s:%s:%s", targetType, targetID, prefix)
}

func exportCountKey(targetType, targetID string) string {
	return fmt.Sprintf("export:cnt:%s:%s", targetType, targetID)
}

// State 每会话已通告 prefix 集（Redis 持久化）。
type State struct {
	redis *redis.Client
}

func NewState(s *storage.Storage) *State {
	return &State{redis: s.Redis()}
}

func (st *State) TargetID(vrf, neighbor, sourceIP string) string {
	sip := storage.NormalizeSourceIP(storage.WindowDownstream, sourceIP)
	if vrf == "gobgp-rr" || vrf == "" {
		return "rr:" + neighbor
	}
	return fmt.Sprintf("tx:%s:%s:%s", vrf, neighbor, sip)
}

func (st *State) MarkAdvertised(ctx context.Context, targetType, targetID, prefix string) error {
	key := exportStateKey(targetType, targetID, prefix)
	exists, _ := st.redis.Exists(ctx, key).Result()
	if err := st.redis.Set(ctx, key, "1", 0).Err(); err != nil {
		return err
	}
	if exists == 0 {
		_ = st.redis.Incr(ctx, exportCountKey(targetType, targetID)).Err()
	}
	return nil
}

// MarkAdvertisedBatch 批量标记已通告（Reconcile 大批量写入，不做逐条 EXISTS）。
func (st *State) MarkAdvertisedBatch(ctx context.Context, targetType, targetID string, prefixes []string) error {
	if len(prefixes) == 0 {
		return nil
	}
	pipe := st.redis.Pipeline()
	for _, pfx := range prefixes {
		if strings.TrimSpace(pfx) == "" {
			continue
		}
		pipe.Set(ctx, exportStateKey(targetType, targetID, pfx), "1", 0)
	}
	pipe.IncrBy(ctx, exportCountKey(targetType, targetID), int64(len(prefixes)))
	_, err := pipe.Exec(ctx)
	return err
}

func (st *State) MarkWithdrawn(ctx context.Context, targetType, targetID, prefix string) error {
	key := exportStateKey(targetType, targetID, prefix)
	n, err := st.redis.Del(ctx, key).Result()
	if err != nil {
		return err
	}
	if n > 0 {
		cntKey := exportCountKey(targetType, targetID)
		v, _ := st.redis.Get(ctx, cntKey).Int64()
		if v > 0 {
			_ = st.redis.Decr(ctx, cntKey).Err()
		}
	}
	return nil
}

func (st *State) IsAdvertised(ctx context.Context, targetType, targetID, prefix string) bool {
	n, _ := st.redis.Exists(ctx, exportStateKey(targetType, targetID, prefix)).Result()
	return n > 0
}

func (st *State) SetAdvertisedCount(ctx context.Context, targetType, targetID string, n int) error {
	return st.redis.Set(ctx, exportCountKey(targetType, targetID), n, 0).Err()
}

func (st *State) ClearTarget(ctx context.Context, targetType, targetID string) error {
	pat := fmt.Sprintf("export:%s:%s:*", targetType, targetID)
	var cursor uint64
	for {
		keys, next, err := st.redis.Scan(ctx, cursor, pat, 500).Result()
		if err != nil {
			return err
		}
		if len(keys) > 0 {
			pipe := st.redis.Pipeline()
			for _, k := range keys {
				pipe.Unlink(ctx, k)
			}
			_, _ = pipe.Exec(ctx)
		}
		cursor = next
		if cursor == 0 {
			break
		}
	}
	_ = st.redis.Del(ctx, exportCountKey(targetType, targetID)).Err()
	return nil
}

func (st *State) ListAdvertisedPrefixes(ctx context.Context, targetType, targetID string) (map[string]struct{}, error) {
	out := make(map[string]struct{})
	pat := fmt.Sprintf("export:%s:%s:*", targetType, targetID)
	base := fmt.Sprintf("export:%s:%s:", targetType, targetID)
	var cursor uint64
	for {
		keys, next, err := st.redis.Scan(ctx, cursor, pat, 500).Result()
		if err != nil {
			return out, err
		}
		for _, k := range keys {
			pfx := strings.TrimPrefix(k, base)
			if pfx != "" {
				out[pfx] = struct{}{}
			}
		}
		cursor = next
		if cursor == 0 {
			break
		}
	}
	return out, nil
}
