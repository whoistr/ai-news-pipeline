#!/usr/bin/env python3
"""AI 资讯自动化流水线 CLI。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Force UTF-8 stdout/stderr so paths/logs with non-GBK chars (e.g. U+200C) don't crash on Windows.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

# 支持直接 python run.py 运行（无需 pip install -e .）
_SRC = Path(__file__).resolve().parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from ai_news_pipeline.config import find_config_path, load_config
from ai_news_pipeline.pipeline import (
    collect_news,
    digest_news,
    learning_digest,
    generate_articles,
    plan_articles,
    process_news,
    publish_articles,
    run_daily,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="AI 资讯自动化流水线 — 采集 / 处理 / 选题 / 生成 / 发布",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="config.yaml 路径（默认自动查找）",
    )
    parser.add_argument("--date", default=None, help="指定日期 YYYY-MM-DD（默认今天）")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("daily", help="执行完整日报流水线（采集→处理→生成日报→推送）")
    sub.add_parser("collect", help="采集 AI 资讯")
    sub.add_parser("digest", help="生成每日资讯日报并推送到草稿箱")
    sub.add_parser("learn", help="generate personal learning digest (local, no WeChat push)")
    sub.add_parser("process", help="去重、评分、排序")
    sub.add_parser("plan", help="选取话题并生成 brief.md / research.md")
    gen_p = sub.add_parser("generate", help="LLM 生成 article.md（需 generator.enabled）")
    gen_p.add_argument("--dir", dest="article_dir", default=None, help="指定文章目录")
    pub_p = sub.add_parser("publish", help="发布到微信公众号草稿箱")
    pub_p.add_argument("--dir", dest="article_dir", default=None, help="指定文章目录")
    pub_p.add_argument("--account", default=None, help="覆盖默认账号")

    args = parser.parse_args()
    cfg_path = args.config or find_config_path(Path(__file__).parent)
    cfg = load_config(cfg_path)

    if args.command == "daily":
        run_daily(cfg, args.date)
    elif args.command == "digest":
        out = digest_news(cfg, args.date)
        print(f"[Digest] output: {out}")
    elif args.command == "learn":
        out = learning_digest(cfg, args.date)
        print(f"[Learning] output: {out}")
    elif args.command == "collect":
        collect_news(cfg, args.date)
    elif args.command == "process":
        process_news(cfg, args.date)
    elif args.command == "plan":
        plan_articles(cfg, args.date)
    elif args.command == "generate":
        generate_articles(cfg, args.article_dir)
    elif args.command == "publish":
        if args.article_dir:
            from ai_news_pipeline.publishers.wechat import publish_to_wechat

            publish_to_wechat(Path(args.article_dir), cfg, account=args.account)
        else:
            publish_articles(cfg)


if __name__ == "__main__":
    main()
