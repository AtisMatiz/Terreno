"""Criteria loading and environment access."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.getenv("TERRENO_DB", ROOT / "data" / "terreno.sqlite3"))
SITE_DIR = Path(os.getenv("TERRENO_SITE", ROOT / "site"))


@dataclass
class Criteria:
    raw: dict

    @property
    def states(self) -> list[str]:
        return [s.upper() for s in self.raw["location"].get("states", [])]

    @property
    def municipalities(self) -> list[str]:
        return self.raw["location"].get("municipalities") or []

    @property
    def center(self) -> str | None:
        return self.raw["location"].get("center")

    @property
    def radius_km(self) -> float | None:
        return self.raw["location"].get("radius_km")

    @property
    def price_min(self) -> float:
        return float(self.raw["price"].get("min") or 0)

    @property
    def price_max(self) -> float:
        return float(self.raw["price"].get("max") or 10**12)

    @property
    def area_min(self) -> float:
        return float(self.raw["area"].get("min_ha") or 0)

    @property
    def area_max(self) -> float:
        return float(self.raw["area"].get("max_ha") or 10**9)

    @property
    def max_price_per_ha(self) -> float | None:
        v = self.raw.get("max_price_per_ha")
        return float(v) if v else None

    @property
    def must_have(self) -> list[str]:
        return self.raw.get("must_have") or []

    @property
    def nice_to_have(self) -> list[str]:
        return self.raw.get("nice_to_have") or []

    @property
    def deal_breakers(self) -> list[str]:
        return self.raw.get("deal_breakers") or []

    def profile(self, name: str | None) -> list[str] | None:
        """Source names for a named profile, or None when unset."""
        if not name:
            return None
        profiles = self.raw.get("profiles") or {}
        return profiles.get(name)

    def source_enabled(self, name: str) -> bool:
        return bool((self.raw.get("sources") or {}).get(name, False))

    def budget(self, name: str, default=0):
        return (self.raw.get("budgets") or {}).get(name, default)

    def output(self, name: str, default=None):
        return (self.raw.get("output") or {}).get(name, default)


def load_criteria(path: str | Path | None = None) -> Criteria:
    path = Path(path or os.getenv("TERRENO_CRITERIA", ROOT / "criteria.yaml"))
    with open(path, "r", encoding="utf-8") as fh:
        return Criteria(yaml.safe_load(fh))


# ------------------------------------------------------------------ secrets
def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def llm_enabled() -> bool:
    return env("ENABLE_LLM") in ("1", "true", "yes") and bool(env("ANTHROPIC_API_KEY"))
