"""CLI entry point for `ai-news` console script (pyproject [project.scripts]).

Mirrors run.py so that `ai-news <cmd>` and `python run.py <cmd>` share one
code path. run.py stays for zero-install usage; this module makes the pip
installed entry point work.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ai_news_pipeline.config import find_config_path, load_config
from ai_news_pipeline.pipeline import (
    collect_news,
    digest_news,
    generate_articles,
    learning_digest,
    plan_articles,
    process_news,
    publish_articles,
    run_daily,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="ai-news",
        description="AI 资讯自动化流水线 — 采集 / 处理 / 选题 / 生成 / 发布",
    )
    parser.add_argument("--config", type=Path, default=None, help="config.yaml 路径（默认自动查找）")
    parser.add_argument("--date", default=None, help="指定日期 YYYY-MM-DD（默认今天）")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("daily", help="执行完整日报流水线")
    sub.add_parser("collect", help="采集 AI 资讯")
    sub.add_parser("process", help="去重、评分、排序")
    sub.add_parser("digest", help="生成每日资讯日报并推送")
    sub.add_parser("learn", help="生成个人学习日报（本地，不推送）")
    sub.add_parser("plan", help="选取话题并生成 brief / research")
    gen_p = sub.add_parser("generate", help="LLM 生成 article.md")
    gen_p.add_argument("--dir", dest="article_dir", default=None, help="指定文章目录")
    pub_p = sub.add_parser("publish", help="发布到微信公众号草稿箱")
    pub_p.add_argument("--dir", dest="article_dir", default=None, help="指定文章目录")
    pub_p.add_argument("--account", default=None, help="覆盖默认账号")

    args = parser.parse_args()
    cfg_path = args.config or find_config_path()
    cfg = load_config(cfg_path)

    if args.command == "daily":
        run_daily(cfg, args.date)
    elif args.command == "digest":
        print(f"[Digest] output: {digest_news(cfg, args.date)}")
    elif args.command == "learn":
        print(f"[Learning] output: {learning_digest(cfg, args.date)}")
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