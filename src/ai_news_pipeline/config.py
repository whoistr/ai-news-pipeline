from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
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
        rel = self.config["wechat_publisher"]["skill_path"]
        return (self.repo_root / rel).resolve()

    @property
    def wechat_scripts_dir(self) -> Path:
        scripts = self.config["wechat_publisher"].get("scripts_dir", "scripts")
        return self.wechat_skill_path / scripts

    @property
    def default_account(self) -> str:
        return self.config["account"]["default"]

    @property
    def tz(self):
        """Local timezone for date-window calculations (default Asia/Shanghai).

        Uses zoneinfo for real IANA timezone support. Falls back to a fixed
        UTC+8 offset if zoneinfo data is unavailable (e.g. Windows without
        the tzdata package installed). Never raises.
        """
        tz_str = self.config.get("pipeline", {}).get("timezone", "Asia/Shanghai")
        try:
            return ZoneInfo(tz_str)
        except Exception:
            try:
                return ZoneInfo("Asia/Shanghai")
            except Exception:
                print("[Config] warning: zoneinfo data unavailable, using fixed UTC+8")
            return timezone(timedelta(hours=8))


    def get_categories(self, scope: str = "publish") -> list[dict[str, str]]:
        """Resolve which domain categories to include for a given scope.

        Reads scope.categories from config (a list of category names), then
        looks up each name in the top-level 'categories' section to get its
        news_key / papers_key / label / emoji.

        Args:
            scope: "publish" or "knowledge_base" -- which config section to
                   read the category selection from.

        Returns: list of dicts, each like:
            {"name": "ai", "news_key": "news", "papers_key": "papers",
             "label": "今日资讯", "papers_label": "学术文献",
             "emoji": "📰", "papers_emoji": "📚"}
            Ordered by the scope.categories list.

        Falls back to [ai, semiconductor] if not configured (backward compat).
        """
        scope_cfg = self.config.get(scope, {})
        cat_names = scope_cfg.get("categories", [])
        all_cats = self.config.get("categories", {})

        if not cat_names or not all_cats:
            # Default: AI + semiconductor (legacy behavior)
            cat_names = ["ai", "semiconductor"]
            all_cats = {
                "ai": {"news_key": "news", "papers_key": "papers",
                       "label": "今日资讯", "papers_label": "学术文献",
                       "emoji": "📰", "papers_emoji": "📚"},
                "semiconductor": {"news_key": "semiconductor_news",
                                  "papers_key": "semiconductor_papers",
                                  "label": "半导体资讯", "papers_label": "半导体文献",
                                  "emoji": "🔬", "papers_emoji": "📄"},
            }

        result = []
        for name in cat_names:
            cat = all_cats.get(name, {})
            if not cat:
                continue
            result.append({
                "name": name,
                "news_key": cat.get("news_key", f"{name}_news"),
                "papers_key": cat.get("papers_key", f"{name}_papers"),
                "label": cat.get("label", name),
                "papers_label": cat.get("papers_label", f"{name} 文献"),
                "emoji": cat.get("emoji", "📌"),
                "papers_emoji": cat.get("papers_emoji", "📄"),
            })
        return result

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
