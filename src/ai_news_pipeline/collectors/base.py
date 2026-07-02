from __future__ import annotations

from abc import ABC, abstractmethod

from ai_news_pipeline.models import NewsItem


class BaseCollector(ABC):
    name: str = "base"

    @abstractmethod
    def collect(self) -> list[NewsItem]:
        ...
