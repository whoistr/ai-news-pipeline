from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from ai_news_pipeline.collectors.hn import HackerNewsCollector
from ai_news_pipeline.collectors.rss import RssCollector
from ai_news_pipeline.config import PipelineConfig
from ai_news_pipeline.generators.brief import create_brief
from ai_news_pipeline.generators.daily_digest import generate_daily_digest
from ai_news_pipeline.generators.llm import generate_article_with_llm
from ai_news_pipeline.models import NewsItem, utc_now_iso
from ai_news_pipeline.processors.ranker import load_processed, process_items, process_items_dual, save_processed
from ai_news_pipeline.analyzers.paper_reviewer import review_arxiv_papers
from ai_news_pipeline.publishers.wechat import publish_to_wechat


def collect_news(cfg: PipelineConfig, date_str: str | None = None) -> list[NewsItem]:
    date_str = date_str or datetime.now().strftime("%Y-%m-%d")
    print(f"[Collect] 开始采集 {date_str} 全天资讯...")

    collectors = [
        HackerNewsCollector(cfg, date_str),
        RssCollector(cfg, date_str),
    ]
    all_items: list[NewsItem] = []
    for collector in collectors:
        batch = collector.collect()
        print(f"  - {collector.name}: {len(batch)} 条")
        all_items.extend(batch)

    cfg.raw_dir.mkdir(parents=True, exist_ok=True)
    raw_path = cfg.raw_dir / f"{date_str}.json"
    raw_path.write_text(
        json.dumps(
            {
                "date": date_str,
                "collected_at": utc_now_iso(),
                "count": len(all_items),
                "items": [i.to_dict() for i in all_items],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"[Collect] 原始数据已保存: {raw_path} ({len(all_items)} 条)")

    # ArXiv paper review (LLM-powered star rating)
    arxiv_items = [i for i in all_items if "arxiv" in (i.source or "").lower()]
    if arxiv_items:
        review_cfg = cfg.config.get("collectors", {}).get("arxiv_review", {})
        if review_cfg.get("enabled", False):
            print(f"\n[Review] 开始审阅 {len(arxiv_items)} 篇 arXiv 论文...")
            reviews = review_arxiv_papers(arxiv_items, cfg)
            review_path = cfg.raw_dir / f"{date_str}_review.json"
            review_path.write_text(
                json.dumps({
                    "date": date_str,
                    "reviewed_at": utc_now_iso(),
                    "total_arxiv": len(arxiv_items),
                    "rated_above_threshold": len(reviews),
                    "reviews": reviews,
                }, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            print(f"[Review] 审阅结果已保存: {review_path} ({len(reviews)} 篇 >= {review_cfg.get('min_rating', 3)}星)")

    return all_items


def process_news(cfg: PipelineConfig, date_str: str | None = None) -> list[NewsItem]:
    date_str = date_str or datetime.now().strftime("%Y-%m-%d")
    raw_path = cfg.raw_dir / f"{date_str}.json"

    if raw_path.is_file():
        data = json.loads(raw_path.read_text(encoding="utf-8"))
        items = [NewsItem.from_dict(i) for i in data.get("items", [])]
    else:
        print(f"[Process] 未找到 {raw_path}，先执行采集...")
        items = collect_news(cfg, date_str)

    result = process_items_dual(items, cfg)
    out_path = save_processed(result, cfg, date_str)
    # Summary uses the publish set (the primary/legacy view)
    pub = result.get("publish", {})
    news = pub.get("news", [])
    papers = pub.get("papers", [])
    learn = result.get("learning", {})
    learn_total = sum(len(learn.get(k, [])) for k in ("news", "papers", "semiconductor_news", "semiconductor_papers"))
    useful_news = sum(1 for i in news if (i.raw or {}).get("useful"))
    useful_papers = sum(1 for i in papers if (i.raw or {}).get("useful"))
    print(f"[Process] done: {len(news)} pub-news + {len(papers)} pub-papers + {learn_total} learning items -> {out_path}")
    if news:
        print(f"  --- NEWS top 3 ---")
        for i, item in enumerate(news[:3], 1):
            print(f"  {i}. [{item.score:.1f}] {item.title[:60]}...")
    if papers:
        print(f"  --- PAPERS top 3 ---")
        for i, item in enumerate(papers[:3], 1):
            print(f"  {i}. [{item.score:.1f}] {item.title[:60]}...")
    flat = news + papers
    if flat:
        print(f"  --- Combined top 5 ---")
        for i, item in enumerate(flat[:5], 1):
            u = " [useful]" if (item.raw or {}).get("useful") else ""
            print(f"  {i}. [{item.score:.1f}]{u} {item.title[:60]}...")
    return flat


def _ensure_processed_fresh(cfg: PipelineConfig, date_str: str) -> None:
    """Verify processed.json exists and matches the expected date.

    If process was interrupted (timeout/kill), the file may be stale (yesterday's
    data) or missing. This re-runs process to guarantee digest reads complete data.
    """
    import json as _json
    processed_path = cfg.processed_dir / f"{date_str}.json"
    if not processed_path.is_file():
        print(f"[Digest] No processed data, running process first...")
        process_news(cfg, date_str)
        return
    # Check date stamp matches
    try:
        data = _json.loads(processed_path.read_text(encoding="utf-8"))
        file_date = data.get("date", "")
        if file_date != date_str:
            print(f"[Digest] processed.json date mismatch ({file_date} != {date_str}), re-running process...")
            process_news(cfg, date_str)
    except Exception:
        print(f"[Digest] processed.json corrupted, re-running process...")
        process_news(cfg, date_str)


def digest_news(cfg: PipelineConfig, date_str: str | None = None) -> Path:
    """Generate the PUBLISH digest and auto-push to WeChat (existing behavior)."""
    date_str = date_str or datetime.now().strftime("%Y-%m-%d")
    _ensure_processed_fresh(cfg, date_str)
    out_dir = generate_daily_digest(cfg, date_str, purpose="publish")
    publish_cfg = cfg.config.get("publish", {})
    if publish_cfg.get("auto_publish_digest", False):
        from ai_news_pipeline.publishers.wechat import publish_to_wechat
        result = publish_to_wechat(out_dir, cfg)
        print(f"[Digest] Published to drafts: {result['title']}")
    return out_dir


def learning_digest(cfg: PipelineConfig, date_str: str | None = None) -> Path:
    """Generate the LEARNING digest to a local personal/ dir."""
    date_str = date_str or datetime.now().strftime("%Y-%m-%d")
    _ensure_processed_fresh(cfg, date_str)
    out_dir = generate_daily_digest(cfg, date_str, purpose="learning")
    print(f"[Learning] Generated (local only, not published): {out_dir}")
    return out_dir


def plan_articles(cfg: PipelineConfig, date_str: str | None = None) -> list[str]:
    date_str = date_str or datetime.now().strftime("%Y-%m-%d")
    schedule = cfg.config.get("schedule", {})
    max_topics = int(schedule.get("max_topics", 3))

    try:
        ranked = load_processed(cfg, date_str)
    except FileNotFoundError:
        ranked = process_news(cfg, date_str)

    if not ranked:
        print("[Plan] 没有符合条件的资讯，跳过选题。")
        return []

    account = cfg.default_account
    dirs: list[str] = []
    for item in ranked[:max_topics]:
        related = [r for r in ranked if r.url != item.url][:5]
        out_dir = create_brief(item, cfg, account=account, related=related)
        dirs.append(str(out_dir))
        print(f"[Plan] 已创建 brief: {out_dir}")

    return dirs


def generate_articles(cfg: PipelineConfig, article_dir_path: str | None = None) -> list[str]:
    gen_cfg = cfg.config.get("generator", {})
    if not gen_cfg.get("enabled"):
        print("[Generate] generator.enabled=false，跳过 LLM 生成。")
        print("  请使用 Cursor + wechat-publisher skill 完成写作与配图。")
        return []

    if article_dir_path:
        dirs = [Path(article_dir_path)]
    else:
        account = cfg.default_account
        date_str = datetime.now().strftime("%Y-%m-%d")
        base = cfg.articles_dir / account
        dirs = sorted(base.glob(f"{date_str}-*")) if base.is_dir() else []

    generated: list[str] = []
    for d in dirs:
        if not d.is_dir():
            continue
        try:
            out = generate_article_with_llm(d, cfg)
            generated.append(str(out))
            print(f"[Generate] 已生成: {out}")
        except Exception as exc:  # noqa: BLE001
            print(f"[Generate] 失败 ({d}): {exc}")
    return generated


def publish_articles(cfg: PipelineConfig, article_dir_path: str | None = None) -> list[dict]:
    publish_cfg = cfg.config.get("publish", {})
    if not publish_cfg.get("auto_publish") and not article_dir_path:
        print("[Publish] auto_publish=false，跳过自动发布。")
        print("  手动发布: python run.py publish --dir <文章目录>")
        return []

    if article_dir_path:
        dirs = [Path(article_dir_path)]
    else:
        account = cfg.default_account
        date_str = datetime.now().strftime("%Y-%m-%d")
        base = cfg.articles_dir / account
        publish_count = int(cfg.config.get("schedule", {}).get("publish_count", 1))
        dirs = sorted(base.glob(f"{date_str}-*"))[:publish_count] if base.is_dir() else []

    results: list[dict] = []
    for d in dirs:
        if not (d / "article.md").is_file():
            print(f"[Publish] 跳过（无 article.md）: {d}")
            continue
        try:
            result = publish_to_wechat(d, cfg)
            results.append(result)
            print(f"[Publish] 草稿已创建: {result['title']}")
        except Exception as exc:  # noqa: BLE001
            print(f"[Publish] 失败 ({d}): {exc}")
    return results


def run_daily(cfg: PipelineConfig, date_str: str | None = None) -> None:
    date_str = date_str or datetime.now().strftime("%Y-%m-%d")
    print(f"\n{'='*50}")
    print(f" AI 资讯日报流水线 — {date_str}")
    print(f"{'='*50}\n")

    collect_news(cfg, date_str)
    process_news(cfg, date_str)
    dirs = plan_articles(cfg, date_str)

    if cfg.config.get("generator", {}).get("enabled"):
        generate_articles(cfg)

    if cfg.config.get("publish", {}).get("auto_publish"):
        publish_articles(cfg)

    print(f"\n{'='*50}")
    print(" 流水线完成")
    if dirs:
        print("\n下一步（推荐）:")
        print("  1. 在 Cursor 中对以下目录运行 wechat-publisher skill:")
        for d in dirs:
            print(f"     → {d}")
        print("  2. 完成配图与 article.md 后:")
        print("     python run.py publish --dir <文章目录>")
    print(f"{'='*50}\n")
