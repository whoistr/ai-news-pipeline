from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class NewsItem:
    title: str
    url: str
    source: str
    published_at: str | None = None
    summary: str = ""
    score: float = 0.0
    tags: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> NewsItem:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
