"""GoBGP Agent客户端 - Python适配层"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)


class GoBGPClient:
    """GoBGP Agent HTTP客户端"""
    
    def __init__(self, base_url: Optional[str] = None):
        self.base_url = base_url or os.environ.get("GOBGP_AGENT_URL", "http://127.0.0.1:9179")
        self.client = httpx.AsyncClient(timeout=30.0)
        logger.info(f"GoBGP Agent客户端初始化: {self.base_url}")
    
    async def close(self):
        """关闭客户端"""
        await self.client.aclose()
    
    async def health(self) -> Dict[str, Any]:
        """健康检查"""
        try:
            resp = await self.client.get(f"{self.base_url}/health")
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"GoBGP Agent健康检查失败: {e}")
            return {"status": "error", "message": str(e)}
    
    async def get_status(self) -> Dict[str, Any]:
        """获取系统状态"""
        try:
            resp = await self.client.get(f"{self.base_url}/api/status")
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"获取状态失败: {e}")
            return {}
    
    async def list_routes(self) -> List[Dict[str, Any]]:
        """列出所有BGP学习路由"""
        try:
            resp = await self.client.get(f"{self.base_url}/api/routes")
            resp.raise_for_status()
            data = resp.json()
            return data.get("routes", [])
        except Exception as e:
            logger.error(f"获取路由列表失败: {e}")
            return []
    
    async def get_route_count(self) -> int:
        """获取路由数量"""
        try:
            resp = await self.client.get(f"{self.base_url}/api/routes/count")
            resp.raise_for_status()
            data = resp.json()
            return data.get("count", 0)
        except Exception as e:
            logger.error(f"获取路由数量失败: {e}")
            return 0
    
    async def add_neighbor(self, address: str, remote_as: int) -> Dict[str, Any]:
        """添加BGP邻居（下游）"""
        try:
            resp = await self.client.post(
                f"{self.base_url}/api/neighbors/add",
                json={"address": address, "remote_as": remote_as}
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"添加邻居失败: {e}")
            raise
    
    async def remove_neighbor(self, address: str) -> Dict[str, Any]:
        """删除BGP邻居"""
        try:
            resp = await self.client.post(
                f"{self.base_url}/api/neighbors/remove",
                json={"address": address}
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"删除邻居失败: {e}")
            raise
    
    async def get_rr_status(self) -> Dict[str, Any]:
        """获取RR连接状态"""
        try:
            resp = await self.client.get(f"{self.base_url}/api/rr/status")
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"获取RR状态失败: {e}")
            return {"connected": False, "frozen": False}
    
    async def freeze(self) -> Dict[str, Any]:
        """冻结系统（测试用）"""
        try:
            resp = await self.client.post(f"{self.base_url}/api/rr/freeze")
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"冻结失败: {e}")
            raise
    
    async def unfreeze(self) -> Dict[str, Any]:
        """解冻系统（测试用）"""
        try:
            resp = await self.client.post(f"{self.base_url}/api/rr/unfreeze")
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"解冻失败: {e}")
            raise


# 全局客户端实例
_gobgp_client: Optional[GoBGPClient] = None


def get_gobgp_client() -> GoBGPClient:
    """获取全局GoBGP客户端"""
    global _gobgp_client
    if _gobgp_client is None:
        _gobgp_client = GoBGPClient()
    return _gobgp_client


async def close_gobgp_client():
    """关闭全局客户端"""
    global _gobgp_client
    if _gobgp_client:
        await _gobgp_client.close()
        _gobgp_client = None
