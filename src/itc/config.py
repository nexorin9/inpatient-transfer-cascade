"""配置面：全部凭据与地址从环境变量（或 .env）注入，无默认密钥、无硬编码。

业务层只通过 ``get_settings()`` 读取配置；测试可注入自定义环境。
"""
from __future__ import annotations

import os
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_PACKAGE_RULES_DIR = Path(__file__).resolve().parent / "rules" / "catalogs"


class ConfigurationError(RuntimeError):
    """配置缺失或非法：启动自检必须明确报错，不静默降级。"""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    his_base_url: str = Field(default="", description="院内 HIS / ESB 基地址")
    his_api_token: str = Field(default="", description="HIS 出站访问令牌", repr=False)
    llm_base_url: str = Field(default="", description="OpenAI-compatible 模型服务基地址")
    llm_api_key: str = Field(default="", description="模型服务访问令牌", repr=False)
    llm_model: str = Field(default="", description="判定长嘱归属所用模型名")
    llm_timeout_seconds: float = Field(default=30.0, gt=0, description="单次模型调用超时（秒）")
    itc_state_db: str = Field(default="~/.itc/state.sqlite3", description="本地 SQLite 状态库路径")
    itc_rules_dir: str = Field(default="", description="院内维护的规则目录；留空用包内默认目录")
    itc_log_dir: str = Field(default="", description="结构化日志目录；留空写 logs/")

    @property
    def state_db_path(self) -> Path:
        return Path(os.path.expanduser(self.itc_state_db))

    @property
    def rules_dir(self) -> Path:
        return Path(self.itc_rules_dir) if self.itc_rules_dir else _PACKAGE_RULES_DIR

    @property
    def his_ready(self) -> bool:
        return bool(self.his_base_url.strip() and self.his_api_token.strip())

    @property
    def llm_ready(self) -> bool:
        return bool(
            self.llm_base_url.strip()
            and self.llm_api_key.strip()
            and self.llm_model.strip()
        )


def get_settings(**overrides) -> Settings:
    """构造配置。overrides 用于测试与 CLI 显式传参，不改变产品默认路径。"""
    return Settings(**overrides)


def require_his_credentials(settings: Settings | None = None) -> Settings:
    """HIS 出站前自检：凭据缺失明确报错，绝不静默降级到本地替身。"""
    cfg = settings or get_settings()
    missing = [
        name
        for name, value in (
            ("HIS_BASE_URL", cfg.his_base_url),
            ("HIS_API_TOKEN", cfg.his_api_token),
        )
        if not value or not value.strip()
    ]
    if missing:
        raise ConfigurationError(
            "HIS 出站凭据缺失：" + ", ".join(missing) + "；请在环境变量或 .env 中配置后重试"
        )
    return cfg


def doctor_report(settings: Settings | None = None) -> dict:
    """配置齐备性报告（不做网络请求；连通性由 CLI doctor 单独探测）。"""
    cfg = settings or get_settings()
    return {
        "HIS_BASE_URL": "已配置" if cfg.his_base_url.strip() else "未配置",
        "HIS_API_TOKEN": "已配置" if cfg.his_api_token.strip() else "未配置",
        "LLM_BASE_URL": "已配置" if cfg.llm_base_url.strip() else "未配置（模型判定将降级）",
        "LLM_API_KEY": "已配置" if cfg.llm_api_key.strip() else "未配置（模型判定将降级）",
        "LLM_MODEL": cfg.llm_model or "未配置（模型判定将降级）",
        "LLM_TIMEOUT_SECONDS": str(cfg.llm_timeout_seconds),
        "ITC_STATE_DB": str(cfg.state_db_path),
        "his_ready": cfg.his_ready,
        "llm_ready": cfg.llm_ready,
    }
