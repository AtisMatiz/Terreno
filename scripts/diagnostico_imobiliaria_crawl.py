"""Compara, em hosts reais da SDB (`sites_descobertos`) classificados como
`imobiliaria`, a estratégia sem API (`terreno.sources.imobiliaria_crawl`)
contra o Tavily (`include_domains`) -- mesma pergunta de sempre: quanto tempo
cada uma leva e quantos anúncios reais cada uma acha, para decidir com dados
qual usar em produção. Não escreve nada no banco; só lê a lista de hosts.

    python3 scripts/diagnostico_imobiliaria_crawl.py [N]

N (padrão 30) é quantos hosts imobiliária, os mais estabelecidos (maior
`ocorrencias`), entram na comparação.
"""

from __future__ import annotations

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from terreno import http  # noqa: E402
from terreno.config import env  # noqa: E402
from terreno.extract import rules  # noqa: E402
from terreno.sources import imobiliaria_crawl  # noqa: E402
from terreno.store import Store  # noqa: E402

TAVILY_API = "https://api.tavily.com/search"
QUERY = "sítio OR fazenda OR chácara à venda"
MAX_CANDIDATOS = 8
PARALELISMO = 20


def _hosts(n: int) -> list[str]:
    store = Store("data/terreno.sqlite3")
    rows = store.db.execute(
        """SELECT host FROM sites_descobertos
           WHERE categoria = 'imobiliaria' AND promovido_em IS NOT NULL
           ORDER BY ocorrencias DESC LIMIT ?""",
        (n,),
    ).fetchall()
    store.close()
    return [r["host"] for r in rows]


def _extrair_em_paralelo(urls: list[str]) -> tuple[int, int]:
    """Busca cada URL e roda a extração de regras -- devolve
    (tentadas, com_listing_usavel: price e area presentes)."""
    if not urls:
        return 0, 0
    usaveis = 0

    def _um(url: str):
        resp = http.get(url, timeout=20, retries=1)
        if resp is None:
            return None
        return rules.extract(resp.text, url, source="diagnostico")

    with ThreadPoolExecutor(max_workers=min(PARALELISMO, len(urls))) as pool:
        for fut in as_completed({pool.submit(_um, u): u for u in urls}):
            listing = fut.result()
            if listing and listing.price and listing.area_ha:
                usaveis += 1
    return len(urls), usaveis


def _tavily(host: str, token: str) -> list[str]:
    if not token:
        return []
    resp = http._session.post(
        TAVILY_API,
        json={"api_key": token, "query": QUERY, "include_domains": [host],
              "max_results": MAX_CANDIDATOS, "search_depth": "basic"},
        timeout=25,
    )
    if resp.status_code != 200:
        return []
    return [r.get("url", "") for r in resp.json().get("results", []) if r.get("url")]


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    hosts = _hosts(n)
    token = env("TAVILY_API_KEY")
    print(f"Comparando {len(hosts)} host(s) imobiliária -- crawler sem API vs Tavily "
          f"(token {'presente' if token else 'AUSENTE — Tavily será 0 em tudo'}).\n")

    resultados = []
    t_total_code = t_total_tavily = 0.0
    tot_cand_code = tot_ok_code = tot_cand_tav = tot_ok_tav = 0

    for host in hosts:
        t0 = time.monotonic()
        candidatos_code = imobiliaria_crawl.crawl_host(host, max_candidatos=MAX_CANDIDATOS)
        tentadas_code, ok_code = _extrair_em_paralelo(candidatos_code)
        dt_code = time.monotonic() - t0

        t0 = time.monotonic()
        candidatos_tav = _tavily(host, token)
        tentadas_tav, ok_tav = _extrair_em_paralelo(candidatos_tav)
        dt_tav = time.monotonic() - t0

        t_total_code += dt_code
        t_total_tavily += dt_tav
        tot_cand_code += tentadas_code
        tot_ok_code += ok_code
        tot_cand_tav += tentadas_tav
        tot_ok_tav += ok_tav

        linha = {
            "host": host,
            "code_candidatos": tentadas_code, "code_usaveis": ok_code, "code_segundos": round(dt_code, 1),
            "tavily_candidatos": tentadas_tav, "tavily_usaveis": ok_tav, "tavily_segundos": round(dt_tav, 1),
        }
        resultados.append(linha)
        print(f"{host:40s}  code: {tentadas_code:2d} cand / {ok_code:2d} úteis / {dt_code:5.1f}s   "
              f"tavily: {tentadas_tav:2d} cand / {ok_tav:2d} úteis / {dt_tav:5.1f}s")

    print("\n=== TOTAIS ===")
    print(f"code:   {tot_cand_code} candidatos tentados, {tot_ok_code} com listing usável, "
          f"{t_total_code:.1f}s no total ({t_total_code / max(len(hosts), 1):.1f}s/host)")
    print(f"tavily: {tot_cand_tav} candidatos tentados, {tot_ok_tav} com listing usável, "
          f"{t_total_tavily:.1f}s no total ({t_total_tavily / max(len(hosts), 1):.1f}s/host)")

    print("\nJSON completo:")
    print(json.dumps(resultados, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
