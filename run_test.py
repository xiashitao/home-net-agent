#!/usr/bin/env python3
"""家庭网络 Agent 本地联调测试脚本
用于快速测试与旁路由 Nikki API、OpenWrt SSH 的连通性及网络体检
"""
import asyncio
import sys
from config.settings import get_settings
from core.diagnostics import NetworkDiagnostics
from core.nikki_client import NikkiClient
from core.openwrt_client import OpenWrtClient


async def main():
    print("=" * 60)
    print("🚀 家庭网络 Agent - 本地连通性与诊断测试")
    print("=" * 60)

    settings = get_settings()
    print(f"[*] 读取配置完成:")
    print(f"    - Nikki 地址: {settings.nikki.api_base_url}")
    print(
        f"    - OpenWrt SSH: {settings.openwrt.host}:{settings.openwrt.port} (用户: {settings.openwrt.username})"
    )
    print(f"    - LLM 模型: {settings.llm.model} ({settings.llm.base_url})")
    print("-" * 60)

    # 1. 初始化客户端
    nikki = NikkiClient(settings.nikki)
    openwrt = OpenWrtClient(settings.openwrt)
    diagnostics = NetworkDiagnostics(nikki, bypass_ip=settings.nikki.host)

    # 2. 测试 Nikki API
    print("\n[1/3] 测试旁路由 Nikki (Mihomo) REST API 连通性...")
    try:
        ver = await nikki.get_version()
        print(f"  ✅ Nikki API 连接成功! 核心版本: {ver.get('version', '未知')}")

        configs = await nikki.get_configs()
        print(f"  当前代理运行模式: {configs.get('mode')}")

        groups = await nikki.get_proxy_groups()
        print(f"  获取到策略组数量: {len(groups)}")
        for g in groups[:3]:
            print(f"    - 策略组 [{g['name']}]: 当前节点 -> {g['now']}")

    except Exception as e:
        print(f"  ❌ Nikki API 连接失败: {e}")
        print("  提示: 请检查旁路由 IP、9090 端口是否放行，或是否有配置 secret。")

    # 3. 测试 OpenWrt SSH
    print("\n[2/3] 测试 OpenWrt SSH 连接与命令执行...")
    try:
        status = await openwrt.get_system_status()
        print(f"  ✅ SSH 连接成功! 旁路由固件: {status.get('release')}")
        print(f"  系统运行时间: {status.get('uptime')}")

        clients = await openwrt.get_wifi_stations()
        print(f"  当前连在专属 WiFi 上的终端数量: {len(clients)}")
        for c in clients:
            print(
                f"    - 设备: {c['hostname']} | IP: {c['ip']} | MAC: {c['mac']} | 信号: {c.get('signal_dbm')} dBm"
            )

    except Exception as e:
        print(f"  ❌ OpenWrt SSH 连接失败: {e}")
        print("  提示: 请检查 OpenWrt SSH 端口、root 密码或公钥配置。")

    # 4. 执行全屋分层网络体检
    print("\n[3/3] 执行全屋分层网络诊断...")
    try:
        report = await diagnostics.run_full_diagnosis()
        print(f"  局域网连通: {'✅' if report['lan']['alive'] else '❌'}")
        print(f"  国内宽带:   {'✅' if report['domestic']['alive'] else '❌'}")
        print(
            f"  代理核心:   {'✅' if report['proxy_service']['alive'] else '❌'}"
        )
        print(f"  海外网络:   {'✅' if report['overseas']['alive'] else '❌'}")
        print(f"\n  👉 诊断结论: {report['conclusion']}")
        print(f"  👉 行动建议: {report['action_advice']}")
    except Exception as e:
        print(f"  ❌ 网络诊断运行失败: {e}")

    print("\n" + "=" * 60)
    print("测试完毕。")


if __name__ == "__main__":
    asyncio.run(main())
