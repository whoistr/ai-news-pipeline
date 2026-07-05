"""Daily knowledge card generator: single daily digest with two-layer structure.

Generates ONE Markdown file per day with fact + thinking layers.
Thinking depth scales by importance:

  1-2 (noise):     facts only
  3   (important): facts + why important + how to use
  4-5 (must-read): full analysis + star marker

Output: {output_dir}/{date}/{date} AI日报.md
"""

from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from ai_news_pipeline.config import PipelineConfig


# -- Constants ---------------------------------------------------

_PILLARS = ["趋势", "场景", "技术", "工具", "方法论", "实践"]

_IMPORTANCE_LABELS = {
    1: "噪音",
    2: "留意",
    3: "重要",
    4: "必看",
    5: "里程碑",
}

_WEEKDAYS = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]


# -- Text helpers ------------------------------------------------

def _clean_html(text: str, max_len: int = 400) -> str:
    """Strip HTML tags, entities, arXiv announce noise; collapse whitespace."""
    text = re.sub(r"<[^>]+>", "", text or "")
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    text = text.replace("&quot;", '"').replace("&#39;", "'")
    if "Abstract:" in text:
        text = text.split("Abstract:", 1)[1].strip()
    text = re.sub(r"arXiv:\S+\s+Announce Type:\s*\S+\s*", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_len] if max_len > 0 else text


def _format_weekday(date_str: str) -> str:
    try:
        return _WEEKDAYS[datetime.strptime(date_str, "%Y-%m-%d").weekday()]
    except ValueError:
        return ""


def _get_importance(item: dict) -> int:
    """Importance from llm_gates.importance -> rating -> default 2."""
    gates = (item.get("raw") or {}).get("llm_gates") or {}
    imp = gates.get("importance") or gates.get("rating") or 0
    if not imp:
        return 2
    try:
        return max(1, min(5, int(imp)))
    except (TypeError, ValueError):
        return 2


def _effective_importance(item: dict, thinking: dict | None) -> int:
    """Use LLM-assigned importance if available, else fall back to gates/default."""
    th = thinking or {}
    llm_imp = th.get("importance", 0)
    if llm_imp:
        return llm_imp
    return _get_importance(item)


# -- Source extraction -------------------------------------------

def _extract_handle_from_url(url: str) -> str:
    """Extract @handle from nitter/twitter/generic URLs."""
    if not url:
        return ""
    # nitter.net/{handle}/status/{id}
    m = re.search(r"nitter\.net/([A-Za-z0-9_]+)/status", url)
    if m:
        return m.group(1)
    # x.com/{handle}/status or twitter.com/{handle}/status
    m = re.search(r"(?:x\.com|twitter\.com)/([A-Za-z0-9_]+)/status", url)
    if m:
        return m.group(1)
    return ""


def _is_corrupted_source(source: str) -> bool:
    """Check if source name is corrupted (contains ?? or is empty)."""
    if not source:
        return True
    return "??" in source or source.strip() == ""


def _get_source_author(item: dict, thinking: dict | None) -> str:
    """Author name: LLM extraction > feed title > unknown."""
    th = thinking or {}
    if th.get("source_author"):
        return th["source_author"]

    source = item.get("source", "")
    if not _is_corrupted_source(source):
        # Feed titles like "AK (daily papers)" -> keep as is
        return source
    return "未知来源"


def _get_source_handle(item: dict, thinking: dict | None) -> str:
    """Handle: LLM extraction > URL extraction."""
    th = thinking or {}
    if th.get("source_handle"):
        h = th["source_handle"].strip()
        if h and not h.startswith("@"):
            h = "@" + h
        return h

    url = item.get("url", "")
    handle = _extract_handle_from_url(url)
    return "@" + handle if handle else ""


def _get_source_date(item: dict) -> str:
    """Original publish date YYYY-MM-DD from published_at."""
    pub = item.get("published_at", "")
    return pub[:10] if pub and len(pub) >= 10 else ""


def _render_source(item: dict, thinking: dict | None) -> str:
    """Render the source line per spec: [作者 (@handle)](url) · 原始日期."""
    url = item.get("url", "")
    author = _get_source_author(item, thinking)
    handle = _get_source_handle(item, thinking)
    source_date = _get_source_date(item) or "未知日期"

    if handle:
        label = f"{author} ({handle})"
    else:
        label = author

    if url:
        return f"来源：[{label}]({url}) · 原始日期：{source_date}"
    return f"来源：{label} · 原始日期：{source_date}"


# -- Title helpers -----------------------------------------------

_STOP_WORDS = frozenset({
    "The", "A", "An", "New", "How", "Why", "What", "Is", "Are",
    "Will", "Can", "Has", "Have", "Its", "Our", "Your", "This", "That",
    "With", "From", "Into", "For", "And", "But", "Not", "All", "More",
    "Most", "Best", "Top", "Via", "About", "Was", "Were",
})


def _get_entry_title(item: dict, thinking: dict | None) -> str:
    """Entry title: LLM-generated > gates.cn_title > truncated original."""
    th = thinking or {}
    if th.get("title"):
        return th["title"]

    gates = (item.get("raw") or {}).get("llm_gates") or {}
    cn = gates.get("cn_title", "")
    if cn:
        return cn

    # Fallback: truncate original title
    title = item.get("title", "")
    if len(title) > 60:
        title = title[:60].rsplit(" ", 1)[0] + "..."
    return title


def _extract_key_terms(title: str) -> set[str]:
    """Terms for related-item matching: PascalCase tokens + version strings."""
    if not title:
        return set()
    tokens = set(re.findall(r"[A-Z][a-zA-Z0-9]+", title))
    tokens = {t for t in tokens if len(t) >= 3 and t not in _STOP_WORDS}
    tokens.update(re.findall(r"\b[A-Z]{2,}[- ]?\d(?:\.\d)?\b", title))
    # Also match Chinese terms (2+ chars) for better matching
    tokens.update(re.findall(r"[\u4e00-\u9fff]{2,}", title))
    return tokens


def _find_related(items: list[dict], titles: list[str], top_n: int = 3) -> dict[str, list[str]]:
    """{url: [related titles]} by shared key terms."""
    url_terms: dict[str, set[str]] = {}
    for item, title in zip(items, titles):
        url = item.get("url", "")
        url_terms[url] = _extract_key_terms(title)

    related: dict[str, list[str]] = {}
    for i, (u1, terms1) in enumerate(url_terms.items()):
        matches = []
        for j, (u2, terms2) in enumerate(url_terms.items()):
            if i != j and terms1 & terms2:
                shared = len(terms1 & terms2)
                matches.append((shared, titles[j]))
        matches.sort(key=lambda x: -x[0])
        related[u1] = [t for _, t in matches[:top_n]]
    return related


# -- LLM thinking layer ------------------------------------------

_THINKING_PROMPT = """\
你是个人知识管理分析师。请对以下 AI 资讯进行评估和知识卡片生成。

核心原则：对每条资讯都问"so what? 那又怎样？"——经不起这一问的，importance 降到 1-2，只填 facts。

第一步：为每条生成主题式标题
- 8-15字，中文优先，能独立看懂"这讲的是什么"
- 不是原文前半句截断，是提炼后的主题
- 例：原文"Coding agents are real users of the Hub now i.e. Claude Code alone is ~24%"
  → 标题"Claude Code 占 Hugging Face 24% 流量"

第二步：判定 importance（1-5）
1=噪音（知道有这事就行）2=留意 3=重要 4=必看 5=里程碑
锚点：5=旗舰发布/范式转变/重大政策 4=重要产品/关键研究/行业格局变化
      3=有具体影响的进展（含硬数据、KOL战略观点、可操作的产品变化）
      2=有点意思但影响有限 1=热闹但经不起 so what?
注意：不要因为来源是社交媒体就一律给 2 分。包含硬数据（%数据）、
战略洞察、可操作信息的内容至少 3 分。

第三步：提取来源信息
- source_author: 一手发布方/原作者（不是转载者）。从内容和URL判断，抓不到写"未知来源"，绝不写"??"。
  - nitter.net/xxx 的 xxx 就是账号名
  - 英文账号名可直接用作 author
  - 转载场景填原始作者，不填转发者。例：dotey 转发 Sergey Brin 发言 → author 填 Sergey Brin
- source_handle: @账号（从URL提取，如 @dotey）。无则留空。

第四步：根据 importance 生成思考层
- importance 1-2：只填 facts + to_pillar，其余留空
      （注意～to_pillar 无论何时都必填，哪怕只是初判）
- importance 3：填 facts + why三问 + use个人/企业 + to_pillar
- importance 4-5：全部字段都填

字段说明：
- facts: ≤3 句中性压缩。不评价、不加工、只压缩主干。超 3 句说明还没想清楚。
- why_benefit: 谁受益？谁受损？具体到群体/公司/角色
- why_trend: 是孤立事件还是某趋势的拼图？哪个趋势？
- why_future: 如果成规模，3-6 个月后会有什么不同？
- use_personal: 个人现在能立刻做什么？要具体动作
- use_enterprise: 企业可以怎么落地到某个场景？
- use_judgment: 它验证或推翻了哪个常见判断？（仅 4-5 分填）
- my_judgment: 你的第一反应，不用润色，观点种子（仅 4-5 分填）
- to_pillar: 结晶去向，必须从以下选一个：趋势 / 场景 / 技术 / 工具 / 方法论 / 实践

只输出 JSON 数组，不要 markdown 包裹，不要解释：
[{{
  "idx": 1,
  "title": "主题式标题",
  "importance": 3,
  "source_author": "作者名",
  "source_handle": "@handle",
  "facts": "",
  "why_benefit": "",
  "why_trend": "",
  "why_future": "",
  "use_personal": "",
  "use_enterprise": "",
  "use_judgment": "",
  "my_judgment": "",
  "to_pillar": ""
}}]

待分析内容：
__ITEMS__"""


def _repair_json(text: str) -> str:
    """Best-effort repair of truncated JSON array."""
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


def _build_items_block(items: list[dict]) -> str:
    blocks = []
    for i, item in enumerate(items, 1):
        title = item.get("title", "")
        source = item.get("source", "")
        url = item.get("url", "")
        gates = (item.get("raw") or {}).get("llm_gates") or {}
        cn_title = gates.get("cn_title", "")
        summary = _clean_html(item.get("summary", ""), 400)
        blocks.append(
            f"--- {i} ---\n"
            f"原标题: {title}\n"
            f"来源标注: {source}\n"
            f"URL: {url}\n"
            f"已有中文标题: {cn_title or '(无)'}\n"
            f"摘要: {summary or '(无)'}"
        )
    return "\n\n".join(blocks)


def _call_thinking_batch(batch: list[dict], cfg: PipelineConfig) -> list[dict]:
    """One LLM call for a batch; returns list of per-item dicts with idx."""
    import requests

    llm = cfg.config.get("llm", {})
    api_key = llm.get("api_key", "")
    if not api_key or api_key == "sk-...":
        return []
    base_url = llm.get("base_url", "https://api.openai.com/v1").rstrip("/")
    models_cfg = cfg.config.get("models", {})
    model = models_cfg.get("knowledge") or models_cfg.get("scorer") or models_cfg.get("enhance") or llm.get("model", "gpt-4o")

    prompt = _THINKING_PROMPT.replace("__ITEMS__", _build_items_block(batch))

    for attempt in range(5):
        try:
            resp = requests.post(
                f"{base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "User-Agent": "Mozilla/5.0",
                },
                json={
                    "model": model,
                    "temperature": 0.4,
                    "max_tokens": 3000 * len(batch),
                    "messages": [
                        {"role": "system", "content": "你是知识管理分析师，只输出JSON数组，不要markdown包裹。"},
                        {"role": "user", "content": prompt},
                    ],
                },
                timeout=120,
            )
            resp.raise_for_status()
            msg = resp.json()["choices"][0]["message"]
            raw = msg.get("content") or msg.get("reasoning_content") or ""
            raw = re.sub(r"```(?:json)?\s*", "", raw).strip()
            m = re.search(r"\[.*", raw, re.DOTALL)
            if not m:
                continue
            try:
                parsed = json.loads(m.group(0))
            except json.JSONDecodeError:
                try:
                    parsed = json.loads(_repair_json(m.group(0)))
                except json.JSONDecodeError:
                    continue

            out = []
            for r in parsed:
                if not isinstance(r, dict):
                    continue
                idx = int(str(r.get("idx", 0)).strip())
                pillar = str(r.get("to_pillar", "")).strip()
                if pillar not in _PILLARS:
                    pillar = ""
                imp_val = r.get("importance", 0)
                try:
                    imp_val = max(1, min(5, int(imp_val)))
                except (TypeError, ValueError):
                    imp_val = 0
                out.append({
                    "idx": idx,
                    "title": str(r.get("title", "")),
                    "importance": imp_val,
                    "source_author": str(r.get("source_author", "")),
                    "source_handle": str(r.get("source_handle", "")),
                    "facts": str(r.get("facts", "")),
                    "why_benefit": str(r.get("why_benefit", "")),
                    "why_trend": str(r.get("why_trend", "")),
                    "why_future": str(r.get("why_future", "")),
                    "use_personal": str(r.get("use_personal", "")),
                    "use_enterprise": str(r.get("use_enterprise", "")),
                    "use_judgment": str(r.get("use_judgment", "")),
                    "my_judgment": str(r.get("my_judgment", "")),
                    "to_pillar": pillar,
                })
            return out
        except Exception as e:
            status = getattr(getattr(e, "response", None), "status_code", 0)
            if status == 429:
                wait = (2 ** attempt) * 5
                print(f"  [Thinking] attempt {attempt+1}: 429 -> wait {wait}s")
                time.sleep(wait)
                continue
            print(f"  [Thinking] attempt {attempt+1}: {e}")
            time.sleep(2)
    return []


def _generate_thinking_fields(items: list[dict], cfg: PipelineConfig) -> dict[str, dict]:
    """Generate thinking-layer fields for all items via LLM batch."""
    llm = cfg.config.get("llm", {})
    if not llm.get("api_key") or llm["api_key"] == "sk-...":
        return {}

    batch_size, concurrency = 6, 3
    slices = [items[j:j + batch_size] for j in range(0, len(items), batch_size)]
    print(f"  [Thinking] {len(items)} items, {len(slices)} batches x {batch_size}")

    results: dict[str, dict] = {}
    done = 0
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futures = {ex.submit(_call_thinking_batch, s, cfg): s for s in slices}
        for f in as_completed(futures):
            done += 1
            batch_items = futures[f]
            for r in f.result():
                pos = r.pop("idx", 0) - 1
                if 0 <= pos < len(batch_items):
                    url = batch_items[pos].get("url", "")
                    if url:
                        results[url] = r
            print(f"    [{done}/{len(slices)}] thinking batch done")
    return results


def _heuristic_facts(item: dict) -> str:
    """Fallback facts when LLM unavailable: compress summary or core_points."""
    gates = (item.get("raw") or {}).get("llm_gates") or {}
    summary = _clean_html(item.get("summary", ""), 150)
    if summary:
        return summary
    cp = gates.get("core_points") or []
    if cp:
        return "。".join(cp[:3]) + "。"
    return item.get("title", "")


# -- Entry rendering ---------------------------------------------

def _build_entry(
    item: dict,
    thinking: dict | None,
    importance: int,
    entry_num: int,
    related_titles: list[str] | None = None,
) -> str:
    """Render one entry per spec: numbered, bold small headings, proper source."""
    title = _get_entry_title(item, thinking)
    th = thinking or {}
    facts = th.get("facts", "") or _heuristic_facts(item)

    star = " ★" if importance >= 4 else ""
    lines: list[str] = [f"## {entry_num}. {title}{star}", ""]

    # Metadata line
    meta = [f"`importance: {importance}` ({_IMPORTANCE_LABELS.get(importance, '')})"]
    pillar = th.get("to_pillar", "")
    if not pillar:
        pillar = "待定"
    meta.append(f"`to_pillar: {pillar}`")
    lines.append(" · ".join(meta))

    # Source line
    lines.append(_render_source(item, thinking))
    lines.append("")

    # Facts layer (always present)
    lines.append("**事实**")
    lines.append("")
    lines.append(facts)
    lines.append("")

    # Thinking layer: importance >= 3
    if importance >= 3:
        lines.append("**为什么重要**")
        lines.append("")
        if th.get("why_benefit"):
            lines.append(f"- 谁受益/受损：{th['why_benefit']}")
        if th.get("why_trend"):
            lines.append(f"- 趋势拼图：{th['why_trend']}")
        if th.get("why_future"):
            lines.append(f"- 3-6 月后：{th['why_future']}")
        lines.append("")

        lines.append("**我能怎么用**")
        lines.append("")
        if th.get("use_personal"):
            lines.append(f"- 个人：{th['use_personal']}")
        if th.get("use_enterprise"):
            lines.append(f"- 企业：{th['use_enterprise']}")
        if importance >= 4 and th.get("use_judgment"):
            lines.append(f"- 判断校准：{th['use_judgment']}")
        lines.append("")

    # Judgment: importance >= 4
    if importance >= 4 and th.get("my_judgment"):
        lines.append("**我的判断**")
        lines.append("")
        lines.append(th["my_judgment"])
        lines.append("")

    # Related links
    if related_titles:
        links = " · ".join(f"[[{t}]]" for t in related_titles[:3])
        lines.append(f"**相关**：{links}")
        lines.append("")

    lines.append("---")
    lines.append("")
    return "\n".join(lines)










# -- Vault path resolution ---------------------------------------

def _resolve_vault_dir(cfg: PipelineConfig) -> Path:
    """Resolve the output directory from config.

    New format: knowledge_base.vault_root + knowledge_base.news_subpath
    Legacy:     knowledge_base.output_dir (full path, backward compat)

    Output: {vault_root}/{news_subpath}/{date}/{date} AI日报.md
    """
    kb_cfg = cfg.config.get("knowledge_base", {})

    vault_root = kb_cfg.get("vault_root", "")
    if vault_root:
        subpath = kb_cfg.get("news_subpath", "03 知识库/1-IdeaBox/每日资讯收集")
        return (Path(vault_root).expanduser() / subpath).resolve()

    output_dir = kb_cfg.get("output_dir", "")
    if output_dir:
        return Path(output_dir).expanduser()

    return Path("")


# -- Daily file assembly -----------------------------------------

def _compute_covers(items: list[dict], date_str: str) -> str:
    """Compute covers field: date range of source_dates."""
    dates = set()
    for item in items:
        sd = _get_source_date(item)
        if sd:
            dates.add(sd)
    if not dates:
        return date_str
    sorted_dates = sorted(dates)
    if len(sorted_dates) == 1:
        return sorted_dates[0]
    return f"{sorted_dates[0]}~{sorted_dates[-1]}"


def _build_daily_file(
    date_str: str,
    items: list[dict],
    thinking_map: dict[str, dict],
    related_map: dict[str, list[str]],
) -> str:
    """Assemble the complete daily digest Markdown string per spec."""
    weekday = _format_weekday(date_str)

    # Pre-compute titles for related linking
    titles = [_get_entry_title(item, thinking_map.get(item.get("url", ""), {}))
              for item in items]

    # Sort: highest importance first, stable within same level
    indexed = sorted(
        enumerate(items),
        key=lambda pair: (
            -_effective_importance(pair[1], thinking_map.get(items[pair[0]].get("url", ""), {})),
            pair[0],
        ),
    )

    star_count = sum(
        1 for it in items
        if _effective_importance(it, thinking_map.get(it.get("url", ""), {})) >= 4
    )
    total = len(items)
    covers = _compute_covers(items, date_str)

    # Frontmatter
    fm = ["---", f"date: {date_str}"]
    if weekday:
        fm.append(f"weekday: {weekday}")
    fm += [
        "tags:",
        "  - 资讯日报",
        "  - 状态/素材",
        f"total: {total}",
        f"star_count: {star_count}",
        f"covers: {covers}",
        "---",
        "",
    ]

    # Header
    header = [
        f"# {date_str} AI 日报",
        "",
        f"> 共 {total} 条 · ★ 结晶候选 {star_count} 条",
        "> 分级：1-2 噪音留档 · 3 重要深析 · 4-5 必看结晶",
        f"> 覆盖范围：{covers}",
        "",
        "---",
        "",
    ]

    # Entries (numbered)
    entries = []
    for entry_num, (orig_idx, item) in enumerate(indexed, 1):
        url = item.get("url", "")
        thinking = thinking_map.get(url, {})
        importance = _effective_importance(item, thinking)
        related = related_map.get(url, [])
        entries.append(_build_entry(item, thinking, importance, entry_num, related))

    # Footer
    footer = [
        "> 处理完的条目将 状态/素材 改为 状态/已沉淀，并在 to_pillar 填入去向。",
        "> ★ 条目是结晶候选，每周 review 时优先提炼进 [[5-5 趋势与观点 MOC|5.5]]。",
        "",
    ]

    return (
        "\n".join(fm)
        + "\n".join(header)
        + "\n".join(entries)
        + "\n".join(footer)
    )

# -- Public API: AI daily digest --------------------------------

def generate_daily_knowledge_card(
    cfg: PipelineConfig,
    date_str: str,
    items: list[dict],
) -> Path:
    """Generate the AI daily digest (news items) and write to the vault.

    Output path: {vault_root}/{news_subpath}/{date}/{date} AI日报.md

    Args:
        cfg: Pipeline config (reads knowledge_base.vault_root + news_subpath)
        date_str: Date for folder/file name (YYYY-MM-DD)
        items: List of news item dicts (news + semi_news)

    Returns: Path to the generated file (empty Path if skipped).
    """
    if not items:
        print("[KnowledgeCard] No items to process, skipping")
        return Path("")

    out_base = _resolve_vault_dir(cfg)
    if not str(out_base):
        print("[KnowledgeCard] knowledge_base.vault_root not configured, skipping")
        return Path("")

    out_dir = out_base / date_str
    out_dir.mkdir(parents=True, exist_ok=True)

    # Generate thinking layer via LLM
    use_llm = cfg.config.get("publish", {}).get("digest_use_llm", True)
    thinking_map: dict[str, dict] = {}
    if use_llm:
        cooldown = int(
            cfg.config.get("processor", {})
            .get("llm_scorer", {})
            .get("enhance_cooldown_seconds", 60)
        )
        if cooldown > 0:
            print(f"[KnowledgeCard] Cooling down {cooldown}s before thinking layer...")
            time.sleep(cooldown)
        thinking_map = _generate_thinking_fields(items, cfg)

    # Pre-compute titles for related-link matching
    titles = [_get_entry_title(item, thinking_map.get(item.get("url", ""), {}))
              for item in items]
    related_map = _find_related(items, titles)

    # Build and write
    content = _build_daily_file(date_str, items, thinking_map, related_map)
    out_file = out_dir / f"{date_str} AI日报.md"

    if out_file.exists():
        backup = out_dir / f"{date_str} AI日报.bak.md"
        if backup.exists():
            backup.unlink()
        out_file.rename(backup)
        print(f"[KnowledgeCard] Backed up existing file to {backup.name}")

    out_file.write_text(content, encoding="utf-8")

    star_count = sum(
        1 for it in items
        if _effective_importance(it, thinking_map.get(it.get("url", ""), {})) >= 4
    )
    enriched = sum(1 for v in thinking_map.values() if v.get("facts"))
    print(f"[KnowledgeCard] {len(items)} entries ({star_count}*) -> {out_file}")
    if thinking_map:
        print(f"  Thinking layer: {enriched}/{len(items)} items enriched")

    return out_file


# -- Paper digest rendering --------------------------------------

_PAPER_PROMPT = """\
你是学术文献筛选分析师。请对以下 AI/半导体论文生成文献日报的候选条目信息。

核心原则：文献日报是"读不读的决策助手"。目标是让读者 10 秒内判断这篇值不值得读。

第一步：判定 importance（1-5），★ 校准标准：
- 4-5 分（★）：有实证 + 与主线强相关 + 突破性贡献。纯理论无实证不上 3 分。
- 3 分：有实证，值得一看，但非主线突破。
- 1-2 分：纯理论无实证、与主线弱相关、增量改进。

第二步：为每篇论文生成以下字段：
- title: 8-15字中文主题式标题，能独立看懂这篇论文讲什么
- one_liner: "一句话概括"，说清核心贡献（含关键数据/方法名），≤40字
- core_problem: "核心问题"，这篇论文试图解决什么问题？（必填，≤30字）
- why_worth: "为什么值得读"，importance≥3 时必填。说清它对哪个群体/方向有价值。
- predicted_value: "预判价值"，importance≥3 时必填。如果成立，3-6个月后对你/行业有什么用？
- judgment: 你对这篇论文的判断（值得读/可选/跳过），加一句话理由
- to_pillar: 结晶去向，从以下选一个：趋势 / 场景 / 技术 / 工具 / 方法论 / 实践
- venue: 发表场所（从摘要/标题推断，如 ICML/NeurIPS/arXiv preprint；抓不到填"preprint"）
- literature_type: 文献类型（research/survey/benchmark/system；抓不到填"research"）
- source_author: 论文第一作者或通讯作者（抓不到填"未知作者"）
- source_handle: 作者 @ 账号（通常无，留空）
- related_topics: 相关主题词（2-3个，用于生成双链。如 ["多智能体","分布式协商"]）

只输出 JSON 数组，不要 markdown 包裹：
[{{
  "idx": 1,
  "title": "",
  "importance": 3,
  "one_liner": "",
  "core_problem": "",
  "why_worth": "",
  "predicted_value": "",
  "judgment": "",
  "to_pillar": "技术",
  "venue": "ICML 2026",
  "literature_type": "research",
  "source_author": "作者名",
  "source_handle": "",
  "related_topics": ["主题1", "主题2"]
}}]

待分析论文：
__ITEMS__"""


def _build_paper_items_block(items: list[dict]) -> str:
    blocks = []
    for i, item in enumerate(items, 1):
        title = item.get("title", "")
        source = item.get("source", "")
        summary = _clean_html(item.get("summary", ""), 500)
        blocks.append(
            f"--- {i} ---\n"
            f"标题: {title}\n"
            f"来源: {source}\n"
            f"摘要: {summary or '(无)'}"
        )
    return "\n\n".join(blocks)


def _call_paper_batch(batch: list[dict], cfg: PipelineConfig) -> list[dict]:
    """LLM call for paper digest entries."""
    import requests

    llm = cfg.config.get("llm", {})
    api_key = llm.get("api_key", "")
    if not api_key or api_key == "sk-...":
        return []
    base_url = llm.get("base_url", "https://api.openai.com/v1").rstrip("/")
    models_cfg = cfg.config.get("models", {})
    model = models_cfg.get("knowledge") or models_cfg.get("scorer") or llm.get("model", "gpt-4o")

    prompt = _PAPER_PROMPT.replace("__ITEMS__", _build_paper_items_block(batch))

    for attempt in range(5):
        try:
            resp = requests.post(
                f"{base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "User-Agent": "Mozilla/5.0",
                },
                json={
                    "model": model,
                    "temperature": 0.3,
                    "max_tokens": 1500 * len(batch),
                    "messages": [
                        {"role": "system", "content": "你是学术文献分析师，只输出JSON数组，不要markdown包裹。"},
                        {"role": "user", "content": prompt},
                    ],
                },
                timeout=120,
            )
            resp.raise_for_status()
            msg = resp.json()["choices"][0]["message"]
            raw = msg.get("content") or msg.get("reasoning_content") or ""
            raw = re.sub(r"```(?:json)?\s*", "", raw).strip()
            m = re.search(r"\[.*", raw, re.DOTALL)
            if not m:
                continue
            try:
                parsed = json.loads(m.group(0))
            except json.JSONDecodeError:
                try:
                    parsed = json.loads(_repair_json(m.group(0)))
                except json.JSONDecodeError:
                    continue

            out = []
            for r in parsed:
                if not isinstance(r, dict):
                    continue
                idx = int(str(r.get("idx", 0)).strip())
                pillar = str(r.get("to_pillar", "")).strip()
                if pillar not in _PILLARS:
                    pillar = ""
                imp_val = r.get("importance", 0)
                try:
                    imp_val = max(1, min(5, int(imp_val)))
                except (TypeError, ValueError):
                    imp_val = 0
                rt = r.get("related_topics") or []
                if isinstance(rt, str):
                    rt = [s.strip() for s in rt.split(",") if s.strip()]
                out.append({
                    "idx": idx,
                    "title": str(r.get("title", "")),
                    "importance": imp_val,
                    "one_liner": str(r.get("one_liner", "")),
                    "core_problem": str(r.get("core_problem", "")),
                    "why_worth": str(r.get("why_worth", "")),
                    "predicted_value": str(r.get("predicted_value", "")),
                    "judgment": str(r.get("judgment", "")),
                    "to_pillar": pillar,
                    "venue": str(r.get("venue", "preprint")),
                    "literature_type": str(r.get("literature_type", "research")),
                    "source_author": str(r.get("source_author", "")),
                    "source_handle": str(r.get("source_handle", "")),
                    "related_topics": rt,
                })
            return out
        except Exception as e:
            status = getattr(getattr(e, "response", None), "status_code", 0)
            if status == 429:
                wait = (2 ** attempt) * 5
                print(f"  [Paper] attempt {attempt+1}: 429 -> wait {wait}s")
                time.sleep(wait)
                continue
            print(f"  [Paper] attempt {attempt+1}: {e}")
            time.sleep(2)
    return []


def _generate_paper_fields(items: list[dict], cfg: PipelineConfig) -> dict[str, dict]:
    """Generate paper digest fields via LLM. Returns {url: {title, one_liner, ...}}."""
    llm = cfg.config.get("llm", {})
    if not llm.get("api_key") or llm["api_key"] == "sk-...":
        return {}

    batch_size, concurrency = 6, 3
    slices = [items[j:j + batch_size] for j in range(0, len(items), batch_size)]
    print(f"  [Paper] {len(items)} papers, {len(slices)} batches x {batch_size}")

    results: dict[str, dict] = {}
    done = 0
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futures = {ex.submit(_call_paper_batch, s, cfg): s for s in slices}
        for f in as_completed(futures):
            done += 1
            batch_items = futures[f]
            for r in f.result():
                pos = r.pop("idx", 0) - 1
                if 0 <= pos < len(batch_items):
                    url = batch_items[pos].get("url", "")
                    if url:
                        results[url] = r
            print(f"    [{done}/{len(slices)}] paper batch done")
    return results


def _build_paper_entry(
    item: dict,
    paper: dict | None,
    importance: int,
    entry_num: int,
    related_titles: list[str] | None = None,
) -> str:
    """Render one paper entry: compact candidate format with read checkbox.

    Uses bold small headings (not h3) to match AI daily layout.
    """
    pp = paper or {}
    title = pp.get("title", "") or item.get("title", "")[:60]
    one_liner = pp.get("one_liner", "") or _clean_html(item.get("summary", ""), 100)
    core_problem = pp.get("core_problem", "")
    why_worth = pp.get("why_worth", "")
    predicted_value = pp.get("predicted_value", "")
    judgment = pp.get("judgment", "")

    star = " ★" if importance >= 4 else ""
    lines: list[str] = [f"## {entry_num}. {title}{star}", ""]

    # Metadata line
    meta_parts = [f"`importance: {importance}`"]
    pillar = pp.get("to_pillar", "")
    if pillar:
        meta_parts.append(f"`to_pillar: {pillar}`")
    venue = pp.get("venue", "")
    if venue:
        meta_parts.append(f"`venue: {venue}`")
    lit_type = pp.get("literature_type", "")
    if lit_type:
        meta_parts.append(f"`type: {lit_type}`")
    lines.append(" · ".join(meta_parts))

    # Source line
    lines.append(_render_source(item, pp))
    lines.append("")

    # One-liner (always present)
    lines.append(f"**一句话概括**：{one_liner}")
    lines.append("")

    # Core problem (always present, required by spec)
    if core_problem:
        lines.append(f"**核心问题**：{core_problem}")
        lines.append("")

    # Thinking layer: importance >= 3
    if importance >= 3:
        if why_worth:
            lines.append(f"**为什么值得读**：{why_worth}")
            lines.append("")
        if predicted_value:
            lines.append(f"**预判价值**：{predicted_value}")
            lines.append("")

    # Judgment (always present if LLM provided it)
    if judgment:
        lines.append(f"**判断**：{judgment}")
        lines.append("")

    # Related links
    if related_titles:
        links = " · ".join(f"[[{ttl}]]" for ttl in related_titles[:3])
        lines.append(f"**相关**：{links}")
        lines.append("")

    # Read checkbox
    lines.append("- [ ] 已读 → 建笔记")
    lines.append("")

    lines.append("---")
    lines.append("")
    return "\n".join(lines)


def _build_paper_daily_file(
    date_str: str,
    items: list[dict],
    paper_map: dict[str, dict],
) -> str:
    """Assemble the paper digest: compact candidates, no deep thinking layer."""
    weekday = _format_weekday(date_str)

    # Pre-compute titles for related linking
    titles = [pp.get("title", "") or items[i].get("title", "")[:60]
              for i, pp in enumerate(
                  [paper_map.get(items[j].get("url", ""), {}) for j in range(len(items))]
              )]
    related_map = _find_related(items, titles)

    # Sort by importance descending
    ordered = sorted(
        enumerate(items),
        key=lambda pair: (
            -_effective_importance(pair[1], paper_map.get(items[pair[0]].get("url", ""), {})),
            pair[0],
        ),
    )

    star_count = sum(
        1 for it in items
        if _effective_importance(it, paper_map.get(it.get("url", ""), {})) >= 4
    )
    total = len(items)
    covers = _compute_covers(items, date_str)

    # Frontmatter (aligned with spec: literature_type / venue / read_status)
    fm = ["---", f"date: {date_str}"]
    if weekday:
        fm.append(f"weekday: {weekday}")
    fm += [
        "tags:",
        "  - 文献日报",
        "  - 状态/素材",
        f"total: {total}",
        f"star_count: {star_count}",
        f"covers: {covers}",
        "read_status: 未读",
        "---",
        "",
    ]

    header = [
        f"# {date_str} 文献日报",
        "",
        f"> 共 {total} 篇 · ★ 必读 {star_count} 篇",
        "> 未读论文只在聚合页作候选，读完才建独立笔记",
        f"> 覆盖范围：{covers}",
        "",
        "---",
        "",
    ]

    # Build related links: first try title overlap (related_map), then fall
    # back to shared related_topics from LLM output so every 3+ entry has a link.
    entries = []
    for entry_num, (orig_idx, item) in enumerate(ordered, 1):
        url = item.get("url", "")
        paper = paper_map.get(url, {})
        importance = _effective_importance(item, paper)
        related = related_map.get(url, [])
        # Fallback: if no title-overlap links but importance >= 3, link to
        # other entries sharing a related_topic keyword.
        if not related and importance >= 3:
            my_topics = set(tp.lower() for tp in paper.get("related_topics", []))
            if my_topics:
                for other_idx, other_item in ordered:
                    if other_idx == orig_idx:
                        continue
                    other_url = other_item.get("url", "")
                    other_paper = paper_map.get(other_url, {})
                    other_topics = set(tp.lower() for tp in other_paper.get("related_topics", []))
                    shared = my_topics & other_topics
                    if shared:
                        other_title = other_paper.get("title", "") or other_item.get("title", "")[:60]
                        if other_title and other_title not in related:
                            related.append(other_title)
                        if len(related) >= 3:
                            break
        entries.append(_build_paper_entry(item, paper, importance, entry_num, related))

    footer = [
        "> 每周 review：标了 ★ 的安排精读，读完套模板建独立 .md 归入 3.2 AI文献。",
        "",
    ]

    return "\n".join(fm) + "\n".join(header) + "\n".join(entries) + "\n".join(footer)


def generate_paper_knowledge_card(
    cfg: PipelineConfig,
    date_str: str,
    items: list[dict],
) -> Path:
    """Generate the paper daily digest and write to the vault.

    Output path: {vault_root}/{news_subpath}/{date}/{date} 文献日报.md

    Uses a compact candidate format (one-liner + judgment + read checkbox),
    NOT the deep thinking layer -- unread papers don't deserve deep analysis.

    Args:
        cfg: Pipeline config (reads knowledge_base.vault_root + news_subpath)
        date_str: Date for folder/file name (YYYY-MM-DD)
        items: List of paper item dicts (papers + semi_papers)

    Returns: Path to the generated file (empty Path if skipped).
    """
    if not items:
        print("[PaperCard] No papers to process, skipping")
        return Path("")

    out_base = _resolve_vault_dir(cfg)
    if not str(out_base):
        print("[PaperCard] knowledge_base.vault_root not configured, skipping")
        return Path("")

    out_dir = out_base / date_str
    out_dir.mkdir(parents=True, exist_ok=True)

    # Generate paper fields via LLM
    use_llm = cfg.config.get("publish", {}).get("digest_use_llm", True)
    paper_map: dict[str, dict] = {}
    if use_llm:
        cooldown = int(
            cfg.config.get("processor", {})
            .get("llm_scorer", {})
            .get("enhance_cooldown_seconds", 60)
        )
        if cooldown > 0:
            print(f"[PaperCard] Cooling down {cooldown}s...")
            time.sleep(cooldown)
        paper_map = _generate_paper_fields(items, cfg)

    content = _build_paper_daily_file(date_str, items, paper_map)
    out_file = out_dir / f"{date_str} 文献日报.md"

    if out_file.exists():
        backup = out_dir / f"{date_str} 文献日报.bak.md"
        if backup.exists():
            backup.unlink()
        out_file.rename(backup)
        print(f"[PaperCard] Backed up existing file to {backup.name}")

    out_file.write_text(content, encoding="utf-8")

    star_count = sum(
        1 for it in items
        if _effective_importance(it, paper_map.get(it.get("url", ""), {})) >= 4
    )
    print(f"[PaperCard] {len(items)} papers ({star_count}★) -> {out_file}")
    return out_file
