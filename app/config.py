"""Typed, validated configuration (config.yaml) and the agent registry (agents.yaml)."""

import hashlib
import ipaddress
import os
import re
import secrets
import tempfile
from pathlib import Path
from typing import Literal, Optional, Union
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from croniter import croniter
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from . import settings

PathLike = Union[str, Path]

AGENT_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
EMAIL_ADDRESS = re.compile(r"^[^@\s<>]+@[^@\s<>]+$")
AGENTS_HEADER = (
    "# Agents allowed to report to the dashboard (API keys are stored only as SHA-256 hashes).\n"
    "# Add one with: python -m app agent add NAME\n"
    "# Edits take effect without a restart. Comments are not preserved when the CLI rewrites this file.\n"
)


class ConfigError(Exception):
    """Raised for a missing or invalid configuration file, with a readable message."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _check_http_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("must be an http:// or https:// URL")
    return value


class Badge(_Model):
    label: str = Field(min_length=1)
    image_url: str
    link: Optional[str] = None

    @field_validator("image_url", "link")
    @classmethod
    def _check_urls(cls, value: Optional[str]) -> Optional[str]:
        return value if value is None else _check_http_url(value)


class ServerConfig(_Model):
    host: str = "0.0.0.0"
    port: int = Field(5002, ge=1, le=65535)
    workers: int = Field(2, ge=1, le=16)
    threads: int = Field(2, ge=1, le=32)


class Drive(_Model):
    path: str = Field(min_length=1)
    label: str = Field(min_length=1)


class S3Bucket(_Model):
    bucket: str = Field(min_length=1)
    label: str = Field(min_length=1)
    # cloudwatch: free daily size per storage class. list: scans the bucket, sizes per top-level prefix.
    method: Literal["cloudwatch", "list"] = "cloudwatch"
    region: Optional[str] = None


class StorageConfig(_Model):
    drives: list[Drive] = Field(default_factory=list)
    s3_buckets: list[S3Bucket] = Field(default_factory=list)
    aws_region: str = "us-east-1"
    drive_interval_minutes: int = Field(15, ge=1)
    s3_interval_hours: int = Field(12, ge=1)

    @model_validator(mode="after")
    def _unique_labels(self) -> "StorageConfig":
        for kind, items in (("drive", self.drives), ("s3 bucket", self.s3_buckets)):
            labels = [item.label for item in items]
            if len(set(labels)) != len(labels):
                raise ValueError(f"{kind} labels must be unique")
        return self


class DigestConfig(_Model):
    enabled: bool = True
    schedule: str = "0 8 * * *"

    @field_validator("schedule")
    @classmethod
    def _cron(cls, value: str) -> str:
        if len(value.split()) != 5 or not croniter.is_valid(value):
            raise ValueError("must be a valid 5-field cron expression")
        return value


class OfflineAlertConfig(_Model):
    enabled: bool = True
    max_alerts: int = Field(1, ge=0)
    repeat_interval_minutes: int = Field(60, ge=1)
    send_recovery: bool = True


def _check_address(value: str) -> str:
    if not EMAIL_ADDRESS.match(value):
        raise ValueError(f"'{value}' is not an email address")
    return value


class EmailConfig(_Model):
    smtp_server: str = ""
    smtp_port: int = Field(587, ge=1, le=65535)
    use_tls: bool = True
    from_addr: str = ""
    to_addrs: list[str] = Field(default_factory=list)
    digest: DigestConfig = Field(default_factory=DigestConfig)
    offline_alert: OfflineAlertConfig = Field(default_factory=OfflineAlertConfig)

    @field_validator("to_addrs")
    @classmethod
    def _check_recipients(cls, value: list) -> list:
        return [_check_address(item) for item in value]

    @field_validator("from_addr")
    @classmethod
    def _check_sender(cls, value: str) -> str:
        return _check_address(value) if value else value


class RetentionConfig(_Model):
    runs_days: int = Field(30, ge=1)
    rollup_days: int = Field(365, ge=1)
    status_days: int = Field(365, ge=1)
    storage_days: int = Field(365, ge=1)
    log_lines_per_log: int = Field(5000, ge=100)
    log_days: int = Field(30, ge=1)


class Config(_Model):
    timezone: str = "UTC"
    server: ServerConfig = Field(default_factory=ServerConfig)
    badges: list[Badge] = Field(default_factory=list)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    email: EmailConfig = Field(default_factory=EmailConfig)
    retention: RetentionConfig = Field(default_factory=RetentionConfig)

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone '{value}'") from exc
        return value


class AgentConfig(_Model):
    name: str = Field(pattern=AGENT_NAME_PATTERN)
    key_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    enabled: bool = True
    grace_minutes: int = Field(60, ge=1, le=10080)
    notes: str = ""
    allowed_ip: Optional[str] = None
    offline_alerts: bool = True

    @field_validator("allowed_ip")
    @classmethod
    def _valid_ip(cls, value: Optional[str]) -> Optional[str]:
        if value is not None:
            ipaddress.ip_address(value)
        return value


class AgentsFile(_Model):
    agents: list[AgentConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique(self) -> "AgentsFile":
        names = [a.name for a in self.agents]
        hashes = [a.key_hash for a in self.agents]
        if len(set(names)) != len(names):
            raise ValueError("agent names must be unique")
        if len(set(hashes)) != len(hashes):
            raise ValueError("agent key hashes must be unique")
        return self


def _format_errors(path: Path, exc: ValidationError) -> str:
    lines = [f"{path}: invalid configuration"]
    for err in exc.errors():
        where = ".".join(str(part) for part in err["loc"]) or "(top level)"
        lines.append(f"  {where}: {err['msg']}")
    return "\n".join(lines)


def _read_yaml(path: Path) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: not valid YAML: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    return data


def load_config(path: Optional[PathLike] = None) -> Config:
    """Load and validate config.yaml."""
    path = Path(path or settings.CONFIG_PATH)
    if not path.exists():
        raise ConfigError(f"{path} not found; copy config/config.example.yaml to config/config.yaml")
    try:
        return Config.model_validate(_read_yaml(path))
    except ValidationError as exc:
        raise ConfigError(_format_errors(path, exc)) from exc


def load_agents(path: Optional[PathLike] = None) -> list[AgentConfig]:
    """Load the agent registry; a missing file means no agents are registered."""
    path = Path(path or settings.AGENTS_PATH)
    if not path.exists():
        return []
    try:
        return AgentsFile.model_validate(_read_yaml(path)).agents
    except ValidationError as exc:
        raise ConfigError(_format_errors(path, exc)) from exc


def generate_key() -> str:
    return secrets.token_hex(20)


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _write_agents(agents: list[AgentConfig], path: Path) -> None:
    body = AGENTS_HEADER + yaml.safe_dump(
        {"agents": [a.model_dump(exclude_none=True) for a in agents]},
        sort_keys=False, default_flow_style=False,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".agents-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(body)
        os.chmod(tmp_name, 0o640)
        os.replace(tmp_name, path)
    except OSError:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def add_agent(name: str, grace_minutes: int = 60, notes: str = "",
              allowed_ip: Optional[str] = None, path: Optional[PathLike] = None) -> str:
    """Register an agent and return its API key; only the key's hash is stored."""
    path = Path(path or settings.AGENTS_PATH)
    agents = load_agents(path)
    key = generate_key()
    try:
        entry = AgentConfig(name=name, key_hash=hash_key(key), grace_minutes=grace_minutes,
                            notes=notes, allowed_ip=allowed_ip)
    except ValidationError as exc:
        raise ConfigError(_format_errors(path, exc)) from exc
    if any(a.name == name for a in agents):
        raise ConfigError(f"agent '{name}' already exists")
    _write_agents(agents + [entry], path)
    return key


def set_agent_enabled(name: str, enabled: bool, path: Optional[PathLike] = None) -> None:
    """Enable or disable a registered agent without touching its key."""
    path = Path(path or settings.AGENTS_PATH)
    agents = load_agents(path)
    for agent in agents:
        if agent.name == name:
            agent.enabled = enabled
            _write_agents(agents, path)
            return
    raise ConfigError(f"agent '{name}' not found")
