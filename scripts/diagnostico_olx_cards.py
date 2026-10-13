"""O que a página de resultados da OLX já entrega, sem abrir nenhum anúncio?

As páginas de detalhe da OLX respondem 403 do runner (medido 2026-10-13: um
navegador real leu 1 de 120). Se a própria página de resultados -- que carrega
bem -- já traz preço, município e área, não precisamos delas. Este script
imprime (1) o texto de alguns cards e (2) o trecho do payload de streaming do
Next.js (`self.__next_f.push`) em volta de um anúncio, para ver quais campos
existem. Rode num runner do GitHub Actions (aqui o proxy do sandbox bloqueia):

    python scripts/diagnostico_olx_cards.py
"""

from __future__ import annotations

import re
import sys

from playwright.sync_api import sync_playwright

URL = "https://www.olx.com.br/imoveis/terrenos/estado-sp"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/17.0 Safari/605.1.15")
CARD_JS = """els => els.map(a => {
  let n = a, t = a.innerText || '';
  for (let i = 0; i < 4 && !/R\\$/.test(t) && n.parentElement; i++) {
    n = n.parentElement;
    if (n.querySelectorAll('a[href*="olx.com.br"]').length > 1) break;
    t = n.innerText || '';
  }
  return [a.getAttribute('href'), t.slice(0, 500)];
})"""


def main() -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_context(user_agent=UA, locale="pt-BR").new_page()
        resp = page.goto(URL, timeout=45000, wait_until="domcontentloaded")
        page.wait_for_timeout(7000)
        print("status:", resp.status if resp else None)

        cards = [(h, t) for h, t in page.eval_on_selector_all("a", CARD_JS)
                 if h and re.search(r"-\d{6,}/?(\?|$)", h)]
        print(f"\n{len(cards)} cards com link de anúncio. Os 6 primeiros:")
        for h, t in cards[:6]:
            print("-" * 60)
            print(h[-90:])
            print(repr(t))

        html = page.content()
        print(f"\nHTML: {len(html)} bytes")
        for chave in ("subject", "municipality", "neighbourhood", "location",
                      "description", "size", "price"):
            print(f"  ocorrências de \"{chave}\":", len(re.findall(f'"{chave}"', html)))
        i = html.find('"subject"')
        if i != -1:
            print("\ntrecho em volta do 1º \"subject\":")
            print(html[max(0, i - 300): i + 1500])
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
