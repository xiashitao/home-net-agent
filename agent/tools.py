import json
from typing import Any, Dict, List, Optional
from core.diagnostics import NetworkDiagnostics
from core.nikki_client import NikkiClient
from core.openwrt_client import OpenWrtClient
from config.settings import ProtectedService


class NetworkTools:
    """提供给大模型调用的标准化家庭网络运维工具集"""

    def __init__(
        self,
        nikki: NikkiClient,
        openwrt: OpenWrtClient,
        diagnostics: NetworkDiagnostics,
        protected_services: Optional[List[ProtectedService]] = None,
    ):
        self.nikki = nikki
        self.openwrt = openwrt
        self.diagnostics = diagnostics
        self.protected_services = protected_services or []

    # ==================== 受保护服务护栏 (Layer 2) ====================

    def _match_forbidden_keywords(self, node_name: str) -> List[Dict[str, Any]]:
        """检查节点名是否命中任一受保护服务的禁用关键词"""
        hits: List[Dict[str, Any]] = []
        name_lower = (node_name or "").lower()
        for svc in self.protected_services:
            for kw in svc.forbidden_node_keywords:
                if kw.lower() in name_lower:
                    hits.append(
                        {
                            "service": svc.name,
                            "keyword": kw,
                            "domains": svc.domains,
                            "advice": svc.advice,
                        }
                    )
                    break  # 每个服务命中一次即可
        return hits

    @staticmethod
    def _is_main_group(group_name: str) -> bool:
        """判断是否为主策略组 (主组承载绝大多数分流流量，含受保护服务)

        采用子串匹配以兼容机场配置中常见的 emoji 前缀组名，
        如 "🔰 选择节点"、"♻️ 自动选择"、"✅ 默认" 等。
        """
        name = (group_name or "").strip().lower()
        if not name:
            return False
        return any(
            kw in name
            for kw in ("global", "proxy", "选择节点", "节点选择", "默认")
        )

    async def _protected_groups(self) -> set:
        """通过分流规则反查: 哪些策略组实际承载受保护服务的域名流量"""
        protected: set = set()
        if not self.protected_services:
            return protected
        try:
            rules = await self.nikki.get_rules()
        except Exception:
            return protected

        all_domains = [
            d.lower() for svc in self.protected_services for d in svc.domains
        ]
        for rule in rules:
            payload = (rule.get("payload") or "").lower()
            target = rule.get("proxy") or ""
            if not payload or not target:
                continue
            for domain in all_domains:
                # 兼容 DOMAIN-SUFFIX 双向包含 (规则 payload 与受保护域名互为后缀)
                if (
                    payload == domain
                    or domain.endswith("." + payload)
                    or payload.endswith("." + domain)
                ):
                    protected.add(target)
                    break
        return protected

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        """返回符合 OpenAI / 兼容协议的 Function Calling JSON Schema"""
        return [
            {
                "type": "function",
                "function": {
                    "name": "diagnose_network",
                    "description": "进行全方位分层网络健康检查 (局域网连通性、国内宽带、DNS响应、旁路由核心状态及海外代理连通性)",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_proxy_status",
                    "description": "查看当前旁路由的代理运行模式 (Rule/Global/Direct) 以及各策略组当前选中的节点",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "test_and_rank_nodes",
                    "description": "对指定策略组 (或默认组) 下的所有节点进行批量并发测速，并按延迟从低到高排序返回",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "group_name": {
                                "type": "string",
                                "description": "策略组名称，例如 'GLOBAL'、'PROXY'、'节点选择'。留空则自动检测主策略组。",
                            }
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "switch_proxy_node",
                    "description": "为指定的策略组切换代理节点。内置安全护栏: 若目标节点会破坏受保护服务 (如 Gemini 不支持香港节点) 会拒绝执行并返回原因，此时应向用户解释风险并推荐替代节点; 仅当用户明确知晓风险并坚持要求时才携带 force=true 强制切换",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "group_name": {
                                "type": "string",
                                "description": "策略组名称 (如 'GLOBAL', 'PROXY', '节点选择')",
                            },
                            "node_name": {
                                "type": "string",
                                "description": "目标节点的完整名称",
                            },
                            "force": {
                                "type": "boolean",
                                "description": "强制切换 (跳过受保护服务护栏)，默认 false",
                            },
                        },
                        "required": ["group_name", "node_name"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "set_proxy_mode",
                    "description": "切换旁路由代理模式 (Rule: 规则分流, Global: 全局代理, Direct: 全部直连)",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "mode": {
                                "type": "string",
                                "enum": ["Rule", "Global", "Direct"],
                                "description": "运行模式",
                            }
                        },
                        "required": ["mode"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "list_connected_devices",
                    "description": "查看当前连接在旁路由专属 WiFi 上的所有终端设备列表 (包括设备名称、IP、MAC、信号强度)",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "trace_domain_route",
                    "description": "实时追踪指定网站/App 的流量实际走代理节点还是直连 (查看当前活跃连接的命中规则与出口链路)。建议在目标应用正在播放/使用时查询，例如用关键词 'bili' 可同时匹配 bilibili.com / bilivideo.com / hdslb.com 等 B站全家桶域名",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "keyword": {
                                "type": "string",
                                "description": "域名或目标 IP 关键词，如 'bili' (B站)、'youtube'、'openai'",
                            }
                        },
                        "required": ["keyword"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_traffic_balance",
                    "description": "查询机场订阅的流量余额与到期时间 (已用/总量/剩余/用量百分比/到期日/剩余天数)，适用于用户询问“流量还剩多少”、“套餐什么时候过期”等问题",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "inspect_device_traffic",
                    "description": "查看当前各设备在旁路由上的实时活跃连接、下行/上行流量消耗及正在访问的域名",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "restart_proxy_service",
                    "description": "当旁路由代理核心异常或卡死时，通过 SSH 重启 Nikki 服务进程",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_openwrt_system_status",
                    "description": "查看 OpenWrt 旁路由硬件的 CPU 负载、系统运行时间及内存使用率",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
        ]

    async def execute_tool(self, name: str, arguments: Dict[str, Any]) -> str:
        """分发并执行具体的工具逻辑，并返回格式化 JSON 结果字符串"""
        try:
            if name == "diagnose_network":
                res = await self.diagnostics.run_full_diagnosis()
                return json.dumps(res, ensure_ascii=False)

            elif name == "get_proxy_status":
                configs = await self.nikki.get_configs()
                groups = await self.nikki.get_proxy_groups()
                return json.dumps(
                    {"mode": configs.get("mode"), "groups": groups},
                    ensure_ascii=False,
                )

            elif name == "test_and_rank_nodes":
                group_name = arguments.get("group_name")
                if not group_name:
                    # 按当前运行模式探测真正生效的主组 (Rule 模式下 GLOBAL 无效)
                    main_group = await self.nikki.get_effective_main_group()
                    group_name = main_group["name"] if main_group else ""

                if not group_name:
                    return json.dumps({"error": "未检测到可用的策略组"})

                results = await self.nikki.test_group_nodes_delay(group_name)

                # 标记会破坏受保护服务的节点，供大模型决策时主动避开 (Layer 1 辅助)
                for r in results:
                    hit = self._match_forbidden_keywords(r.get("node", ""))
                    if hit:
                        r["protected_violation"] = hit[0]["service"]

                return json.dumps(
                    {"group": group_name, "ranked_nodes": results},
                    ensure_ascii=False,
                )

            elif name == "switch_proxy_node":
                group = arguments["group_name"]
                node = arguments["node_name"]
                force = bool(arguments.get("force", False))

                # ===== 受保护服务护栏: 禁用节点硬校验 =====
                hits = self._match_forbidden_keywords(node)
                if hits and not force:
                    protected_groups = await self._protected_groups()
                    if group in protected_groups or self._is_main_group(group):
                        hit = hits[0]
                        return json.dumps(
                            {
                                "refused": True,
                                "error": (
                                    f"已拒绝切换: 节点 [{node}] 命中禁用关键词 "
                                    f"[{hit['keyword']}]，会破坏受保护服务 [{hit['service']}]"
                                ),
                                "protected_service": hit["service"],
                                "related_domains": hit["domains"],
                                "advice": hit["advice"] or "请选择其他地区的节点",
                                "hint": "若用户已知晓风险并明确坚持，可携带 force=true 重新调用",
                            },
                            ensure_ascii=False,
                        )

                await self.nikki.select_node(group, node)
                result = {
                    "success": True,
                    "message": f"已将策略组 [{group}] 切换至 [{node}]",
                }
                if hits:
                    result["warning"] = (
                        f"⚠️ 节点 [{node}] 含禁用关键词 [{hits[0]['keyword']}]，"
                        f"可能影响 [{hits[0]['service']}]，请留意该服务可用性"
                    )
                return json.dumps(result, ensure_ascii=False)

            elif name == "set_proxy_mode":
                mode = arguments["mode"]
                await self.nikki.set_mode(mode)
                return json.dumps(
                    {"success": True, "message": f"代理模式已切换为 [{mode}]"}
                )

            elif name == "list_connected_devices":
                clients = await self.openwrt.get_wifi_stations()
                return json.dumps(
                    {"count": len(clients), "devices": clients},
                    ensure_ascii=False,
                )

            elif name == "trace_domain_route":
                keyword = arguments.get("keyword", "")
                result = await self.nikki.find_connections_by_keyword(keyword)
                return json.dumps(result, ensure_ascii=False)

            elif name == "get_traffic_balance":
                result = await self.nikki.get_subscription_info()
                return json.dumps(result, ensure_ascii=False)

            elif name == "inspect_device_traffic":
                traffic = await self.nikki.get_active_connections()
                return json.dumps(traffic, ensure_ascii=False)

            elif name == "restart_proxy_service":
                ok = await self.openwrt.restart_service("nikki")
                return json.dumps(
                    {"success": ok, "message": "Nikki 服务重启指令已发送并执行"}
                )

            elif name == "get_openwrt_system_status":
                status = await self.openwrt.get_system_status()
                return json.dumps(status, ensure_ascii=False)

            else:
                return json.dumps({"error": f"未知工具函数: {name}"})

        except Exception as e:
            return json.dumps({"error": f"工具执行异常: {str(e)}"}, ensure_ascii=False)
