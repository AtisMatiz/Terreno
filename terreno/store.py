"""SQLite persistence: listing history, price changes, geocode cache, budgets.

The database is committed to the repo. That is what makes "new since last run"
and price-drop tracking work without any server.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

from .models import Listing

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    key             TEXT PRIMARY KEY,
    source          TEXT NOT NULL,
    source_id       TEXT,
    url             TEXT NOT NULL,
    title           TEXT,
    description     TEXT,
    price           REAL,
    price_first     REAL,
    area_ha         REAL,
    price_per_ha    REAL,
    municipality    TEXT,
    uf              TEXT,
    lat             REAL,
    lon             REAL,
    image           TEXT,
    posted_at       TEXT,
    score           REAL DEFAULT 0,
    reasons         TEXT,
    dimensoes       TEXT,
    first_seen      TEXT NOT NULL,
    last_seen       TEXT NOT NULL,
    dismissed       INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_listings_last_seen ON listings(last_seen);
CREATE INDEX IF NOT EXISTS idx_listings_uf ON listings(uf);

CREATE TABLE IF NOT EXISTS price_history (
    key       TEXT NOT NULL,
    seen_at   TEXT NOT NULL,
    price     REAL,
    PRIMARY KEY (key, seen_at)
);

CREATE TABLE IF NOT EXISTS geocache (
    query TEXT PRIMARY KEY,
    lat   REAL,
    lon   REAL,
    ts    TEXT
);

-- Spend ledger for the metered free tiers (Brave queries, Apify credits).
CREATE TABLE IF NOT EXISTS budget_ledger (
    resource TEXT NOT NULL,
    month    TEXT NOT NULL,     -- YYYY-MM
    used     REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (resource, month)
);

CREATE TABLE IF NOT EXISTS runs (
    started_at TEXT PRIMARY KEY,
    summary    TEXT
);

-- Tracks whether each source is actually producing results, run over run, so
-- a source silently blocked for weeks (Caixa's CAPTCHA wall, PGFN's timeout)
-- surfaces as a Telegram alert instead of requiring someone to read a workflow
-- log by hand -- which is exactly how both of those were found tonight.
CREATE TABLE IF NOT EXISTS source_health (
    source                TEXT PRIMARY KEY,
    consecutive_failures  INTEGER NOT NULL DEFAULT 0,
    last_ok               TEXT,
    last_checked          TEXT NOT NULL
);

-- Candidatos do Brave que o orçamento de tempo não deixou visitar. Sem isso,
-- toda execução redescobre as mesmas ~800 páginas e visita só as primeiras
-- ~30 dentro do prazo, sempre as mesmas -- a fila persistente é o que garante
-- que o resto seja alcançado ao longo de várias execuções, em vez de nunca.
CREATE TABLE IF NOT EXISTS brave_pendentes (
    url            TEXT PRIMARY KEY,
    dica           TEXT,
    descoberto_em  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_brave_pendentes_descoberto
    ON brave_pendentes(descoberto_em);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


class Store:
    def __init__(self, path: str | Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self) -> None:
        self.db.commit()
        self.db.close()

    # ------------------------------------------------------------- listings
    def upsert(self, listing: Listing) -> Listing:
        """Insert or refresh a listing, annotating it with new/price-drop state."""
        row = self.db.execute(
            "SELECT first_seen, price, price_first FROM listings WHERE key = ?",
            (listing.key,),
        ).fetchone()
        now = _now()

        if row is None:
            listing.is_new = True
            listing.first_seen = now
            listing.price_first = listing.price
        else:
            listing.is_new = False
            listing.first_seen = row["first_seen"]
            listing.price_first = row["price_first"] if row["price_first"] else listing.price
            if listing.price and listing.price_first and listing.price != row["price"]:
                listing.price_drop = round(listing.price - listing.price_first, 2)
        listing.last_seen = now

        d = listing.to_row()
        self.db.execute(
            """
            INSERT INTO listings (key, source, source_id, url, title, description,
                price, price_first, area_ha, price_per_ha, municipality, uf, lat, lon,
                image, posted_at, score, reasons, dimensoes, first_seen, last_seen)
            VALUES (:key, :source, :source_id, :url, :title, :description,
                :price, :price_first, :area_ha, :price_per_ha, :municipality, :uf,
                :lat, :lon, :image, :posted_at, :score, :reasons, :dimensoes,
                :first_seen, :last_seen)
            ON CONFLICT(key) DO UPDATE SET
                url=excluded.url, title=excluded.title, description=excluded.description,
                price=excluded.price, area_ha=excluded.area_ha,
                price_per_ha=excluded.price_per_ha, municipality=excluded.municipality,
                uf=excluded.uf, lat=excluded.lat, lon=excluded.lon, image=excluded.image,
                posted_at=excluded.posted_at, score=excluded.score,
                reasons=excluded.reasons, dimensoes=excluded.dimensoes,
                last_seen=excluded.last_seen
            """,
            d,
        )
        if listing.price:
            self.db.execute(
                "INSERT OR REPLACE INTO price_history (key, seen_at, price) VALUES (?,?,?)",
                (listing.key, now[:10], listing.price),
            )
        return listing

    def recent(self, keep_days: int) -> list[sqlite3.Row]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
        return self.db.execute(
            "SELECT * FROM listings WHERE first_seen >= ? AND dismissed = 0 "
            "ORDER BY score DESC, first_seen DESC",
            (cutoff,),
        ).fetchall()

    def price_series(self, key: str) -> list[tuple[str, float]]:
        rows = self.db.execute(
            "SELECT seen_at, price FROM price_history WHERE key = ? ORDER BY seen_at",
            (key,),
        ).fetchall()
        return [(r["seen_at"], r["price"]) for r in rows]

    def seen_urls(self) -> set[str]:
        """URLs already stored — Layer B skips re-fetching these."""
        return {r["url"] for r in self.db.execute("SELECT url FROM listings")}

    # ------------------------------------------------------------- geocache
    def geocode_cached(self, query: str):
        row = self.db.execute(
            "SELECT lat, lon FROM geocache WHERE query = ?", (query,)
        ).fetchone()
        return (row["lat"], row["lon"]) if row else None

    def geocode_put(self, query: str, lat, lon) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO geocache (query, lat, lon, ts) VALUES (?,?,?,?)",
            (query, lat, lon, _now()),
        )
        self.db.commit()

    # -------------------------------------------------------------- budgets
    def budget_used(self, resource: str) -> float:
        row = self.db.execute(
            "SELECT used FROM budget_ledger WHERE resource = ? AND month = ?",
            (resource, _month()),
        ).fetchone()
        return float(row["used"]) if row else 0.0

    def budget_spend(self, resource: str, amount: float = 1.0) -> None:
        self.db.execute(
            """INSERT INTO budget_ledger (resource, month, used) VALUES (?,?,?)
               ON CONFLICT(resource, month) DO UPDATE SET used = used + excluded.used""",
            (resource, _month(), amount),
        )
        self.db.commit()

    def budget_remaining(self, resource: str, monthly_cap: float) -> float:
        return max(0.0, monthly_cap - self.budget_used(resource))

    # ----------------------------------------------------------------- runs
    def record_run(self, summary: str) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO runs (started_at, summary) VALUES (?,?)",
            (_now(), summary),
        )
        self.db.commit()

    # --------------------------------------------------------- source_health
    def health_update(self, source: str, ok: bool) -> int:
        """Record this run's outcome for `source`; returns the new
        consecutive-failure streak (0 when `ok`)."""
        agora = _now()
        if ok:
            self.db.execute(
                """INSERT INTO source_health (source, consecutive_failures, last_ok, last_checked)
                   VALUES (?, 0, ?, ?)
                   ON CONFLICT(source) DO UPDATE SET
                     consecutive_failures = 0, last_ok = excluded.last_ok,
                     last_checked = excluded.last_checked""",
                (source, agora, agora),
            )
            self.db.commit()
            return 0

        row = self.db.execute(
            "SELECT consecutive_failures FROM source_health WHERE source = ?", (source,)
        ).fetchone()
        novo = (row["consecutive_failures"] + 1) if row else 1
        self.db.execute(
            """INSERT INTO source_health (source, consecutive_failures, last_ok, last_checked)
               VALUES (?, ?, NULL, ?)
               ON CONFLICT(source) DO UPDATE SET
                 consecutive_failures = excluded.consecutive_failures,
                 last_checked = excluded.last_checked""",
            (source, novo, agora),
        )
        self.db.commit()
        return novo

    # ------------------------------------------------------ brave_pendentes
    def brave_pendentes_carregar(self, limite: int) -> list[tuple[str, str]]:
        """Candidatos ainda não visitados, mais antigos primeiro -- é isso que
        garante que o backlog eventualmente seja coberto em vez de crescer
        para sempre atrás dos candidatos recém-descobertos."""
        linhas = self.db.execute(
            "SELECT url, dica FROM brave_pendentes ORDER BY descoberto_em ASC LIMIT ?",
            (limite,),
        ).fetchall()
        return [(r["url"], r["dica"] or "") for r in linhas]

    def brave_pendentes_adicionar(self, candidatos: dict[str, str]) -> None:
        """Registra candidatos ainda não visitados. INSERT OR IGNORE preserva
        o descoberto_em original de quem já estava na fila -- se sobrescrevesse
        a data, perderia a ordem FIFO a cada execução."""
        if not candidatos:
            return
        agora = _now()
        self.db.executemany(
            "INSERT OR IGNORE INTO brave_pendentes (url, dica, descoberto_em) VALUES (?, ?, ?)",
            [(url, dica, agora) for url, dica in candidatos.items()],
        )
        self.db.commit()

    def brave_pendentes_remover(self, urls) -> None:
        """Remove candidatos já tentados (com sucesso ou não) -- não retentamos
        indefinidamente uma página que não deu em nada."""
        urls = list(urls)
        if not urls:
            return
        self.db.executemany(
            "DELETE FROM brave_pendentes WHERE url = ?", [(u,) for u in urls]
        )
        self.db.commit()

    def brave_pendentes_total(self) -> int:
        return self.db.execute(
            "SELECT COUNT(*) AS n FROM brave_pendentes"
        ).fetchone()["n"]

    def brave_pendentes_podar(self, maximo: int) -> int:
        """Descarta os candidatos mais antigos além do limite, para a fila não
        crescer sem parar se a descoberta correr mais rápido que a visita."""
        excesso = self.brave_pendentes_total() - maximo
        if excesso <= 0:
            return 0
        self.db.execute(
            "DELETE FROM brave_pendentes WHERE url IN "
            "(SELECT url FROM brave_pendentes ORDER BY descoberto_em ASC LIMIT ?)",
            (excesso,),
        )
        self.db.commit()
        return excesso
