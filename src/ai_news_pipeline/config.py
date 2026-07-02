from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class PipelineConfig:
    root: Path
    config: dict[str, Any] = field(default_factory=dict)

    @property
    def articles_dir(self) -> Path:
        rel = self.config["pipeline"]["articles_dir"]
        return (self.root / rel).resolve()

    @property
    def raw_dir(self) -> Path:
        rel = self.config["pipeline"]["raw_dir"]
        return (self.root / rel).resolve()

    @property
    def processed_dir(self) -> Path:
        rel = self.config["pipeline"]["processed_dir"]
        return (self.root / rel).resolve()

    @property
    def repo_root(self) -> Path:
        return self.root.parent.resolve()

    @property
    def wechat_skill_path(self) -> Path:
        # 1. Env var override (highest priority) for standalone skill installs
        env_path = os.environ.get("WECHAT_PUBLISHER_PATH")
        if env_path and Path(env_path).is_dir():
            return Path(env_path).resolve()
        configured = self.config["wechat_publisher"]["skill_path"]
        # 2. Absolute path in config
        if os.path.isabs(configured):
            return Path(configured).resolve()
        # 3. Relative: resolve against skills dir (sibling layout).
        #    config root = <skills>/ai-news-pipeline/, so root.parent = <skills>/
        return (self.repo_root / configured).resolve()

    @property
    def wechat_scripts_dir(self) -> Path:
        scripts = self.config["wechat_publisher"].get("scripts_dir", "scripts")
        return self.wechat_skill_path / scripts

    @property
    def default_account(self) -> str:
        return self.config["account"]["default"]

    @property
    def tz(self) -> timezone:
        """Local timezone for date-window calculations (default UTC+8 / Asia-Shanghai)."""
        # Simple fixed-offset: config timezone string parsed to hours.
        tz_str = self.config.get("pipeline", {}).get("timezone", "Asia/Shanghai")
        offset_hours = 8 if "Shanghai" in tz_str or "China" in tz_str else 0
        return timezone(timedelta(hours=offset_hours))

    def date_window(self, date_str: str) -> tuple[datetime, datetime]:
        """Return [start, end) UTC datetimes covering the full calendar day of date_str in local tz.

        Collects everything published during date_str 00:00:00 ~ 23:59:59 local time.
        """
        local_date = datetime.strptime(date_str, "%Y-%m-%d")
        start_local = local_date.replace(tzinfo=self.tz)
        end_local = (local_date + timedelta(days=1)).replace(tzinfo=self.tz)
        return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def find_config_path(start: Path | None = None) -> Path:
    """Locate config.yaml: env > cwd > pipeline root."""
    env_path = os.environ.get("AI_NEWS_CONFIG")
    if env_path:
        path = Path(env_path)
        if path.is_file():
            return path.resolve()
        raise FileNotFoundError(f"AI_NEWS_CONFIG 指向的文件不存在: {env_path}")

    candidates = [
        Path.cwd() / "config.yaml",
        Path.cwd() / "ai-news-pipeline" / "config.yaml",
    ]
    if start:
        candidates.insert(0, start / "config.yaml")

    pipeline_root = Path(__file__).resolve().parents[2]
    candidates.append(pipeline_root / "config.yaml")

    for path in candidates:
        if path.is_file():
            return path.resolve()

    example = pipeline_root / "config.yaml.example"
    raise FileNotFoundError(
        f"未找到 config.yaml。请复制 {example} 为 config.yaml 并填入配置。"
    )


def load_config(config_path: Path | None = None) -> PipelineConfig:
    path = config_path or find_config_path()
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    root = path.parent.resolve()
    return PipelineConfig(root=root, config=data)
