"""OLX, unlike wimoveis/imovelweb, passes curl_cffi cleanly (no Cloudflare
challenge) -- but the search page's data now streams in via Next.js App
Router's RSC payload (`self.__next_f.push(...)`) instead of the classic
`__NEXT_DATA__` script tag, so a plain HTTP fetch never sees a listing.
A real browser hydrates that stream into a normal DOM regardless, which is
a fundamentally different (better-odds) case than the Cloudflare-challenge
sites already tested -- there is no known extra JS-challenge layer here to
defeat, "only" client-side rendering to wait out.

Run from a GitHub Actions runner (this sandbox's TLS-intercepting proxy
makes any headless-browser measurement here meaningless):

    python3 scripts/diagnostico_olx_navegador.py
"""

from __future__ import annotations

import re
import sys

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("AVISO: playwright não instalado. Instale com:\n\n"
          "    python3 -m pip install playwright\n"
          "    python3 -m playwright install --with-deps chromium\n")
    sys.exit(1)

URL = "https://www.olx.com.br/imoveis/terrenos/estado-sp"


def main() -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                        "Version/17.0 Safari/605.1.15"),
            locale="pt-BR",
        )
        page = context.new_page()
        try:
            # "networkidle" nunca se resolve em páginas com telemetria/ads
            # contínuos (medido: timeout puro aos 30s) -- não é sinal de
            # bloqueio, é a condição de espera errada para um site real.
            page.goto(URL, timeout=30_000, wait_until="domcontentloaded")
        except Exception as exc:  # noqa: BLE001
            print(f"ERRO ao carregar: {type(exc).__name__}: {exc}")
            browser.close()
            return 1

        page.wait_for_timeout(6_000)
        titulo = page.title()
        html = page.content()
        print(f"título: {titulo!r}")
        print(f"tamanho do HTML renderizado: {len(html)}")

        # "/vi/" era um palpite -- não bateu com nada. Em vez de adivinhar
        # de novo, lista os hrefs reais (todo <a>) e mostra os que parecem
        # anúncio (terminam em dígitos, o padrão mais estável de qualquer
        # portal de classificados) para descobrir o padrão certo direto dos
        # dados, não de suposição.
        hrefs = page.eval_on_selector_all("a", "els => els.map(e => e.getAttribute('href'))")
        hrefs = [h for h in hrefs if h]
        print(f"total de <a href> na página: {len(hrefs)}")
        candidatos = [h for h in hrefs if re.search(r"\d{6,}", h)]
        print(f"hrefs com um número de 6+ dígitos (candidato a id de anúncio): {len(candidatos)}")
        for h in candidatos[:15]:
            print(" -", h)
        if not candidatos:
            print("amostra de hrefs (nenhum candidato claro encontrado):")
            for h in hrefs[:20]:
                print(" -", h)

        if "Just a moment" in titulo or "Just a moment" in html[:2000]:
            print("\nRESULTADO: bloqueado por desafio JS (inesperado para OLX).")
        elif candidatos:
            print(f"\nRESULTADO: OK -- {len(candidatos)} link(s) candidato(s) a "
                  "anúncio visível(is) após renderização.")
        else:
            print("\nRESULTADO: AMBÍGUO -- página carregou (título real, HTML "
                  "grande) mas nenhum link com padrão de id de anúncio "
                  "encontrado; ver a amostra de hrefs acima.")
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
