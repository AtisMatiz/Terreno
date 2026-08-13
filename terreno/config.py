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
    def zona_melhor(self) -> list[str]:
        return self._loc.get("zona_melhor") or []

    @property
    def zona_boa(self) -> list[str]:
        return self._loc.get("zona_boa") or []

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


def _merge(base: dict, over: dict) -> dict:
    """Deep-merge `over` into `base`, without mutating either."""
    out = dict(base)
    for key, value in (over or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load_criteria(path: str | Path | None = None) -> Criteria:
    path = Path(path or os.getenv("TERRENO_CRITERIA", ROOT / "criteria.yaml"))
    with open(path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)

    # TERRENO_OVERRIDES lets the page trigger a run with different location,
    # area or price without committing a file first. Malformed JSON is ignored
    # with a warning rather than failing the run — a bad form submission should
    # not stop the scheduled search.
    override = env("TERRENO_OVERRIDES")
    if override:
        import json
        import logging
        try:
            raw = _merge(raw, json.loads(override))
            logging.getLogger("terreno.config").info(
                "critérios sobrescritos via TERRENO_OVERRIDES")
        except ValueError as exc:
            logging.getLogger("terreno.config").warning(
                "TERRENO_OVERRIDES inválido, ignorado: %s", exc)
    return Criteria(raw)


def salvar_criterios(criteria: Criteria, path: str | Path | None = None) -> None:
    """Persist the effective criteria back to disk, so a run launched from the
    page becomes the new default instead of reverting on the next cron."""
    path = Path(path or os.getenv("TERRENO_CRITERIA", ROOT / "criteria.yaml"))
    with open(path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(criteria.raw, fh, allow_unicode=True, sort_keys=False)


# ---------------------------------------------------------------- segredos
def _carregar_dotenv() -> None:
    """Lê `.env` da raiz do projeto para o ambiente, uma vez, no import.

    Antes disto só o `scripts/run_local.sh` carregava o `.env` (via `set -a`),
    então rodar `python -m terreno.run` direto — exatamente o que o README
    manda fazer no Quick start, logo depois de mandar criar o `.env` —
    ignorava o arquivo inteiro e a fonte reclamava de credencial faltando com
    a credencial ali no disco. Carregar aqui vale para qualquer forma de
    invocar o pipeline, que é o único jeito de a promessa do README ser
    verdadeira.

    `setdefault`, não sobrescrita: variáveis já presentes no ambiente ganham.
    No GitHub Actions os segredos chegam como variáveis de ambiente e não há
    `.env` nenhum, e um `.env` esquecido no disco nunca deve silenciosamente
    substituir um segredo passado de propósito.
    """
    caminho = ROOT / ".env"
    try:
        texto = caminho.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        # Ausente é o caso normal (CI). Ilegível merece aviso, mas não pode
        # derrubar o import: um .env salvo como RTF pelo TextEdit, por
        # exemplo, falha aqui e não deve levar o programa junto.
        return
    for linha in texto.splitlines():
        linha = linha.strip()
        if not linha or linha.startswith("#") or "=" not in linha:
            continue
        chave, _, valor = linha.partition("=")
        chave, valor = chave.strip(), valor.strip()
        # `export FOO=bar` também é forma comum de escrever um .env.
        if chave.startswith("export "):
            chave = chave[len("export "):].strip()
        if len(valor) >= 2 and valor[0] == valor[-1] and valor[0] in "\"'":
            valor = valor[1:-1]
        if chave:
            os.environ.setdefault(chave, valor)


_carregar_dotenv()


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def llm_enabled() -> bool:
    return env("ENABLE_LLM") in ("1", "true", "yes") and bool(env("ANTHROPIC_API_KEY"))
