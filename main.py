#!/usr/bin/env python3
"""家庭网络 Agent 24/7 主服务入口
运行在家庭服务器上，提供飞书机器人交互和后台主动巡检守护
"""
import asyncio
import logging
import signal
import sys
import uvicorn
from agent.brain import NetworkAgentBrain
from agent.tools import NetworkTools
from bot.feishu_bot import FeishuBotService
from config.settings import get_settings
from core.diagnostics import NetworkDiagnostics
from core.nikki_client import NikkiClient
from core.openwrt_client import OpenWrtClient
from watcher.watchdog import NetworkWatchdog
from web.server import create_web_app, get_lan_ips

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("Main")


async def main():
    logger.info("==========================================")
    logger.info(" 家庭网络智能运维 Agent (Home Network Agent)")
    logger.info("==========================================")

    # 1. 加载配置
    settings = get_settings()

    # 2. 初始化核心底层驱动
    nikki = NikkiClient(settings.nikki)
    openwrt = OpenWrtClient(settings.openwrt)
    diagnostics = NetworkDiagnostics(nikki, bypass_ip=settings.nikki.host)

    # 3. 初始化 Agent 工具集与大模型大脑 (携带受保护服务护栏规则)
    tools = NetworkTools(
        nikki=nikki,
        openwrt=openwrt,
        diagnostics=diagnostics,
        protected_services=settings.protected_services,
    )
    brain = NetworkAgentBrain(
        config=settings.llm,
        tools=tools,
        protected_services=settings.protected_services,
    )

    # 4. 初始化飞书机器人服务
    feishu_bot = FeishuBotService(config=settings.feishu, brain=brain)

    # 5. 初始化后台主动巡检看门狗
    watchdog = NetworkWatchdog(
        config=settings.watchdog,
        diagnostics=diagnostics,
        nikki=nikki,
        bot=feishu_bot,
    )

    # 6. 启动飞书 WebSocket 长连接监听器
    if settings.feishu.app_id and settings.feishu.app_secret:
        logger.info("正在启动飞书机器人长连接监听服务...")
        feishu_bot.start_websocket_listener()
    else:
        logger.warning(
            "未配置 Feishu app_id / app_secret，跳过飞书机器人启动 (仅作为本地看门狗运行)。"
        )

    # 7. 启动局域网 Web 控制台服务
    web_server = None
    web_task = None
    if settings.web.enabled:
        web_app = create_web_app(
            settings=settings,
            nikki=nikki,
            openwrt=openwrt,
            diagnostics=diagnostics,
            brain=brain,
            watchdog=watchdog,
        )
        uv_config = uvicorn.Config(
            app=web_app,
            host=settings.web.host,
            port=settings.web.port,
            log_level="warning",
            access_log=False,
        )
        web_server = uvicorn.Server(uv_config)
        web_task = asyncio.create_task(web_server.serve())

        lan_ips = get_lan_ips()
        logger.info("==========================================")
        logger.info(" 局域网 Web 控制台已启动:")
        logger.info(f"   👉 本机访问:   http://127.0.0.1:{settings.web.port}")
        for ip in lan_ips:
            logger.info(f"   👉 局域网访问: http://{ip}:{settings.web.port}")
        logger.info("==========================================")

    # 8. 优雅停机信号处理
    stop_event = asyncio.Event()

    def _signal_handler():
        logger.info("接收到退出信号，正在停止所有后台任务...")
        watchdog.stop()
        if web_server:
            web_server.should_exit = True
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            asyncio.get_running_loop().add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            pass

    # 9. 启动巡检循环并常驻运行
    watchdog_task = asyncio.create_task(watchdog.start())

    logger.info("系统初始化完毕，主守护进程已进入常驻监听状态。")
    try:
        await stop_event.wait()
    finally:
        watchdog.stop()
        if web_server:
            web_server.should_exit = True
        if web_task:
            await web_task
        await watchdog_task
        logger.info("服务已安全退出。")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
