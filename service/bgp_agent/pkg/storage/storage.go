package storage

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"time"
	
	"github.com/go-redis/redis/v8"
	"github.com/tecbot/gorocksdb"
)

// Route 路由条目
type Route struct {
	Prefix    string
	Nexthop   string
	ASPath    string
	RemoteAS  uint32
	UpdatedAt time.Time
}

// Storage 存储层：Redis（热缓存）+ RocksDB（持久化）
type Storage struct {
	redis    *redis.Client
	rocksdb  *gorocksdb.DB
	rocksOpt *gorocksdb.Options
}

// NewStorage 创建存储实例
func NewStorage(redisAddr, rocksPath string) (*Storage, error) {
	// 初始化Redis
	rdb := redis.NewClient(&redis.Options{
		Addr:         redisAddr,
		DB:           0,
		DialTimeout:  5 * time.Second,
		ReadTimeout:  3 * time.Second,
		WriteTimeout: 3 * time.Second,
		PoolSize:     100,
	})
	
	// 测试Redis连接
	ctx := context.Background()
	if err := rdb.Ping(ctx).Err(); err != nil {
		return nil, fmt.Errorf("连接Redis失败: %w", err)
	}
	log.Printf("Redis连接成功: %s", redisAddr)
	
	// 初始化RocksDB
	opts := gorocksdb.NewDefaultOptions()
	opts.SetCreateIfMissing(true)
	opts.SetMaxOpenFiles(1000)
	opts.SetWriteBufferSize(256 * 1024 * 1024) // 256MB
	opts.SetCompression(gorocksdb.SnappyCompression)
	
	db, err := gorocksdb.OpenDb(opts, rocksPath)
	if err != nil {
		rdb.Close()
		return nil, fmt.Errorf("打开RocksDB失败: %w", err)
	}
	log.Printf("RocksDB打开成功: %s", rocksPath)
	
	return &Storage{
		redis:    rdb,
		rocksdb:  db,
		rocksOpt: opts,
	}, nil
}

// SetRoute 设置路由到Redis热缓存
func (s *Storage) SetRoute(ctx context.Context, prefix, nexthop, aspath string, asn uint32) error {
	route := Route{
		Prefix:    prefix,
		Nexthop:   nexthop,
		ASPath:    aspath,
		RemoteAS:  asn,
		UpdatedAt: time.Now(),
	}
	
	data, err := json.Marshal(route)
	if err != nil {
		return fmt.Errorf("序列化路由失败: %w", err)
	}
	
	key := fmt.Sprintf("bgp:route:%s", prefix)
	return s.redis.Set(ctx, key, data, 0).Err()
}

// GetRoute 从Redis获取路由
func (s *Storage) GetRoute(ctx context.Context, prefix string) (*Route, error) {
	key := fmt.Sprintf("bgp:route:%s", prefix)
	data, err := s.redis.Get(ctx, key).Bytes()
	if err == redis.Nil {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	
	var route Route
	if err := json.Unmarshal(data, &route); err != nil {
		return nil, err
	}
	
	return &route, nil
}

// DeleteRoute 从Redis删除路由
func (s *Storage) DeleteRoute(ctx context.Context, prefix string) error {
	key := fmt.Sprintf("bgp:route:%s", prefix)
	return s.redis.Del(ctx, key).Err()
}

// ListRoutes 列出Redis中所有路由
func (s *Storage) ListRoutes(ctx context.Context) ([]Route, error) {
	keys, err := s.redis.Keys(ctx, "bgp:route:*").Result()
	if err != nil {
		return nil, err
	}
	
	routes := make([]Route, 0, len(keys))
	for _, key := range keys {
		data, err := s.redis.Get(ctx, key).Bytes()
		if err != nil {
			continue
		}
		
		var route Route
		if err := json.Unmarshal(data, &route); err != nil {
			continue
		}
		
		routes = append(routes, route)
	}
	
	return routes, nil
}

// PersistRoute 持久化路由到RocksDB
func (s *Storage) PersistRoute(ctx context.Context, prefix, nexthop, aspath string, asn uint32) error {
	route := Route{
		Prefix:    prefix,
		Nexthop:   nexthop,
		ASPath:    aspath,
		RemoteAS:  asn,
		UpdatedAt: time.Now(),
	}
	
	data, err := json.Marshal(route)
	if err != nil {
		return fmt.Errorf("序列化路由失败: %w", err)
	}
	
	wo := gorocksdb.NewDefaultWriteOptions()
	defer wo.Destroy()
	
	key := []byte(fmt.Sprintf("route:%s", prefix))
	return s.rocksdb.Put(wo, key, data)
}

// LoadPersistedRoutes 从RocksDB加载所有持久化路由
func (s *Storage) LoadPersistedRoutes(ctx context.Context) ([]Route, error) {
	ro := gorocksdb.NewDefaultReadOptions()
	defer ro.Destroy()
	
	it := s.rocksdb.NewIterator(ro)
	defer it.Close()
	
	routes := make([]Route, 0, 10000)
	
	for it.SeekToFirst(); it.Valid(); it.Next() {
		data := it.Value().Data()
		
		var route Route
		if err := json.Unmarshal(data, &route); err != nil {
			log.Printf("反序列化路由失败: %v", err)
			continue
		}
		
		routes = append(routes, route)
	}
	
	if err := it.Err(); err != nil {
		return nil, err
	}
	
	return routes, nil
}

// GetEffectiveRIB 获取有效RIB（用于TX恢复）
func (s *Storage) GetEffectiveRIB(ctx context.Context) ([]Route, error) {
	// 优先从Redis读取（热缓存）
	routes, err := s.ListRoutes(ctx)
	if err == nil && len(routes) > 0 {
		return routes, nil
	}
	
	// Redis为空，从RocksDB恢复
	return s.LoadPersistedRoutes(ctx)
}

// Close 关闭存储
func (s *Storage) Close() error {
	if s.redis != nil {
		s.redis.Close()
	}
	if s.rocksdb != nil {
		s.rocksdb.Close()
	}
	if s.rocksOpt != nil {
		s.rocksOpt.Destroy()
	}
	return nil
}

// GetStats 获取存储统计
func (s *Storage) GetStats(ctx context.Context) (map[string]interface{}, error) {
	stats := make(map[string]interface{})
	
	// Redis统计
	redisInfo, err := s.redis.Info(ctx, "memory").Result()
	if err == nil {
		stats["redis_info"] = redisInfo
	}
	
	keys, err := s.redis.Keys(ctx, "bgp:route:*").Result()
	if err == nil {
		stats["redis_route_count"] = len(keys)
	}
	
	// RocksDB统计
	rocksStats := s.rocksdb.GetProperty("rocksdb.stats")
	stats["rocksdb_stats"] = rocksStats
	
	return stats, nil
}
