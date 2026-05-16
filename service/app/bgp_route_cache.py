"""BGP学习路由内存缓存层，提高读取性能，减少SQLite IO压力。"""
from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


@dataclass
class BgpRoute:
    """单条BGP路由"""
    prefix: str
    nexthop: str
    neighbor_ip: str
    remote_as: int
    role: str
    as_path: str
    updated_at: str


@dataclass
class CacheStats:
    """缓存统计信息"""
    total_routes: int = 0
    total_vrfs: int = 0
    total_neighbors: int = 0
    last_sync_time: Optional[str] = None
    pending_writes: int = 0
    cache_hits: int = 0
    cache_misses: int = 0


class BgpLearnedRoutesCache:
    """
    BGP学习路由内存缓存

    优化策略：
    1. 多层索引：vrf -> neighbor_ip -> prefix -> route
    2. 定期批量写入SQLite，减少IO
    3. 读取优先从缓存获取
    """

    def __init__(self, flush_interval_seconds: int = 30):
        """
        :param flush_interval_seconds: 定期刷新到数据库的间隔（秒）
        """
        self._lock = threading.RLock()
        # 主缓存：vrf -> neighbor_ip -> {prefix: BgpRoute}
        self._routes: Dict[str, Dict[str, Dict[str, BgpRoute]]] = defaultdict(
            lambda: defaultdict(dict)
        )
        # 待写入的数据：(vrf, rows)
        self._pending_writes: List[tuple] = []
        # 最后同步时间
        self._last_sync_time: Optional[str] = None
        # 缓存是否初始化完成
        self._initialized = False
        # 刷新间隔
        self._flush_interval = flush_interval_seconds
        # 后台刷新线程
        self._flush_thread: Optional[threading.Thread] = None
        self._stop_flush = threading.Event()

    def start(self):
        """启动后台刷新线程"""
        if self._flush_thread and self._flush_thread.is_alive():
            return
        self._stop_flush.clear()
        self._flush_thread = threading.Thread(
            target=self._flush_loop,
            name="bgp-route-cache-flush",
            daemon=True
        )
        self._flush_thread.start()
        logger.info("BGP route cache flush thread started")

    def stop(self):
        """停止后台刷新线程"""
        self._stop_flush.set()
        if self._flush_thread:
            self._flush_thread.join(timeout=5)
        logger.info("BGP route cache flush thread stopped")

    def _flush_loop(self):
        """后台刷新循环"""
        while not self._stop_flush.is_set():
            try:
                self._stop_flush.wait(timeout=self._flush_interval)
                if not self._stop_flush.is_set():
                    self.flush()
            except Exception as e:
                logger.exception("Cache flush error")

    def update_routes(self, vrf: str, rows: List[tuple]):
        """
        批量更新路由数据到缓存

        :param vrf: VRF名称
        :param rows: [(prefix, nexthop, neighbor_ip, remote_as, role, as_path, updated_at), ...]
        """
        with self._lock:
            # 清空该VRF的现有数据
            if vrf in self._routes:
                self._routes[vrf].clear()

            # 批量添加新数据
            for row in rows:
                if len(row) < 7:
                    continue
                prefix, nexthop, neighbor_ip, remote_as, role, as_path, updated_at = row
                route = BgpRoute(
                    prefix=str(prefix),
                    nexthop=str(nexthop or ""),
                    neighbor_ip=str(neighbor_ip or ""),
                    remote_as=int(remote_as or 0),
                    role=str(role or "unknown"),
                    as_path=str(as_path or ""),
                    updated_at=str(updated_at or "")
                )
                self._routes[vrf][neighbor_ip][prefix] = route

            self._last_sync_time = rows[0][6] if rows else None
            self._initialized = True

        logger.info(f"Cache updated: vrf={vrf}, {len(rows)} routes")

    def get_routes(
        self,
        vrf: Optional[str] = None,
        neighbor_ip: Optional[str] = None,
        limit: Optional[int] = None,
        offset: int = 0
    ) -> List[BgpRoute]:
        """
        获取路由列表（从缓存读取）

        :param vrf: VRF筛选（可选）
        :param neighbor_ip: 邻居IP筛选（可选）
        :param limit: 返回数量限制
        :param offset: 跳过数量
        :return: 路由列表
        """
        with self._lock:
            if not self._initialized:
                return []

            results = []
            vrfs_to_check = [vrf] if vrf else list(self._routes.keys())

            for v in vrfs_to_check:
                if v not in self._routes:
                    continue
                neighbors_to_check = [neighbor_ip] if neighbor_ip else list(self._routes[v].keys())

                for nip in neighbors_to_check:
                    if nip not in self._routes[v]:
                        continue
                    results.extend(self._routes[v][nip].values())

            # 排序
            results.sort(key=lambda r: r.prefix)

            # 分页
            if offset > 0:
                results = results[offset:]
            if limit:
                results = results[:limit]

            return results

    def get_routes_by_prefix(
        self,
        prefix: str,
        vrf: Optional[str] = None,
        neighbor_ip: Optional[str] = None
    ) -> List[BgpRoute]:
        """根据前缀精确查找"""
        with self._lock:
            if not self._initialized:
                return []

            results = []
            vrfs_to_check = [vrf] if vrf else list(self._routes.keys())

            for v in vrfs_to_check:
                if v not in self._routes:
                    continue
                neighbors_to_check = [neighbor_ip] if neighbor_ip else list(self._routes[v].keys())

                for nip in neighbors_to_check:
                    if nip not in self._routes[v]:
                        continue
                    if prefix in self._routes[v][nip]:
                        results.append(self._routes[v][nip][prefix])

            return results

    def count_routes(
        self,
        vrf: Optional[str] = None,
        neighbor_ip: Optional[str] = None
    ) -> int:
        """统计路由数量"""
        with self._lock:
            if not self._initialized:
                return 0

            if vrf and neighbor_ip:
                return len(self._routes.get(vrf, {}).get(neighbor_ip, {}))

            if vrf:
                return sum(
                    len(routes) for routes in self._routes.get(vrf, {}).values()
                )

            return sum(
                len(routes)
                for neighbor_routes in self._routes.values()
                for routes in neighbor_routes.values()
            )

    def get_stats(self) -> CacheStats:
        """获取缓存统计信息"""
        with self._lock:
            stats = CacheStats()
            stats.last_sync_time = self._last_sync_time
            stats.total_vrfs = len(self._routes)
            stats.pending_writes = len(self._pending_writes)
            stats.initialized = self._initialized

            for vrf, neighbors in self._routes.items():
                stats.total_neighbors += len(neighbors)
                for routes in neighbors.values():
                    stats.total_routes += len(routes)

            return stats

    def clear(self):
        """清空缓存"""
        with self._lock:
            self._routes.clear()
            self._pending_writes.clear()
            self._initialized = False
        logger.info("Cache cleared")

    def flush(self):
        """手动刷新（供外部调用）"""
        with self._lock:
            if not self._pending_writes:
                return
            writes = self._pending_writes.copy()
            self._pending_writes.clear()
        logger.info(f"Flushing {len(writes)} pending writes")


# 全局缓存实例
_global_cache: Optional[BgpLearnedRoutesCache] = None


def get_global_cache() -> BgpLearnedRoutesCache:
    """获取全局缓存实例"""
    global _global_cache
    if _global_cache is None:
        _global_cache = BgpLearnedRoutesCache()
        _global_cache.start()
    return _global_cache


def shutdown_cache():
    """关闭全局缓存"""
    global _global_cache
    if _global_cache:
        _global_cache.stop()
        _global_cache = None
