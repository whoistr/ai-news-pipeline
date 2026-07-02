from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from ai_news_pipeline.config import PipelineConfig
from ai_news_pipeline.models import NewsItem, utc_now_iso


def slugify(text: str, max_len: int = 48) -> str:
    text = text.lower()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text.strip())
    text = re.sub(r"-+", "-", text)
    return text[:max_len].strip("-") or "ai-news"


def article_dir(cfg: PipelineConfig, account: str, slug: str, date_str: str | None = None) -> Path:
    date_str = date_str or datetime.now().strftime("%Y-%m-%d")
    path = cfg.articles_dir / account / f"{date_str}-{slug}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def create_brief(
    item: NewsItem,
    cfg: PipelineConfig,
    account: str | None = None,
    related: list[NewsItem] | None = None,
) -> Path:
    account = account or cfg.default_account
    slug = slugify(item.title)
    out_dir = article_dir(cfg, account, slug)
    gen_cfg = cfg.config.get("generator", {})

    keywords = _extract_keywords(item, related or [])
    brief_path = out_dir / "brief.md"
    brief_path.write_text(
        _render_brief(item, account, gen_cfg, keywords, related or []),
        encoding="utf-8",
    )

    research_path = out_dir / "research.md"
    research_path.write_text(_render_research(item, related or []), encoding="utf-8")

    meta_path = out_dir / "meta.json"
    meta_path.write_text(
        json.dumps(
            {
                "topic": item.title,
                "slug": slug,
                "account": account,
                "source_url": item.url,
                "source": item.source,
                "score": item.score,
                "created_at": utc_now_iso(),
                "article_structure": gen_cfg.get("article_structure", "timeline-news"),
                "opening_hook": gen_cfg.get("opening_hook", "hard-number"),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return out_dir


def _extract_keywords(item: NewsItem, related: list[NewsItem]) -> list[str]:
    words = re.findall(r"[A-Za-z]{3,}", item.title)
    keywords = [w.lower() for w in words[:5]]
    for r in related[:2]:
        keywords.extend(re.findall(r"[A-Za-z]{4,}", r.title)[:2])
    seen: set[str] = set()
    out: list[str] = []
    for k in keywords:
        kl = k.lower()
        if kl not in seen:
            seen.add(kl)
            out.append(kl)
    return out[:5] or ["ai", "llm"]


def _render_brief(
    item: NewsItem,
    account: str,
    gen_cfg: dict,
    keywords: list[str],
    related: list[NewsItem],
) -> str:
    structure = gen_cfg.get("article_structure", "timeline-news")
    hook = gen_cfg.get("opening_hook", "hard-number")
    related_lines = "\n".join(f"- [{r.title}]({r.url}) ({r.source})" for r in related[:5])

    safe_title = item.title.replace('"', "'")
    return f"""---
topic: "{safe_title}"
account: {account}
article_structure: {structure}
opening_hook: {hook}
keywords: [{", ".join(keywords)}]
source_url: {item.url}
auto_generated: true
---

# 选题摘要

- **核心话题**: {item.title}
- **来源**: {item.source}
- **原文链接**: {item.url}
- **评分**: {item.score:.1f}
- **目标账号**: {account}

## 关键词

{", ".join(keywords)}

## 相关素材（同日候选）

{related_lines or "（无）"}

## 写作要求

1. 按 `{structure}` 结构撰写，开头使用 `{hook}` 钩子
2. 2500-4000 字，3-5 个小节
3. 每 500 字混用至少 4 种行内标记（** == ++ %% && !! @@ ^^）
4. 遵守 wechat-publisher SKILL.md 的反 AI 检测清单
5. 输出到同目录 `article.md`

## 下一步

- **Cursor Agent 模式（推荐）**: 对本目录执行 wechat-publisher skill 完整 7 阶段流程
- **命令行 LLM**: `python run.py generate --dir {account}/...`
"""


def _render_research(item: NewsItem, related: list[NewsItem]) -> str:
    lines = [
        f"# 调研素材 — {item.title}",
        "",
        f"采集时间: {utc_now_iso()}",
        "",
        "## 权威层",
        "",
        f"### [{item.title}]({item.url})",
        f"- 来源: {item.source}",
        f"- 发布时间: {item.published_at or '未知'}",
        f"- 摘要: {item.summary or '（无）'}",
        "",
    ]
    if related:
        lines.extend(["## 同日相关", ""])
        for r in related:
            lines.append(f"- [{r.title}]({r.url}) — {r.source} (score={r.score:.1f})")
    return "\n".join(lines) + "\n"
