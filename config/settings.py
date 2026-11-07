import os
from pathlib import Path
from typing import List, Optional
import yaml
from pydantic import BaseModel, Field


class NikkiConfig(BaseModel):
    host: str = "192.168.1.2"
    port: int = 9090
    secret: str = ""
    test_url: str = "https://www.gstatic.com/generate_204"
    timeout_ms: int = 3000
    # 机场订阅地址 (用于查询流量余额/到期时间, 响应头 subscription-userinfo 协议)
    subscription_url: str = ""

    @property
    def api_base_url(self) -> str:
        return f"http://{self.host}:{self.port}"


class OpenWrtConfig(BaseModel):
    host: str = "192.168.1.2"
    port: int = 22
    username: str = "root"
    password: Optional[str] = None
    private_key_path: Optional[str] = None
    wifi_interface: str = "wlan0"


class LLMConfig(BaseModel):
    provider: str = "openai"
    base_url: str = "https://api.deepseek.com/v1"
    api_key: str = ""
    model: str = "deepseek-chat"
    temperature: float = 0.2


class FeishuConfig(BaseModel):
    app_id: str = ""
    app_secret: str = ""
    admin_open_ids: List[str] = Field(default_factory=list)


class WatchdogConfig(BaseModel):
    enabled: bool = True
    interval_minutes: int = 5
    high_latency_threshold_ms: int = 600
    failure_threshold_count: int = 2
    daily_report_time: str = "09:00"


class WebConfig(BaseModel):
    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = 8080


class ProtectedService(BaseModel):
    """受保护服务声明 (Layer 1/2: Agent 切换节点时的智能护栏)

    例如 Gemini 不支持香港节点，可声明禁用关键词，
    Agent 推荐/切换节点时会主动避开并硬性拦截危险切换。
    """

    name: str = ""
    domains: List[str] = Field(default_factory=list)
    forbidden_node_keywords: List[str] = Field(default_factory=list)
    advice: str = ""


class Settings(BaseModel):
    nikki: NikkiConfig = Field(default_factory=NikkiConfig)
    openwrt: OpenWrtConfig = Field(default_factory=OpenWrtConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    feishu: FeishuConfig = Field(default_factory=FeishuConfig)
    watchdog: WatchdogConfig = Field(default_factory=WatchdogConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    protected_services: List[ProtectedService] = Field(default_factory=list)


_cached_settings: Optional[Settings] = None


def get_settings(config_path: Optional[str] = None) -> Settings:
    """加载并缓存配置文件设置"""
    global _cached_settings
    if _cached_settings is not None and config_path is None:
        return _cached_settings

    # 优先查找指定路径，否则按顺序查找 config.yaml、config.example.yaml
    search_paths = []
    if config_path:
        search_paths.append(Path(config_path))
    else:
        root_dir = Path(__file__).parent.parent
        search_paths.append(root_dir / "config" / "config.yaml")
        search_paths.append(root_dir / "config.yaml")
        search_paths.append(root_dir / "config" / "config.example.yaml")

    data = {}
    for path in search_paths:
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                loaded = yaml.safe_load(f)
                if loaded:
                    data = loaded
            break

    settings = Settings(**data)
    if config_path is None:
        _cached_settings = settings
    return settings
