"""News key-point extractor: structured analysis for knowledge cards."""

from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from typing import Any

import requests

from ai_news_pipeline.config import PipelineConfig


_NEWS_EXTRACT_PROMPT = """你是资深科技分析师。请对以下 {count} 条 AI/科技资讯进行结构化要点提取。

对每条资讯，输出一个 JSON 对象，用 JSON 数组包裹：
[
  {{
    "idx": 资讯编号,
    "event_summary": "一句话概括核心事件（谁做了什么，结果如何）",
    "companies_products": "涉及的公司和产品（逗号分隔）",
    "industry_impact": "对行业的影响（1-2句，具体说改变什么）",
    "background": "相关背景信息",
    "topic": "主题分类（如：大模型/Agent/芯片/开源/监管/应用落地）"
  }}
]

下面是待分析资讯：
{items}"""


def _clean_summary(text: str, max_len: int = 500) -> str:
    text = re.sub(r"<[^>]+>", "", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    if "Abstract:" in text:
        text = text.split("Abstract:", 1)[1].strip()
    return text[:max_len]


def _build_items_block(items):
    blocks = []
    for i, item in enumerate(items, 1):
        summary = _clean_summary(item.get("summary", ""), 400)
        blocks.append(
            f"--- {i} ---\n"
            f"标题: {item.get('title', '')}\n"
            f"来源: {item.get('source', '')}\n"
            f"摘要: {summary or '(无)'}"
        )
    return "\n\n".join(blocks)


def _repair_json(text: str) -> str:
    s = text.strip()
    try:
        json.loads(s)
        return s
    except json.JSONDecodeError:
        pass
    last = s.rfind("}")
    if last != -1:
        s = s[: last + 1]
    opens = s.count("[") - s.count("]")
    braces = s.count("{") - s.count("}")
    s = re.sub(r",\s*$", "", s)
    return s + ("}" * max(braces, 0)) + ("]" * max(opens, 0))


def _call_extract_batch(batch, cfg: PipelineConfig):
    llm = cfg.config.get("llm", {})
    api_key = llm.get("api_key", "")
    if not api_key or api_key == "sk-...":
        return []
    base_url = llm.get("base_url", "https://api.openai.com/v1").rstrip("/")
    model = cfg.config.get("models", {}).get("enhance") or llm.get("model", "gpt-4o")
    prompt = _NEWS_EXTRACT_PROMPT.format(count=len(batch), items=_build_items_block(batch))

    for attempt in range(3):
        try:
            resp = requests.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
                json={"model": model, "temperature": 0.3, "max_tokens": 600 * len(batch),
                      "messages": [{"role": "system", "content": "你是科技分析师，只输出JSON数组。"},
                                   {"role": "user", "content": prompt}]},
                timeout=120,
            )
            resp.raise_for_status()
            msg = resp.json()["choices"][0]["message"]
            raw = msg.get("content") or msg.get("reasoning_content") or ""
            raw = re.sub(r"```(?:json)?\s*", "", raw).strip()
            m = re.search(r"\[.*\]", raw, re.DOTALL)
            if not m:
                continue
            try:
                rows = json.loads(m.group(0))
            except json.JSONDecodeError:
                rows = json.loads(_repair_json(m.group(0)))
            out = []
            for r in rows:
                if not isinstance(r, dict):
                    continue
                idx = int(str(r.get("idx", 0)).strip()) - 1
                if 0 <= idx < len(batch):
                    item = batch[idx]
                    out.append({
                        "url": item.get("url", ""),
                        "title": item.get("title", ""),
                        "event_summary": str(r.get("event_summary", "")),
                        "companies_products": str(r.get("companies_products", "")),
                        "industry_impact": str(r.get("industry_impact", "")),
                        "background": str(r.get("background", "")),
                        "topic": str(r.get("topic", "")),
                    })
            return out
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else 0
            if status == 429:
                wait = (2 ** attempt) * 5
                print(f"  [NewsExtract] attempt {attempt+1}: 429 -> wait {wait}s")
                time.sleep(wait)
                continue
            time.sleep(2)
        except Exception:
            time.sleep(1)
    return []


_print_lock = Lock()


def extract_news_insights(items, cfg: PipelineConfig):
    """Extract structured insights from news items via LLM.

    Returns {url: {event_summary, companies_products, industry_impact, background, topic}}.
    """
    llm = cfg.config.get("llm", {})
    if not llm.get("api_key") or llm["api_key"] == "sk-...":
        return {}
    if not items:
        return {}

    batch_size = 8
    concurrency = 3
    batches = [items[i:i + batch_size] for i in range(0, len(items), batch_size)]
    print(f"  [NewsExtract] {len(items)} items, {len(batches)} batches")

    results = {}
    done = 0
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futures = {ex.submit(_call_extract_batch, b, cfg): b for b in batches}
        for f in as_completed(futures):
            done += 1
            for row in f.result():
                results[row["url"]] = {
                    "event_summary": row.get("event_summary", ""),
                    "companies_products": row.get("companies_products", ""),
                    "industry_impact": row.get("industry_impact", ""),
                    "background": row.get("background", ""),
                    "topic": row.get("topic", ""),
                }
            with _print_lock:
                print(f"    [{done}/{len(batches)}] extracted")
    return results


def heuristic_news_insight(item):
    """Fallback when LLM is unavailable."""
    title = item.get("title", "")
    summary = _clean_summary(item.get("summary", ""), 200)
    return {
        "event_summary": title,
        "companies_products": "",
        "industry_impact": "",
        "background": summary[:150] if summary else "",
        "topic": "",
    }
