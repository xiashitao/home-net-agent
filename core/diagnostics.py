import asyncio
import re
import sys
import time
from typing import Any, Dict, List, Optional
import httpx
from .nikki_client import NikkiClient


class NetworkDiagnostics:
    """分层网络健康体检与故障定位引擎"""

    def __init__(self, nikki_client: NikkiClient, bypass_ip: str = "192.168.1.2"):
        self.nikki = nikki_client
        self.bypass_ip = bypass_ip

    async def _ping_host(self, host: str, timeout: float = 2.0) -> Optional[float]:
        """通过 ICMP ping 测试指定主机的连通性及往返延迟 (ms)

        - Linux (含 Docker 容器) 的 -W 单位为秒，macOS/BSD 为毫秒，需按平台区分
        - 延迟取自 ping 汇总行的 avg RTT，而非子进程总耗时估算
        """
        # Linux: -W 秒；macOS: -W 毫秒
        if sys.platform == "darwin":
            wait_arg = str(int(timeout * 1000))
        else:
            wait_arg = str(int(timeout))

        cmd = ["ping", "-c", "2", "-W", wait_arg, host]
        start_time = time.time()
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(
                proc.communicate(), timeout=timeout + 1.5
            )
            if proc.returncode != 0:
                return None

            out = stdout.decode("utf-8", errors="ignore")
            # Linux:  rtt min/avg/max/mdev = 8.912/9.541/10.170/0.439 ms
            # macOS:  round-trip min/avg/max/stddev = 8.912/9.541/10.170/0.439 ms
            # busybox: round-trip min/avg/max = 8.9/9.5/10.1 ms
            m = re.search(r"=\s*([\d.]+)/([\d.]+)/([\d.]+)", out)
            if m:
                return float(m.group(2))

            # 兑底：汇总行解析失败时退化为总耗时估算，保证返回可用数值
            elapsed = (time.time() - start_time) * 1000 / 2
            return round(elapsed, 1)
        except Exception:
            return None

    async def _http_check(self, url: str, timeout: float = 3.0) -> Optional[float]:
        """测试指定 URL 的 HTTP 握手响应延迟 (ms)"""
        start = time.time()
        try:
            async with httpx.AsyncClient(timeout=timeout, verify=False) as client:
                resp = await client.get(url)
                if resp.status_code in [200, 204, 301, 302]:
                    return round((time.time() - start) * 1000, 1)
                return None
        except Exception:
            return None

    async def run_full_diagnosis(self) -> Dict[str, Any]:
        """执行全方位分层网络诊断"""
        # 1. 检查家庭服务器与旁路由的局域网连通性
        lan_ping = await self._ping_host(self.bypass_ip)
        lan_ok = lan_ping is not None

        # 2. 检查国内直连连通性 (阿里DNS与百度)
        domestic_dns_ping = await self._ping_host("223.5.5.5")
        domestic_http = await self._http_check("https://www.baidu.com")
        domestic_ok = domestic_dns_ping is not None or domestic_http is not None

        # 3. 检查 Nikki (Mihomo) 核心服务是否存活
        nikki_version = None
        current_node = None
        node_delay = None
        proxy_api_ok = False

        try:
            ver_info = await self.nikki.get_version()
            nikki_version = ver_info.get("version", "未知")
            proxy_api_ok = True

            # 获取真正生效的主组及其当前选中节点 (Rule 模式下 GLOBAL 无效)
            main_group = await self.nikki.get_effective_main_group()
            if main_group:
                current_node = main_group.get("now")

            if current_node:
                node_delay = await self.nikki.test_node_delay(current_node)
        except Exception:
            proxy_api_ok = False

        # 4. 检查海外网络连通性 (Google 204)
        overseas_http = await self._http_check(
            "https://www.google.com/generate_204", timeout=4.0
        )
        overseas_ok = overseas_http is not None or (
            node_delay is not None and node_delay > 0
        )

        # 5. 综合诊断结论推导
        conclusion = ""
        action_advice = ""

        if not lan_ok:
            conclusion = "🚨 局域网断开：家庭服务器无法连接到旁路由 (IP: " + self.bypass_ip + ")"
            action_advice = "请检查旁路由电源、网线，或确认旁路由 IP 是否变动。"
        elif not domestic_ok:
            conclusion = "🚨 全屋光猫/宽带掉线：国内基础网络无法访问。"
            action_advice = "宽带可能欠费、光猫断纤或主路由拨号断开，建议检查客厅主路由拨号状态。"
        elif not proxy_api_ok:
            conclusion = "⚠️ 旁路由系统在线，但 Nikki (Mihomo) 核心进程异常或 API 端口无法访问。"
            action_advice = "建议重启旁路由上的 Nikki 服务 (`/etc/init.d/nikki restart`)。"
        elif not overseas_ok:
            conclusion = f"⚠️ 国内网络正常，但当前代理节点 [{current_node}] 无法连通外网。"
            action_advice = "建议通过 Agent 测速并切换到其他低延迟的备用节点。"
        else:
            conclusion = f"✅ 全网通畅：国内宽带健康，当前代理节点 [{current_node}] 运行良好 (延迟: {node_delay or overseas_http}ms)。"
            action_advice = "无需人工干预，网络处于最佳状态。"

        return {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "lan": {"target": self.bypass_ip, "alive": lan_ok, "latency_ms": lan_ping},
            "domestic": {
                "alive": domestic_ok,
                "dns_latency_ms": domestic_dns_ping,
                "http_latency_ms": domestic_http,
            },
            "proxy_service": {
                "alive": proxy_api_ok,
                "version": nikki_version,
                "current_node": current_node,
                "node_delay_ms": node_delay,
            },
            "overseas": {"alive": overseas_ok, "latency_ms": overseas_http},
            "conclusion": conclusion,
            "action_advice": action_advice,
        }
