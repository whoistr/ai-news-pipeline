"""Ranker: split news vs academic, dedupe/score/rank per category."""

from __future__ import annotations

import json
import re
import math
from datetime import datetime, timezone
from datetime import timedelta
from difflib import SequenceMatcher
from pathlib import Path

from ai_news_pipeline.config import PipelineConfig
from ai_news_pipeline.models import NewsItem, utc_now_iso


# ── Helpers ────────────────────────────────────────────────────

def _normalize_title(title: str) -> str:
    t = title.lower()
    t = re.sub(r"[^\w\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _title_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, _normalize_title(a), _normalize_title(b)).ratio()


def _is_arxiv(item: NewsItem) -> bool:
    src = (item.source or "").lower()
    tags = item.tags or []
    return "arxiv" in src or "arxiv" in tags


def _is_semiconductor(item: NewsItem) -> bool:
    """An item belongs to the semiconductor vertical if tagged or keyword-matched."""
    tags = item.tags or []
    # NOTE: we deliberately do NOT trust a feed-level "semiconductor" tag here.
    # Some RSS sources (e.g. AnandTech/Tom's Hardware) are general hardware media
    # tagged with category=semiconductor at the feed level, which would force every
    # item from that feed into the semiconductor bucket even when the item is about
    # AI agent security or unrelated news. We judge by CONTENT keywords only; the
    # feed tag is kept for downstream weighting but not for hard classification.
    text = f"{item.title} {item.summary}".lower()
    # Short acronyms (dram/nand/euv/...) must match as whole words, otherwise
    # substrings cause false positives: "dram" inside "dramatically". Longer
    # multi-word terms stay as substring match.
    short_acronyms = {"dram", "nand", "edram", "euv", "duv", "gaa", "hbm", "asml", "tsmc", "ic design"}
    semi_kw = [
        "semiconductor", "chip", "foundry", "lithography", "wafer", "asml",
        "tsmc", "samsung semiconductor", "intel foundry", "gaa", "finfet",
        "3nm", "2nm", "euv", "duv", "packaging", "chiplet", "hbm",
        "nvidia blackwell", "amd instinct", "qualcomm", "broadcom",
        "memory chip", "dram", "nand", "edram", "fabrication plant",
        "ic design", "eda ", "cadence", "synopsys",
    ]
    for kw in semi_kw:
        if kw in short_acronyms:
            if re.search(r"(?<![a-z0-9])" + re.escape(kw) + r"(?![a-z0-9])", text):
                return True
        elif kw in text:
            return True
    return False


def _recency_boost(published_at: str | None) -> float:
    if not published_at:
        return 0.5
    try:
        dt = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
    except ValueError:
       return 0.5
    age_hours = (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds() / 3600
    # Smooth exponential decay (half-life ~24h) instead of hard step edges.
    # Steps let "fresh but mediocre" self-media beat "slightly older but important"
    # first-party posts that update less often. Continuous decay keeps recency
   # influence without that cliff.
    # Anchors: <=6h ~2.45, <=24h ~1.5, <=48h ~0.9, <=72h ~0.55.
    if age_hours < 0:
        age_hours = 0.0
    return round(3.0 * math.exp(-age_hours / 24.0), 3)


def _keyword_boost(text: str, cfg: PipelineConfig) -> float:
    proc = cfg.config.get("processor", {})
    rules = proc.get("keyword_boost", [])
    # Per-term cap: count a keyword once no matter how often it repeats.
    # Without this a title stuffed with "agent ... agent ... agent agent"
    # racks up +8.0 and dominates ranking -- frequency spam, not importance.
    per_term_cap = float(proc.get("keyword_boost_per_term_cap", 1.0))
    # Total cap so keyword relevance can't outweigh source quality + freshness.
    total_cap = float(proc.get("keyword_boost_total_cap", 4.0))
    boost = 0.0
    text_lower = text.lower()
    for rule in rules:
        term = rule.get("term", "")
        weight = float(rule.get("weight", 1.0))
        if term and term.lower() in text_lower:
            boost += min(weight, per_term_cap)
    return min(boost, total_cap)
    return boost


def _arxiv_quality_factor(item: NewsItem) -> float:
    """Stricter quality factor for arXiv papers.

    Tightened: papers without concrete experimental evidence are now actively
    penalized (not just 'not rewarded'). Only papers with real benchmarks,
    datasets, or quantitative results score >= 1.0. Concept/survey/framework
    papers with no experiments are heavily down-weighted.
    """
    factor = 1.0
    raw = item.raw or {}
    if raw.get("arxiv_announce_type", "") != "cross":
        factor *= 0.5  # was 0.6, stricter

    author_count = raw.get("author_count", 0)
    if author_count <= 1:
        factor *= 0.4  # single-author = likely concept paper
    elif author_count <= 2:
        factor *= 0.85  # small team, slight penalty
    elif author_count >= 5:
        factor *= 1.1  # was 1.15

    text = f"{item.title} {item.summary}".lower()

    hype_terms = ["framework", "novel", "unified", "paradigm", "synergy",
                  "empower", "revolutionize", "rethink", "pioneering",
                  "comprehensive", "robust", "scalable", "towards", "exploring"]
    concrete_terms = ["accuracy", "f1", "bleu", "rouge", "mmlu", "humaneval",
                      "imagenet", "coco", "sota", "state-of-the-art",
                      "dataset", "experiment", "ablation", "outperform",
                      "achieve", "improve", "reduce", "%", "percent",
                      "baseline", "benchmark", "evaluation", "results"]

    hype_count = sum(1 for t in hype_terms if t in text)
    concrete_count = sum(1 for t in concrete_terms if t in text)

    # Core change: papers with NO concrete signals are penalized, not just neutral
    if concrete_count == 0:
        factor *= 0.6  # no experiments/benchmarks/data at all -> strong penalty
    elif concrete_count == 1:
        factor *= 0.85  # weak evidence
    elif hype_count > 3 and concrete_count < 3:
        factor *= 0.7  # lots of buzzwords, little substance
    elif concrete_count >= 3:
        factor *= 1.1  # solid experimental work

    if len(item.summary) < 300:
        factor *= 0.7  # was 250 threshold + 0.75, stricter

    # Bonus for quantitative rigor
    if re.search(r"\d+\.?\d*%", item.summary):
        factor *= 1.1
    if re.search(r"\d+(\.\d+)?\s*(F1|BLEU|accuracy|AUC|ROUGE)", text):
        factor *= 1.1  # was 1.15

    return round(factor, 2)


def _news_quality_factor(item: NewsItem) -> float:
    """Stricter quality factor for news/blog items.

    Tightened: now checks AI relevance (items with no AI/ML signal are penalized),
    rewards substantive depth (benchmarks, model names, code), and actively
    down-weights gossip/product-update fluff that isn't real AI news.
    """
    factor = 1.0
    text = f"{item.title} {item.summary}".lower()

    # AI relevance gate: if none of these appear, the item is likely off-topic
    # (e.g. Windows PC news, generic startup gossip) and should be penalized.
    ai_signals = ["ai", "ml", "llm", "gpt", "claude", "gemini", "model",
                  "agent", "neural", "deep learning", "transformer", "diffusion",
                  "openai", "anthropic", "google", "nvidia", "huggingface",
                  "machine learning", "artificial intelligence", "training",
                  "fine-tune", "rag", "embedding", "multimodal", "inference"]
    ai_hits = sum(1 for s in ai_signals if s in text)
    if ai_hits == 0:
        factor *= 0.5  # not AI-related at all -> strong penalty
    elif ai_hits == 1:
        factor *= 0.85  # weakly related
    elif ai_hits >= 3:
        factor *= 1.05  # deeply AI-focused

    # Substantive depth signals (reward real technical content)
    substance_signals = ["benchmark", "dataset", "api", "github", "open source",
                         "open-source", "code", "paper", "arxiv", "model",
                         "parameters", "tokens", "gpu", "evaluation", "release",
                         "launch", "architecture", "performance"]
    substance_hits = sum(1 for s in substance_signals if s in text)
    if substance_hits >= 3:
        factor *= 1.1  # rich technical substance
    elif substance_hits == 0:
        factor *= 0.85  # no technical substance

    # Reward informative length (but only if it has substance, checked above)
    if len(item.summary) > 300:
        factor *= 1.05
    if len(item.summary) > 600:
        factor *= 1.05

    # Numeric data = real metrics
    if re.search(r"\d+\.?\d*%", item.summary) or re.search(r"\$\d+[MBK]?", item.summary):
        factor *= 1.1  # was 1.15, slightly toned down

    # Penalize clickbait and fluff
    clickbait = ["shocking", "you won't believe", "mind-blowing", "insane",
                 "game-changer", "revolutionary", "this changes everything",
                 "viral", "breaking", "exclusive"]
    if sum(1 for t in clickbait if t in text) >= 2:
        factor *= 0.6  # was 0.7, stricter

    # Penalize off-topic categories that sneak into AI feeds
    off_topic = ["windows defender", "windows pc", "netflix", "movie", "celebrity",
                 "ant colony", "radar", "minecraft", "gaming", "crypto", "nft"]
    if any(t in text for t in off_topic):
        factor *= 0.5

    return round(factor, 2)


def _is_useful(item: NewsItem, is_arxiv: bool, score: float) -> bool:
    """Determine if an item is 'useful information' worth keeping."""
    if is_arxiv:
        return score >= 2.5  # ArXiv: lower bar since academic content is inherently more filtered
    else:
        # News: must have some depth and relevance
        return score >= 3.0 and len(item.summary or "") > 50


# ── Scoring / Dedup ────────────────────────────────────────────

def _source_authority_bonus(item: NewsItem, cfg: PipelineConfig) -> float:
    """Flat bonus for first-party / authoritative sources.

    The feed-weight system already trusts official blogs more, but recency and
    keyword boosts can let high-volume self-media outrank them. This is a small,
    final-stage nudge that re-asserts source authority in the ranking. Configured
    via processor.source_authority_bonus (default 1.0); set to 0 to disable.
    """
    bonus = float(cfg.config.get("processor", {}).get("source_authority_bonus", 1.0))
    if bonus <= 0:
        return 0.0
    src = (item.source or "").strip()
    tags = item.tags or []
    # Authority sources: official vendor/research blogs + named expert analysts.
    authority = {
        "OpenAI Blog", "Google AI Blog", "Hugging Face Blog", "Mistral News",
        "Anthropic", "DeepMind", "Meta AI", "Microsoft Research",
        "Import AI (Jack Clark)", "SemiAnalysis", "Simon Willison's Blog",
        "AI Snake Oil", "LessWrong AI", "MIT Technology Review",
    }
    if src in authority or any(t == "authority" for t in tags):
        return bonus
    return 0.0


def _dedupe_x_retweets(items: list[NewsItem]) -> list[NewsItem]:
    """Collapse X retweets of the same original tweet.

    nitter RSS prefixes retweets with "RT by @A: <original text>". Multiple
    accounts retweeting the same post produce near-identical items that the
    title-similarity dedup misses (prefix differs: "RT by @A:" vs "RT by @B:").
    Here we extract the original text after the prefix, group by it, and keep
    only the highest-scoring copy.
    """
    by_orig: dict[str, NewsItem] = {}
    non_rt: list[NewsItem] = []
    for it in items:
        is_x = "x-kol" in (it.tags or []) or "x-official" in (it.tags or [])
        if not is_x:
            non_rt.append(it)
            continue
        m = re.match(r"^(?:RT by @\S+:\s*)?(.+)", it.title, re.DOTALL)
        orig = m.group(1).strip().lower()[:80] if m else it.title.lower()[:80]
        # Only group if it was actually a retweet (had RT prefix)
        was_rt = it.title.startswith("RT by") or it.title.startswith("R to")
        if was_rt and orig in by_orig:
            existing = by_orig[orig]
            if it.score > existing.score:
                by_orig[orig] = it
        else:
            if was_rt:
                by_orig[orig] = it
            else:
                non_rt.append(it)
    kept = non_rt + list(by_orig.values())
    if len(kept) < len(items):
        print(f"  [X dedup] collapsed {len(items) - len(kept)} duplicate retweets")
    return kept


def _x_quality_factor(item: NewsItem) -> float:
    """Quality factor tuned for X/Twitter content.

    X tweets are short by nature, so we do NOT penalize short summaries (unlike
    _news_quality_factor). Instead we reward tweets that carry real signal:
    mentions of specific metrics, product names, or links to code/papers.
    """
    factor = 1.0
    text = f"{item.title} {item.summary}".lower()

    # Reward concrete signals (numbers, percentages, model names)
    if re.search(r"\d+\.?\d*%", text):
        factor *= 1.1
    if re.search(r"https?://\S+", text):
        factor *= 1.05  # links to papers/code/repos add value
    # Penalize pure emoji/link spam (already filtered in collector, but double-check)
    text_only = re.sub(r"https?://\S+", "", text)
    text_only = re.sub(r"[\U0001f000-\U0001ffff]", "", text_only).strip()
    if len(text_only) < 20:
        factor *= 0.5

    return round(factor, 2)


def _score_item(item: NewsItem, cfg: PipelineConfig) -> float:
    base = item.score or 1.0


    text = f"{item.title} {item.summary}"

    if _is_arxiv(item):
        base *= _arxiv_quality_factor(item)
    elif "x-kol" in (item.tags or []) or "x-official" in (item.tags or []):
        base *= _x_quality_factor(item)
    else:
        base *= _news_quality_factor(item)

    return (
        base
        + _recency_boost(item.published_at)
        + _keyword_boost(text, cfg)
        + _source_authority_bonus(item, cfg)
    )


def _dedupe_items(items: list[NewsItem], threshold: float) -> list[NewsItem]:
    kept: list[NewsItem] = []
    for item in sorted(items, key=lambda x: x.score, reverse=True):
        duplicate = False
        for existing in kept:
            if _title_similarity(item.title, existing.title) >= threshold:
                duplicate = True
                break
            if item.url == existing.url:
                duplicate = True
                break
        if not duplicate:
            kept.append(item)
    return kept


# ── Main processing ────────────────────────────────────────────

def _load_history_urls(cfg: PipelineConfig, exclude_date: str, lookback_days: int) -> set[str]:
    """Load URLs + normalized titles from past N days of processed data to dedup against."""
    seen: set[str] = set()
    try:
        target = datetime.strptime(exclude_date, "%Y-%m-%d")
    except ValueError:
        return seen
    for d in range(1, lookback_days + 1):
        past = (target - timedelta(days=d)).strftime("%Y-%m-%d")
        path = cfg.processed_dir / f"{past}.json"
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        for section in ("news", "papers", "semiconductor", "semiconductor_news", "semiconductor_papers"):
            sec = data.get(section)
            if not isinstance(sec, dict):
                continue
            for it in sec.get("items", []):
                url = (it.get("url") or "").strip()
                if url:
                    seen.add(url)
                title = _normalize_title(it.get("title", ""))
                if title:
                    seen.add(f"t:{title}")
    return seen


def process_items(items: list[NewsItem], cfg: PipelineConfig, skip_llm: bool = False) -> dict[str, list[NewsItem]]:
    """Split into news + literature + semiconductor, process each separately.

    Returns {"news": [...], "papers": [...], "semiconductor": [...]}.
    """
    proc = cfg.config.get("processor", {})
    threshold = float(proc.get("dedupe_similarity", 0.85))
    min_score_news = float(proc.get("min_score_news", 3.0))
    min_score_papers = float(proc.get("min_score_papers", 2.5))
    cap_news = int(proc.get("cap_news", 10))
    cap_papers = int(proc.get("cap_papers", 10))
    min_score_semi = float(proc.get("min_score_semiconductor", 2.0))
    cap_semi = int(proc.get("cap_semiconductor", 5))
    min_score_semi_papers = float(proc.get("min_score_semiconductor_papers", 2.0))
    cap_semi_news = int(proc.get("cap_semiconductor_news", cap_semi))
    cap_semi_papers = int(proc.get("cap_semiconductor_papers", 3))

    # Split: semiconductor first, then arxiv papers, remainder = AI news
    semi_items = [i for i in items if _is_semiconductor(i)]
    remaining = [i for i in items if not _is_semiconductor(i)]
    arxiv_items = [i for i in remaining if _is_arxiv(i)]
    news_items = [i for i in remaining if not _is_arxiv(i)]
    # Further split semiconductor into news vs arxiv papers
    semi_news = [i for i in semi_items if not _is_arxiv(i)]
    semi_papers = [i for i in semi_items if _is_arxiv(i)]

    print(f"[Process] split: {len(news_items)} AI-news + {len(arxiv_items)} AI-papers + {len(semi_news)} semi-news + {len(semi_papers)} semi-papers")

    # Score
    for item in items:
        item.score = _score_item(item, cfg)

    # X retweet collapse: multiple accounts retweeting the same original tweet
    # produce near-identical items that title-similarity dedup misses. Collapse
    # them here, before category splitting and cross-day dedup.
    before = len(news_items) + len(arxiv_items)
    news_items = _dedupe_x_retweets(news_items)
    arxiv_items = _dedupe_x_retweets(arxiv_items)
    after = len(news_items) + len(arxiv_items)
    if before != after:
        items = news_items + arxiv_items + semi_news + semi_papers

    # Cross-day dedup: remove items already published in recent days
    lookback = int(proc.get("dedup_lookback_days", 7))
    history_urls = _load_history_urls(cfg, datetime.now().strftime("%Y-%m-%d"), lookback)
    if history_urls:
        def _not_seen(it: NewsItem) -> bool:
            if it.url.strip() in history_urls:
                return False
            if f"t:{_normalize_title(it.title)}" in history_urls:
                return False
            return True
        n_before = len(news_items) + len(arxiv_items) + len(semi_news) + len(semi_papers)
        news_items = [i for i in news_items if _not_seen(i)]
        arxiv_items = [i for i in arxiv_items if _not_seen(i)]
        semi_news = [i for i in semi_news if _not_seen(i)]
        semi_papers = [i for i in semi_papers if _not_seen(i)]
        n_after = len(news_items) + len(arxiv_items) + len(semi_news) + len(semi_papers)
        if n_before != n_after:
            print(f"[Process] cross-day dedup: removed {n_before - n_after} already-published items")

    # Process each category
    # LLM three-gate scorer (importance/timeliness/actionability). Applied as an
    # ADDITIVE bonus so genuinely important items (e.g. a flagship tech report that
    # scores low on the heuristic) can cross the min_score bar regardless of the
    # low arxiv feed_weight. Falls back to pure heuristic when disabled/failed.
    scorer_cfg = cfg.config.get("processor", {}).get("llm_scorer", {})
    llm_enabled = bool(scorer_cfg.get("enabled", False)) and len(items) > 0 and not skip_llm
    evaluate_n = int(scorer_cfg.get("evaluate_top_n", 15))
    # Papers get a WIDER evaluation net: heuristic scores arxiv on a low
    # feed_weight (0.25-0.35), so its ranking is unreliable for importance.
   # A flagship tech report can sit at 1.1 and never enter a top-15 net.
    evaluate_n_papers = int(scorer_cfg.get("evaluate_top_n_papers", 30))

    def process_category(cat_items: list[NewsItem], min_score: float, cap: int,
                         label: str, is_arxiv: bool, llm_scores: dict | None = None) -> list[NewsItem]:
        deduped = _dedupe_items(cat_items, threshold)
        deduped.sort(key=lambda x: x.score, reverse=True)
        if llm_scores:
            boosted = 0
            for item in deduped:
                hit = llm_scores.get(item.url)
                if hit:
                    item.score = round(item.score + hit["bonus"], 3)
                    item.raw = item.raw or {}
                    item.raw["llm_gates"] = {
                        "importance": hit["importance"],
                        "timeliness": hit["timeliness"],
                        "actionability": hit["actionability"],
                        "reason": hit["reason"],
                        "bonus": hit["bonus"],
                        "cn_title": hit.get("cn_title", ""),
                        "rating": hit.get("rating", 3),
                        "why_worth": hit.get("why_worth", ""),
                        "core_points": hit.get("core_points") or [],
                    }
                    if abs(hit["bonus"]) >= 0.5:
                        boosted += 1
            if boosted:
                print(f"  [{label}] LLM bonus applied to {boosted} items")
            deduped.sort(key=lambda x: x.score, reverse=True)
        result = []
        for item in deduped:
            if item.score < min_score:
                continue
            item.raw = item.raw or {}
            item.raw["useful"] = _is_useful(item, is_arxiv, item.score)
            result.append(item)
            if len(result) >= cap:
                break
        useful = sum(1 for i in result if i.raw.get("useful"))
        print(f"  [{label}] {len(result)} kept (cap={cap}, min_score={min_score}, useful={useful})")
        if result:
            gates = result[0].raw.get("llm_gates") if result[0].raw else None
            gate_str = f" imp={gates['importance']}" if gates else ""
            print(f"    top: [{result[0].score:.1f}]{gate_str} {result[0].title[:60]}...")
        return result

    # Collect candidates from ALL categories and score them in a single shared
    # batch pool. This replaces the old per-category serial scoring (which ran
    # 4 sequential ThreadPoolExecutor rounds) with one parallel pool. Scores are
    # keyed by URL, so process_category can pass the shared dict and each
    # category only matches its own items.
    all_candidates: list[NewsItem] = []
    if llm_enabled:
        for cat_items, is_arxiv in [
            (news_items, False), (arxiv_items, True),
            (semi_news, False), (semi_papers, True),
        ]:
            if len(cat_items) < 2:
                continue
            deduped = _dedupe_items(cat_items, threshold)
            deduped.sort(key=lambda x: x.score, reverse=True)
            n = evaluate_n_papers if is_arxiv else evaluate_n
            candidates = deduped[:n]
            if len(candidates) >= 2:
                all_candidates.extend(candidates)
    all_scores: dict = {}
    if all_candidates:
        try:
            from ai_news_pipeline.processors.llm_scorer import score_candidates_with_llm
            all_scores = score_candidates_with_llm(all_candidates, cfg, "all")
        except Exception as e:
            print(f"  [LLMScore] disabled or failed ({e}); using heuristic only")

    news_ranked = process_category(news_items, min_score_news, cap_news, "news", False, all_scores)
    papers_ranked = process_category(arxiv_items, min_score_papers, cap_papers, "papers", True, all_scores)
    semi_news_ranked = process_category(semi_news, min_score_semi, cap_semi_news, "semiconductor_news", False, all_scores)
    semi_papers_ranked = process_category(semi_papers, min_score_semi_papers, cap_semi_papers, "semiconductor_papers", True, all_scores)

    # Backfill: any item that survived into a category but sat OUTSIDE the
    # top-N LLM evaluation net (e.g. ranked ~21st heuristically but promoted by
    # dedup/cap) would reach the digest without three-gate scores. Give those a
    # second-pass LLM eval so every published item has consistent judgement.
    if llm_enabled:
        gaps: list[NewsItem] = []
        for bucket in (news_ranked, papers_ranked, semi_news_ranked, semi_papers_ranked):
            for it in bucket:
                if not (it.raw or {}).get("llm_gates"):
                    gaps.append(it)
        if len(gaps) >= 2:
            try:
                from ai_news_pipeline.processors.llm_scorer import score_candidates_with_llm
                backfill = score_candidates_with_llm(gaps, cfg, "backfill")
                for it in gaps:
                    hit = backfill.get(it.url)
                    if hit:
                        it.score = round(it.score + hit["bonus"], 3)
                        it.raw = it.raw or {}
                        it.raw["llm_gates"] = {
                            "importance": hit["importance"],
                            "timeliness": hit["timeliness"],
                            "actionability": hit["actionability"],
                            "reason": hit["reason"],
                            "bonus": hit["bonus"],
                            "cn_title": hit.get("cn_title", ""),
                            "rating": hit.get("rating", 3),
                            "why_worth": hit.get("why_worth", ""),
                            "core_points": hit.get("core_points") or [],
                        }
                # Re-sort each category after backfill bonuses
                news_ranked.sort(key=lambda x: x.score, reverse=True)
                papers_ranked.sort(key=lambda x: x.score, reverse=True)
                semi_news_ranked.sort(key=lambda x: x.score, reverse=True)
                semi_papers_ranked.sort(key=lambda x: x.score, reverse=True)
            except Exception as e:  # noqa: BLE001
                print(f"  [LLMScore:backfill] failed ({e}); leaving heuristic scores")

    return {
        "news": news_ranked,
        "papers": papers_ranked,
        "semiconductor_news": semi_news_ranked,
        "semiconductor_papers": semi_papers_ranked,
    }


def _filter_by_purpose(items: list[NewsItem], purpose: str) -> list[NewsItem]:
    """Keep items whose source purpose matches.

    purpose="publish" -> keep publish + both.
    purpose="learning" -> keep learning + both.
    Items without a feed_purpose tag default to "publish".
    """
    out = []
    for it in items:
        fp = (it.raw or {}).get("feed_purpose", "publish")
        if fp == "both" or fp == purpose:
            out.append(it)
    return out


def _x_topic_boost(items: list[NewsItem], cfg: PipelineConfig) -> None:
    """????:? X ??? x_keywords ????????(???? score)?

    nitter ??? RSS ??,????????????"?????"????
    ?????:? learning ??? X ????,? config ? x_keywords ?
    ????+??,????????? topic ? boost ????? AI Agent /
    LLM / RAG ??????? learning ??????,???"????"?
    ?? X ??(x-kol/x-official tag)??,????? RSS ???
    """
    x_topics = cfg.config.get("processor", {}).get("x_keywords", [])
    if not x_topics:
        return
    boosted = 0
    for item in items:
        tags = item.tags or []
        is_x = "x-kol" in tags or "x-official" in tags
        if not is_x:
            continue
        text = (f"{item.title} {item.summary}").lower()
        best_boost = 0.0
        matched_topics = []
        for rule in x_topics:
            for kw in rule.get("keywords", []):
                if kw.lower() in text:
                    b = float(rule.get("boost", 1.0))
                    if b > best_boost:
                        best_boost = b
                    if rule.get("topic") not in matched_topics:
                        matched_topics.append(rule.get("topic"))
                    break  # ?????????,???? topic
        if best_boost > 0:
            item.score = round(item.score + best_boost - 1.0, 3)  # ??? = boost-1.0
            item.raw = item.raw or {}
            item.raw["x_topics"] = matched_topics
            boosted += 1
    if boosted:
        print(f"[Process] X ????: {boosted}/{sum(1 for i in items if 'x-kol' in (i.tags or []) or 'x-official' in (i.tags or []))} X ???????")


def process_items_dual(items: list[NewsItem], cfg: PipelineConfig) -> dict[str, dict]:
    """Run the ranking pipeline separately for publish and learning purposes.

    Returns {"publish": {category dict}, "learning": {category dict}}.
    Items tagged purpose=both appear in both sets. This is the dual-mode entry
    point: pipeline.py calls this so processed.json carries two independent
    result sets, and the digest step can generate a publish?? (-> WeChat) and
    a learning?? (-> local only) without cross-contamination.
    """
    pub_items = _filter_by_purpose(items, "publish")
    learn_items = _filter_by_purpose(items, "learning")
    print(f"[Process] dual split: {len(pub_items)} publish + {len(learn_items)} learning ({len(items)} total, both counted twice)")
    # ?????? learning ???(publish ??????,??? X ?????)
    _x_topic_boost(learn_items, cfg)
    return {
        "publish": process_items(pub_items, cfg),
        "learning": process_items(learn_items, cfg, skip_llm=True),
    }


def process_items_flat(items: list[NewsItem], cfg: PipelineConfig) -> list[NewsItem]:
    """Legacy flat output for backward compatibility."""
    result = process_items(items, cfg)
    return (
        result["news"] + result["papers"]
        + result.get("semiconductor_news", [])
        + result.get("semiconductor_papers", [])
    )


# ── Save / Load ────────────────────────────────────────────────

def save_processed(
    result: dict[str, list[NewsItem]] | list[NewsItem],
    cfg: PipelineConfig,
    date_str: str | None = None,
) -> Path:
    date_str = date_str or datetime.now().strftime("%Y-%m-%d")
    cfg.processed_dir.mkdir(parents=True, exist_ok=True)
    out_path = cfg.processed_dir / f"{date_str}.json"

    if isinstance(result, dict):
        # Dual-mode: result has top-level "publish" and "learning" sub-dicts
        if "publish" in result or "learning" in result:
            def _section_payload(sec):
                return {
                    "news": {"count": len(sec.get("news", [])), "items": [i.to_dict() for i in sec.get("news", [])]},
                    "papers": {"count": len(sec.get("papers", [])), "items": [i.to_dict() for i in sec.get("papers", [])]},
                    "semiconductor_news": {"count": len(sec.get("semiconductor_news", [])), "items": [i.to_dict() for i in sec.get("semiconductor_news", [])]},
                    "semiconductor_papers": {"count": len(sec.get("semiconductor_papers", [])), "items": [i.to_dict() for i in sec.get("semiconductor_papers", [])]},
                }
            payload = {
                "date": date_str,
                "processed_at": utc_now_iso(),
                "publish": _section_payload(result.get("publish", {})),
                "learning": _section_payload(result.get("learning", {})),
            }
        else:
            news = result.get("news", [])
            papers = result.get("papers", [])
            semi_news = result.get("semiconductor_news", [])
            semi_papers = result.get("semiconductor_papers", [])
            payload = {
                "date": date_str,
                "processed_at": utc_now_iso(),
                "news": {"count": len(news), "items": [i.to_dict() for i in news]},
                "papers": {"count": len(papers), "items": [i.to_dict() for i in papers]},
                "semiconductor_news": {"count": len(semi_news), "items": [i.to_dict() for i in semi_news]},
                "semiconductor_papers": {"count": len(semi_papers), "items": [i.to_dict() for i in semi_papers]},
            }
    else:
        payload = {
            "date": date_str,
            "processed_at": utc_now_iso(),
            "count": len(result),
            "items": [i.to_dict() for i in result],
        }

    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return out_path


def load_processed(cfg: PipelineConfig, date_str: str, purpose: str = "publish") -> list[NewsItem]:
    path = cfg.processed_dir / f"{date_str}.json"
    if not path.is_file():
        raise FileNotFoundError(f"未找到处理结果: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    items = []
    # Dual-mode: data has "publish"/"learning" sub-dicts
    if "publish" in data or "learning" in data:
        section_data = data.get(purpose, {})
        for sec_name in ("news", "papers", "semiconductor_news", "semiconductor_papers"):
            sec = section_data.get(sec_name)
            if sec:
                items.extend(NewsItem.from_dict(i) for i in sec.get("items", []))
    elif "news" in data:
        items.extend(NewsItem.from_dict(i) for i in data["news"].get("items", []))
        items.extend(NewsItem.from_dict(i) for i in data["papers"].get("items", []))
        for section in ("semiconductor_news", "semiconductor_papers", "semiconductor"):
            semi_data = data.get(section)
            if semi_data:
                items.extend(NewsItem.from_dict(i) for i in semi_data.get("items", []))
    else:
        items = [NewsItem.from_dict(i) for i in data.get("items", [])]
    return items
