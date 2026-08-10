"""The one shape every source normalizes into."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Listing:
    source: str                     # "olx", "vivareal", "brave", "facebook", ...
    url: str
    title: str = ""
    description: str = ""
    price: float | None = None      # BRL
    area_ha: float | None = None
    municipality: str = ""
    uf: str = ""
    lat: float | None = None
    lon: float | None = None
    image: str = ""                 # hotlinked thumbnail, never downloaded
    source_id: str = ""             # portal's own id, when exposed
    posted_at: str = ""             # portal's publication date, when exposed

    # Filled by the pipeline, not by sources.
    price_per_ha: float | None = None
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)
    first_seen: str = field(default_factory=_now)
    last_seen: str = field(default_factory=_now)
    price_first: float | None = None
    is_new: bool = False
    price_drop: float | None = None  # negative = cheaper than first seen

    @property
    def key(self) -> str:
        """Stable identity for this exact listing at this source."""
        basis = f"{self.source}:{self.source_id}" if self.source_id else self.url
        return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]

    @property
    def fuzzy_key(self) -> str:
        """Identity across sources, to catch the same plot cross-posted.

        Price is bucketed to 5k and area to 0.5 ha so that small edits between
        one portal and another still collapse to a single card.
        """
        price_bucket = int((self.price or 0) / 5000)
        area_bucket = int((self.area_ha or 0) * 2)
        muni = re.sub(r"[^a-z]", "", (self.municipality or "").lower())
        return f"{self.uf.lower()}|{muni}|{price_bucket}|{area_bucket}"

    def to_row(self) -> dict:
        d = asdict(self)
        d["reasons"] = "\n".join(self.reasons)
        d["key"] = self.key
        return d

    def to_json(self) -> dict:
        d = asdict(self)
        d["key"] = self.key
        return d
