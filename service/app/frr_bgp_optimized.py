#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
高性能 BGP 路由通告引擎 - 优化版本

优化策略:
1. 路由预处理 - 去重、过滤、排序
2. 智能速率控制 - 根据系统负载动态调整
3. 异步并行执行 - 多线程注入
4. FRR 参数优化 - 调整 BGP 定时器和缓冲区
5. 系统级调优 - Linux 内核参数
"""

import subprocess
import tempfile
import os
import time
import threading
import queue
import psutil
import logging

logger = logging.getLogger(__name__)

class RouteAdvertiseEngine:
    """高性能路由通告引擎"""
    
    def __init__(self):
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._workers = []
        self._stats = {
            'total_routes': 0,
            'processed': 0,
            'failed': 0,
            'start_time': None,
            'end_time': None
        }
    
    def _get_system_load(self):
        """获取系统负载指标"""
        return {
            'cpu_percent': psutil.cpu_percent(interval=0.1),
            'memory_percent': psutil.virtual_memory().percent,
            'bgpd_cpu': self._get_bgpd_cpu_usage()
        }
    
    def _get_bgpd_cpu_usage(self):
        """获取 bgpd 进程 CPU 使用率"""
        for proc in psutil.process_iter(['name', 'cpu_percent']):
            if proc.info['name'] == 'bgpd':
                return proc.info['cpu_percent']
        return 0
    
    def _calculate_optimal_batch_size(self, load):
        """根据系统负载动态计算最优批处理大小"""
        base_size = 1000
        
        # CPU 负载调整
        if load['cpu_percent'] > 80:
            base_size = 200
        elif load['cpu_percent'] > 60:
            base_size = 500
        elif load['cpu_percent'] > 40:
            base_size = 800
        
        # bgpd 专用调整
        if load['bgpd_cpu'] > 90:
            base_size = 100
        elif load['bgpd_cpu'] > 70:
            base_size = 300
        
        return base_size
    
    def _calculate_delay(self, load):
        """根据系统负载计算批次间隔延迟"""
        if load['cpu_percent'] > 80 or load['bgpd_cpu'] > 90:
            return 0.5  # 高负载时增加延迟
        elif load['cpu_percent'] > 60:
            return 0.2
        return 0.05  # 正常负载
    
    def _calculate_conservative_batch_size(self, load):
        """保守策略 - 根据系统负载动态计算批处理大小（百万路由专用）"""
        # 保守策略，更小的批处理
        base_size = 200
        
        # CPU 负载调整
        if load['cpu_percent'] > 70:
            base_size = 50
        elif load['cpu_percent'] > 50:
            base_size = 100
        elif load['cpu_percent'] > 30:
            base_size = 150
        
        # bgpd 专用调整（更敏感）
        if load['bgpd_cpu'] > 80:
            base_size = 50
        elif load['bgpd_cpu'] > 60:
            base_size = 100
        elif load['bgpd_cpu'] > 40:
            base_size = 150
        
        return base_size
    
    def _calculate_conservative_delay(self, load):
        """保守策略 - 根据系统负载计算批次间隔延迟（百万路由专用）"""
        # 保守策略，更长的延迟
        if load['cpu_percent'] > 70 or load['bgpd_cpu'] > 80:
            return 2.0  # 高负载时强制延迟 2秒
        elif load['cpu_percent'] > 50 or load['bgpd_cpu'] > 60:
            return 1.0
        elif load['cpu_percent'] > 30 or load['bgpd_cpu'] > 40:
            return 0.5
        return 0.2  # 正常负载延迟 0.2秒
    
    def _preprocess_routes(self, routes):
        """路由预处理: 去重、过滤、排序"""
        if not routes:
            return []
        
        # 去重 - 使用字典保持最新的 nexthop
        seen = {}
        for prefix, nexthop in routes:
            seen[prefix] = nexthop
        
        # 按前缀长度排序（短前缀优先，减少路由表碎片化）
        sorted_routes = sorted(seen.items(), key=lambda x: (x[0].split('/')[1], x[0]))
        
        logger.info(f"预处理完成: 原始 {len(routes)} 条 → 去重后 {len(sorted_routes)} 条")
        return sorted_routes
    
    def _worker_process_batch(self, vrf, batch_queue, results_queue):
        """工作线程处理批次"""
        while not self._stop_event.is_set():
            try:
                batch = batch_queue.get(timeout=1)
                if batch is None:
                    break
                
                batch_num, routes = batch
                result = self._execute_ip_batch(vrf, routes)
                results_queue.put((batch_num, result))
                
            except queue.Empty:
                continue
            except Exception as e:
                logger.error(f"Worker error: {e}")
    
    def _execute_ip_batch(self, vrf, routes):
        """执行单批次路由注入"""
        start_time = time.time()
        errors = []
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.sh', delete=False) as f:
            for prefix, nexthop in routes:
                if nexthop:
                    f.write(f"route replace vrf {vrf} {prefix} via {nexthop}\n")
                else:
                    f.write(f"route replace vrf {vrf} {prefix}\n")
            temp_file = f.name
        
        try:
            cmd = ["bash", "-c", f"ip -batch {temp_file}"]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            
            if result.returncode != 0 and "File exists" not in result.stderr:
                errors.append(result.stderr[:100])
            
            elapsed = time.time() - start_time
            return {
                'count': len(routes),
                'success': len(routes),
                'errors': errors,
                'elapsed': elapsed
            }
        finally:
            try:
                os.unlink(temp_file)
            except:
                pass
    
    def optimize_frr_config(self, vrf):
        """优化 FRR/BGP 配置参数"""
        config_cmds = [
            f"router bgp 65000 vrf {vrf}",
            "bgp router-id 139.159.43.207",
            "bgp bestpath as-path multipath-relax",
            "bgp bestpath compare-routerid",
            "timers bgp 30 90",  # 调整 BGP 定时器
            "neighbor default timers 30 90",
            "exit",
            f"ip route vrf {vrf} maximum-paths 64"
        ]
        
        try:
            with tempfile.NamedTemporaryFile(mode='w', suffix='.conf', delete=False) as f:
                f.write('\n'.join(config_cmds))
                temp_file = f.name
            
            cmd = ['vtysh', '-f', temp_file]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            
            if result.returncode == 0:
                logger.info(f"FRR config optimized for {vrf}")
            else:
                logger.error(f"FRR config optimization failed: {result.stderr}")
        finally:
            try:
                os.unlink(temp_file)
            except:
                pass
    
    def optimize_system_params(self):
        """优化 Linux 系统参数"""
        params = {
            '/proc/sys/net/ipv4/tcp_syncookies': '1',
            '/proc/sys/net/ipv4/tcp_tw_reuse': '1',
            '/proc/sys/net/ipv4/tcp_tw_recycle': '1',
            '/proc/sys/net/core/somaxconn': '65535',
            '/proc/sys/net/core/netdev_max_backlog': '30000',
            '/proc/sys/net/ipv4/tcp_max_syn_backlog': '16384',
            '/proc/sys/net/ipv4/route/max_size': '2000000',
            '/proc/sys/net/ipv4/route/flush': '0'
        }
        
        for path, value in params.items():
            try:
                with open(path, 'w') as f:
                    f.write(value)
                logger.info(f"Set {path} = {value}")
            except Exception as e:
                logger.warning(f"Failed to set {path}: {e}")
    
    def advertise_routes(self, vrf, routes, max_workers=2):
        """
        极致性能优化的路由通告 - 针对百万级路由
        使用更保守的策略，避免压垮系统
        
        参数:
            vrf: VRF 名称
            routes: [(prefix, nexthop), ...] 路由列表
            max_workers: 并行工作线程数
        
        返回: 统计结果
        """
        self._stop_event.clear()
        self._stats = {
            'total_routes': len(routes),
            'processed': 0,
            'failed': 0,
            'start_time': time.time(),
            'end_time': None,
            'batches': 0,
            'avg_speed': 0
        }
        
        # 1. 路由预处理
        routes = self._preprocess_routes(routes)
        total = len(routes)
        
        if total == 0:
            return self._stats
        
        logger.info(f"开始向 {vrf} 通告 {total} 条路由（保守模式）")
        
        # 2. 优化系统参数
        self.optimize_system_params()
        
        # 3. 设置队列（使用更小的队列大小）
        batch_queue = queue.Queue(maxsize=5)
        results_queue = queue.Queue()
        
        # 4. 启动工作线程（减少线程数）
        for _ in range(max_workers):
            worker = threading.Thread(
                target=self._worker_process_batch,
                args=(vrf, batch_queue, results_queue),
                daemon=True
            )
            worker.start()
            self._workers.append(worker)
        
        # 5. 智能调度路由批次
        processed = 0
        batch_num = 0
        
        try:
            while processed < total and not self._stop_event.is_set():
                # 获取当前系统负载
                load = self._get_system_load()
                
                # 动态计算批处理大小和延迟（更保守）
                batch_size = self._calculate_conservative_batch_size(load)
                delay = self._calculate_conservative_delay(load)
                
                # 提取当前批次
                end_idx = min(processed + batch_size, total)
                batch = routes[processed:end_idx]
                
                # 提交到队列
                batch_queue.put((batch_num, batch))
                batch_num += 1
                
                processed += len(batch)
                
                # 更新统计
                with self._lock:
                    self._stats['processed'] = processed
                    self._stats['batches'] = batch_num
                
                # 输出进度（降低日志频率）
                if batch_num % 10 == 0 or processed == total:
                    progress = (processed / total) * 100
                    elapsed = time.time() - self._stats['start_time']
                    speed = processed / elapsed if elapsed > 0 else 0
                    self._stats['avg_speed'] = speed
                    
                    logger.info(
                        f"VRF:{vrf} Progress: {progress:.1f}% ({processed}/{total}) "
                        f"| Speed: {speed:.0f} routes/s | Batch: {batch_size} | "
                        f"CPU: {load['cpu_percent']:.0f}% | BGPd: {load['bgpd_cpu']:.0f}%"
                    )
                
                # 速率控制延迟（强制延迟，避免系统过载）
                if delay > 0 and processed < total:
                    time.sleep(delay)
            
            # 发送结束信号
            for _ in range(max_workers):
                batch_queue.put(None)
            
            # 等待所有工作线程完成
            for worker in self._workers:
                worker.join(timeout=300)
            
            # 汇总结果
            while not results_queue.empty():
                _, result = results_queue.get()
                self._stats['failed'] += (result['count'] - result['success'])
            
        except Exception as e:
            logger.error(f"路由通告失败: {e}")
            self._stop_event.set()
        
        finally:
            self._stop_event.set()
            self._stats['end_time'] = time.time()
            self._workers = []
            
            total_elapsed = self._stats['end_time'] - self._stats['start_time']
            avg_speed = self._stats['processed'] / total_elapsed if total_elapsed > 0 else 0
            
            logger.info(
                f"VRF:{vrf} 路由通告完成: {self._stats['processed']}/{total} 条 "
                f"| 耗时: {total_elapsed:.2f}s | 平均速度: {avg_speed:.0f} routes/s"
            )
        
        return self._stats
    
    def stop(self):
        """停止所有正在进行的操作"""
        self._stop_event.set()


def add_bgp_networks_optimized(vrf: str, prefixes_with_nexthop: list) -> dict:
    """
    高性能批量路由通告接口
    
    参数:
        vrf: VRF 名称
        prefixes_with_nexthop: [(prefix, nexthop), ...] 路由列表
    
    返回: {"added": 数量, "failed": 数量, "elapsed": 耗时, "speed": 速度}
    """
    engine = RouteAdvertiseEngine()
    stats = engine.advertise_routes(vrf, prefixes_with_nexthop)
    
    return {
        'added': stats['processed'],
        'failed': stats['failed'],
        'total': stats['total_routes'],
        'elapsed': (stats['end_time'] - stats['start_time']) if stats['end_time'] else 0,
        'speed': stats['avg_speed'],
        'method': 'optimized-parallel'
    }


def add_bgp_networks_batch(vrf: str, prefixes_with_nexthop: list, timeout_s: int = 60) -> dict:
    """
    兼容旧接口的批量路由通告方法（使用优化引擎）
    """
    return add_bgp_networks_optimized(vrf, prefixes_with_nexthop)
