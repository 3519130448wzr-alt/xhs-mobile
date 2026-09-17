"""Validated configuration, with secrets only read from environment variables."""

import os
import tomllib
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


def validate_target(target: int) -> int:
    """One strict boundary for newly requested per-keyword collection targets."""
    if type(target) is not int or not 1 <= target <= 500:
        raise ValueError("每个关键词目标须为 1 至 500 的整数")
    return target


class Policy(BaseModel):
    """Execution policy persisted on each task; loading never changes its budgets."""

    model_config = ConfigDict(extra="forbid")
    action_interval: float = Field(default=3, ge=0.1)
    page_timeout: float = Field(default=30, ge=1, le=300)
    max_retries: int = Field(default=2, ge=0, le=10)
    cooldown_seconds: int = Field(default=1800, ge=1)
    max_detail_visits: int = Field(default=100, ge=1)
    max_list_swipes: int = Field(default=50, ge=1)
    max_no_progress: int = Field(default=3, ge=1)


class TaskLimits(BaseModel):
    """Creation caps are separate from the execution policy of existing tasks."""

    model_config = ConfigDict(extra="forbid")
    max_detail_visits: int = Field(default=1500, ge=100, le=1500, strict=True)
    max_list_swipes: int = Field(default=750, ge=50, le=750, strict=True)


class DeviceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    serial_env: str
    session_ref: str = Field(min_length=1)
    app_package: str = ""
    cloud_console_url: str | None = None

    @field_validator("cloud_console_url")
    @classmethod
    def valid_console_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or any(char.isspace() or ord(char) < 32 for char in value)
        ):
            raise ValueError("cloud_console_url must be an HTTPS URL without credentials")
        return value

    @field_validator("serial_env")
    @classmethod
    def valid_environment_name(cls, value: str) -> str:
        import re

        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", value):
            raise ValueError("serial_env must be an uppercase environment variable name")
        return value

    def serial(self) -> str:
        serial = os.environ.get(self.serial_env, "").strip()
        if not serial:
            raise ValueError(f"请设置设备环境变量 {self.serial_env}，不能默认操作第一台设备")
        return serial


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    state_dir: Path = Path("var")
    profile_path: Path = Path("profiles/template.toml")
    adb_path: str = "adb"
    database_url_env: str = "XHS_DATABASE_URL"
    devices: dict[str, DeviceConfig] = Field(default_factory=dict)
    policy: Policy = Field(default_factory=Policy)
    task_limits: TaskLimits = Field(default_factory=TaskLimits)

    def new_task_policy(self, target: int) -> Policy:
        """Build a new task's bounded policy; never call this when resuming a task.

        Old configuration commonly has a 100/50 policy. Those legacy defaults
        must not reject a larger new target, or alter existing persisted tasks.
        All timing, cooldown and retry fields remain the configured values.
        """
        target = validate_target(target)
        details = max(100, 3 * target)
        swipes = max(50, (details + 1) // 2)
        if details > self.task_limits.max_detail_visits:
            raise ValueError("目标所需详情预算超过 task_limits.max_detail_visits")
        if swipes > self.task_limits.max_list_swipes:
            raise ValueError("目标所需列表预算超过 task_limits.max_list_swipes")
        return self.policy.model_copy(update={
            "max_detail_visits": details, "max_list_swipes": swipes,
        })

    def device(self, device_id: str) -> DeviceConfig:
        if device_id not in self.devices:
            raise ValueError(f"未配置设备 {device_id!r}；可用标识：{', '.join(self.devices)}")
        return self.devices[device_id]

    def database_url(self) -> str:
        value = os.environ.get(self.database_url_env, "")
        if not value.startswith("postgresql+psycopg://"):
            raise ValueError(
                f"请设置 {self.database_url_env}=postgresql+psycopg://...；"
                "生产入口只支持 PostgreSQL"
            )
        return value


def load_settings(path: Path) -> Settings:
    with path.open("rb") as stream:
        settings = Settings.model_validate(tomllib.load(stream))
    base = path.resolve().parent
    for name in ("state_dir", "profile_path"):
        value = getattr(settings, name)
        if not value.is_absolute():
            setattr(settings, name, base / value)
    return settings
