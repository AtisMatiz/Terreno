"""Um navegador de verdade consegue passar do desafio JS da Cloudflare em
wimoveis/imovelweb? Teste isolado, não escreve nada.

Contexto (2026-08-13): nem curl_cffi (qualquer imitação de navegador) nem o
desbloqueador pago (ZenRows, mesmo com `js_render=true`) passam desses dois
hosts -- ambos servem um desafio JS real ("Just a moment...", Cloudflare),
não um bloqueio de impressão digital que uma imitação melhor resolveria. Um
teste com um leitor headless gratuito (Jina Reader) bateu na mesma parede,
confirmando que o desafio precisa de um navegador de verdade executando JS,
não apenas de um cliente HTTP bem disfarçado.

Rode isto de um runner do GitHub Actions (não deste sandbox -- o proxy que
intercepta TLS aqui derruba a conexão do Chromium antes de qualquer coisa
acontecer, um problema do ambiente de teste, não do site real):

    python3 scripts/diagnostico_navegador.py

Cada alvo é medido duas vezes: (a) Chromium headless comum, (b) o mesmo com
os patches de disfarce mínimos que a Cloudflare checa (navigator.webdriver,
plugins, chrome runtime). Se (a) falha e (b) passa, o disfarce é o que
importa. Se as duas falham, não é (só) disfarce -- pode ser a faixa de IP do
runner, e nesse caso um navegador de verdade não ajuda de graça.
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

TIMEOUT_MS = 30_000

# Patch mínimo: só o que a Cloudflare de fato confere no desafio JS, não uma
# blindagem completa. `navigator.webdriver` é o sinal mais barato e mais
# checado; os demais cobrem os próximos mais comuns.
STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
Object.defineProperty(navigator, 'languages', {get: () => ['pt-BR', 'pt', 'en']});
window.chrome = window.chrome || {runtime: {}};
"""

ALVOS = [
    ("wimoveis", "https://www.wimoveis.com.br/terrenos-venda-sao-paulo.html"),
    ("imovelweb", "https://www.imovelweb.com.br/terrenos-venda-sao-paulo.html"),
]


def _tentar(playwright, url: str, *, stealth: bool) -> str:
    browser = playwright.chromium.launch(headless=True)
    try:
        context = browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"),
            locale="pt-BR",
        )
        if stealth:
            context.add_init_script(STEALTH_JS)
        page = context.new_page()
        try:
            page.goto(url, timeout=TIMEOUT_MS, wait_until="domcontentloaded")
        except Exception as exc:  # noqa: BLE001 -- o erro é o dado
            return f"ERRO {type(exc).__name__}"
        # O desafio, quando resolvido por um navegador de verdade, redireciona
        # sozinho em poucos segundos -- esperar aqui é a diferença entre medir
        # o desafio e medir a página final.
        page.wait_for_timeout(6_000)
        titulo = page.title()
        texto = page.content()
        if "Just a moment" in titulo or "Just a moment" in texto[:2000]:
            return "BLOQUEADO (desafio JS não resolvido)"
        if "propriedades/" in texto or "chacaras-sitios" in texto.lower():
            return f"OK (título: {titulo!r})"
        return f"AMBÍGUO (título: {titulo!r}, {len(texto)} bytes)"
    finally:
        browser.close()


def main() -> int:
    with sync_playwright() as p:
        largura = max(len(nome) for nome, _ in ALVOS)
        print(f"{'fonte'.ljust(largura)}  {'sem disfarce'.ljust(40)}{'com disfarce'.ljust(40)}")
        print("-" * (largura + 82))
        for nome, url in ALVOS:
            sem = _tentar(p, url, stealth=False)
            com = _tentar(p, url, stealth=True)
            print(f"{nome.ljust(largura)}  {sem.ljust(40)}{com.ljust(40)}")
    print("\nPronto. Cole esta saída na conversa.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
