import asyncio
import re
from typing import Any, Dict, List, Optional
import paramiko
from config.settings import OpenWrtConfig


class OpenWrtClient:
    """OpenWrt 旁路由底层 SSH 操作客户端"""

    def __init__(self, config: OpenWrtConfig):
        self.config = config

    def _exec_sync(self, command: str, timeout: int = 10) -> str:
        """执行底层 SSH 命令并获取输出 (同步)"""
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        connect_kwargs: Dict[str, Any] = {
            "hostname": self.config.host,
            "port": self.config.port,
            "username": self.config.username,
            "timeout": timeout,
        }

        if self.config.private_key_path:
            connect_kwargs["key_filename"] = self.config.private_key_path
        elif self.config.password:
            connect_kwargs["password"] = self.config.password

        try:
            client.connect(**connect_kwargs)
            stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
            out = stdout.read().decode("utf-8", errors="ignore").strip()
            err = stderr.read().decode("utf-8", errors="ignore").strip()
            if err and not out:
                return err
            return out
        finally:
            client.close()

    async def execute_command(self, command: str, timeout: int = 10) -> str:
        """异步执行 SSH 命令封装"""
        return await asyncio.to_thread(self._exec_sync, command, timeout)

    async def get_dhcp_leases(self) -> Dict[str, Dict[str, str]]:
        """获取 OpenWrt DHCP 租约列表 (/tmp/dhcp.leases)，建立 MAC -> IP/主机名 映射"""
        content = await self.execute_command("cat /tmp/dhcp.leases 2>/dev/null")
        leases = {}
        for line in content.splitlines():
            parts = line.strip().split()
            if len(parts) >= 4:
                # 格式: <timestamp> <mac> <ip> <hostname> <client_id>
                mac = parts[1].lower()
                ip = parts[2]
                hostname = parts[3] if parts[3] != "*" else "未知设备"
                leases[mac] = {"mac": mac, "ip": ip, "hostname": hostname}
        return leases

    async def get_wifi_stations(self) -> List[Dict[str, Any]]:
        """获取当前连接到旁路由 WiFi 的所有终端设备"""
        iface = self.config.wifi_interface

        # 优先使用 iwinfo，如果未安装则回退至 iw dev station dump
        cmd = f"iwinfo {iface} assoclist 2>/dev/null || iw dev {iface} station dump 2>/dev/null"
        output = await self.execute_command(cmd)

        leases = await self.get_dhcp_leases()
        clients = []

        # 处理 iwinfo assoclist 格式
        # 常见输出: 00:11:22:33:44:55  -56 dBm / -95 dBm (SNR 39)  120 ms ago
        mac_regex = re.compile(r"([0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5})")

        current_client: Optional[Dict[str, Any]] = None
        for line in output.splitlines():
            line_str = line.strip()
            mac_match = mac_regex.search(line_str)
            if mac_match and ("Station" in line_str or len(line_str.split()) <= 8):
                if current_client:
                    clients.append(current_client)
                mac = mac_match.group(1).lower()
                device_info = leases.get(mac, {})
                current_client = {
                    "mac": mac,
                    "ip": device_info.get("ip", "未知IP"),
                    "hostname": device_info.get("hostname", "未命名终端"),
                    "signal_dbm": None,
                    "rx_rate": None,
                    "tx_rate": None,
                }

                # 提取同行的信号强度 (如 -56 dBm)
                sig_match = re.search(r"(-?\d+)\s*dBm", line_str)
                if sig_match and current_client:
                    current_client["signal_dbm"] = int(sig_match.group(1))
            elif current_client:
                # 解析 station dump 的多行细节
                if "signal:" in line_str:
                    sig_match = re.search(r"(-?\d+)\s*dBm", line_str)
                    if sig_match:
                        current_client["signal_dbm"] = int(sig_match.group(1))
                elif "rx bitrate:" in line_str:
                    current_client["rx_rate"] = (
                        line_str.split(":", 1)[1].strip()
                    )
                elif "tx bitrate:" in line_str:
                    current_client["tx_rate"] = (
                        line_str.split(":", 1)[1].strip()
                    )

        if current_client:
            clients.append(current_client)

        return clients

    async def get_system_status(self) -> Dict[str, Any]:
        """获取 OpenWrt 系统的负载、内存及运行时间 (单次 SSH 批量执行以提升速度)"""
        cmd = 'uptime; echo "===SPLIT==="; free -m 2>/dev/null || cat /proc/meminfo; echo "===SPLIT==="; cat /etc/openwrt_release 2>/dev/null'
        combined_out = await self.execute_command(cmd, timeout=5)
        
        parts = combined_out.split("===SPLIT===")
        uptime_out = parts[0].strip() if len(parts) > 0 else ""
        mem_out = parts[1].strip() if len(parts) > 1 else ""
        release_out = parts[2].strip() if len(parts) > 2 else ""

        # 提取系统版本
        release_desc = "OpenWrt"
        for line in release_out.splitlines():
            if "DISTRIB_DESCRIPTION=" in line:
                release_desc = line.split("=", 1)[1].replace('"', "").strip()
                break

        # 提取内存使用情况
        mem_str = "正常"
        for line in mem_out.splitlines():
            if line.strip().startswith("Mem:"):
                parts = line.split()
                if len(parts) >= 3:
                    try:
                        total = int(parts[1])
                        used = int(parts[2])
                        if total > 10000:
                            total //= 1024
                            used //= 1024
                        pct = round((used / total) * 100, 1) if total > 0 else 0
                        mem_str = f"{used}MB / {total}MB ({pct}%)"
                    except Exception:
                        pass
                break

        # 提取运行时间与负载
        uptime_clean = uptime_out
        load_clean = "--"
        if "load average:" in uptime_out:
            p = uptime_out.split("load average:")
            load_clean = p[1].strip()
            up_part = p[0].strip()
            if "up " in up_part:
                uptime_clean = up_part.split("up ", 1)[1].rstrip(",")

        return {
            "uptime": uptime_clean,
            "load": load_clean,
            "memory": mem_str,
            "release": release_desc,
        }

    async def restart_service(self, service_name: str = "nikki") -> bool:
        """重启 OpenWrt 上的指定服务 (如 nikki / network / firewall)"""
        cmd = f"/etc/init.d/{service_name} restart"
        res = await self.execute_command(cmd)
        return "failed" not in res.lower()
