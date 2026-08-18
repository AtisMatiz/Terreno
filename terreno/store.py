"""SQLite persistence: listing history, price changes, geocode cache, budgets.

The database is committed to the repo. That is what makes "new since last run"
and price-drop tracking work without any server.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

from .models import Listing
from .site_categoria import IMOBILIARIA, classificar

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
    area_alqueires  REAL,
    area_alqueire_tipo TEXT,
    distancia_centro_km REAL,
    destaques       TEXT,
    estrelas        TEXT,
    disponibilidade TEXT,
    notificavel     INTEGER DEFAULT 1,
    imagem_analise  TEXT,
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
    descoberto_em  TEXT NOT NULL,
    falhas         INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_brave_pendentes_descoberto
    ON brave_pendentes(descoberto_em);

-- Candidatos que esgotaram `brave_max_falhas` tentativas de visita -- hoje,
-- na prática, wimoveis/imovelweb, que servem um desafio JS da Cloudflare
-- real, não uma instabilidade passageira (ver SESSION_NOTES, 2026-08-13).
-- Antes disso era DELETE puro: a URL (e o crédito da Brave que a achou) se
-- perdia de vez, mesmo sabendo que o bloqueio de hoje pode não ser o de
-- amanhã. Aqui ela fica arquivada -- fora da fila ativa (não se tenta de
-- novo sem decisão explícita), mas pronta para um transporte que funcione,
-- sem precisar gastar a Brave outra vez para redescobrir a mesma URL.
CREATE TABLE IF NOT EXISTS brave_frios (
    url            TEXT PRIMARY KEY,
    dica           TEXT,
    descoberto_em  TEXT NOT NULL,
    motivo         TEXT
);

-- Hosts que a Brave já provou mais de uma vez serem um site especializado em
-- vender imóveis (uma extração de listing real, com preço/área, não uma
-- página qualquer) sem estarem na lista curada à mão em `sites_alvo`. Depois
-- de `limiar` extrações confirmadas (ver brave_visit.SITES_DESCOBERTOS_LIMIAR)
-- o host é "promovido": passa a entrar na mesma rotação de consultas `site:`
-- que os manuais, só que numa cadência semanal própria (`ultima_consulta`),
-- independente de o pipeline como um todo rodar todo dia.
CREATE TABLE IF NOT EXISTS sites_descobertos (
    host            TEXT PRIMARY KEY,
    ocorrencias     INTEGER NOT NULL DEFAULT 0,
    primeira_vez    TEXT NOT NULL,
    ultima_vez      TEXT NOT NULL,
    promovido_em    TEXT,
    ultima_consulta TEXT,
    categoria       TEXT NOT NULL DEFAULT ''
);
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
        self._migrate()
        self.db.commit()

    def _migrate(self) -> None:
        """CREATE TABLE IF NOT EXISTS never alters a table that already
        exists -- and the database is a file committed to the repo, not
        recreated each run, so a new column needs an explicit ALTER TABLE
        for everyone still running the old schema."""
        cols = {r["name"] for r in self.db.execute("PRAGMA table_info(brave_pendentes)")}
        if "falhas" not in cols:
            self.db.execute(
                "ALTER TABLE brave_pendentes ADD COLUMN falhas INTEGER NOT NULL DEFAULT 0"
            )

        # Same reason, for the scoring/notification fields added later. Declared
        # as (nome, tipo) so adding one more is a single line rather than
        # another hand-written ALTER.
        novas = (
            ("area_alqueires", "REAL"),
            ("area_alqueire_tipo", "TEXT"),
            ("distancia_centro_km", "REAL"),
            ("destaques", "TEXT"),
            ("estrelas", "TEXT"),
            ("disponibilidade", "TEXT"),
            ("notificavel", "INTEGER DEFAULT 1"),
            ("imagem_analise", "TEXT"),
        )
        existentes = {r["name"] for r in self.db.execute("PRAGMA table_info(listings)")}
        for nome, tipo in novas:
            if nome not in existentes:
                self.db.execute(f"ALTER TABLE listings ADD COLUMN {nome} {tipo}")

        # 2026-08-17: SDB (sites_descobertos) split into imobiliária vs outro
        # (see site_categoria.py) so each category can be scanned by a
        # different strategy going forward.
        sd_cols = {r["name"] for r in self.db.execute("PRAGMA table_info(sites_descobertos)")}
        if "categoria" not in sd_cols:
            self.db.execute(
                "ALTER TABLE sites_descobertos ADD COLUMN categoria TEXT NOT NULL DEFAULT ''"
            )
        # Backfill hosts already in the table before this column existed --
        # host-only signal (no page text kept around from their original
        # discovery), same as every host's first classification would get.
        sem_categoria = self.db.execute(
            "SELECT host FROM sites_descobertos WHERE categoria = ''"
        ).fetchall()
        if sem_categoria:
            self.db.executemany(
                "UPDATE sites_descobertos SET categoria = ? WHERE host = ?",
                [(classificar(r["host"]), r["host"]) for r in sem_categoria],
            )

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
                image, posted_at, score, reasons, dimensoes, area_alqueires,
                area_alqueire_tipo, distancia_centro_km, destaques, estrelas,
                disponibilidade, notificavel, imagem_analise, first_seen, last_seen)
            VALUES (:key, :source, :source_id, :url, :title, :description,
                :price, :price_first, :area_ha, :price_per_ha, :municipality, :uf,
                :lat, :lon, :image, :posted_at, :score, :reasons, :dimensoes,
                :area_alqueires, :area_alqueire_tipo, :distancia_centro_km,
                :destaques, :estrelas, :disponibilidade, :notificavel,
                :imagem_analise, :first_seen, :last_seen)
            ON CONFLICT(key) DO UPDATE SET
                url=excluded.url, title=excluded.title, description=excluded.description,
                price=excluded.price, area_ha=excluded.area_ha,
                price_per_ha=excluded.price_per_ha, municipality=excluded.municipality,
                uf=excluded.uf, lat=excluded.lat, lon=excluded.lon, image=excluded.image,
                posted_at=excluded.posted_at, score=excluded.score,
                reasons=excluded.reasons, dimensoes=excluded.dimensoes,
                area_alqueires=excluded.area_alqueires,
                area_alqueire_tipo=excluded.area_alqueire_tipo,
                distancia_centro_km=excluded.distancia_centro_km,
                destaques=excluded.destaques, estrelas=excluded.estrelas,
                disponibilidade=excluded.disponibilidade,
                notificavel=excluded.notificavel,
                imagem_analise=excluded.imagem_analise,
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

    def dismiss_many(self, keys: list[str]) -> int:
        """Mark listings as permanently dismissed (e.g. sold -- see
        VENDIDOS_PATH). Idempotent: already-dismissed keys are a no-op, so
        this is safe to call with the same list every run."""
        keys = [k for k in (keys or []) if k]
        if not keys:
            return 0
        antes = self.db.execute(
            "SELECT COUNT(*) FROM listings WHERE dismissed = 0 AND key IN "
            f"({','.join('?' * len(keys))})", keys,
        ).fetchone()[0]
        self.db.executemany(
            "UPDATE listings SET dismissed = 1 WHERE key = ?", [(k,) for k in keys]
        )
        self.db.commit()
        return antes

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

    def brave_pendentes_registrar_falha(self, urls, limiar: int = 2,
                                        motivo: str = "") -> set[str]:
        """Um candidato que não deu para nem baixar (timeout, conexão recusada)
        ganha uma nova chance na próxima execução em vez de ser descartado na
        hora -- pode ter sido uma instabilidade passageira do site. Só depois
        de `limiar` falhas *entre execuções* ele sai da fila ativa -- não mais
        por DELETE (ver `brave_frios` acima): vai para o arquivo frio, de onde
        só volta por decisão explícita, nunca sozinho. Isso é deliberadamente
        separado de uma página que carregou mas não tinha anúncio nenhum: essa
        é descartada (de vez, sem arquivo) na hora, porque visitar de novo não
        vai mudar o que já foi lido com sucesso.

        Retorna as URLs movidas para o frio nesta chamada, para quem chamou logar."""
        urls = list(urls)
        if not urls:
            return set()
        self.db.executemany(
            "UPDATE brave_pendentes SET falhas = falhas + 1 WHERE url = ?",
            [(u,) for u in urls],
        )
        placeholders = ",".join("?" for _ in urls)
        linhas = self.db.execute(
            f"SELECT url, dica, descoberto_em FROM brave_pendentes "
            f"WHERE url IN ({placeholders}) AND falhas >= ?",
            (*urls, limiar),
        ).fetchall()
        descartados = {r["url"] for r in linhas}
        if descartados:
            self.db.executemany(
                "INSERT OR REPLACE INTO brave_frios (url, dica, descoberto_em, motivo) "
                "VALUES (?, ?, ?, ?)",
                [(r["url"], r["dica"], r["descoberto_em"], motivo) for r in linhas],
            )
            self.db.executemany(
                "DELETE FROM brave_pendentes WHERE url = ?", [(u,) for u in descartados]
            )
        self.db.commit()
        return descartados

    def brave_frios_adicionar(self, candidatos: dict[str, str], motivo: str = "") -> int:
        """Registra candidatos direto no arquivo frio, sem passar pela fila
        ativa -- para hosts já sabidos bloqueados hoje (wimoveis/imovelweb),
        não vale gastar uma tentativa de visita real só para redescobrir o
        que já se sabe. Retorna quantos eram genuinamente novos."""
        if not candidatos:
            return 0
        agora = _now()
        cur = self.db.executemany(
            "INSERT OR IGNORE INTO brave_frios (url, dica, descoberto_em, motivo) "
            "VALUES (?, ?, ?, ?)",
            [(url, dica, agora, motivo) for url, dica in candidatos.items()],
        )
        self.db.commit()
        return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

    def brave_frios_total(self) -> int:
        return self.db.execute(
            "SELECT COUNT(*) AS n FROM brave_frios"
        ).fetchone()["n"]

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

    # ----------------------------------------------------- sites_descobertos
    def registrar_extracao_brave(self, host: str, limiar: int = 2, texto: str = "") -> bool:
        """Conta uma extração de listing bem-sucedida da Brave nesse host.
        Quando o total de ocorrências atinge `limiar`, promove o host pela
        primeira vez -- ver a nota da tabela em SCHEMA. Retorna True só na
        chamada em que a promoção acontece, para quem chamou poder logar.

        `texto` (o corpo da página, quando disponível) reclassifica o host
        via `site_categoria.classificar` -- este é o único ponto do pipeline
        em que o texto completo da página já foi buscado, então é a melhor
        chance de pegar uma menção a CRECI que o snippet da busca nunca
        mostraria."""
        agora = _now()
        row = self.db.execute(
            "SELECT ocorrencias, promovido_em, categoria FROM sites_descobertos WHERE host = ?",
            (host,),
        ).fetchone()
        ocorrencias = (row["ocorrencias"] if row else 0) + 1
        ja_promovido = bool(row and row["promovido_em"])
        promovendo_agora = not ja_promovido and ocorrencias >= limiar
        categoria_atual = (row["categoria"] if row else "") or ""
        # Nunca rebaixa uma classificação já forte -- só herda a nova se a
        # atual ainda está em branco ou se o texto agora prova imobiliária.
        categoria = categoria_atual or classificar(host, texto)
        if categoria_atual != IMOBILIARIA and classificar(host, texto) == IMOBILIARIA:
            categoria = IMOBILIARIA
        self.db.execute(
            """INSERT INTO sites_descobertos
                   (host, ocorrencias, primeira_vez, ultima_vez, promovido_em, categoria)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(host) DO UPDATE SET
                 ocorrencias = excluded.ocorrencias,
                 ultima_vez = excluded.ultima_vez,
                 promovido_em = COALESCE(sites_descobertos.promovido_em, excluded.promovido_em),
                 categoria = excluded.categoria""",
            (host, ocorrencias, agora, agora, agora if promovendo_agora else None, categoria),
        )
        self.db.commit()
        return promovendo_agora

    def sites_alvo_semear(self, hosts) -> None:
        """Garante que cada host curado (`criteria.sites_alvo`) participe da
        mesma rotação semanal que os hosts auto-descobertos, em vez de ser
        consultado sem parar a cada execução -- ver `brave_discover._site_
        queries` (2026-08-13). `ocorrencias=0` e `promovido_em` já preenchido:
        um site curado não precisa provar nada, só entrar na fila; ON
        CONFLICT DO NOTHING preserva o estado de quem já estava lá (inclusive
        `ultima_consulta`, para não resetar o relógio de quem já foi
        consultado). `categoria='imobiliaria'` direto -- um site curado à
        mão foi escolhido justamente por ser um portal/agência de anúncios,
        não precisa passar pelo classificador genérico para provar isso."""
        hosts = list(hosts)
        if not hosts:
            return
        agora = _now()
        self.db.executemany(
            """INSERT INTO sites_descobertos
                   (host, ocorrencias, primeira_vez, ultima_vez, promovido_em, categoria)
               VALUES (?, 0, ?, ?, ?, ?)
               ON CONFLICT(host) DO NOTHING""",
            [(h, agora, agora, agora, IMOBILIARIA) for h in hosts],
        )
        # A host already present (e.g. discovered generically before it was
        # ever added to sites_alvo) may still carry the generic classifier's
        # "outro" guess -- the INSERT above never touches it, ON CONFLICT DO
        # NOTHING is unconditional. Being hand-curated is stronger evidence
        # than any text-based guess, so correct it explicitly here too.
        self.db.executemany(
            "UPDATE sites_descobertos SET categoria = ? WHERE host = ? AND categoria != ?",
            [(IMOBILIARIA, h, IMOBILIARIA) for h in hosts],
        )
        self.db.commit()

    def hosts_conhecidos(self) -> set[str]:
        """Every host já rastreado em `sites_descobertos`, promovido ou não --
        o que a busca genérica da Brave (`brave_discover.TEMPLATES`) deve
        parar de tratar como candidato a *site novo*, mesmo que ainda valha a
        pena visitar a URL em si (isso é decidido em `discover()`, não aqui)."""
        return {r["host"] for r in self.db.execute("SELECT host FROM sites_descobertos")}

    def sites_descobertos_avistar(self, hosts, titulos: dict[str, str] | None = None) -> None:
        """Registra um host no instante em que a busca genérica da Brave o
        acha por trás de uma URL nova -- antes de qualquer extração real --
        para que ele conte como "já encontrado" a partir de agora e não
        volte a consumir cota de descoberta de site todo dia. `ocorrencias`
        continua em 0: a promoção para a rotação semanal `site:` (ver
        `registrar_extracao_brave`) ainda depende de extrações reais, não de
        ter aparecido numa busca.

        `titulos` (host -> título+descrição do resultado da busca, quando
        disponível) alimenta a primeira classificação em `categoria` -- o
        único texto que existe sobre um host recém-visto antes de qualquer
        visita real à página."""
        hosts = list(hosts)
        if not hosts:
            return
        titulos = titulos or {}
        agora = _now()
        self.db.executemany(
            """INSERT INTO sites_descobertos (host, ocorrencias, primeira_vez, ultima_vez, categoria)
               VALUES (?, 0, ?, ?, ?)
               ON CONFLICT(host) DO NOTHING""",
            [(h, agora, agora, classificar(h, titulos.get(h, ""))) for h in hosts],
        )
        self.db.commit()

    def sites_descobertos_por_categoria(self, categoria: str, dias: int = 7) -> list[str]:
        """Promoted hosts of one `categoria` due for a query this week --
        the weekly-due gate exists to ration a metered API's budget
        (`tavily_discover.py`), so it applies here regardless of category."""
        limite = (datetime.now(timezone.utc) - timedelta(days=dias)).isoformat()
        linhas = self.db.execute(
            """SELECT host FROM sites_descobertos
               WHERE promovido_em IS NOT NULL
                 AND categoria = ?
                 AND (ultima_consulta IS NULL OR ultima_consulta < ?)
               ORDER BY ocorrencias DESC""",
            (categoria, limite),
        ).fetchall()
        return [r["host"] for r in linhas]

    def sites_descobertos_hosts(self, categoria: str) -> list[str]:
        """Every promoted host of one `categoria`, no weekly-due gate at
        all -- for a strategy with no metered cost to ration, like
        `imobiliaria_crawl.py`'s twice-weekly full sweep. Rationing here
        would only mean skipping hosts for no reason; the gate in
        `sites_descobertos_por_categoria` exists purely to protect a
        metered API's monthly budget, which a plain HTTP crawl doesn't
        have."""
        linhas = self.db.execute(
            """SELECT host FROM sites_descobertos
               WHERE promovido_em IS NOT NULL AND categoria = ?
               ORDER BY ocorrencias DESC""",
            (categoria,),
        ).fetchall()
        return [r["host"] for r in linhas]

    def sites_descobertos_marcar_consultado(self, hosts) -> None:
        hosts = list(hosts)
        if not hosts:
            return
        agora = _now()
        self.db.executemany(
            "UPDATE sites_descobertos SET ultima_consulta = ? WHERE host = ?",
            [(agora, h) for h in hosts],
        )
        self.db.commit()
