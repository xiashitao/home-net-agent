import asyncio
import logging
from pathlib import Path
import socket
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agent.brain import NetworkAgentBrain
from config.settings import Settings
from core.diagnostics import NetworkDiagnostics
from core.nikki_client import NikkiClient
from core.openwrt_client import OpenWrtClient
from watcher.watchdog import NetworkWatchdog

logger = logging.getLogger("WebServer")


def get_lan_ips() -> List[str]:
    """获取本机在局域网中的所有 IPv4 地址"""
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        outbound_ip = s.getsockname()[0]
        if not outbound_ip.startswith("127."):
            ips.add(outbound_ip)
        s.close()
    except Exception:
        pass

    try:
        hostname = socket.gethostname()
        for ip in socket.gethostbyname_ex(hostname)[2]:
            if not ip.startswith("127.") and ":" not in ip:
                ips.add(ip)
    except Exception:
        pass

    res = sorted(list(ips))
    return res or ["127.0.0.1"]


class ModeRequest(BaseModel):
    mode: str


class NodeSelectRequest(BaseModel):
    group: str
    node: str


class ChatRequest(BaseModel):
    message: str
    history: Optional[List[Dict[str, Any]]] = None


def create_web_app(
    settings: Settings,
    nikki: NikkiClient,
    openwrt: OpenWrtClient,
    diagnostics: NetworkDiagnostics,
    brain: Optional[NetworkAgentBrain] = None,
    watchdog: Optional[NetworkWatchdog] = None,
) -> FastAPI:
    """创建并配置 FastAPI Web 控制台应用"""
    app = FastAPI(
        title="Home Network Agent Dashboard",
        description="家庭网络智能运维 Web 控制台",
        version="1.0.0",
    )

    # 允许局域网跨域访问
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    templates_dir = Path(__file__).parent / "templates"
    static_dir = Path(__file__).parent / "static"

    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # ==================== 页面渲染 ====================
    @app.get("/", response_class=HTMLResponse)
    async def index_page():
        index_file = templates_dir / "index.html"
        if not index_file.exists():
            return HTMLResponse(
                "<h1>Index page not found</h1>", status_code=404
            )
        with open(index_file, "r", encoding="utf-8") as f:
            content = f.read()
        return HTMLResponse(content)

    # ==================== API 接口 ====================
    @app.get("/api/info")
    async def get_server_info():
        """获取服务器基础信息与局域网 IP"""
        lan_ips = get_lan_ips()
        return {
            "lan_ips": lan_ips,
            "port": settings.web.port,
            "nikki_host": settings.nikki.host,
            "openwrt_host": settings.openwrt.host,
            "watchdog_enabled": settings.watchdog.enabled,
            "llm_model": settings.llm.model,
        }

    @app.get("/api/status")
    async def get_system_status():
        """获取综合运行状态概览"""
        status_data: Dict[str, Any] = {
            "nikki": {"online": False, "mode": "未知", "version": "未知"},
            "openwrt": {"online": False, "uptime": "未知", "release": "未知"},
            "active_group": None,
            "current_node": None,
        }

        # 1. 尝试获取 Nikki 核心状态 (设定 2.5s 超时避免阻塞)
        try:
            ver = await asyncio.wait_for(nikki.get_version(), timeout=2.5)
            conf = await asyncio.wait_for(nikki.get_configs(), timeout=2.5)
            status_data["nikki"]["online"] = True
            status_data["nikki"]["version"] = ver.get("version", "未知")
            status_data["nikki"]["mode"] = conf.get("mode", "未知")

            groups = await asyncio.wait_for(nikki.get_proxy_groups(), timeout=2.5)
            for g in groups:
                if g.get("name") in ["GLOBAL", "PROXY", "节点选择", "默认"]:
                    status_data["active_group"] = g.get("name")
                    status_data["current_node"] = g.get("now")
                    break
            if not status_data["active_group"] and groups:
                status_data["active_group"] = groups[0].get("name")
                status_data["current_node"] = groups[0].get("now")
        except Exception as e:
            logger.debug(f"Nikki API 状态查询异常或超时: {e}")

        # 2. 尝试获取 OpenWrt 系统状态 (设定 2.5s 超时)
        try:
            ow_status = await asyncio.wait_for(openwrt.get_system_status(), timeout=2.5)
            if ow_status and not ow_status.get("error"):
                status_data["openwrt"]["online"] = True
                status_data["openwrt"]["uptime"] = ow_status.get("uptime", "未知")
                status_data["openwrt"]["release"] = ow_status.get("release", "未知")
                status_data["openwrt"]["load"] = ow_status.get("load")
        except Exception as e:
            logger.debug(f"OpenWrt 状态查询异常或超时: {e}")

        return status_data

    @app.get("/api/diagnosis")
    async def run_diagnosis():
        """执行全方位网络体检"""
        try:
            res = await diagnostics.run_full_diagnosis()
            return res
        except Exception as e:
            logger.error(f"网络体检执行失败: {e}", exc_info=True)
            return JSONResponse(
                status_code=500,
                content={"error": f"网络诊断异常: {str(e)}"},
            )

    @app.get("/api/proxies")
    async def get_proxies():
        """获取代理模式、策略组和候选节点"""
        try:
            conf = await nikki.get_configs()
            groups = await nikki.get_proxy_groups()
            return {
                "mode": conf.get("mode", "Rule"),
                "groups": groups,
            }
        except Exception as e:
            return JSONResponse(
                status_code=500,
                content={"error": f"获取代理配置失败: {str(e)}"},
            )

    @app.post("/api/proxies/mode")
    async def set_proxy_mode(req: ModeRequest):
        """切换分流模式 (Rule, Global, Direct)"""
        try:
            await nikki.set_mode(req.mode)
            return {"success": True, "mode": req.mode}
        except Exception as e:
            return JSONResponse(
                status_code=400,
                content={"error": f"切换模式失败: {str(e)}"},
            )

    @app.get("/api/proxies/nodes/test")
    async def test_nodes(group: Optional[str] = Query(None)):
        """并发测速策略组下的节点"""
        try:
            if not group:
                groups = await nikki.get_proxy_groups()
                for g in groups:
                    if g.get("name") in ["GLOBAL", "PROXY", "节点选择", "默认"]:
                        group = g.get("name")
                        break
                if not group and groups:
                    group = groups[0].get("name")

            if not group:
                raise HTTPException(status_code=400, detail="未找到可用策略组")

            results = await nikki.test_group_nodes_delay(group)
            return {"group": group, "nodes": results}
        except Exception as e:
            return JSONResponse(
                status_code=500,
                content={"error": f"节点测速失败: {str(e)}"},
            )

    @app.post("/api/proxies/nodes/select")
    async def select_node(req: NodeSelectRequest):
        """切换策略组选中的节点"""
        try:
            await nikki.select_node(req.group, req.node)
            return {
                "success": True,
                "message": f"策略组 [{req.group}] 已切换至 [{req.node}]",
            }
        except Exception as e:
            return JSONResponse(
                status_code=500,
                content={"error": f"切换节点失败: {str(e)}"},
            )

    @app.get("/api/devices")
    async def get_devices():
        """获取专属 WiFi 连接设备以及网络流量消耗"""
        wifi_clients = []
        traffic = {"total_connections": 0, "total_download_mb": 0, "total_upload_mb": 0, "devices": []}

        try:
            wifi_clients = await openwrt.get_wifi_stations()
        except Exception as e:
            logger.debug(f"获取 WiFi 客户端列表失败: {e}")

        try:
            traffic = await nikki.get_active_connections()
        except Exception as e:
            logger.debug(f"获取活跃流量连接失败: {e}")

        return {
            "wifi_clients": wifi_clients,
            "traffic": traffic,
        }

    @app.get("/api/system")
    async def get_system_detail():
        """获取 OpenWrt 底层系统详情"""
        try:
            status = await openwrt.get_system_status()
            return status
        except Exception as e:
            return JSONResponse(
                status_code=500,
                content={"error": f"获取系统信息失败: {str(e)}"},
            )

    @app.post("/api/system/restart-nikki")
    async def restart_nikki():
        """重启 Nikki 代理服务"""
        try:
            ok = await openwrt.restart_service("nikki")
            return {
                "success": ok,
                "message": "Nikki 重启指令已下发至 OpenWrt",
            }
        except Exception as e:
            return JSONResponse(
                status_code=500,
                content={"error": f"重启服务失败: {str(e)}"},
            )

    @app.post("/api/chat")
    async def chat_with_agent(req: ChatRequest):
        """与家庭网络 Agent 智能对话 (支持自然语言排障与操作)"""
        if not brain:
            return JSONResponse(
                status_code=503,
                content={"reply": "大模型大脑未初始化或未配置 API Key。"},
            )

        try:
            reply = await brain.chat(
                user_message=req.message,
                history=req.history,
            )
            return {"reply": reply}
        except Exception as e:
            logger.error(f"Chat 执行异常: {e}", exc_info=True)
            return JSONResponse(
                status_code=500,
                content={"reply": f"🤖 抱歉，处理您的指令时出现错误：{str(e)}"},
            )

    return app
