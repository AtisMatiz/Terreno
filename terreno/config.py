"""Carregamento dos critérios e acesso ao ambiente."""

from __future__ import annotations

import os
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.getenv("TERRENO_DB", ROOT / "data" / "terreno.sqlite3"))
SITE_DIR = Path(os.getenv("TERRENO_SITE", ROOT / "site"))
REGIOES_PATH = Path(os.getenv("TERRENO_REGIOES", ROOT / "data" / "regioes.yaml"))


def fold(text: str) -> str:
    """Minúsculas sem acento — usado para comparar nomes de municípios."""
    text = unicodedata.normalize("NFKD", (text or "").lower().strip())
    return "".join(c for c in text if not unicodedata.combining(c))


@dataclass
class Criteria:
    raw: dict

    # ------------------------------------------------------------ localização
    @property
    def _loc(self) -> dict:
        return self.raw.get("localizacao") or {}

    @property
    def states(self) -> list[str]:
        return [s.upper() for s in self._loc.get("estados", [])]

    @property
    def regiao(self) -> str | None:
        return self._loc.get("regiao")

    @property
    def municipalities(self) -> list[str]:
        """Municípios explícitos, ou os da região quando ela é a única pista.

        Deixar `municipios` vazio e informar uma `regiao` busca a região
        inteira; informar os dois restringe aos municípios listados.
        """
        explicit = self._loc.get("municipios") or []
        if explicit:
            return explicit
        return regiao_municipios(self.regiao)

    @property
    def center(self) -> str | None:
        return self._loc.get("centro")

    @property
    def radius_km(self) -> float | None:
        return self._loc.get("raio_km")

    # ------------------------------------------------------------ preço/área
    @property
    def price_min(self) -> float:
        return float((self.raw.get("preco") or {}).get("min") or 0)

    @property
    def price_max(self) -> float:
        return float((self.raw.get("preco") or {}).get("max") or 10**12)

    @property
    def area_min(self) -> float:
        return float((self.raw.get("area") or {}).get("min_ha") or 0)

    @property
    def area_max(self) -> float:
        return float((self.raw.get("area") or {}).get("max_ha") or 10**9)

    @property
    def max_price_per_ha(self) -> float | None:
        v = self.raw.get("max_preco_por_ha")
        return float(v) if v else None

    @property
    def nota_minima(self) -> float:
        return float(self.output("nota_minima", 0.0) or 0.0)

    # ------------------------------------------------------------ fontes
    def profile(self, name: str | None) -> list[str] | None:
        if not name:
            return None
        return (self.raw.get("perfis") or {}).get(name)

    def source_enabled(self, name: str) -> bool:
        return bool((self.raw.get("fontes") or {}).get(name, False))

    def budgets(self) -> dict:
        return self.raw.get("orcamentos") or {}

    def output(self, name: str, default=None):
        return (self.raw.get("saida") or {}).get(name, default)


# ---------------------------------------------------------------- regiões
_regioes_cache: dict | None = None


def _regioes() -> dict:
    global _regioes_cache
    if _regioes_cache is None:
        try:
            with open(REGIOES_PATH, "r", encoding="utf-8") as fh:
                _regioes_cache = yaml.safe_load(fh) or {}
        except FileNotFoundError:
            _regioes_cache = {}
    return _regioes_cache


def regiao_municipios(nome: str | None) -> list[str]:
    """Municípios de uma região nomeada; lista vazia se ela não existir."""
    if not nome:
        return []
    alvo = fold(nome)
    for chave, dados in _regioes().items():
        if fold(chave) == alvo:
            return dados.get("municipios") or []
    return []


def load_criteria(path: str | Path | None = None) -> Criteria:
    path = Path(path or os.getenv("TERRENO_CRITERIA", ROOT / "criteria.yaml"))
    with open(path, "r", encoding="utf-8") as fh:
        return Criteria(yaml.safe_load(fh))


# ---------------------------------------------------------------- segredos
def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def llm_enabled() -> bool:
    return env("ENABLE_LLM") in ("1", "true", "yes") and bool(env("ANTHROPIC_API_KEY"))
