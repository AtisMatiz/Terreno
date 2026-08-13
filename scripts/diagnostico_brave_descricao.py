"""wimoveis/imovelweb estão fora do `site:` do Brave (`SKIP_HOSTS` em
`brave_discover.py`) porque a página não pode ser visitada depois -- mas o
próprio resultado de busca já vem com um `description` (o snippet, igual ao
de qualquer buscador) que o código de produção nunca lê. Este script testa,
sem tocar em nada do pipeline, se esse snippet sozinho já carrega preço/área
suficiente para valer a pena capturá-lo mesmo sem visitar a página.

Não escreve nada, não consome o teto de nenhuma outra fonte. Gasta consultas
reais do Brave -- rode só quando quiser mesmo os resultados.

    python3 scripts/diagnostico_brave_descricao.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from terreno import http  # noqa: E402
from terreno.config import env  # noqa: E402

API = "https://api.search.brave.com/res/v1/web/search"
HOSTS = ["wimoveis.com.br", "imovelweb.com.br"]
QUERY_TEMPLATE = "fazenda OR sítio OR chácara à venda São Paulo site:{host}"


def main() -> int:
    token = env("BRAVE_API_KEY")
    if not token:
        print("BRAVE_API_KEY não configurada -- nada a testar.")
        return 1

    resultados = []
    for host in HOSTS:
        query = QUERY_TEMPLATE.format(host=host)
        data = http.get_json(
            API,
            params={"q": query, "country": "BR", "count": 10},
            headers={"X-Subscription-Token": token, "Accept": "application/json"},
        )
        if not data:
            print(f"{host}: sem resposta (ver log acima -- provavelmente 402 de "
                  f"cota mensal esgotada, já visto hoje num run real)")
            continue
        hits = (data.get("web") or {}).get("results", [])
        print(f"\n=== {host}: {len(hits)} resultado(s) ===")
        for i, r in enumerate(hits, 1):
            item = {
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "description": r.get("description", ""),
                "age": r.get("age", ""),
            }
            resultados.append((host, item))
            print(f"\n[{host} #{i}]")
            print(f"  title: {item['title']}")
            print(f"  url: {item['url']}")
            print(f"  age: {item['age']}")
            print(f"  description: {item['description']}")

    print(f"\n\nTotal capturado: {len(resultados)} resultado(s).")
    print("JSON completo (para inspecionar campos que a impressão acima não mostrou):")
    print(json.dumps(resultados, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
