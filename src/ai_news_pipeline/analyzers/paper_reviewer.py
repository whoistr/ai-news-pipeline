"""arXiv paper reviewer: LLM-powered quality analysis and star rating."""

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


# ── Heuristic helpers ──────────────────────────────────────────

_CONCEPT_TITLE_PATTERNS = [
    r"\bconcept\b", r"\bvision\b", r"\bfuture\b", r"\bsurvey\b",
    r"\broadmap\b", r"\btaxonomy\b", r"\bprospect\b", r"\blandscape\b",
    r"\bposition\s+paper\b", r"\bgrand\s+challenge", r"\boutlook\b",
    r"\brethinking\b", r"\breimagining\b", r"\breinventing\b",
]

_STRONG_EXPERIMENT_SIGNALS = [
    r"\d{1,3}\.\d+%",
    r"\d+\.\d+\s*(F1|BLEU|ROUGE|accuracy|AUC|mAP|IoU)",
    r"\b(ImageNet|COCO|CIFAR|MNIST|GLUE|SuperGLUE|SQuAD|MMLU|HumanEval|"
    r"GSM8K|MATH|ARC|HellaSwag|WinoGrande|TruthfulQA)\b",
    r"\b(ablation|outperform|SOTA|state-of-the-art|beats?|surpass(es|ed)?|"
    r"achieve[sd]?|improve[sd]?\s+by)\b",
    r"(?:accuracy|precision|recall|F1|BLEU|ROUGE|AUC)\s*(?:of\s*)?\d+\.?\d*%?",
    r"\b(\d+[KM]?\s*(parameters?|samples?|images?|videos?|tokens?|hours?))\b",
]

_CCF_A_VENUES = {
    "AAAI", "NeurIPS", "ICML", "IJCAI", "ACL", "EMNLP", "NAACL",
    "CVPR", "ICCV", "ECCV", "KDD", "SIGIR", "WWW", "SIGMOD", "VLDB",
    "ICDE", "OSDI", "SOSP", "CCS", "S&P", "USENIX", "NDSS",
    "MobiCom", "SIGCOMM", "ISCA", "MICRO", "HPCA", "SC",
    "PLDI", "POPL", "ICSE", "FSE", "ASE", "ISSTA",
    "TPAMI", "IJCV", "TIP", "TIFS", "TKDE", "TOIS", "JMLR",
    "TOCS", "TODS", "TOS", "TCAD", "TC", "TPDS",
}

_CCF_B_VENUES = {
    "ICASSP", "COLING", "CoNLL", "EACL", "AACL",
    "ICPR", "ICDAR", "BMVC", "FG", "WACV",
    "CIKM", "WSDM", "ICDM", "SDM", "DASFAA", "ECML-PKDD",
    "ICRA", "IROS", "ROBIO",
    "DATE", "DAC", "ICCAD", "EMSOFT", "CODES+ISSS",
    "PACT", "CGO", "CCGRID", "CLUSTER", "ICPADS",
    "RECOMB", "ISMB", "PSB",
    "TACL", "TASLP", "TMM", "TCSVT", "TITS", "TNNLS",
    "Neurocomputing", "Pattern Recognition", "KBS",
    "INS", "ESWA", "IPM", "SPL",
}


def _has_strong_experiments(text: str) -> bool:
    text_lower = text.lower()
    count = 0
    for pat in _STRONG_EXPERIMENT_SIGNALS:
        if re.search(pat, text_lower):
            count += 1
            if count >= 2:
                return True
    return False


def _is_concept_title(title: str) -> bool:
    title_lower = title.lower()
    return any(re.search(p, title_lower) for p in _CONCEPT_TITLE_PATTERNS)


def _detect_ccf_venue(journal_ref: str) -> tuple:
    if not journal_ref:
        return None, None
    text_upper = journal_ref.upper()
    for venue in sorted(_CCF_A_VENUES, key=len, reverse=True):
        pat = r"\b" + re.escape(venue.upper()) + r"\b"
        if re.search(pat, text_upper):
            return "A", venue
    for venue in sorted(_CCF_B_VENUES, key=len, reverse=True):
        pat = r"\b" + re.escape(venue.upper()) + r"\b"
        if re.search(pat, text_upper):
            return "B", venue
    return None, None


def _detect_ccf_in_abstract(abstract: str) -> tuple:
    if not abstract:
        return None, None
    pub_pat = r"(accepted|published|appears?|to appear|presented)\s+(at|in|on)\s+(\S.{2,80})"
    for m in re.finditer(pub_pat, abstract, re.IGNORECASE):
        context = m.group(3).upper()
        for venue in sorted(_CCF_A_VENUES, key=len, reverse=True):
            if re.search(r"\b" + re.escape(venue.upper()) + r"\b", context):
                return "A", venue
        for venue in sorted(_CCF_B_VENUES, key=len, reverse=True):
            if re.search(r"\b" + re.escape(venue.upper()) + r"\b", context):
                return "B", venue
    return None, None


# ── Batch LLM reviewer ─────────────────────────────────────────

_BATCH_REVIEW_PROMPT = """你是一位顶会审稿人。请对以下 {count} 篇 arXiv 论文进行严格评审。

对每篇论文，只输出一个 JSON 对象。用 JSON 数组包裹所有结果：
[
  {{
    "idx": 论文编号,
    "innovation": "一句话概括核心创新（若非原创算法而是应用调参，标记为「工程性」）",
    "limitation": "最大局限性（如：缺少对比、数据集偏小、假设过强）",
    "rating": 数字1-5,
    "reason": "给这个评级的简要理由（30字以内）"
  }},
  ...
]

评级标准：
5 = 突破性创新，理论+实验双强
4 = 扎实创新，实验充分
3 = 有亮点但局限明显
2 = 增量改进，实验不足
1 = 概念炒作/水文

下面是待审论文：
{papers}"""


def _build_batch_prompt(items: list[NewsItem]) -> str:
    paper_blocks = []
    for i, item in enumerate(items, 1):
        raw = item.raw or {}
        abstract = item.summary
        if "Abstract:" in abstract:
            abstract = abstract.split("Abstract:", 1)[1].strip()
        if len(abstract) > 800:
            abstract = abstract[:800] + "..."
        venue_info = raw.get("arxiv_journal_reference", "") or "未标注"
        paper_blocks.append(
            f"--- 论文 {i} ---\n"
            f"标题：{item.title}\n"
            f"作者数：{raw.get('author_count', '?')}\n"
            f"发表：{venue_info}\n"
            f"摘要：{abstract}"
        )
    return _BATCH_REVIEW_PROMPT.format(
        count=len(items),
        papers="\n\n".join(paper_blocks),
    )


def _call_llm_batch(items: list[NewsItem], cfg: PipelineConfig) -> list[dict[str, Any]]:
    """Call LLM with a batch of papers, return list of review dicts."""
    llm = cfg.config.get("llm", {})
    api_key = llm.get("api_key", "")
    base_url = llm.get("base_url", "https://api.openai.com/v1").rstrip("/")
    model = cfg.config.get("models", {}).get("paper_review") or llm.get("model", "gpt-4o")

    if not api_key or api_key == "sk-...":
        return []

    prompt = _build_batch_prompt(items)

    for attempt in range(2):
        try:
            resp = requests.post(
                f"{base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json", "User-Agent": "Mozilla/5.0",
                },
                json={
                    "model": model,
                    "temperature": 0.3,
                    "max_tokens": 800 * len(items),
                    "messages": [
                        {"role": "system", "content": "你是一位严格的顶会审稿人。只输出 JSON 数组，不要任何额外文字。"},
                        {"role": "user", "content": prompt},
                    ],
                },
                timeout=120,
            )
            resp.raise_for_status()
            msg = resp.json()["choices"][0]["message"]
            content = msg.get("content") or msg.get("reasoning_content") or ""
            # Strip markdown code fences (```json ... ```) some models add
            content = re.sub(r"```(?:json)?\s*", "", content).strip()

            # Extract JSON array from response
            json_match = re.search(r"\[.*\]", content, re.DOTALL)
            if json_match:
                try:
                    reviews = json.loads(json_match.group(0))
                except json.JSONDecodeError:
                    cleaned = re.sub(r",\s*}", "}", json_match.group(0))
                    cleaned = re.sub(r",\s*\]", "]", cleaned)
                    reviews = json.loads(cleaned)
                # Map back to original items by idx
                result = []
                for r in reviews:
                    idx = int(r.get("idx", 0)) - 1
                    if 0 <= idx < len(items):
                        result.append({
                            "title": items[idx].title,
                            "url": items[idx].url,
                            "source": items[idx].source,
                            "innovation": r.get("innovation", ""),
                            "limitation": r.get("limitation", ""),
                            "rating": int(r.get("rating", 2)),
                            "reason": r.get("reason", ""),
                        })
                return result

            # Fallback: try parsing the whole thing
            reviews = json.loads(content)
            result = []
            for i, r in enumerate(reviews):
                if i < len(items):
                    result.append({
                        "title": items[i].title,
                        "url": items[i].url,
                        "source": items[i].source,
                        "innovation": r.get("innovation", ""),
                        "limitation": r.get("limitation", ""),
                        "rating": int(r.get("rating", 2)),
                        "reason": r.get("reason", ""),
                    })
            return result

        except (json.JSONDecodeError, KeyError, ValueError) as e:
            print(f"[Review] Batch JSON parse error (attempt {attempt+1}): {e}")
            time.sleep(1)
        except requests.RequestException as e:
            print(f"[Review] Batch API error (attempt {attempt+1}): {e}")
            time.sleep(2)
    return []


# ── Main review entry ──────────────────────────────────────────

_progress_lock = Lock()

def _heuristic_review(item: NewsItem, ccf_level, ccf_venue, exp_count) -> dict[str, Any] | None:
    """Quick heuristic rating, returns None if should be filtered."""
    title = item.title
    has_experiments = exp_count >= 2
    has_ccf = ccf_level is not None
    min_rating = 3

    if has_ccf and has_experiments:
        rating, reason = 4, f"CCF-{ccf_level}({ccf_venue})+{exp_count}项实验"
    elif has_experiments:
        rating, reason = 3, f"{exp_count}项实验信号"
    elif has_ccf:
        rating, reason = 3, f"CCF-{ccf_level}({ccf_venue})录用"
    else:
        rating, reason = 2, "缺少实验数据/CCF录用"

    if rating < min_rating:
        return None

    return {
        "title": title, "url": item.url, "source": item.source,
        "innovation": f"CCF-{ccf_level}({ccf_venue}) + {exp_count}项实验信号" if has_ccf else f"实验信号{exp_count}项",
        "limitation": "详见原文",
        "rating": rating,
        "reason": reason,
    }


def review_arxiv_papers(items: list[NewsItem], cfg: PipelineConfig) -> list[dict[str, Any]]:
    """Review arXiv papers with batched parallel LLM.

    Flow:
    1. Heuristic filtering (concept titles, experiment signals, CCF detection)
    2. Fast-track papers with CCF+experiments get 4* immediately
    3. Remaining papers batched and sent to LLM in parallel
    4. Results merged and sorted by rating
    """
    review_cfg = cfg.config.get("collectors", {}).get("arxiv_review", {})
    if not review_cfg.get("enabled", False):
        print("[Review] disabled, skipping")
        return []

    min_rating = int(review_cfg.get("min_rating", 3))
    force_llm = review_cfg.get("force_llm", False)
    batch_size = int(review_cfg.get("batch_size", 5))
    llm_concurrency = int(review_cfg.get("llm_concurrency", 3))

    results: list[dict[str, Any]] = []
    llm_queue: list[NewsItem] = []
    filtered_count = 0

    # ── Phase 1: heuristic triage ──
    for item in items:
        title = item.title
        abstract = item.summary or ""
        raw = item.raw or {}

        if _is_concept_title(title) and not _has_strong_experiments(abstract):
            filtered_count += 1
            continue

        ccf_level, ccf_venue = _detect_ccf_venue(raw.get("arxiv_journal_reference", ""))
        if ccf_level is None:
            ccf_level, ccf_venue = _detect_ccf_in_abstract(abstract)

        has_ccf = ccf_level is not None
        has_experiments = _has_strong_experiments(abstract)
        exp_count = sum(1 for p in _STRONG_EXPERIMENT_SIGNALS if re.search(p, abstract.lower()))

        # CCF + experiments → fast-track
        if has_ccf and has_experiments and not force_llm:
            results.append({
                "title": title, "url": item.url, "source": item.source,
                "innovation": f"CCF-{ccf_level}({ccf_venue})+{exp_count}项实验",
                "limitation": "详见原文",
                "rating": 4,
                "reason": f"CCF-{ccf_level}+{exp_count}项实验信号",
            })
            continue

        # No LLM at all → heuristic only
        if not force_llm:
            r = _heuristic_review(item, ccf_level, ccf_venue, exp_count)
            if r:
                results.append(r)
            else:
                filtered_count += 1
            continue

        # LLM enabled → queue for batch review
        llm_queue.append(item)

    # ── Phase 2: parallel batched LLM review ──
    if force_llm and llm_queue:
        print(f"[Review] LLM review: {len(llm_queue)} papers in batches of {batch_size}, concurrency={llm_concurrency}")

        # Split into batches
        batches = [llm_queue[i:i + batch_size] for i in range(0, len(llm_queue), batch_size)]
        total_batches = len(batches)
        completed_batches = 0

        def review_batch(batch: list[NewsItem], batch_idx: int) -> list[dict]:
            nonlocal completed_batches
            reviews = _call_llm_batch(batch, cfg)
            if not reviews:
                fallback = []
                for item in batch:
                    abstract = item.summary or ""
                    raw = item.raw or {}
                    ccl, ccv = _detect_ccf_venue(raw.get("arxiv_journal_reference", ""))
                    if ccl is None:
                        ccl, ccv = _detect_ccf_in_abstract(abstract)
                    ec = sum(1 for p in _STRONG_EXPERIMENT_SIGNALS if re.search(p, abstract.lower()))
                    r = _heuristic_review(item, ccl, ccv, ec)
                    if r:
                        fallback.append(r)
                with _progress_lock:
                    completed_batches += 1
                    print(f"  [{completed_batches}/{total_batches}] batch {batch_idx+1}: LLM failed, heuristic fallback ({len(fallback)} papers)")
                return fallback

            with _progress_lock:
                completed_batches += 1
                ratings = [r.get("rating", "?") for r in reviews]
                print(f"  [{completed_batches}/{total_batches}] batch {batch_idx+1}: {len(reviews)} papers rated {ratings}")
            return reviews

        with ThreadPoolExecutor(max_workers=llm_concurrency) as executor:
            futures = {executor.submit(review_batch, batch, i): i for i, batch in enumerate(batches)}
            for future in as_completed(futures):
                batch_results = future.result()
                for r in batch_results:
                    if int(r.get("rating", 2)) >= min_rating:
                        results.append(r)
                    else:
                        filtered_count += 1

    # ── Phase 3: sort and return ──
    print(f"\n[Review] done: {len(results)} >= {min_rating}*, filtered {filtered_count}")
    results.sort(key=lambda r: r["rating"], reverse=True)
    return results
