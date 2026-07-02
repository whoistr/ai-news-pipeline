"""LLM three-gate scorer for news + papers.

Applies importance / timeliness / actionability judgement to the candidates that
survive heuristic粗筛. The heuristic ranker is good at structural filtering
(cross-list, author count, anti-hype) but cannot judge *importance* -- e.g. a
flagship "Qwen Technical Report" scores low because it has no benchmark %, yet it
matters far more than a niche benchmark paper that scores high.

Flow inside process_items:
  heuristic score -> take top ~15 per category -> this LLM scorer -> additive
  bonus -> re-sort by final score -> min_score filter -> cap

The blend is ADDITIVE on purpose: heuristic_base + llm_bonus. A multiplicative
blend would let a low feed_weight (arxiv 0.25-0.35) permanently cap the ceiling,
so genuinely important arxiv items could never cross the threshold. Additive lets
importance lift an item past the line regardless of its source weight.

Graceful degradation: if LLM is disabled / API fails / returns nothing, callers
fall back to pure heuristic scores (current behavior), so this never blocks the
daily run.
"""

from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from typing import Any

import requests

from ai_news_pipeline.config import PipelineConfig
from ai_news_pipeline.models import NewsItem


# Importance weight dominates: it is the gate that distinguishes "matters" from
# "looks tidy". Timeliness is light because the heuristic recency decay already
# handles freshness; here we only down-weight stale-on-arrival concepts.
GATE_WEIGHTS = {
    "importance": 2.5,
    "actionability": 0.6,
    "timeliness": 0.3,
}


def llm_bonus(importance: float, timeliness: float, actionability: float) -> float:
    """Additive bonus from three-gate scores (each 1-5, neutral at 3).

    Range about [-3.0, +3.0]. Importance is weighted highest: a 5-star-important
    item can clear the min_score bar even from a low heuristic base.
    """
    return round(
        (importance - 3) * GATE_WEIGHTS["importance"]
        + (actionability - 3) * GATE_WEIGHTS["actionability"]
        + (timeliness - 3) * GATE_WEIGHTS["timeliness"],
        3,
    )


_PROMPT_TEMPLATE = """??AI??????/???????{count}??????????????????

????(??1-5?,3?=??):
1. importance ???:??????/????????/??????/????????5=?????(????????????????),1=?????
2. timeliness ???:AI????3-6?????????????5=?????,1=?????????
3. actionability ????:??????????(????/GitHub Star/?????/????/API)?5=??+??+????,1=????????

????????:????(??/?????????)+????(??/????/????)?
??????:????(????+????+???????)+????+?????

???????????(??????,???????????):
    - cn_title: ????(15-25?,????/???,???????????)????????,????????!
- rating: ????1-5(5=??,4=????,3=????,2=??,1=????)
    - why_worth: ??????(?????????,2-3?,??????,50??)
    - core_points: ??????(3-4???,??10-18?,?????)

???JSON??,??markdown??,????:
[
  {{"idx": 1, "importance": 5, "timeliness": 4, "actionability": 3, "reason": "<=20?",
    "cn_title": "????", "rating": 4, "why_worth": "??????",
    "core_points": ["??1", "??2", "??3"]}},
  ...
]

????:
{items}"""


def _clean(s: str, n: int = 500) -> str:
    s = re.sub(r"<[^>]+>", "", s or "")
    s = re.sub(r"\s+", " ", s).strip()
    if "Abstract:" in s:
        s = s.split("Abstract:", 1)[1].strip()
    return s[:n]


def _repair_json(text: str) -> str:
    """Best-effort repair of truncated JSON array (same idea as daily_digest)."""
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


def _build_items_block(items: list[NewsItem]) -> str:
    blocks = []
    for i, it in enumerate(items, 1):
        summary = _clean(it.summary, 300)
        blocks.append(
            f"--- {i} ---\n"
            f"标题: {it.title}\n"
            f"来源: {it.source}\n"
            f"摘要: {summary or '(无)'}"
        )
    return "\n\n".join(blocks)


def _call_batch(batch: list[NewsItem], cfg: PipelineConfig) -> list[dict[str, Any]]:
    llm = cfg.config.get("llm", {})
    api_key = llm.get("api_key", "")
    if not api_key or api_key == "sk-...":
        return []
    base_url = llm.get("base_url", "https://api.openai.com/v1").rstrip("/")
    model = cfg.config.get("models", {}).get("scorer") or llm.get("model", "gpt-4o")
    prompt = _PROMPT_TEMPLATE.format(count=len(batch), items=_build_items_block(batch))

    for attempt in range(5):
        try:
            resp = requests.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
                # Reasoning models (e.g. GLM) emit long chain-of-thought; budget
                # generously so the final JSON isn't truncated mid-output.
                json={"model": model, "temperature": 0.3, "max_tokens": 1200 * len(batch),
                      "messages": [{"role": "system", "content": "??AI?????????????(cn_title/why_worth/core_points)???????,????????????JSON??,??markdown?"},
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
                    cp = r.get("core_points") or []
                    if isinstance(cp, str):
                        cp = [l.strip() for l in cp.split("\n") if l.strip()]
                    out.append({
                        "url": batch[idx].url,
                        "importance": max(1, min(5, int(r.get("importance", 3)))),
                        "timeliness": max(1, min(5, int(r.get("timeliness", 3)))),
                        "actionability": max(1, min(5, int(r.get("actionability", 3)))),
                        "reason": str(r.get("reason", ""))[:40],
                        "cn_title": str(r.get("cn_title", ""))[:60],
                        "rating": max(1, min(5, int(r.get("rating", 3)))),
                        "why_worth": str(r.get("why_worth", ""))[:120],
                        "core_points": cp,
                    })
            return out
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else 0
            if status == 429:
                wait = (2 ** attempt) * 5  # 5s, 10s, 20s
                print(f"  [LLMScore] batch attempt {attempt+1}: 429 rate limit -> wait {wait}s")
                time.sleep(wait)
                continue
            print(f"  [LLMScore] batch attempt {attempt+1}: HTTP {status}")
            time.sleep(2)
        except (json.JSONDecodeError, KeyError, ValueError, TypeError) as e:
            print(f"  [LLMScore] batch attempt {attempt+1}: {e}")
            time.sleep(1)
        except requests.RequestException as e:
            print(f"  [LLMScore] batch attempt {attempt+1}: {e}")
            time.sleep(2)
            print(f"  [LLMScore] batch attempt {attempt+1}: {e}")
            time.sleep(1)
    return []


_print_lock = Lock()


def score_candidates_with_llm(
    items: list[NewsItem],
    cfg: PipelineConfig,
    label: str = "",
) -> dict[str, dict[str, Any]]:
    """Score a list of candidate items through the three gates.

    Returns {url: {importance, timeliness, actionability, reason, bonus}}.
    Empty dict on any failure (caller falls back to pure heuristic).
    """
    scorer_cfg = cfg.config.get("processor", {}).get("llm_scorer", {})
    if not scorer_cfg.get("enabled", False):
        return {}
    llm = cfg.config.get("llm", {})
    if not llm.get("api_key") or llm["api_key"] == "sk-...":
        return {}
    if len(items) < 2:
        return {}

    batch_size = int(scorer_cfg.get("batch_size", 10))
    concurrency = int(scorer_cfg.get("concurrency", 3))
    batches = [items[i:i + batch_size] for i in range(0, len(items), batch_size)]
    tag = f"[{label}]" if label else "[LLMScore]"
    print(f"  {tag} {len(items)} candidates, {len(batches)} batches x {batch_size}, concurrency={concurrency}")

    results: dict[str, dict[str, Any]] = {}
    done = 0
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futures = {ex.submit(_call_batch, b, cfg): b for b in batches}
        for f in as_completed(futures):
            done += 1
            for row in f.result():
                bonus = llm_bonus(row["importance"], row["timeliness"], row["actionability"])
                results[row["url"]] = {
                    "importance": row["importance"],
                    "timeliness": row["timeliness"],
                    "actionability": row["actionability"],
                    "reason": row["reason"],
                    "bonus": bonus,
                    "cn_title": row.get("cn_title", ""),
                    "rating": row.get("rating", 3),
                    "why_worth": row.get("why_worth", ""),
                    "core_points": row.get("core_points") or [],
                }
            with _print_lock:
                rated = [r.get("importance") for r in f.result()]
                print(f"    [{done}/{len(batches)}] scored importance={rated}")
    return results
