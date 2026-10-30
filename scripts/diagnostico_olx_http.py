"""O que a OLX entrega pela escada HTTP (curl_cffi -> desbloqueador), sem navegador?

Em 2026-10-30 o Chromium do runner passou a receber a página "Attention
Required" da Cloudflare; a escada HTTP (ZenRows) ainda devolve 50 links de
anúncio, mas sem preço/município. Este script mostra onde, no HTML bruto, esses
campos moram (texto do <a>, JSON embutido, outro lugar):

    python scripts/diagnostico_olx_http.py
"""

from __future__ import annotations

import re
import sys

from terreno import http
from terreno.sources import olx

URL = "https://www.olx.com.br/imoveis/terrenos/estado-sp?o=1&ps=100000&pe=2000000"


def main() -> int:
    resp = http.get(URL, timeout=90, retries=1)
    if resp is None:
        print("sem resposta")
        return 0
    html = resp.text
    print("HTML:", len(html), "bytes")
    for chave in ("R$", '"price"', '"subject"', '"municipality"', '"location"',
                  "__next_f", "__NEXT_DATA__", "application/ld+json"):
        print(f"  {chave!r}: {html.count(chave)}")

    cards = olx._cards_do_html(html, URL)
    print(f"\n{len(cards)} âncoras de anúncio; primeiras 3 (texto extraído):")
    for h, t in cards[:3]:
        print("-" * 60)
        print(h[-100:])
        print(repr(t[:300]))

    achou = re.search(r'<a\b[^>]*href=["\'][^"\']*-\d{6,}[^"\']*["\'][^>]*>', html)
    if achou:
        i = achou.start()
        print("\nHTML bruto em volta da 1ª âncora de anúncio:")
        print(html[max(0, i - 200): i + 1200])

    m = re.search(r'\d{9,10}', html[html.find("__next_f"):] if "__next_f" in html else "")
    j = html.find('"price"')
    if j != -1:
        print("\ntrecho em volta do 1º \"price\":")
        print(html[max(0, j - 400): j + 600])
    return 0


if __name__ == "__main__":
    sys.exit(main())
