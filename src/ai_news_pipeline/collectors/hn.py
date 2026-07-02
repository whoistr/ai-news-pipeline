from __future__ import annotations

from datetime import datetime, timedelta, timezone

import requests

from ai_news_pipeline.collectors.base import BaseCollector
from ai_news_pipeline.config import PipelineConfig
from ai_news_pipeline.models import NewsItem


class HackerNewsCollector(BaseCollector):
    name = "hackernews"

    def __init__(self, cfg: PipelineConfig, date_str: str | None = None) -> None:
        self.cfg = cfg
        coll = cfg.config.get("collectors", {})
        self.queries: list[str] = coll.get("hn_queries", ["AI"])
        self.max_per_query: int = int(coll.get("hn_max_per_query", 5))
        self.hours_back: int = int(coll.get("hn_hours_back", 48))
        self.date_str = date_str

    def collect(self) -> list[NewsItem]:
        if self.date_str:
            start_utc, end_utc = self.cfg.date_window(self.date_str)
        else:
            cutoff_dt = datetime.now(timezone.utc) - timedelta(hours=self.hours_back)
            start_utc, end_utc = cutoff_dt, datetime.now(timezone.utc)
        items: list[NewsItem] = []
        seen_urls: set[str] = set()

        for query in self.queries:
            try:
                batch = self._search(query, start_utc, end_utc)
            except requests.RequestException as exc:
                print(f"[HN] 查询失败 ({query}): {exc}")
                continue
            for item in batch:
                if item.url in seen_urls:
                    continue
                seen_urls.add(item.url)
                items.append(item)

        return items

    def _search(self, query: str, start_utc: datetime, end_utc: datetime) -> list[NewsItem]:
        url = "https://hn.algolia.com/api/v1/search"
        params = {
            "query": query,
            "tags": "story",
            "hitsPerPage": self.max_per_query,
        }
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        hits = resp.json().get("hits", [])

        results: list[NewsItem] = []
        for hit in hits:
            created = datetime.fromtimestamp(int(hit.get("created_at_i", 0)), tz=timezone.utc)
            if created < start_utc or created >= end_utc:
                continue

            link = hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID')}"
            title = (hit.get("title") or "").strip()
            if not title:
                continue

            results.append(
                NewsItem(
                    title=title,
                    url=link,
                    source="Hacker News",
                    published_at=created.isoformat(),
                    summary=f"HN points: {hit.get('points', 0)} | comments: {hit.get('num_comments', 0)}",
                    tags=["hackernews", query.lower()],
                    raw={"hn_id": hit.get("objectID"), "points": hit.get("points", 0)},
                )
            )
        return results
