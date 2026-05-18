package storage

import (
	"context"
	"encoding/json"
	"fmt"
	"strconv"
	"strings"
	"time"

	"github.com/go-redis/redis/v8"
	"github.com/tecbot/gorocksdb"
)

// PeerRoute 按 peer 维度的路由（百万级 RIB 主记录）。
type PeerRoute struct {
	Window     string    `json:"window"`
	VRF        string    `json:"vrf"`
	NeighborIP string    `json:"neighbor_ip"`
	Prefix     string    `json:"prefix"`
	Nexthop    string    `json:"nexthop"`
	ASPath     string    `json:"as_path"`
	RemoteAS   uint32    `json:"remote_as"`
	UpdatedAt  time.Time `json:"updated_at"`
}

// PeerPolicy 是否把从对端收到的路由写入 Redis/RocksDB。
type PeerPolicy struct {
	Window       string `json:"window"`
	VRF          string `json:"vrf"`
	NeighborIP   string `json:"neighbor_ip"`
	StoreRoutes  bool   `json:"store_routes"`
	AdvertiseOut bool   `json:"advertise_out"`
}

func peerRouteRedisKey(window, vrf, neighbor, prefix string) string {
	return fmt.Sprintf("rib:%s:%s:%s:%s", window, vrf, neighbor, prefix)
}

func peerRouteRocksKey(window, vrf, neighbor, prefix string) []byte {
	return []byte(fmt.Sprintf("r:%s:%s:%s:%s", window, vrf, neighbor, prefix))
}

func peerCountRedisKey(window, vrf, neighbor string) string {
	return fmt.Sprintf("rib:cnt:%s:%s:%s", window, vrf, neighbor)
}

func peerPolicyRedisKey(vrf, neighbor string) string {
	return fmt.Sprintf("peer:policy:%s:%s", vrf, neighbor)
}

func peerScanPrefix(window, vrf, neighbor string) string {
	return fmt.Sprintf("rib:%s:%s:%s:", window, vrf, neighbor)
}

func rocksScanPrefix(window, vrf, neighbor string) []byte {
	return []byte(fmt.Sprintf("r:%s:%s:%s:", window, vrf, neighbor))
}

// SetPeerPolicy 保存 peer 入库/通告策略（OP 同步）。
func (s *Storage) SetPeerPolicy(ctx context.Context, p PeerPolicy) error {
	data, err := json.Marshal(p)
	if err != nil {
		return err
	}
	return s.redis.Set(ctx, peerPolicyRedisKey(p.VRF, p.NeighborIP), data, 0).Err()
}

// GetPeerPolicy 读取策略；不存在时 store_routes 默认 false。
func (s *Storage) GetPeerPolicy(ctx context.Context, vrf, neighbor string) (PeerPolicy, error) {
	key := peerPolicyRedisKey(vrf, neighbor)
	data, err := s.redis.Get(ctx, key).Bytes()
	if err == redis.Nil {
		return PeerPolicy{VRF: vrf, NeighborIP: neighbor}, nil
	}
	if err != nil {
		return PeerPolicy{}, err
	}
	var p PeerPolicy
	if err := json.Unmarshal(data, &p); err != nil {
		return PeerPolicy{}, err
	}
	return p, nil
}

// ShouldStoreRoutes 是否对该 peer 持久化收到的路由。
func (s *Storage) ShouldStoreRoutes(ctx context.Context, vrf, neighbor string) bool {
	p, err := s.GetPeerPolicy(ctx, vrf, neighbor)
	if err != nil {
		return false
	}
	return p.StoreRoutes
}

// UpsertPeerRoute 写入 Redis + 加入 RocksDB 批量队列（由调用方 PersistPeerRouteBatch）。
func (s *Storage) UpsertPeerRoute(ctx context.Context, rt PeerRoute) (bool, error) {
	if rt.Window == "" || rt.VRF == "" || rt.NeighborIP == "" || rt.Prefix == "" {
		return false, fmt.Errorf("incomplete peer route")
	}
	if rt.UpdatedAt.IsZero() {
		rt.UpdatedAt = time.Now()
	}
	rkey := peerRouteRedisKey(rt.Window, rt.VRF, rt.NeighborIP, rt.Prefix)
	exists, err := s.redis.Exists(ctx, rkey).Result()
	if err != nil {
		return false, err
	}
	isNew := exists == 0
	data, err := json.Marshal(rt)
	if err != nil {
		return false, err
	}
	if err := s.redis.Set(ctx, rkey, data, 0).Err(); err != nil {
		return false, err
	}
	if isNew {
		_ = s.redis.Incr(ctx, peerCountRedisKey(rt.Window, rt.VRF, rt.NeighborIP)).Err()
	}
	if err := s.persistPeerRouteOne(rt); err != nil {
		return isNew, err
	}
	return isNew, nil
}

func (s *Storage) persistPeerRouteOne(rt PeerRoute) error {
	data, err := json.Marshal(rt)
	if err != nil {
		return err
	}
	wo := gorocksdb.NewDefaultWriteOptions()
	defer wo.Destroy()
	return s.rocksdb.Put(wo, peerRouteRocksKey(rt.Window, rt.VRF, rt.NeighborIP, rt.Prefix), data)
}

// PersistPeerRouteBatch RocksDB 批量写入。
func (s *Storage) PersistPeerRouteBatch(routes []PeerRoute) error {
	if len(routes) == 0 {
		return nil
	}
	wo := gorocksdb.NewDefaultWriteOptions()
	defer wo.Destroy()
	batch := gorocksdb.NewWriteBatch()
	defer batch.Destroy()
	for _, rt := range routes {
		data, err := json.Marshal(rt)
		if err != nil {
			continue
		}
		batch.Put(peerRouteRocksKey(rt.Window, rt.VRF, rt.NeighborIP, rt.Prefix), data)
	}
	return s.rocksdb.Write(wo, batch)
}

// DeletePeerRoute 删除单条。
func (s *Storage) DeletePeerRoute(ctx context.Context, window, vrf, neighbor, prefix string) error {
	rkey := peerRouteRedisKey(window, vrf, neighbor, prefix)
	n, err := s.redis.Del(ctx, rkey).Result()
	if err != nil {
		return err
	}
	if n > 0 {
		cntKey := peerCountRedisKey(window, vrf, neighbor)
		v, _ := s.redis.Get(ctx, cntKey).Int64()
		if v > 0 {
			_ = s.redis.Decr(ctx, cntKey).Err()
		}
	}
	wo := gorocksdb.NewDefaultWriteOptions()
	defer wo.Destroy()
	return s.rocksdb.Delete(wo, peerRouteRocksKey(window, vrf, neighbor, prefix))
}

// CountPeerRoutes 该 peer 在 Redis 计数器中的条数（O(1)）。
func (s *Storage) CountPeerRoutes(ctx context.Context, window, vrf, neighbor string) (int64, error) {
	v, err := s.redis.Get(ctx, peerCountRedisKey(window, vrf, neighbor)).Int64()
	if err == redis.Nil {
		return 0, nil
	}
	return v, err
}

// ListPeerRoutesPage 分页列举（RocksDB 迭代 skip/limit，不一次性加载全表）。
func (s *Storage) ListPeerRoutesPage(window, vrf, neighbor string, offset, limit int) ([]PeerRoute, int, error) {
	if limit <= 0 {
		limit = 100
	}
	if limit > 5000 {
		limit = 5000
	}
	ctx := context.Background()
	total64, _ := s.CountPeerRoutes(ctx, window, vrf, neighbor)
	total := int(total64)

	prefix := rocksScanPrefix(window, vrf, neighbor)
	ro := gorocksdb.NewDefaultReadOptions()
	defer ro.Destroy()
	it := s.rocksdb.NewIterator(ro)
	defer it.Close()

	skip := offset
	out := make([]PeerRoute, 0, limit)
	for it.Seek(prefix); it.Valid(); it.Next() {
		k := it.Key().Data()
		if !strings.HasPrefix(string(k), string(prefix)) {
			break
		}
		if skip > 0 {
			skip--
			continue
		}
		data := it.Value().Data()
		var rt PeerRoute
		if err := json.Unmarshal(data, &rt); err != nil {
			continue
		}
		out = append(out, rt)
		if len(out) >= limit {
			break
		}
	}
	return out, total, it.Err()
}

// IteratePeerRoutes 按批回调（通告百万级用）；从 RocksDB 流式扫描，避免一次加载。
func (s *Storage) IteratePeerRoutes(window, vrf, neighbor string, batchSize int, fn func([]PeerRoute) error) error {
	if batchSize <= 0 {
		batchSize = 1000
	}
	prefix := rocksScanPrefix(window, vrf, neighbor)
	ro := gorocksdb.NewDefaultReadOptions()
	defer ro.Destroy()
	it := s.rocksdb.NewIterator(ro)
	defer it.Close()

	batch := make([]PeerRoute, 0, batchSize)
	for it.Seek(prefix); it.Valid(); it.Next() {
		k := it.Key().Data()
		if !strings.HasPrefix(string(k), string(prefix)) {
			break
		}
		data := it.Value().Data()
		var rt PeerRoute
		if err := json.Unmarshal(data, &rt); err != nil {
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

func peerRoutePrefixFromRedisKey(key, window, vrf, neighbor string) (string, bool) {
	base := peerScanPrefix(window, vrf, neighbor)
	if !strings.HasPrefix(key, base) {
		return "", false
	}
	pfx := strings.TrimPrefix(key, base)
	if pfx == "" {
		return "", false
	}
	return pfx, true
}

// PurgePeerRoutesNotIn 删除该 peer 在 Redis/RocksDB 中不在 keep 集合里的前缀。
func (s *Storage) PurgePeerRoutesNotIn(ctx context.Context, window, vrf, neighbor string, keep map[string]struct{}) (int, error) {
	if keep == nil {
		keep = map[string]struct{}{}
	}
	removed := 0
	pat := peerScanPrefix(window, vrf, neighbor) + "*"
	var cursor uint64
	for {
		keys, next, err := s.redis.Scan(ctx, cursor, pat, 500).Result()
		if err != nil {
			return removed, err
		}
		for _, k := range keys {
			pfx, ok := peerRoutePrefixFromRedisKey(k, window, vrf, neighbor)
			if !ok {
				continue
			}
			if _, exists := keep[pfx]; exists {
				continue
			}
			if err := s.DeletePeerRoute(ctx, window, vrf, neighbor, pfx); err != nil {
				continue
			}
			removed++
		}
		cursor = next
		if cursor == 0 {
			break
		}
	}
	rocksPrefix := rocksScanPrefix(window, vrf, neighbor)
	ro := gorocksdb.NewDefaultReadOptions()
	defer ro.Destroy()
	it := s.rocksdb.NewIterator(ro)
	defer it.Close()
	wo := gorocksdb.NewDefaultWriteOptions()
	defer wo.Destroy()
	for it.Seek(rocksPrefix); it.Valid(); it.Next() {
		k := string(it.Key().Data())
		if !strings.HasPrefix(k, string(rocksPrefix)) {
			break
		}
		pfx := strings.TrimPrefix(k, string(rocksPrefix))
		if pfx == "" {
			continue
		}
		if _, exists := keep[pfx]; exists {
			continue
		}
		if err := s.rocksdb.Delete(wo, []byte(k)); err != nil {
			continue
		}
	}
	if err := it.Err(); err != nil {
		return removed, err
	}
	_, _ = s.RebuildPeerCount(ctx, window, vrf, neighbor)
	return removed, nil
}

// IngestPeerRoutes 将对端 ADJ-IN 快照写入 RIB，并删除 Adj-RIB-In 中已不存在的前缀。
func (s *Storage) IngestPeerRoutes(ctx context.Context, window, vrf, neighbor string, routes []PeerRoute) (ingested int, removed int, err error) {
	keep := make(map[string]struct{}, len(routes))
	batch := make([]PeerRoute, 0, 1000)
	for _, rt := range routes {
		rt.Window = window
		rt.VRF = vrf
		rt.NeighborIP = neighbor
		if rt.Prefix == "" {
			continue
		}
		keep[rt.Prefix] = struct{}{}
		_, err := s.UpsertPeerRoute(ctx, rt)
		if err != nil {
			continue
		}
		ingested++
		batch = append(batch, rt)
		if len(batch) >= 1000 {
			_ = s.PersistPeerRouteBatch(batch)
			batch = batch[:0]
		}
	}
	if len(batch) > 0 {
		_ = s.PersistPeerRouteBatch(batch)
	}
	removed, err = s.PurgePeerRoutesNotIn(ctx, window, vrf, neighbor, keep)
	return ingested, removed, err
}

// RebuildPeerCount 从 Redis SCAN 重建计数（维护用）。
func (s *Storage) RebuildPeerCount(ctx context.Context, window, vrf, neighbor string) (int64, error) {
	pat := peerScanPrefix(window, vrf, neighbor) + "*"
	var cursor uint64
	var n int64
	for {
		keys, next, err := s.redis.Scan(ctx, cursor, pat, 500).Result()
		if err != nil {
			return n, err
		}
		n += int64(len(keys))
		cursor = next
		if cursor == 0 {
			break
		}
	}
	_ = s.redis.Set(ctx, peerCountRedisKey(window, vrf, neighbor), strconv.FormatInt(n, 10), 0).Err()
	return n, nil
}
