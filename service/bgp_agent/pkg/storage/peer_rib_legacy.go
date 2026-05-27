package storage

import (
	"context"
	"encoding/json"
	"log"
	"net"
	"strings"

	"github.com/go-redis/redis/v8"
	"github.com/tecbot/gorocksdb"
)

func legacyDownstreamRedisBase(vrf, neighbor string) string {
	return "rib:downstream:" + vrf + ":" + neighbor + ":"
}

func legacyDownstreamCountKey(vrf, neighbor string) string {
	return "rib:cnt:downstream:" + vrf + ":" + neighbor
}

// legacyDownstreamRoutePrefixFromRedisKey 旧格式 rib:downstream:{vrf}:{neighbor}:{prefix}（无 source_ip 段）。
func legacyDownstreamRoutePrefixFromRedisKey(key, vrf, neighbor string) (string, bool) {
	base := legacyDownstreamRedisBase(vrf, neighbor)
	if !strings.HasPrefix(key, base) {
		return "", false
	}
	tail := strings.TrimPrefix(key, base)
	if tail == "" || strings.Contains(tail, ":") {
		return "", false
	}
	if !strings.Contains(tail, "/") {
		return "", false
	}
	return tail, true
}

func legacyDownstreamRocksPrefix(vrf, neighbor string) []byte {
	return []byte("r:downstream:" + vrf + ":" + neighbor + ":")
}

func legacyDownstreamPrefixFromRocksKey(key []byte, vrf, neighbor string) (string, bool) {
	base := string(legacyDownstreamRocksPrefix(vrf, neighbor))
	s := string(key)
	if !strings.HasPrefix(s, base) {
		return "", false
	}
	tail := strings.TrimPrefix(s, base)
	if tail == "" || strings.Contains(tail, ":") {
		return "", false
	}
	if !strings.Contains(tail, "/") {
		return "", false
	}
	return tail, true
}

func normalizeMigrateSourceIP(sourceIP string) string {
	s := strings.TrimSpace(sourceIP)
	if s == "" || s == "0.0.0.0" {
		return "_default_"
	}
	if net.ParseIP(s) == nil {
		return "_default_"
	}
	return s
}

// MigrateLegacyDownstreamPeerRIB 将无 source_ip 段的旧 downstream RIB 迁入新 key（Redis + RocksDB）。
func (s *Storage) MigrateLegacyDownstreamPeerRIB(ctx context.Context, vrf, neighbor, sourceIP string) (int, error) {
	vrf = strings.TrimSpace(vrf)
	neighbor = strings.TrimSpace(neighbor)
	if vrf == "" || neighbor == "" {
		return 0, nil
	}
	sip := normalizeMigrateSourceIP(sourceIP)
	migrated := 0

	redisBase := legacyDownstreamRedisBase(vrf, neighbor)
	iter := s.redis.Scan(ctx, 0, redisBase+"*", 500).Iterator()
	for iter.Next(ctx) {
		key := iter.Val()
		pfx, ok := legacyDownstreamRoutePrefixFromRedisKey(key, vrf, neighbor)
		if !ok {
			continue
		}
		data, err := s.redis.Get(ctx, key).Bytes()
		if err != nil {
			continue
		}
		var rt PeerRoute
		if err := json.Unmarshal(data, &rt); err != nil {
			continue
		}
		rt.Window = WindowDownstream
		rt.VRF = vrf
		rt.NeighborIP = neighbor
		rt.Prefix = pfx
		if strings.TrimSpace(rt.SourceIP) == "" {
			if sip != "_default_" {
				rt.SourceIP = sip
			}
		}
		if _, err := s.UpsertPeerRoute(ctx, rt); err != nil {
			log.Printf("legacy rib migrate upsert %s: %v", pfx, err)
			continue
		}
		_, _ = s.redis.Del(ctx, key).Result()
		migrated++
	}
	if err := iter.Err(); err != nil && err != redis.Nil {
		return migrated, err
	}

	rocksBase := legacyDownstreamRocksPrefix(vrf, neighbor)
	ro := gorocksdb.NewDefaultReadOptions()
	defer ro.Destroy()
	it := s.rocksdb.NewIterator(ro)
	defer it.Close()
	var rocksDeletes [][]byte
	for it.Seek(rocksBase); it.Valid(); it.Next() {
		k := it.Key().Data()
		if !strings.HasPrefix(string(k), string(rocksBase)) {
			break
		}
		pfx, ok := legacyDownstreamPrefixFromRocksKey(k, vrf, neighbor)
		if !ok {
			continue
		}
		data := it.Value().Data()
		var rt PeerRoute
		if err := json.Unmarshal(data, &rt); err != nil {
			continue
		}
		rt.Window = WindowDownstream
		rt.VRF = vrf
		rt.NeighborIP = neighbor
		rt.Prefix = pfx
		if strings.TrimSpace(rt.SourceIP) == "" && sip != "_default_" {
			rt.SourceIP = sip
		}
		if _, err := s.UpsertPeerRoute(ctx, rt); err != nil {
			continue
		}
		rocksDeletes = append(rocksDeletes, append([]byte(nil), k...))
		migrated++
	}
	if len(rocksDeletes) > 0 {
		wo := gorocksdb.NewDefaultWriteOptions()
		defer wo.Destroy()
		batch := gorocksdb.NewWriteBatch()
		defer batch.Destroy()
		for _, k := range rocksDeletes {
			batch.Delete(k)
		}
		_ = s.rocksdb.Write(wo, batch)
	}

	if migrated > 0 {
		_, _ = s.RebuildPeerCount(ctx, WindowDownstream, vrf, neighbor, sourceIP)
		_ = s.redis.Del(ctx, legacyDownstreamCountKey(vrf, neighbor)).Err()
		log.Printf("legacy downstream rib migrated vrf=%s neighbor=%s source=%s count=%d",
			vrf, neighbor, sip, migrated)
	}
	return migrated, nil
}
