import json
from typing import Any, Dict, List
from core.diagnostics import NetworkDiagnostics
from core.nikki_client import NikkiClient
from core.openwrt_client import OpenWrtClient


class NetworkTools:
    """提供给大模型调用的标准化家庭网络运维工具集"""

    def __init__(
        self,
        nikki: NikkiClient,
        openwrt: OpenWrtClient,
        diagnostics: NetworkDiagnostics,
    ):
        self.nikki = nikki
        self.openwrt = openwrt
        self.diagnostics = diagnostics

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
                    "description": "为指定的策略组切换代理节点",
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
                    groups = await self.nikki.get_proxy_groups()
                    target_group = next(
                        (
                            g["name"]
                            for g in groups
                            if g["name"] in ["GLOBAL", "PROXY", "节点选择", "默认"]
                        ),
                        None,
                    )
                    group_name = target_group or (groups[0]["name"] if groups else "")

                if not group_name:
                    return json.dumps({"error": "未检测到可用的策略组"})

                results = await self.nikki.test_group_nodes_delay(group_name)
                return json.dumps(
                    {"group": group_name, "ranked_nodes": results},
                    ensure_ascii=False,
                )

            elif name == "switch_proxy_node":
                group = arguments["group_name"]
                node = arguments["node_name"]
                await self.nikki.select_node(group, node)
                return json.dumps(
                    {"success": True, "message": f"已将策略组 [{group}] 切换至 [{node}]"}
                )

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
