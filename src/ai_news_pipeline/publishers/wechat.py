from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from ai_news_pipeline.config import PipelineConfig


def _extract_title(md_text: str) -> str:
    lines = md_text.splitlines()
    for line in lines:
        if line.startswith("# "):
            return line[2:].strip()
    # Fallback: first non-empty line
    for line in lines:
        s = line.strip()
        if s and not s.startswith(">") and not s.startswith("---"):
            return s[:64]
    return "未命名"


def _extract_digest(md_text: str) -> str:
    in_front = False
    for line in md_text.splitlines():
        if line.strip() == "---":
            in_front = not in_front
            continue
        if in_front:
            continue
        if line.startswith("# "):
            continue
        if line.startswith("> "):
            text = line[2:].strip()
            text = re.sub(r"\*\*|==|\+\+|%%|&&|!!|@@|\^\^", "", text)
            return text[:120]
    return ""


def publish_to_wechat(article_dir: Path, cfg: PipelineConfig, account: str | None = None) -> dict:
    account = account or cfg.default_account
    publish_cfg = cfg.config.get("publish", {})
    scripts = cfg.wechat_scripts_dir
    publish_py = scripts / "publish.py"

    if not publish_py.is_file():
        raise FileNotFoundError(f"wechat-publisher not found: {publish_py}")

    article_md = article_dir / "article.md"
    if not article_md.is_file():
        raise FileNotFoundError(f"Missing article.md: {article_md}")

    cover = article_dir / "cover.jpg"
    if not cover.is_file():
        cover = article_dir / "cover.png"

    md_text = article_md.read_text(encoding="utf-8")
    title = _extract_title(md_text)
    digest = _extract_digest(md_text)

    cmd = [
        sys.executable, str(publish_py),
        "--account", account,
        "--input", str(article_md),
        "--title", title,
    ]
    if digest:
        cmd.extend(["--digest", digest])
    if cover.is_file():
        cmd.extend(["--cover", str(cover)])

    threshold = publish_cfg.get("ai_score_threshold")
    if threshold is not None:
        cmd.extend(["--ai-score-threshold", str(threshold)])
    if publish_cfg.get("skip_ai_score"):
        cmd.append("--skip-ai-score")

    print(f"[Publish] {' '.join(str(c) for c in cmd)}")
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    # encoding/errors target the PARENT's decoding of child output (text=True
    # otherwise uses locale.getpreferredencoding() = GBK on Chinese Windows).
    result = subprocess.run(
        cmd, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
        cwd=str(scripts.parent), env=env, check=False,
    )

    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)

    if result.returncode != 0:
        raise RuntimeError(f"Publish failed (exit {result.returncode})")

    return {"title": title, "account": account, "article_dir": str(article_dir)}
