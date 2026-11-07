import asyncio
from datetime import datetime, timedelta
import logging
from typing import Optional, Tuple
from bot.feishu_bot import FeishuBotService
from config.settings import WatchdogConfig
from core.diagnostics import NetworkDiagnostics
from core.nikki_client import NikkiClient

logger = logging.getLogger("Watchdog")


class NetworkWatchdog:
    """家庭网络后台主动巡检与故障自愈守护进程"""

    def __init__(
        self,
        config: WatchdogConfig,
        diagnostics: NetworkDiagnostics,
        nikki: NikkiClient,
        bot: FeishuBotService,
    ):
        self.config = config
        self.diagnostics = diagnostics
        self.nikki = nikki
        self.bot = bot

        self._consecutive_failures = 0
        self._last_daily_report_date: Optional[str] = None
        self._last_check_time: Optional[datetime] = None
        self._stop_event = asyncio.Event()

        # 解析日报时间点 "HH:MM"，非法配置则停用日报功能
        try:
            h, m = map(int, (config.daily_report_time or "").split(":"))
            self._daily_report_time: Optional[Tuple[int, int]] = (h, m)
        except ValueError:
            logger.warning(
                f"日报时间配置无效: {config.daily_report_time!r}，日报功能已停用"
            )
            self._daily_report_time = None

    async def _auto_heal_proxy(self) -> str:
        """自愈逻辑：并发测速并自动切换到最佳可用节点"""
        try:
            # 按当前运行模式探测真正生效的主组 (Rule 模式下切 GLOBAL 是无效自愈)
            main_group = await self.nikki.get_effective_main_group()
            if not main_group:
                return "未找到可供切换的代理策略组。"
            target_group = main_group["name"]

            ranked = await self.nikki.test_group_nodes_delay(target_group)
            alive_nodes = [n for n in ranked if n.get("alive")]
            if not alive_nodes:
                return "所有备用节点均测速超时，无法自动恢复。"

            best_node = alive_nodes[0]
            await self.nikki.select_node(target_group, best_node["node"])
            return f"已成功将 [{target_group}] 切换至最快节点 **[{best_node['node']}]** (延迟: {best_node['delay']}ms)"
        except Exception as e:
            return f"自愈过程抛出异常: {str(e)}"

    def _crossed_report_time(
        self, now: datetime, prev: Optional[datetime]
    ) -> bool:
        """判断 (prev, now] 区间是否跨过了每日日报时间点 (兼容跨天巡检)"""
        if self._daily_report_time is None or prev is None:
            return False
        occurrence = now.replace(
            hour=self._daily_report_time[0],
            minute=self._daily_report_time[1],
            second=0,
            microsecond=0,
        )
        # 检查今天与昨天两个时间点，覆盖巡检间隔跨午夜的情景
        return any(
            prev < occurrence - timedelta(days=d) <= now for d in (0, 1)
        )

    async def run_single_check(self):
        """执行单次巡检与自愈动作"""
        logger.info("Watchdog 正在执行网络巡检...")
        now = datetime.now()
        prev_check = self._last_check_time
        self._last_check_time = now
        report = await self.diagnostics.run_full_diagnosis()

        lan_ok = report["lan"]["alive"]
        domestic_ok = report["domestic"]["alive"]
        overseas_ok = report["overseas"]["alive"]
        proxy_node = report["proxy_service"]["current_node"]
        node_delay = report["proxy_service"]["node_delay_ms"]

        # 1. 宽带掉线检查
        if not domestic_ok:
            logger.warning("检测到国内网络异常！")
            await self.bot.broadcast_alert(
                "🚨 全屋光猫/宽带掉线警告",
                f"**检测时间**: {report['timestamp']}\n\n"
                f"家庭服务器无法访问国内互联网，光猫或客厅主路由可能掉线。\n\n"
                f"**建议**: 检查客厅光猫及主路由器电源与拨号状态。",
                color="red",
            )
            return

        # 2. 节点故障或海外断网检查与自愈
        if (
            not overseas_ok
            or node_delay is None
            or node_delay > self.config.high_latency_threshold_ms
        ):
            self._consecutive_failures += 1
            logger.warning(
                f"代理异常 (连续第 {self._consecutive_failures} 次)，节点: {proxy_node}, 延迟: {node_delay}"
            )

            if self._consecutive_failures >= self.config.failure_threshold_count:
                heal_msg = await self._auto_heal_proxy()
                await self.bot.broadcast_alert(
                    "⚠️ 旁路由节点异常自愈提醒",
                    f"**检测时间**: {report['timestamp']}\n\n"
                    f"原节点 **[{proxy_node}]** 出现严重丢包或超时 (延迟: {node_delay}ms)。\n\n"
                    f"**自愈动作**: {heal_msg}",
                    color="orange",
                )
                self._consecutive_failures = 0
        else:
            self._consecutive_failures = 0

        # 3. 定时网络健康日报 (跨过时间点即触发，避免巡检时刻不精确命中而漏发)
        current_date_str = now.strftime("%Y-%m-%d")

        if (
            self._crossed_report_time(now, prev_check)
            and self._last_daily_report_date != current_date_str
        ):
            self._last_daily_report_date = current_date_str

            # 订阅流量余额 (未配置或失败时静默跳过, 不影响日报)
            sub_line = ""
            try:
                sub = await self.nikki.get_subscription_info()
                if not sub.get("error"):
                    sub_line = (
                        f"- **订阅流量**: 已用 {sub['used_gb']}GB"
                        + (
                            f" / {sub['total_gb']}GB (剩余 {sub['remaining_gb']}GB,"
                            f" 用量 {sub['usage_percent']}%)"
                            if sub.get("total_gb")
                            else ""
                        )
                        + (
                            f"，套餐到期 {sub['expire_date']} (剩 {sub['days_left']} 天)"
                            if sub.get("expire_date")
                            else ""
                        )
                        + "\n"
                    )
            except Exception:
                pass

            await self.bot.broadcast_alert(
                "☀️ 家庭网络早间健康简报",
                f"**汇报时间**: {report['timestamp']}\n\n"
                f"- **局域网连通**: 正常 (旁路由延迟 {report['lan']['latency_ms']}ms)\n"
                f"- **国内宽带**: 正常 (DNS延迟 {report['domestic']['dns_latency_ms']}ms)\n"
                f"- **当前代理节点**: `{proxy_node}` (延迟: {node_delay}ms)\n"
                f"{sub_line}"
                f"- **综合结论**: {report['conclusion']}",
                color="green",
            )

    async def start(self):
        """启动巡检循环"""
        if not self.config.enabled:
            logger.info("Watchdog 未启用，跳过后台循环。")
            return

        self._stop_event.clear()
        logger.info(
            f"Watchdog 守护进程已启动，巡检间隔: {self.config.interval_minutes} 分钟"
        )

        while not self._stop_event.is_set():
            try:
                await self.run_single_check()
            except Exception as e:
                logger.error(f"Watchdog 巡检发生未捕获异常: {e}", exc_info=True)

            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self.config.interval_minutes * 60,
                )
                # 如果 stop_event 被触发，直接跳出循环退出
                break
            except asyncio.TimeoutError:
                # 正常超时，进行下一次巡检
                pass

    def stop(self):
        self._stop_event.set()
