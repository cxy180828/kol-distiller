"""配置管理模块"""

import os
import yaml
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional


# 项目根目录
ROOT_DIR = Path(__file__).parent.parent
KOLS_DIR = ROOT_DIR / "kols"
DISCUSSIONS_DIR = ROOT_DIR / "discussions"
LOGS_DIR = ROOT_DIR / "logs"


@dataclass
class LLMConfig:
    base_url: str
    api_key: str
    model: str
    temperature_classify: float = 0.1
    temperature_distill: float = 0.3
    temperature_discuss: float = 0.7


@dataclass
class TwitterConfig:
    auth_token: str
    ct0: str


@dataclass
class MarketDataConfig:
    source: str = "binance"
    base_url: str = "https://api.binance.com"


@dataclass
class ScheduleConfig:
    fetch_interval_hours: int = 6
    distill_day: int = 6  # 0=周一, 6=周日
    early_distill_threshold: int = 15


@dataclass
class DistillConfig:
    lookback_days: int = 30
    initial_fetch_count: int = 500
    batch_size: int = 10


@dataclass
class AppConfig:
    llm: LLMConfig
    twitter: TwitterConfig
    market_data: MarketDataConfig
    schedule: ScheduleConfig
    distill: DistillConfig


def load_config(config_path: Optional[str] = None) -> AppConfig:
    """加载配置文件"""
    if config_path is None:
        config_path = ROOT_DIR / "config.yaml"
    else:
        config_path = Path(config_path)

    if not config_path.exists():
        raise FileNotFoundError(
            f"配置文件不存在: {config_path}\n"
            f"请复制 config.example.yaml 为 config.yaml 并填入你的配置"
        )

    with open(config_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    # 验证必填字段
    _validate_config(raw)

    return AppConfig(
        llm=LLMConfig(**raw["llm"]),
        twitter=TwitterConfig(**raw["twitter"]),
        market_data=MarketDataConfig(**raw.get("market_data", {})),
        schedule=ScheduleConfig(**raw.get("schedule", {})),
        distill=DistillConfig(**raw.get("distill", {})),
    )


def _validate_config(raw: dict):
    """验证配置完整性"""
    errors = []

    # LLM配置
    llm = raw.get("llm", {})
    if not llm.get("base_url") or llm["base_url"] == "https://your-api-endpoint/v1":
        errors.append("llm.base_url 未配置")
    if not llm.get("api_key") or llm["api_key"] == "sk-xxxxx":
        errors.append("llm.api_key 未配置")
    if not llm.get("model"):
        errors.append("llm.model 未配置")

    # Twitter配置
    twitter = raw.get("twitter", {})
    if not twitter.get("auth_token"):
        errors.append("twitter.auth_token 未配置")
    if not twitter.get("ct0"):
        errors.append("twitter.ct0 未配置")

    if errors:
        raise ValueError(
            "配置文件有误:\n" + "\n".join(f"  - {e}" for e in errors)
        )


def ensure_dirs():
    """确保必要的目录存在"""
    KOLS_DIR.mkdir(exist_ok=True)
    DISCUSSIONS_DIR.mkdir(exist_ok=True)
    LOGS_DIR.mkdir(exist_ok=True)


def get_kol_dir(handle: str) -> Path:
    """获取某个KOL的数据目录"""
    # 去掉@前缀
    handle = handle.lstrip("@")
    kol_dir = KOLS_DIR / handle
    kol_dir.mkdir(exist_ok=True)
    (kol_dir / "history").mkdir(exist_ok=True)
    return kol_dir


def list_kols() -> list[str]:
    """列出所有已添加的KOL"""
    if not KOLS_DIR.exists():
        return []
    return [
        d.name for d in KOLS_DIR.iterdir()
        if d.is_dir() and (d / "meta.json").exists()
    ]
