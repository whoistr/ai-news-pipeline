from __future__ import annotations

import json
from pathlib import Path

import requests

from ai_news_pipeline.config import PipelineConfig


def generate_article_with_llm(article_path: Path, cfg: PipelineConfig) -> Path:
    gen_cfg = cfg.config.get("generator", {})
    if not gen_cfg.get("enabled"):
        raise RuntimeError("generator.enabled 为 false，请在 config.yaml 中启用并配置 llm")

    llm = cfg.config.get("llm", {})
    api_key = llm.get("api_key", "")
    base_url = llm.get("base_url", "https://api.openai.com/v1").rstrip("/")
    model = llm.get("model", "gpt-4o")
    temperature = float(llm.get("temperature", 0.7))
    target_words = int(gen_cfg.get("target_words", 3000))

    brief_path = article_path / "brief.md"
    research_path = article_path / "research.md"
    if not brief_path.is_file():
        raise FileNotFoundError(f"缺少 brief.md: {brief_path}")

    brief = brief_path.read_text(encoding="utf-8")
    research = research_path.read_text(encoding="utf-8") if research_path.is_file() else ""

    meta = {}
    meta_path = article_path / "meta.json"
    if meta_path.is_file():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))

    system_prompt = """你是微信公众号 AI 领域作者。根据 brief 和 research 写一篇 Markdown 文章。
要求：
- 第一个 # 标题 15-25 字
- 第二行用 > 写摘要引言
- 3-5 个小节，2500-4000 字
- 混用行内标记 ** == ++ %% && !! @@ ^^
- 禁止 AI 套话（首先其次、综上所述、赋能、闭环等）
- 每节末尾留 ![配图描述](placeholder) 占位
- 只输出 Markdown，不要解释"""

    user_prompt = f"""## brief.md\n\n{brief}\n\n## research.md\n\n{research}\n\n目标字数约 {target_words}。"""

    resp = requests.post(
        f"{base_url}/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json", "User-Agent": "Mozilla/5.0",
        },
        json={
            "model": model,
            "temperature": temperature,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        },
        timeout=120,
    )
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"]

    out_path = article_path / "article.md"
    out_path.write_text(content.strip() + "\n", encoding="utf-8")
    return out_path
