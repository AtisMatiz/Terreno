"""The one shape every source normalizes into."""

from __future__ import annotations

import hashlib
import json
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

    # Alqueire as the listing actually stated it. The alqueire is regionally
    # ambiguous (paulista 2.42 ha, mineiro/geral 4.84 ha -- a factor of two),
    # so when the variant cannot be determined from the UF the area is NOT
    # converted: `area_ha` stays None and these carry the honest figure
    # instead. A doubled area silently corrupts price-per-hectare and the area
    # filter, which is worse than admitting the unit is unknown.
    area_alqueires: float | None = None
    area_alqueire_tipo: str = ""    # "paulista" | "mineiro" | "norte" | "" if unknown

    # Filled by the pipeline, not by sources.
    price_per_ha: float | None = None
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)
    # Per-dimension breakdown from terreno.scoring, so the page can show why.
    dimensoes: dict = field(default_factory=dict)
    # Straight-line km to `localizacao.centro` (Monteiro Lobato). None when the
    # listing's municipality could not be geocoded -- treated as neutral by the
    # scorer, never as a penalty, since silence is not evidence of distance.
    distancia_centro_km: float | None = None
    # Short per-theme strings from the scorer, for the notification card. Keys
    # are only present when there is real evidence; absent means unknown, and
    # nothing downstream may substitute filler for a missing key.
    destaques: dict = field(default_factory=dict)
    # Standout features worth flagging without scoring them -- "cachoeira" is
    # explicitly not a requirement but is wanted highlighted when present.
    estrelas: list[str] = field(default_factory=list)
    # "disponivel" | "indisponivel" | "desconhecido". A network failure yields
    # "desconhecido", never "indisponivel": a transient outage must not delete
    # a good listing.
    disponibilidade: str = "desconhecido"
    # False when the listing is worth publishing on the site but not worth a
    # Telegram ping (currently: price per hectare above the notify ceiling).
    notificavel: bool = True
    # Vision read of the listing photos. Only populated for high scorers --
    # see terreno.extract.imagem -- so the token cost tracks the shortlist,
    # not the whole crawl.
    imagem_analise: dict = field(default_factory=dict)
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
        d["dimensoes"] = json.dumps(self.dimensoes, ensure_ascii=False)
        d["key"] = self.key
        return d

    def to_json(self) -> dict:
        d = asdict(self)
        d["key"] = self.key
        return d
