import asyncio
from datetime import datetime
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

    async def find_connections_by_keyword(self, keyword: str) -> Dict[str, Any]:
        """按域名/目标 IP 关键词筛选活跃连接，并汇总其实际出口链路 (代理节点或直连)

        用于回答 "某网站/App 的流量走的是代理还是直连" 这类问题。
        chains[0] 为实际出口 (节点名或 DIRECT)，末位为顶层策略组。
        """
        res = await self._request("GET", "/connections")
        connections = res.get("connections", [])
        kw = keyword.strip().lower()

        matched: List[Dict[str, Any]] = []
        for conn in connections:
            metadata = conn.get("metadata", {})
            host = metadata.get("host") or ""
            dst_ip = metadata.get("destinationIP") or ""
            # 同时匹配域名 (SNI) 与目标 IP，覆盖视频 CDN 常见的纯 IP 直连流量
            if kw not in host.lower() and kw not in dst_ip.lower():
                continue

            chains = conn.get("chains", [])
            rule = conn.get("rule", "")
            rule_payload = conn.get("rulePayload", "")
            matched.append(
                {
                    "host": host or f"[纯IP] {dst_ip}",
                    "destination": f"{dst_ip}:{metadata.get('destinationPort', '')}",
                    "source_device_ip": metadata.get("sourceIP"),
                    "network": metadata.get("network", ""),
                    "rule": f"{rule},{rule_payload}" if rule_payload else rule,
                    "route": chains[0] if chains else "DIRECT",
                    "chains": chains,
                    "download_mb": round(conn.get("download", 0) / (1024 * 1024), 2),
                    "upload_mb": round(conn.get("upload", 0) / (1024 * 1024), 2),
                    "start": conn.get("start", ""),
                }
            )

        # 按实际出口聚合，让大模型直接读出 "谁走了代理、谁直连"
        route_stats: Dict[str, Dict[str, Any]] = {}
        for c in matched:
            key = c["route"]
            if key not in route_stats:
                route_stats[key] = {
                    "route": key,
                    "connections": 0,
                    "download_mb": 0.0,
                    "upload_mb": 0.0,
                    "rules": set(),
                    "hosts": set(),
                }
            s = route_stats[key]
            s["connections"] += 1
            s["download_mb"] += c["download_mb"]
            s["upload_mb"] += c["upload_mb"]
            if c["rule"]:
                s["rules"].add(c["rule"])
            s["hosts"].add(c["host"])

        summary = []
        for s in route_stats.values():
            summary.append(
                {
                    "route": s["route"],
                    "is_proxy": s["route"].upper() not in ("DIRECT", "REJECT", ""),
                    "connections": s["connections"],
                    "download_mb": round(s["download_mb"], 2),
                    "upload_mb": round(s["upload_mb"], 2),
                    "hit_rules": sorted(s["rules"]),
                    "sample_hosts": sorted(s["hosts"])[:5],
                }
            )
        summary.sort(key=lambda x: x["download_mb"], reverse=True)

        matched.sort(key=lambda x: x["download_mb"], reverse=True)
        return {
            "keyword": keyword,
            "matched_connections": len(matched),
            "route_summary": summary,
            "top_connections": matched[:15],
        }

    async def get_subscription_info(self) -> Dict[str, Any]:
        """拉取机场订阅信息，返回流量用量与到期时间

        依据通用订阅协议: 响应头
        subscription-userinfo: upload=<B>; download=<B>; total=<B>; expire=<unix秒>
        """
        url = (self.config.subscription_url or "").strip()
        if not url:
            return {"error": "未配置订阅地址 (config.yaml → nikki.subscription_url)"}
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                resp = await client.get(
                    url, headers={"User-Agent": "clash-verge/v1.7.7"}
                )
        except Exception as e:
            return {"error": f"订阅地址访问失败: {e}"}

        userinfo = resp.headers.get("subscription-userinfo", "")
        if not userinfo:
            return {
                "error": "订阅服务未返回流量信息 (响应头缺少 subscription-userinfo)，"
                "请确认订阅地址正确或机场不支持该协议"
            }

        raw: Dict[str, int] = {}
        for kv in userinfo.replace(" ", "").split(";"):
            if "=" in kv:
                k, v = kv.split("=", 1)
                try:
                    raw[k] = int(v)
                except ValueError:
                    continue

        gb = 1024**3
        upload = raw.get("upload", 0)
        download = raw.get("download", 0)
        total = raw.get("total", 0)
        used = upload + download

        result: Dict[str, Any] = {
            "used_gb": round(used / gb, 2),
            "upload_gb": round(upload / gb, 2),
            "download_gb": round(download / gb, 2),
            "total_gb": round(total / gb, 2) if total else None,
            "remaining_gb": round((total - used) / gb, 2) if total else None,
            "usage_percent": round(used / total * 100, 1) if total else None,
        }
        expire = raw.get("expire", 0)
        if expire > 0:
            expire_dt = datetime.fromtimestamp(expire)
            result["expire_date"] = expire_dt.strftime("%Y-%m-%d")
            result["days_left"] = (expire_dt - datetime.now()).days
        return result

    async def get_effective_main_group(self) -> Optional[Dict[str, Any]]:
        """返回当前运行模式下真正承载分流流量的主策略组 (含 name/type/now/all)

        - Global 模式: GLOBAL 组即总出口
        - Rule/Direct 模式: GLOBAL 不生效，应返回主选择组 (如 "🔰 选择节点")；
          关键词采用子串匹配以兼容 emoji 前缀组名，
          兜底策略取候选节点最多的非 GLOBAL Selector 组 (主组通常挂全部节点)
        """
        groups = await self.get_proxy_groups()
        if not groups:
            return None

        try:
            configs = await self.get_configs()
        except Exception:
            configs = {}
        mode = str(configs.get("mode") or "Rule").lower()

        if mode == "global":
            for g in groups:
                if g["name"].upper() == "GLOBAL":
                    return g
            return groups[0]

        # Rule 模式: 按关键词优先级寻找主选择组 (子串匹配兼容 "🔰 选择节点" 等)
        for kw in ("节点选择", "选择节点"):
            for g in groups:
                if kw in g["name"] and g["name"].upper() != "GLOBAL":
                    return g
        for g in groups:
            if g["name"].upper() != "GLOBAL" and (
                "proxy" in g["name"].lower() or "默认" in g["name"]
            ):
                return g

        # 兑底: 非 GLOBAL 且候选节点最多的 Selector 组
        candidates = [g for g in groups if g["name"].upper() != "GLOBAL"]
        if candidates:
            return max(candidates, key=lambda g: len(g.get("all") or []))
        return None

    async def get_rules(self) -> List[Dict[str, Any]]:
        """获取当前生效的分流规则列表 (用于判断受保护域名实际走向哪个策略组)"""
        res = await self._request("GET", "/rules")
        return res.get("rules", [])

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
            # chains[0] 为实际出口节点 (或 DIRECT)，末位是顶层策略组
            proxy_node = chains[0] if chains else "DIRECT"

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
