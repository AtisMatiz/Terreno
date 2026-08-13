"""wimoveis/imovelweb estão fora do `site:` normal do Brave (iam para
`SKIP_HOSTS`; agora vão para `COLD_HOSTS`, ver `brave_discover.py`) porque a
página não pode ser visitada depois -- mas o próprio resultado de busca já
vem com um `description` (o snippet, igual ao de qualquer buscador) que o
código de produção só passou a guardar em 2026-08-13. Este script testa, sem
tocar em nada do pipeline, se esse snippet sozinho já carrega preço/área
úteis -- e compara três fontes lado a lado: a chave principal do Brave, uma
segunda conta (BRAVE_API_KEY_2, criada depois que a primeira bateu no teto
mensal) e o Tavily (free tier, ver SESSION_NOTES 2026-08-13), que expõe
domínio-alvo como parâmetro nativo (`include_domains`) em vez do truque
`site:` embutido no texto da busca.

Não escreve nada, não consome o teto de nenhuma outra fonte. Gasta consultas
reais -- rode só quando quiser mesmo os resultados.

    python3 scripts/diagnostico_brave_descricao.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from terreno import http  # noqa: E402
from terreno.config import env  # noqa: E402

BRAVE_API = "https://api.search.brave.com/res/v1/web/search"
TAVILY_API = "https://api.tavily.com/search"
HOSTS = ["wimoveis.com.br", "imovelweb.com.br"]
QUERY_TEMPLATE = "fazenda OR sítio OR chácara à venda São Paulo site:{host}"


def _brave(host: str, token: str, rotulo: str) -> list[dict]:
    if not token:
        print(f"[{rotulo}] não configurada -- pulando.")
        return []
    query = QUERY_TEMPLATE.format(host=host)
    data = http.get_json(
        BRAVE_API,
        params={"q": query, "country": "BR", "count": 10},
        headers={"X-Subscription-Token": token, "Accept": "application/json"},
    )
    if not data:
        print(f"[{rotulo}] {host}: sem resposta (ver aviso de erro acima)")
        return []
    hits = (data.get("web") or {}).get("results", [])
    return [{"fonte": rotulo, "host": host, "title": r.get("title", ""),
             "url": r.get("url", ""), "description": r.get("description", ""),
             "age": r.get("age", "")} for r in hits]


def _tavily(host: str, token: str) -> list[dict]:
    if not token:
        print("[tavily] TAVILY_API_KEY não configurada -- pulando.")
        return []
    query = "fazenda OR sítio OR chácara à venda São Paulo"
    resp = http._session.post(
        TAVILY_API,
        json={"api_key": token, "query": query, "include_domains": [host],
              "max_results": 10, "search_depth": "basic"},
        timeout=25,
    )
    if resp.status_code != 200:
        print(f"[tavily] {host}: HTTP {resp.status_code} — {(resp.text or '')[:300]}")
        return []
    data = resp.json()
    hits = data.get("results", [])
    return [{"fonte": "tavily", "host": host, "title": r.get("title", ""),
             "url": r.get("url", ""), "description": r.get("content", ""),
             "age": ""} for r in hits]


def main() -> int:
    token1 = env("BRAVE_API_KEY")
    token2 = env("BRAVE_API_KEY_2")
    tavily_token = env("TAVILY_API_KEY")

    todos: list[dict] = []
    for host in HOSTS:
        print(f"\n=== {host} ===")
        for rotulo, resultado in (
            ("brave#1", _brave(host, token1, "brave#1")),
            ("brave#2", _brave(host, token2, "brave#2")),
            ("tavily", _tavily(host, tavily_token)),
        ):
            print(f"\n-- {rotulo}: {len(resultado)} resultado(s) --")
            for i, item in enumerate(resultado, 1):
                print(f"  [{i}] {item['title']}")
                print(f"      url: {item['url']}")
                print(f"      description: {item['description'][:400]}")
            todos.extend(resultado)

    print(f"\n\nTotal capturado: {len(todos)} resultado(s) nas três fontes.")
    print("JSON completo:")
    print(json.dumps(todos, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
