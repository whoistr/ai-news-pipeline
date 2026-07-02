from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import re as _re

import feedparser
import requests

from ai_news_pipeline.collectors.base import BaseCollector
from ai_news_pipeline.config import PipelineConfig
from ai_news_pipeline.models import NewsItem


class RssCollector(BaseCollector):
    name = "rss"

    def __init__(self, cfg: PipelineConfig, date_str: str | None = None) -> None:
        self.cfg = cfg
        coll = cfg.config.get("collectors", {})
        self.feeds: list[dict] = coll.get("rss_feeds", [])
        self.max_age_hours: int = int(coll.get("max_age_hours", 72))
        self.date_str = date_str

    def collect(self) -> list[NewsItem]:
        if self.date_str:
            start_utc, end_utc = self.cfg.date_window(self.date_str)
        else:
            start_utc = datetime.now(timezone.utc) - timedelta(hours=self.max_age_hours)
            end_utc = datetime.now(timezone.utc)
        items: list[NewsItem] = []
        seen_urls: set[str] = set()

        for feed_cfg in self.feeds:
            title = feed_cfg.get("title", "RSS")
            url = feed_cfg.get("url")
            weight = float(feed_cfg.get("weight", 1.0))
            if not url:
                continue
            try:
                batch = self._fetch_feed(title, url, weight, start_utc, end_utc, feed_cfg)
            except Exception as exc:  # noqa: BLE001 — feed 源多样，统一降级
                print(f"[RSS] {title} 采集失败: {exc}")
                continue
            for item in batch:
                if item.url in seen_urls:
                    continue
                seen_urls.add(item.url)
                items.append(item)

        return items

    def _is_arxiv_feed(self, feed_url: str) -> bool:
        return "arxiv.org" in feed_url

    def _load_arxiv_filter(self):
        coll = self.cfg.config.get("collectors", {})
        af = coll.get("arxiv_quality_filter", {})
        if not af.get("enabled", True):
            return None
        return {
            "cross_list_only": af.get("cross_list_only", True),
            "min_authors": int(af.get("min_authors", 2)),
            "min_abstract_chars": int(af.get("min_abstract_chars", 300)),
            "max_per_feed": int(af.get("max_per_feed", 15)),
            "hype_patterns": af.get("hype_title_patterns", []),
        }

    def _fetch_feed(
        self, source_title: str, feed_url: str, weight: float,
        start_utc: datetime, end_utc: datetime,
        feed_cfg: dict | None = None,
    ) -> list[NewsItem]:
        resp = requests.get(feed_url, timeout=30, headers={"User-Agent": "ai-news-pipeline/0.1"})
        resp.raise_for_status()
        parsed = feedparser.parse(resp.content)

        arxiv_filter = self._load_arxiv_filter() if self._is_arxiv_feed(feed_url) else None

        results: list[NewsItem] = []
        for entry in parsed.entries:
            link = getattr(entry, "link", "") or ""
            title = getattr(entry, "title", "") or ""
            if not link or not title:
                continue

            published = self._parse_date(entry)
            if published and (published < start_utc or published >= end_utc):
                continue

            # ── ArXiv quality filtering ──
            if arxiv_filter:
                announce_type = getattr(entry, "arxiv_announce_type", "") or ""

                # Filter 1: cross-list only
                if arxiv_filter["cross_list_only"] and announce_type != "cross":
                    continue

                # Filter 2: minimum authors
                author_count = 0
                if hasattr(entry, "author") and entry.author:
                    author_count = entry.author.count(",") + 1
                elif hasattr(entry, "authors"):
                    author_count = len(entry.authors)
                if author_count < arxiv_filter["min_authors"]:
                    continue

                # Filter 3: minimum abstract length
                abstract = ""
                if hasattr(entry, "summary"):
                    abstract = entry.summary or ""
                    if "Abstract:" in abstract:
                        abstract = abstract.split("Abstract:", 1)[1].strip()
                if len(abstract) < arxiv_filter["min_abstract_chars"]:
                    continue

                # Filter 4: concept-hype title keywords and buzzword patterns
                title_lower = title.lower()

                # 4a: concept/vision/future/survey title (only if no experiments)
                concept_patterns = [
                    r"\bconcept\b", r"\bvision\b", r"\bfuture\b", r"\bsurvey\b",
                    r"\broadmap\b", r"\btaxonomy\b", r"\bprospect\b", r"\blandscape\b",
                ]
                if any(_re.search(p, title_lower) for p in concept_patterns):
                    exp_signals = [
                        r"\d{1,3}\.\d+%", r"\b(ablation|outperform|SOTA|beats?|achieve[sd]?)\b",
                        r"\b(accuracy|F1|BLEU|precision)\s*(?:of\s*)?\d+\.?\d*%?",
                    ]
                    abstract_lower = abstract.lower()
                    has_experiments = sum(1 for s in exp_signals if _re.search(s, abstract_lower)) >= 2
                    if not has_experiments:
                        continue

                # 4b: hype/buzzword patterns
                skip = False
                for pat in arxiv_filter.get("hype_patterns", []):
                    if _re.search(pat.lower(), title_lower):
                        skip = True
                        break
                if skip:
                    continue
            # ── end ArXiv filtering ──

            summary = ""
            if hasattr(entry, "summary"):
                max_len = 2000 if arxiv_filter else 500
                summary = entry.summary[:max_len]
            elif hasattr(entry, "description"):
                summary = entry.description[:500]

            # feed_type must be read BEFORE the X cleaning block uses it
            feed_type = feed_cfg.get("feed_type", "")

            # X/Twitter content needs special cleaning: nitter RSS wraps tweets in
            # HTML with <p>/<a>/<video> tags, emoji noise, and bare links. Strip it
            # to plain text and drop low-info tweets (just a link, or <15 real chars).
            is_x = feed_type in ("x-official", "x-kol")
            if is_x:
                # Strip RT/R prefixes for scoring/display (keep raw title for dedup)
                clean_title = _re.sub(r"^RT by @\S+:\s*", "", title)
                clean_title = _re.sub(r"^R to @\S+:\s*", "", clean_title)
                if clean_title != title:
                    title = clean_title  # use cleaned title going forward
                # Clean summary: HTML -> text, strip bare links/emoji noise
                summary = _re.sub(r"<[^>]+>", " ", summary)  # strip HTML tags
                summary = summary.replace("&amp;", "&")
                # Count meaningful content (exclude bare URLs and short links)
                text_only = _re.sub(r"https?://\S+", "", summary).strip()
                text_only = _re.sub(r"[\U0001f000-\U0001ffff]", "", text_only).strip()
                if len(text_only) < 15:
                    # Tweet is essentially just a link/emoji with no substance
                    continue

            arxiv_tags = ["arxiv"] if arxiv_filter else []
            arxiv_raw = self._build_raw(entry, weight, arxiv_filter)
            # Propagate feed-level category (e.g. "semiconductor") for downstream splitting
            feed_category = feed_cfg.get("category", "")
            # purpose: publish (default) | learning | both ? drives dual-mode split
            feed_purpose = feed_cfg.get("purpose", "publish")
            # feed_type already read above (before X cleaning)
            # retweets: nitter RSS prefixes retweets with "RT by @xxx:". Default
            # include them, but KOL feeds (e.g. LeCun) can set retweets:false to
            # drop pure retweets and keep only original insight ? they otherwise
            # drown the feed in off-topic political reposts.
            include_retweets = feed_cfg.get("retweets", True)
            if not include_retweets and (title.startswith("RT by @") or title.startswith("R to @")):
                continue
            item_tags = ["rss", source_title.lower().replace(" ", "-"), *arxiv_tags]
            if feed_category:
                item_tags.append(feed_category)
            if feed_type:
                item_tags.append(feed_type)
            arxiv_raw["feed_purpose"] = feed_purpose
            arxiv_raw["feed_type"] = feed_type
            results.append(
                NewsItem(
                    title=title.strip(),
                    url=link.strip(),
                    source=source_title,
                    published_at=published.isoformat() if published else None,
                    summary=summary,
                    score=weight,
                    tags=item_tags,
                    raw=arxiv_raw,
                )
            )
        # Volume cap for arXiv feeds
        if arxiv_filter:
            max_items = arxiv_filter["max_per_feed"]
            if len(results) > max_items:
                results = results[:max_items]

        return results

    @staticmethod
    def _build_raw(entry, weight: float, arxiv_filter: dict | None) -> dict:
        raw = {"feed_weight": weight}
        if arxiv_filter:
            raw["arxiv_announce_type"] = getattr(entry, "arxiv_announce_type", "")
            raw["arxiv_doi"] = getattr(entry, "arxiv_doi", "")
            if hasattr(entry, "author") and entry.author:
                raw["author_count"] = entry.author.count(",") + 1
        return raw

    @staticmethod
    def _parse_date(entry) -> datetime | None:
        for attr in ("published_parsed", "updated_parsed"):
            parsed = getattr(entry, attr, None)
            if parsed:
                return datetime(*parsed[:6], tzinfo=timezone.utc)
        for attr in ("published", "updated"):
            raw = getattr(entry, attr, None)
            if raw:
                try:
                    dt = parsedate_to_datetime(raw)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    return dt.astimezone(timezone.utc)
                except (TypeError, ValueError):
                    continue
        return None
