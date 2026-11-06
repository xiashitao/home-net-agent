import asyncio
import json
import logging
import threading
from typing import Any, Dict, Optional
import httpx
from agent.brain import NetworkAgentBrain
from config.settings import FeishuConfig

logger = logging.getLogger("FeishuBot")


class FeishuBotService:
    """飞书机器人服务 (采用 WebSocket 长连接模式，无需公网 IP)"""

    def __init__(self, config: FeishuConfig, brain: NetworkAgentBrain):
        self.config = config
        self.brain = brain
        self._tenant_token: Optional[str] = None
        self._token_expire_time: float = 0

    async def get_tenant_access_token(self) -> str:
        """获取或刷新飞书自建应用 tenant_access_token"""
        import time

        if self._tenant_token and time.time() < self._token_expire_time:
            return self._tenant_token

        url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
        payload = {
            "app_id": self.config.app_id,
            "app_secret": self.config.app_secret,
        }

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload)
            data = resp.json()
            if data.get("code") == 0:
                self._tenant_token = data.get("tenant_access_token")
                # 提前 5 分钟刷新
                self._token_expire_time = (
                    time.time() + data.get("expire", 7200) - 300
                )
                return self._tenant_token
            raise RuntimeError(f"获取飞书 token 失败: {data.get('msg')}")

    async def send_text_message(
        self, receive_id: str, text: str, receive_id_type: str = "open_id"
    ) -> bool:
        """向指定用户发送文本消息"""
        token = await self.get_tenant_access_token()
        url = f"https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type={receive_id_type}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        body = {
            "receive_id": receive_id,
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        }

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, headers=headers, json=body)
            data = resp.json()
            if data.get("code") != 0:
                logger.error(f"发送飞书消息失败: {data}")
                return False
            return True

    async def send_card_message(
        self, receive_id: str, title: str, content_markdown: str, color: str = "blue"
    ) -> bool:
        """发送结构化卡片消息 (用于报警或体检报告)"""
        token = await self.get_tenant_access_token()
        url = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        card_content = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": color,
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": content_markdown},
                }
            ],
        }

        body = {
            "receive_id": receive_id,
            "msg_type": "interactive",
            "content": json.dumps(card_content, ensure_ascii=False),
        }

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, headers=headers, json=body)
            data = resp.json()
            return data.get("code") == 0

    async def broadcast_alert(
        self, title: str, content: str, color: str = "red"
    ):
        """向所有管理员广播紧急告警"""
        # 未配置飞书凭证时跳过推送，避免抛出异常中断巡检流程
        if not self.config.app_id or not self.config.app_secret:
            logger.warning(f"飞书未配置，已跳过告警推送: [{title}]")
            return

        sent = 0
        for admin_id in self.config.admin_open_ids:
            # 跳过空值与模板占位符 (真实 OpenID 不含 'x' 字符)
            if not admin_id or admin_id.startswith("ou_x"):
                continue
            try:
                ok = await self.send_card_message(
                    admin_id, title, content, color=color
                )
                sent += 1 if ok else 0
            except Exception as e:
                logger.error(f"向管理员 [{admin_id}] 推送告警失败: {e}")

        if sent == 0:
            logger.error(
                f"告警 [{title}] 未能送达任何管理员，请检查 admin_open_ids 配置"
            )

    async def handle_incoming_message(
        self, sender_open_id: str, text: str
    ) -> str:
        """接收用户在飞书中发送的文本，交由 Agent 处理并返回结果"""
        # 安全白名单过滤
        if (
            self.config.admin_open_ids
            and self.config.admin_open_ids[0] != "ou_xxxxxxxxxxxxxxxxxxxxxx"
        ):
            if sender_open_id not in self.config.admin_open_ids:
                return "⛔ 抱歉，当前账号未在家庭网络管理员白名单中。"

        logger.info(f"收到用户 [{sender_open_id}] 指令: {text}")
        reply = await self.brain.chat(text)
        return reply

    def start_websocket_listener(self):
        """启动飞书 WebSocket 长连接客户端监听消息事件"""
        try:
            import lark_oapi as lark
            from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
        except ImportError:
            logger.error("未安装 lark-oapi，无法启动 WebSocket 监听器")
            return

        loop = asyncio.get_event_loop()

        def _on_message_received(data: P2ImMessageReceiveV1) -> None:
            """收到飞书用户私聊或群聊消息的回调"""
            try:
                event = data.event
                sender_id = event.sender.sender_id.open_id
                message = event.message
                if message.message_type != "text":
                    return

                content_dict = json.loads(message.content)
                user_text = content_dict.get("text", "").strip()
                if not user_text:
                    return

                # 在后台异步处理 Agent 推理并回复
                async def _process_and_reply():
                    reply = await self.handle_incoming_message(
                        sender_id, user_text
                    )
                    await self.send_text_message(sender_id, reply)

                asyncio.run_coroutine_threadsafe(_process_and_reply(), loop)

            except Exception as e:
                logger.error(f"处理飞书消息异常: {e}", exc_info=True)

        event_handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(_on_message_received)
            .build()
        )

        cli = lark.ws.Client(
            self.config.app_id,
            self.config.app_secret,
            event_handler=event_handler,
            log_level=lark.LogLevel.INFO,
        )

        def _run_ws():
            try:
                import lark_oapi.ws.client as ws_client
                ws_loop = asyncio.new_event_loop()
                asyncio.set_event_loop(ws_loop)
                ws_client.loop = ws_loop
                cli.start()
            except Exception as e:
                logger.error(f"飞书 WebSocket 客户端运行异常: {e}", exc_info=True)

        logger.info("飞书 WebSocket 长连接客户端正在启动...")
        threading.Thread(target=_run_ws, daemon=True).start()
