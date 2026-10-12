import json
from typing import Any, Dict, List, Optional
from openai import AsyncOpenAI
from config.settings import LLMConfig
from .tools import NetworkTools

SYSTEM_PROMPT = """你是一个专业的家庭网络智能运维工程师 (Home Network Agent)。
你的任务是协助主人监控、管理和维护家里的网络环境。

家庭网络架构背景：
- 全屋网络包含三个路由器：客厅路由器、书房路由器，以及挂在书房路由器下的 OpenWrt 旁路由。
- 旁路由运行了 Nikki (采用 Mihomo/Clash.Meta 核心)，并单独发射了一个专属 WiFi。
- 终端设备连接该 WiFi 即可走规则节点分流。
- 你具备直接控制旁路由 Nikki API (测速、切换节点、查看流量) 以及 OpenWrt 底层系统 (查看 WiFi 连网设备、重启服务) 的能力。

行为准则：
1. 回复语气专业、清晰、简洁，适当使用 emoji 提升可读性。
2. 当用户反馈“网络卡”、“上不了网”或“掉线”时，优先调用 `diagnose_network` 进行分层排查。
3. 当用户想要“换个快节点”或“测速”时，先调用 `test_and_rank_nodes` 查看真实延迟，再调用 `switch_proxy_node` 切换。
4. 任何涉及设备重启或模式切换的关键操作，向用户明确汇报执行结果与当前状态。
5. 数据展示尽量使用清晰的列表或 Markdown 重点加粗，避免堆砌冗长未经整理的原始 JSON。
"""


class NetworkAgentBrain:
    """Agent 大脑核心：意图理解、多轮工具调用与自然语言回复生成"""

    def __init__(self, config: LLMConfig, tools: NetworkTools):
        self.config = config
        self.tools = tools
        self.client = AsyncOpenAI(
            api_key=config.api_key or "sk-dummy",
            base_url=config.base_url,
        )
        self.tool_schemas = tools.get_tool_schemas()

    async def chat(
        self,
        user_message: str,
        history: Optional[List[Dict[str, str]]] = None,
        max_turns: int = 5,
    ) -> str:
        """接收用户自然语言指令，通过 Function Calling 解决问题并生成最终回复"""
        messages: List[Dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]

        # 拼接历史上下文
        if history:
            messages.extend(history)

        messages.append({"role": "user", "content": user_message})

        # Agent 推理与工具调用循环
        for _ in range(max_turns):
            response = await self.client.chat.completions.create(
                model=self.config.model,
                messages=messages,
                tools=self.tool_schemas,
                tool_choice="auto",
                temperature=self.config.temperature,
            )

            response_msg = response.choices[0].message
            tool_calls = response_msg.tool_calls

            # 如果没有工具调用，说明大模型已经生成最终自然语言回复
            if not tool_calls:
                return response_msg.content or "（已完成处理）"

            # 将 assistant 带有 tool_calls 的消息压入历史
            messages.append(response_msg.model_dump())

            # 依次执行大模型决定的工具函数
            for tool_call in tool_calls:
                func_name = tool_call.function.name
                func_args = {}
                try:
                    if tool_call.function.arguments:
                        func_args = json.loads(tool_call.function.arguments)
                except Exception:
                    func_args = {}

                # 执行工具
                tool_result_str = await self.tools.execute_tool(func_name, func_args)

                # 将工具执行结果喂回大模型
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "name": func_name,
                        "content": tool_result_str,
                    }
                )

        # 达到最大轮次仍未终结，做最后一次总结性调用
        final_resp = await self.client.chat.completions.create(
            model=self.config.model,
            messages=messages,
            temperature=self.config.temperature,
        )
        return final_resp.choices[0].message.content or "已执行完毕相关操作。"
