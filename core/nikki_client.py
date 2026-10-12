import asyncio
from typing import Any, Dict, List, Optional
import httpx
from config.settings import NikkiConfig


class NikkiClient:
    """Nikki (Mihomo/Clash.Meta) External Controller REST API 客户端"""

    def __init__(self, config: NikkiConfig):
        self.config = config
        self.base_url = config.api_base_url
        self.headers = {"Content-Type": "application/json"}
        if config.secret:
            self.headers["Authorization"] = f"Bearer {config.secret}"

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json_data: Optional[Dict[str, Any]] = None,
        timeout: float = 10.0,
    ) -> Dict[str, Any]:
        """通用 HTTP 请求封装"""
        url = f"{self.base_url}{path}"
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.request(
                method, url, headers=self.headers, params=params, json=json_data
            )
            resp.raise_for_status()
            if resp.status_code == 204 or not resp.content:
                return {}
            return resp.json()

    async def get_version(self) -> Dict[str, Any]:
        """获取 Mihomo/Nikki 核心版本信息"""
        return await self._request("GET", "/version")

    async def get_configs(self) -> Dict[str, Any]:
        """获取当前配置 (如模式 Rule/Global/Direct)"""
        return await self._request("GET", "/configs")

    async def set_mode(self, mode: str) -> bool:
        """设置运行模式 (Rule, Global, Direct)"""
        valid_modes = ["Rule", "Global", "Direct"]
        matched = next((m for m in valid_modes if m.lower() == mode.lower()), None)
        if not matched:
            raise ValueError(f"无效的模式: {mode}，有效模式包括: {valid_modes}")

        await self._request("PATCH", "/configs", json_data={"mode": matched})
        return True

    async def get_proxies(self) -> Dict[str, Any]:
        """获取所有代理节点及策略组"""
        res = await self._request("GET", "/proxies")
        return res.get("proxies", {})

    async def get_proxy_groups(self) -> List[Dict[str, Any]]:
        """获取所有可切换的策略组信息 (Selector / URLTest)"""
        proxies = await self.get_proxies()
        groups = []
        for name, data in proxies.items():
            if data.get("type") in ["Selector", "URLTest", "Fallback"]:
                groups.append(
                    {
                        "name": name,
                        "type": data.get("type"),
                        "now": data.get("now"),  # 当前选中的节点
                        "all": data.get("all", []),  # 备选节点列表
                    }
                )
        return groups

    async def test_node_delay(
        self,
        node_name: str,
        test_url: Optional[str] = None,
        timeout_ms: Optional[int] = None,
    ) -> Optional[int]:
        """测试指定节点的延迟 (毫秒)，超时或失败返回 None"""
        url = test_url or self.config.test_url
        timeout = timeout_ms or self.config.timeout_ms
        try:
            res = await self._request(
                "GET",
                f"/proxies/{httpx.URL(node_name)}/delay",
                params={"url": url, "timeout": timeout},
                timeout=(timeout / 1000.0) + 1.0,
            )
            return res.get("delay")
        except Exception:
            return None

    async def test_group_nodes_delay(
        self, group_name: str, max_concurrent: int = 6
    ) -> List[Dict[str, Any]]:
        """并发测速策略组下的所有候选节点"""
        proxies = await self.get_proxies()
        group = proxies.get(group_name)
        if not group or "all" not in group:
            raise ValueError(f"未找到策略组: {group_name}")

        candidate_nodes = group.get("all", [])
        sem = asyncio.Semaphore(max_concurrent)

        async def _test(node: str):
            async with sem:
                delay = await self.test_node_delay(node)
                return {
                    "node": node,
                    "delay": delay,
                    "alive": delay is not None and delay > 0,
                    "current": node == group.get("now"),
                }

        tasks = [_test(node) for node in candidate_nodes]
        results = await asyncio.gather(*tasks)

        # 按可用性及延迟从小到大排序
        results.sort(
            key=lambda x: (0 if x["alive"] else 1, x["delay"] if x["delay"] else 99999)
        )
        return results

    async def select_node(self, group_name: str, node_name: str) -> bool:
        """为指定的策略组切换代理节点"""
        await self._request(
            "PUT",
            f"/proxies/{httpx.URL(group_name)}",
            json_data={"name": node_name},
        )
        return True

    async def get_active_connections(self) -> Dict[str, Any]:
        """获取当前活跃网络连接及各设备实时统计"""
        res = await self._request("GET", "/connections")
        connections = res.get("connections", [])

        total_download = res.get("downloadTotal", 0)
        total_upload = res.get("uploadTotal", 0)

        # 按客户端 IP 进行连接与流量聚合分析
        device_stats: Dict[str, Dict[str, Any]] = {}
        for conn in connections:
            metadata = conn.get("metadata", {})
            src_ip = metadata.get("sourceIP") or "未知"
            host = metadata.get("host") or metadata.get("destinationIP")
            rule = conn.get("rule", "Direct")
            chains = conn.get("chains", [])
            proxy_node = chains[-1] if chains else "Direct"

            if src_ip not in device_stats:
                device_stats[src_ip] = {
                    "ip": src_ip,
                    "connections_count": 0,
                    "download_bytes": 0,
                    "upload_bytes": 0,
                    "active_domains": set(),
                    "proxy_nodes_used": set(),
                }

            device_stats[src_ip]["connections_count"] += 1
            device_stats[src_ip]["download_bytes"] += conn.get("download", 0)
            device_stats[src_ip]["upload_bytes"] += conn.get("upload", 0)
            if host:
                device_stats[src_ip]["active_domains"].add(host)
            device_stats[src_ip]["proxy_nodes_used"].add(proxy_node)

        # 整理聚合结果
        devices_summary = []
        for ip, stat in device_stats.items():
            devices_summary.append(
                {
                    "ip": ip,
                    "connections_count": stat["connections_count"],
                    "download_mb": round(stat["download_bytes"] / (1024 * 1024), 2),
                    "upload_mb": round(stat["upload_bytes"] / (1024 * 1024), 2),
                    "sample_domains": list(stat["active_domains"])[:5],
                    "proxy_nodes": list(stat["proxy_nodes_used"]),
                }
            )

        devices_summary.sort(key=lambda x: x["connections_count"], reverse=True)

        return {
            "total_connections": len(connections),
            "total_download_mb": round(total_download / (1024 * 1024), 2),
            "total_upload_mb": round(total_upload / (1024 * 1024), 2),
            "devices": devices_summary,
        }
