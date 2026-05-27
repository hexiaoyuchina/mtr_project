package fib

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"
	"time"

	"bgp_agent/pkg/storage"

	"github.com/go-redis/redis/v8"
	"github.com/tecbot/gorocksdb"
)

func fibRedisKey(window, prefix string) string {
	return fmt.Sprintf("fib:%s:%s", window, prefix)
}

func fibRocksKey(window, prefix string) []byte {
	return []byte(fmt.Sprintf("f:%s:%s", window, prefix))
}

func fibCountKey(window string) string {
	return fmt.Sprintf("fib:cnt:%s", window)
}

func fibScanPrefix(window string) string {
	return fmt.Sprintf("fib:%s:", window)
}

func rocksFibScanPrefix(window string) []byte {
	return []byte(fmt.Sprintf("f:%s:", window))
}

// Store FIB 持久化（Redis + RocksDB）。
type Store struct {
	s *storage.Storage
}

func NewStore(s *storage.Storage) *Store {
	return &Store{s: s}
}

func (st *Store) Get(ctx context.Context, window, prefix string) (*FibRoute, error) {
	data, err := st.s.Redis().Get(ctx, fibRedisKey(window, prefix)).Bytes()
	if err == redis.Nil {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	var rt FibRoute
	if err := json.Unmarshal(data, &rt); err != nil {
		return nil, err
	}
	return &rt, nil
}

func (st *Store) Put(ctx context.Context, rt FibRoute) error {
	if rt.UpdatedAt.IsZero() {
		rt.UpdatedAt = time.Now()
	}
	data, err := json.Marshal(rt)
	if err != nil {
		return err
	}
	rkey := fibRedisKey(rt.Window, rt.Prefix)
	exists, _ := st.s.Redis().Exists(ctx, rkey).Result()
	if err := st.s.Redis().Set(ctx, rkey, data, 0).Err(); err != nil {
		return err
	}
	if exists == 0 {
		_ = st.s.Redis().Incr(ctx, fibCountKey(rt.Window)).Err()
	}
	wo := gorocksdb.NewDefaultWriteOptions()
	defer wo.Destroy()
	return st.s.RocksDB().Put(wo, fibRocksKey(rt.Window, rt.Prefix), data)
}

func (st *Store) Delete(ctx context.Context, window, prefix string) error {
	rkey := fibRedisKey(window, prefix)
	n, err := st.s.Redis().Del(ctx, rkey).Result()
	if err != nil {
		return err
	}
	if n > 0 {
		cntKey := fibCountKey(window)
		v, _ := st.s.Redis().Get(ctx, cntKey).Int64()
		if v > 0 {
			_ = st.s.Redis().Decr(ctx, cntKey).Err()
		}
	}
	wo := gorocksdb.NewDefaultWriteOptions()
	defer wo.Destroy()
	return st.s.RocksDB().Delete(wo, fibRocksKey(window, prefix))
}

func (st *Store) Count(ctx context.Context, window string) (int64, error) {
	v, err := st.s.Redis().Get(ctx, fibCountKey(window)).Int64()
	if err == redis.Nil {
		return 0, nil
	}
	return v, err
}

func (st *Store) ListPage(window string, offset, limit int) ([]FibRoute, int, error) {
	if limit <= 0 {
		limit = 100
	}
	if limit > 5000 {
		limit = 5000
	}
	ctx := context.Background()
	total64, _ := st.Count(ctx, window)
	total := int(total64)

	prefix := rocksFibScanPrefix(window)
	ro := gorocksdb.NewDefaultReadOptions()
	defer ro.Destroy()
	it := st.s.RocksDB().NewIterator(ro)
	defer it.Close()

	skip := offset
	out := make([]FibRoute, 0, limit)
	for it.Seek(prefix); it.Valid(); it.Next() {
		k := it.Key().Data()
		if !strings.HasPrefix(string(k), string(prefix)) {
			break
		}
		if skip > 0 {
			skip--
			continue
		}
		var rt FibRoute
		if err := json.Unmarshal(it.Value().Data(), &rt); err != nil {
			continue
		}
		out = append(out, rt)
		if len(out) >= limit {
			break
		}
	}
	return out, total, it.Err()
}

func (st *Store) Iterate(window string, batchSize int, fn func([]FibRoute) error) error {
	if batchSize <= 0 {
		batchSize = 1000
	}
	prefix := rocksFibScanPrefix(window)
	ro := gorocksdb.NewDefaultReadOptions()
	defer ro.Destroy()
	it := st.s.RocksDB().NewIterator(ro)
	defer it.Close()

	batch := make([]FibRoute, 0, batchSize)
	for it.Seek(prefix); it.Valid(); it.Next() {
		k := it.Key().Data()
		if !strings.HasPrefix(string(k), string(prefix)) {
			break
		}
		var rt FibRoute
		if err := json.Unmarshal(it.Value().Data(), &rt); err != nil {
			continue
		}
		batch = append(batch, rt)
		if len(batch) >= batchSize {
			if err := fn(batch); err != nil {
				return err
			}
			batch = batch[:0]
		}
	}
	if len(batch) > 0 {
		return fn(batch)
	}
	return it.Err()
}
