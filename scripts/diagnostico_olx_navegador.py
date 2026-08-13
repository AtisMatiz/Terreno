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
            page.goto(URL, timeout=30_000, wait_until="networkidle")
        except Exception as exc:  # noqa: BLE001
            print(f"ERRO ao carregar: {type(exc).__name__}: {exc}")
            browser.close()
            return 1

        page.wait_for_timeout(3_000)
        titulo = page.title()
        html = page.content()
        print(f"título: {titulo!r}")
        print(f"tamanho do HTML renderizado: {len(html)}")

        # Depois da hidratação, cartões de anúncio reais devem existir no
        # DOM com algum seletor estável, mesmo que o __NEXT_DATA__ nunca
        # apareça -- olx usa data-testid ou data-ds-component em vários
        # elementos de listagem.
        cartoes = page.locator('a[href*="/vi/"]').count()
        print(f"links de anúncio (a[href*='/vi/']) encontrados: {cartoes}")
        if cartoes:
            primeiro = page.locator('a[href*="/vi/"]').first
            print(f"primeiro href: {primeiro.get_attribute('href')}")
            print(f"primeiro texto: {(primeiro.inner_text() or '')[:200]!r}")

        if "Just a moment" in titulo or "Just a moment" in html[:2000]:
            print("\nRESULTADO: bloqueado por desafio JS (inesperado para OLX).")
        elif cartoes:
            print(f"\nRESULTADO: OK -- {cartoes} anúncios visíveis após renderização.")
        else:
            print("\nRESULTADO: AMBÍGUO -- página carregou mas nenhum link de "
                  "anúncio reconhecido; talvez o seletor esteja errado, não "
                  "necessariamente um bloqueio.")
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
