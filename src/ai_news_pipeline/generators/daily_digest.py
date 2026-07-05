"""Daily digest - detailed card format per user spec."""

from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import requests

from ai_news_pipeline.config import PipelineConfig


def _clean_text(text: str, max_len: int = 100) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    text = text.replace("&quot;", '"').replace("&#39;", "'")
    if "Abstract:" in text:
        text = text.split("Abstract:", 1)[1].strip()
    text = re.sub(r"arXiv:\S+\s+Announce Type:\s*\S+\s*", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text or text.lower().startswith("comment"):
        return ""
    if len(text) > max_len:
        text = text[:max_len].rsplit(" ", 1)[0] + "..."
    return text


_RATING_MAP = {
    5: ("⭐⭐⭐⭐⭐", "必读"),
    4: ("⭐⭐⭐⭐", "强烈推荐"),
    3: ("⭐⭐⭐", "值得一看"),
    2: ("⭐⭐", "可选"),
    1: ("⭐", "了解即可"),
}

_CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩"


def _repair_json(text: str) -> str:
    """Best-effort repair of a possibly-truncated JSON array so json.loads works.

    Balances unclosed brackets/braces and drops a trailing dangling object so we
    can salvage the cards the LLM already finished before hitting max_tokens.
    """
    s = text.strip()
    # If it already parses, leave it alone
    try:
        json.loads(s)
        return s
    except json.JSONDecodeError:
        pass
    # Cut back to the last complete '}' (end of a card object)
    last_obj = s.rfind("}")
    if last_obj != -1:
        s = s[: last_obj + 1]
    # Balance braces/brackets
    opens = s.count("[") - s.count("]")
    braces = s.count("{") - s.count("}")
    # strip a dangling trailing comma
    s = re.sub(r",\s*$", "", s)
    return s + ("}" * max(braces, 0)) + ("]" * max(opens, 0))


def _rating_label(rating: int) -> str:
    """Map a 1-5 rating to a 【stars label】 header prefix."""
    stars, label = _RATING_MAP.get(max(1, min(5, int(rating))), _RATING_MAP[3])
    return f"【{stars} {label}】"


def _format_title(title: str) -> str:
    """Add spaces around embedded ASCII tokens so terms read cleanly in Chinese.

    e.g. "开源Markdown优先的Agent记忆系统" -> "开源 Markdown 优先的 Agent 记忆系统"
    Preserves existing spacing and punctuation.
    """
    # Insert space between CJK and ASCII alnum runs, but not after colons/punct
    t = re.sub(r"([\u4e00-\u9fff])((?:[A-Za-z0-9][A-Za-z0-9.+_-]*)|#+)", r"\1 \2", title)
    t = re.sub(r"((?:[A-Za-z0-9][A-Za-z0-9.+_-]*)|#+)([\u4e00-\u9fff])", r"\1 \2", t)
    # Collapse any double spaces introduced
    t = re.sub(r"  +", " ", t)
    return t


def _is_legacy_arxiv(item: dict) -> bool:
    """Classify a legacy single-bucket semiconductor item as paper vs news.

    Used by the backward-compat reader: old processed files merged semiconductor
    news + arxiv papers into one 'semiconductor' bucket. We re-split by checking
    the arxiv tag so the semiconductor-papers section isn't silently empty.
    """
    src = (item.get("source") or "").lower()
    tags = item.get("tags") or []
    return "arxiv" in src or "arxiv" in tags


def _heuristic_card(item: dict) -> dict:
    """Generate card fields without LLM. Fallback when LLM enhance fails.

    For English content (esp. X/Twitter posts), clean up the raw title/summary
    rather than dumping raw English, so the fallback card stays readable.
    """
    title = item.get("title", "")
    summary = _clean_text(item.get("summary", ""), 200)
    source = item.get("source", "")

    # Strip X/Twitter retweet/reply prefixes that leak from nitter RSS
    title = re.sub(r"^RT by @\S+:\s*", "", title)
    title = re.sub(r"^R to @\S+:\s*", "", title)

    cn_title = title[:60]
    rating = 3  # default

    if summary:
        summary = re.sub(r"^RT by @\S+:\s*", "", summary)
        summary = re.sub(r"^R to @\S+:\s*", "", summary)
        why_worth = summary[:100]
        parts = re.split(r"[.?;?!\u3002\uff01\uff1f]", summary)
        core_points = [p.strip()[:50] for p in parts if len(p.strip()) > 8][:4]
        if len(core_points) < 2:
            core_points = [summary[:60]]
    else:
        why_worth = f"来自 {source} 的资讯"
        core_points = []

    return {
        "cn_title": cn_title,
        "rating": rating,
        "why_worth": why_worth,
        "core_points": core_points,
    }


# ── LLM Enhancement ─────────────────────────────────────────────

_ENHANCE_PROMPT = """你是 AI 日报编辑。为以下每条内容生成移动端卡片文案，返回 JSON 数组：

[{
  "idx": 编号,
  "cn_title": "中文标题，格式：主体名：一句话定位。规则：必须含产品名/技术名；英文术语两侧留空格（如「开源 Markdown 优先的 Agent 记忆系统」）；名词短语要完整别截断（写「记忆系统」而非「记忆」）；15-25字",
  "rating": 评分整数1-5（5=必读 4=强烈推荐 3=值得一看 2=可选 1=了解即可，默认给3-4）,
  "why_worth": "为什么值得看：用口语化中文、可用反问句开头，讲清核心价值和解决的痛点，2-3句、80字内，要有信息量不要空话",
  "core_points": ["要点1", "要点2", "要点3", "要点4"]
}]

要求：
- why_worth 口语化、接地气，能勾起好奇心，避免AI腔
- core_points 每条都是干货要点，10-18字，3-5条，用陈述句
- 只输出 JSON 数组，不要 markdown 包裹，不要解释

__ITEMS__"""

def _enhance_batch(batch_text: str, batch_size: int, cfg: PipelineConfig) -> list[dict]:
    llm = cfg.config.get("llm", {})
    if not llm.get("api_key") or llm["api_key"] == "sk-...":
        return []
    base_url = llm.get("base_url", "https://api.openai.com/v1").rstrip("/")
    model = cfg.config.get("models", {}).get("enhance") or llm.get("model", "gpt-4o")
    max_attempts = 5
    for attempt in range(max_attempts):
        try:
            resp = requests.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {llm['api_key']}", "Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
                # Reasoning models (e.g. GLM) emit chain-of-thought before the JSON,
                # so budget generously to avoid mid-output truncation.
                json={"model": model, "temperature": 0.5, "max_tokens": 1600 * batch_size,
                      "messages": [{"role": "system", "content": "你是 AI 编辑。只输出 JSON 数组，不要 markdown 包裹。"},
                                   {"role": "user", "content": _ENHANCE_PROMPT.replace("__ITEMS__", batch_text)}]},
                timeout=120,
            )
            resp.raise_for_status()
            msg = resp.json()["choices"][0]["message"]
            raw = msg.get("content") or msg.get("reasoning_content") or ""
            # Try to find JSON array, handle common LLM formatting issues
            # Strip markdown code fences (```json ... ```) that reasoning models add
            raw = re.sub(r"```(?:json)?\s*", "", raw).strip()
            m = re.search(r"\[.*", raw, re.DOTALL)  # greedy from first [ to end (handles truncation)
            if m:
                try:
                    parsed = json.loads(_repair_json(m.group(0)))
                    # Ensure idx is int
                    for r in parsed:
                        if isinstance(r, dict):
                            r["idx"] = int(str(r.get("idx", 0)).strip())
                    return parsed
                except (json.JSONDecodeError, KeyError, ValueError):
                    # Try cleaning: remove trailing commas, fix quote issues
                    cleaned = re.sub(r",\s*}", "}", m.group(0))
                    cleaned = re.sub(r",\s*\]", "]", cleaned)
                    try:
                        parsed = json.loads(_repair_json(cleaned))
                        for r in parsed:
                            if isinstance(r, dict):
                                r["idx"] = int(str(r.get("idx", 0)).strip())
                        return parsed
                    except Exception:
                        pass
        except requests.HTTPError as e:
            # 429 rate limit: exponential backoff (GLM enforces per-minute quota;
            # the three-gate scorer in process() already consumed much of it, so the
            # digest enhance pass frequently hits 429). Wait longer each retry.
            status = e.response.status_code if e.response is not None else 0
            wait = (2 ** attempt) * 5 if status == 429 else 2  # 5s,10s,20s,40s,80s
            print(f"  [Enhance] batch attempt {attempt+1}: {e} -> wait {wait}s")
            time.sleep(wait)
        except Exception as e:
            print(f"  [Enhance] batch attempt {attempt+1}: {e}")
            time.sleep(2)
    return []


def _enhance_with_llm(items: list[dict], cfg: PipelineConfig) -> dict[str, dict]:
    batch_size, concurrency = 8, 3
    # Build per-batch slices so the LLM always sees local numbering 1..N and we
    # map back by batch position (robust against the model renumbering globally).
    slices = [items[j:j + batch_size] for j in range(0, len(items), batch_size)]
    batches = []
    for batch_items in slices:
        blocks = []
        for i, item in enumerate(batch_items, 1):
            blocks.append(f"--- {i} ---\n标题: {item['title']}\n摘要: {_clean_text(item.get('summary',''), 300) or '(无)'}")
        batches.append((batch_items, "\n\n".join(blocks)))
    print(f"  [Enhance] LLM: {len(items)} items, {len(batches)} batches x {batch_size}")
    results = {}
    done = 0
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futures = {ex.submit(_enhance_batch, text, len(bi), cfg): (bi, text) for (bi, text) in batches}
        for f in as_completed(futures):
            done += 1
            batch_items, _ = futures[f]
            for r in f.result():
                idx = int(r.get("idx", 0))
                # Local idx 1..N within this batch → position in batch_items
                pos = idx - 1
                if not (0 <= pos < len(batch_items)):
                    continue
                url = batch_items[pos]["url"]
                if url:
                    cp = r.get("core_points", [])
                    if isinstance(cp, str):
                        cp = [l.strip() for l in cp.split("\n") if l.strip()]
                    results[url] = {
                        "cn_title": r.get("cn_title", ""),
                        "rating": r.get("rating", 3),
                        "why_worth": r.get("why_worth", ""),
                        "core_points": cp,
                    }
            print(f"    [{done}/{len(batches)}] enhanced")
    return results


# ── Main ────────────────────────────────────────────────────────

def _pick_headline_with_llm(candidates: list[dict], cfg: PipelineConfig) -> dict | None:
    """Ask the LLM to name the single most important item of the day.

    The scoring ranks "looks high-quality", not "matters most". This is a small,
    final judgement layer: given the top candidates (already scored), the LLM
    picks the one a human editor would lead with, plus a one-line reason. Returns
    None on any failure (LLM disabled, API error, unparseable answer) so the
    digest falls back to score order silently.
    """
    llm = cfg.config.get("llm", {})
    # Honor the same LLM toggle the rest of the digest uses, and require a key.
    use_llm = cfg.config.get("publish", {}).get("digest_use_llm", True)
    if not use_llm or not llm.get("api_key") or llm["api_key"] == "sk-...":
        return None
    if len(candidates) < 2:
        return None

    base_url = llm.get("base_url", "https://api.openai.com/v1").rstrip("/")
    model = cfg.config.get("models", {}).get("headline") or llm.get("model", "gpt-4o")
    blocks = []
    for i, it in enumerate(candidates, 1):
        title = it.get("title", "")[:120]
        src = it.get("source", "")
        blocks.append(f"{i}. [{src}] {title}")
    prompt = (
        "你是AI日报主编。下面是今日已筛选的候选条目(按算法评分排序)。\n"
        "请选出\"如果今天只能发一条,你会发哪条\" —— 即对读者最重要、最值得关注的一条。\n"
        "只输出一行JSON: {\"idx\": 编号, \"reason\": \"一句话理由(<=30字)\"}\n\n"
        + "\n".join(blocks)
    )
    try:
        resp = requests.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {llm['api_key']}", "Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
            json={"model": model, "temperature": 0.2, "max_tokens": 4000,
                  "messages": [{"role": "system", "content": "你是编辑,只输出JSON,不要解释。"},
                               {"role": "user", "content": prompt}]},
            timeout=60,
        )
        resp.raise_for_status()
        msg = resp.json()["choices"][0]["message"]
        raw = msg.get("content") or msg.get("reasoning_content") or ""
        raw = re.sub(r"```(?:json)?\s*", "", raw).strip()
        # Find the JSON object. Prefer a balanced {...}; fall back to repair.
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            return None
        try:
            pick = json.loads(m.group(0))
        except json.JSONDecodeError:
            pick = json.loads(_repair_json(m.group(0)))
        idx = int(pick.get("idx", 0)) - 1
        if 0 <= idx < len(candidates):
            return {"item": candidates[idx], "reason": str(pick.get("reason", ""))[:60]}
    except Exception as e:
        print(f"  [Headline] LLM pick failed: {e}")
    return None

def generate_daily_digest(cfg: PipelineConfig, date_str: str | None = None, purpose: str = "publish") -> Path:
    date_str = date_str or datetime.now().strftime("%Y-%m-%d")
    account = cfg.default_account
    processed_path = cfg.processed_dir / f"{date_str}.json"

    if not processed_path.is_file():
        raise FileNotFoundError("No processed data. Run `python run.py process` first.")

    processed = json.loads(processed_path.read_text(encoding="utf-8"))
    # Dual-mode: read from the purpose section if present, else top-level (legacy)
    if purpose in processed:
        section = processed[purpose]
    else:
        section = processed

    # Config-driven category reading: iterate the categories configured for
    # the publish scope (or learning scope). Each category has news_key +
    # papers_key that map to keys in processed.json.
    scope = "knowledge_base" if purpose == "learning" else "publish"
    cat_list = cfg.get_categories(scope)

    # Build a dict {category_name: {"news": [...], "papers": [...]}} for later
    # use by write_section and headline/enhance logic.
    cat_data: dict[str, dict[str, list]] = {}
    for cat in cat_list:
        news_items_c = section.get(cat["news_key"], {}).get("items", [])
        papers_items_c = section.get(cat["papers_key"], {}).get("items", [])
        # Legacy compat: old format stored semiconductor as a single merged
        # bucket. Split by arxiv tag so semi-papers isn't silently empty.
        if cat["name"] == "semiconductor" and not news_items_c and not papers_items_c:
            legacy = section.get("semiconductor", {}).get("items", [])
            news_items_c = [i for i in legacy if not _is_legacy_arxiv(i)]
            papers_items_c = [i for i in legacy if _is_legacy_arxiv(i)]
        cat_data[cat["name"]] = {"news": news_items_c, "papers": papers_items_c, "cat": cat}

    # Flat lists for enhance/headline
    all_items_flat: list[dict] = []
    for cd in cat_data.values():
        all_items_flat.extend(cd["news"])
        all_items_flat.extend(cd["papers"])

    use_llm = cfg.config.get("publish", {}).get("digest_use_llm", True)
    if use_llm:
        # Cooldown: the three-gate scorer + headline picker already hit the GLM
        # API hard. Wait for the per-minute rate-limit window to reset before
        # starting card enhancement, otherwise every batch gets 429'd.
        cooldown = int(cfg.config.get("processor", {}).get("llm_scorer", {}).get("enhance_cooldown_seconds", 60))
        print(f"[Digest] Cooling down {cooldown}s before card enhancement (API rate-limit reset)...")
        time.sleep(cooldown)
    enhanced = _enhance_with_llm(all_items_flat, cfg) if use_llm else {}

    weekday = datetime.strptime(date_str, "%Y-%m-%d").strftime("%A")
    wcn = {"Monday":"周一","Tuesday":"周二","Wednesday":"周三","Thursday":"周四","Friday":"周五","Saturday":"周六","Sunday":"周日"}.get(weekday,"")
    # Build summary line from configured categories
    cat_counts = []
    for cd in cat_data.values():
        nc = len(cd["news"])
        pc = len(cd["papers"])
        cat = cd["cat"]
        if nc:
            cat_counts.append(f"{nc} 条 {cat['label']}")
        if pc:
            cat_counts.append(f"{pc} 篇 {cat['papers_label']}")
    summary = " + ".join(cat_counts) if cat_counts else "0 条"
    lines = [f"AI 每日资讯 | {date_str} {wcn}", "",
            f"> 今日精选 {summary}", ""]

    # ★ 今日大事件: LLM picks the single most important item of the day.
    # Falls back silently (no headline card) if LLM is off or the pick fails.
    head_candidates = all_items_flat[:8]
    headline = None
    if use_llm and head_candidates:
        headline = _pick_headline_with_llm(head_candidates, cfg)
    if headline:
        h_item = headline["item"]
        # Dedup: remove the headline item from category data so it doesn't
        # appear twice (once as 今日大事件, once inside its category section).
        h_url = h_item.get("url", "")
        for cd in cat_data.values():
            cd["news"] = [i for i in cd["news"] if i.get("url") != h_url]
            cd["papers"] = [i for i in cd["papers"] if i.get("url") != h_url]
        h_url = h_item.get("url", "")
        h_gates = (h_item.get("raw") or {}).get("llm_gates") or {}
        if h_gates.get("cn_title"):
            h_enh = h_gates
        else:
            h_enh = enhanced.get(h_url, {}) or _heuristic_card(h_item)
        lines.append("## ★ 今日大事件")
        lines.append("")
        lines.append(f"**{_format_title(h_enh.get('cn_title', '') or h_item.get('title', '')[:60])}**")
        lines.append("")
        if headline.get("reason"):
            lines.append(f"> {headline['reason']}")
            lines.append("")
        why = h_enh.get("why_worth", "")
        if why:
            lines.append(why)
            lines.append("")
        lines.append(f"原文链接: [{h_item.get('url','')}]({h_item.get('url','')})")
        lines.append("")
        lines.append("---")
        lines.append("")
    def write_section(emoji: str, label: str, items: list[dict]):
        if not items:
            return
        lines.append(f"## {emoji} {label}")
        lines.append("")
        for item in items:
            url = item.get("url", "")
            # Prefer card data already generated by the three-gate scorer (stored
            # in llm_gates during process phase). Only fall back to the separate
            # enhance pass or heuristic if llm_gates has no card fields.
            gates = (item.get("raw") or {}).get("llm_gates") or {}
            if gates.get("cn_title"):
                # Three-gate scorer already produced a Chinese card ? use it
                enh = {
                    "cn_title": gates.get("cn_title", ""),
                    "rating": gates.get("rating", 3),
                    "why_worth": gates.get("why_worth", ""),
                    "core_points": gates.get("core_points") or [],
                }
            else:
                enh = enhanced.get(url, {})
            h = _heuristic_card(item)

            cn_title = enh.get("cn_title", "") or h["cn_title"]
            rating = enh.get("rating", 0) or h["rating"]
            why_worth = enh.get("why_worth", "") or h["why_worth"]
            core_points = enh.get("core_points") or h["core_points"]

            lines.append(f"### {_rating_label(rating)} {_format_title(cn_title)}")
            lines.append("")
            lines.append("📌 **为什么值得看**")
            lines.append("")
            lines.append(why_worth)
            lines.append("")
            lines.append("🔍 **核心内容**")
            lines.append("")
            for i, pt in enumerate(core_points):
                num = _CIRCLED[i] if i < len(_CIRCLED) else f"{i+1}."
                lines.append(f"{num} {_format_title(pt)}")
            lines.append("")
            lines.append("🔗 原文链接")
            lines.append("")
            lines.append(f"👉 [{url}]({url})")
            lines.append("")
            lines.append("---")
            lines.append("")

    # Write sections from configured categories
    for cd in cat_data.values():
        cat = cd["cat"]
        if cd["news"]:
            write_section(cat["emoji"], cat["label"], cd["news"])
        if cd["papers"]:
            write_section(cat["papers_emoji"], cat["papers_label"], cd["papers"])

    lines.append(f"*AI 每日资讯 | {date_str} | 自动生成*")

    # publish -> main account dir (auto-pushes to WeChat)
    # learning -> personal dir (stays local, never published)
    out_account = account if purpose == "publish" else "personal"
    out_dir = cfg.articles_dir / out_account / f"{date_str}-daily"
    out_dir.mkdir(parents=True, exist_ok=True)
    article_md = out_dir / "article.md"
    article_md.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # Cover
    cover = out_dir / "cover.jpg"
    if not cover.is_file():
        try:
            from PIL import Image, ImageDraw, ImageFont
            img = Image.new("RGB", (900, 500), color=(20, 30, 55))
            draw = ImageDraw.Draw(img)
            for j in range(500):
                draw.line([(0, j), (900, j)], fill=(min(20+j//10,85), min(30+j//15,125), min(55+j//8,185)))
            try:
                ft, fs = ImageFont.truetype("arial.ttf", 56), ImageFont.truetype("arial.ttf", 26)
            except Exception:
                ft = fs = ImageFont.load_default()
            draw.text((50, 180), "AI Daily", fill=(255,255,255), font=ft)
            draw.text((50, 260), "AI Daily Digest", fill=(180,200,220), font=fs)
            draw.text((50, 310), date_str, fill=(120,150,180), font=fs)
            img.save(str(cover), "JPEG", quality=90)
        except ImportError:
            pass

    (out_dir / "meta.json").write_text(json.dumps(
        {"type": "daily_digest", "date": date_str,
         "news_count": sum(len(cd["news"]) for cd in cat_data.values()),
         "papers_count": sum(len(cd["papers"]) for cd in cat_data.values()),
         "llm_enhanced": len(enhanced)},
        ensure_ascii=False, indent=2), encoding="utf-8")

    total_n = sum(len(cd["news"]) for cd in cat_data.values())
    total_p = sum(len(cd["papers"]) for cd in cat_data.values())
    print(f"[Digest] {article_md} ({total_n}N+{total_p}P" +
          (f", {len(enhanced)} LLM)" if enhanced else ")"), flush=True)
    return out_dir
