"""Por que cada fonte bloqueada está bloqueada — medido, não suposto.

Rode isto da sua máquina (IP residencial):

    python3 scripts/diagnostico.py

Ele não escreve nada e não commita nada. Só bate uma vez em cada host
problemático de cinco formas diferentes e diz qual passa. As cinco existem
para separar causas que o log normal confunde numa coisa só:

  1. requests              -- o transporte normal, HTTP/1.1, impressão digital
                              de TLS de biblioteca Python.
  2. curl_cffi + nossos    -- o que o código fazia até agora: handshake de
     cabeçalhos               navegador, mas com o nosso User-Agent por cima.
                              Se ESTE falha e o 3 passa, a causa era a
                              incoerência entre handshake e cabeçalho.
  3. curl_cffi limpo       -- handshake de navegador com os cabeçalhos que o
                              próprio curl_cffi escolhe para combinar com ele.
  4. desbloqueador         -- o terceiro transporte, pago (ZenRows). Só roda
                              quando TERRENO_UNBLOCKER e a chave do provedor
                              estão no ambiente; sem isso a coluna diz
                              "não configurado", que NÃO é a mesma coisa que
                              uma falha. ATENÇÃO: esta coluna gasta crédito de
                              verdade — uma requisição premium por host.
  5. curl_cffi, outros     -- o mesmo que 3, variando o navegador imitado.
     navegadores              Fornecedores de anti-bot atualizam suas bases em
                              ritmos diferentes; quando um alvo para de passar,
                              outro costuma continuar passando.

Leitura do resultado:
  * 1 falha e 3 passa            -> era impressão digital de TLS. O código já
                                    corrigido faz isso sozinho agora.
  * 2 falha e 3 passa            -> era exatamente o bug dos cabeçalhos.
  * 1, 2, 3 e 5 falham e 4 passa -> nem impressão digital nem IP resolvem: o
                                    site exige navegador/desafio, e só o
                                    desbloqueador pago passa.
  * 1, 2, 3, 4 e 5 falham        -> nem o serviço pago passa; o alvo pede
                                    sessão/cookie/JS de verdade (ou a conta do
                                    desbloqueador está mal configurada — o
                                    erro dele aparece na coluna).
  * 1 passa                      -> não há bloqueio nenhum nesse host hoje.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402

from terreno import http as thttp  # noqa: E402
from terreno.http import DEFAULT_HEADERS  # noqa: E402

# Catch more than ImportError on purpose: curl_cffi carries a compiled
# extension, and a broken load raises OSError. "Não instalado" and "instalado
# mas quebrado" pedem providências completamente diferentes.
try:
    from curl_cffi import requests as cffi
    CFFI_ERRO = ""
except Exception as exc:  # noqa: BLE001 — see above
    cffi = None
    CFFI_ERRO = f"{type(exc).__name__}: {exc}"

TIMEOUT = 25

# Um alvo por host problemático. São URLs de busca reais, não a home page:
# muitos desses sites servem a home a qualquer um e só aplicam a parede na
# busca, então testar a home daria um "passou" que não vale nada.
ALVOS = [
    ("olx", "https://www.olx.com.br/imoveis/estado-sp", None),
    ("imovelweb", "https://www.imovelweb.com.br/terrenos-venda-sao-paulo.html", None),
    ("wimoveis", "https://www.wimoveis.com.br/terrenos-venda-sao-paulo.html", None),
    ("pgfn", "https://comprei.pgfn.gov.br/gateway/anuncio/publico"
             "?ufs=35&size=10&page=0", None),
    ("mercadolivre-api",
     "https://api.mercadolibre.com/sites/MLB/search?category=MLB1459&limit=5", None),
    ("caixa", "https://venda-imoveis.caixa.gov.br/listaweb/Lista_imoveis_SP.csv", None),
]

# Alvos de imitação a variar no teste 4. "chrome" é o padrão do código.
NAVEGADORES = ["chrome", "chrome124", "safari", "firefox"]


def _resultado(status: int | None, erro: str = "") -> str:
    if erro:
        return f"ERRO {erro}"
    if status == 200:
        return "OK 200"
    return f"HTTP {status}"


def _requests(url: str, headers: dict | None) -> str:
    try:
        r = requests.get(url, headers={**DEFAULT_HEADERS, **(headers or {})},
                         timeout=TIMEOUT)
        return _resultado(r.status_code)
    except Exception as exc:  # noqa: BLE001 — o erro é o dado que queremos
        return _resultado(None, type(exc).__name__)


def _cffi(url: str, headers: dict | None, *, sujo: bool, alvo: str) -> str:
    if cffi is None:
        return "curl_cffi ausente"
    # "sujo" reproduz o bug antigo de propósito: cabeçalhos nossos por cima do
    # handshake do navegador imitado.
    enviar = {**DEFAULT_HEADERS, **(headers or {})} if sujo else dict(headers or {})
    try:
        r = cffi.get(url, headers=enviar, timeout=TIMEOUT, impersonate=alvo)
        return _resultado(r.status_code)
    except Exception as exc:  # noqa: BLE001
        return _resultado(None, type(exc).__name__)


def _unblocker(url: str, headers: dict | None) -> str:
    """O terceiro transporte, medido do mesmo jeito honesto que os outros.

    Fala direto com o provedor em vez de passar pelo `http._via_unblocker`
    porque aqui queremos o status real e o corpo do erro — inclusive quando
    ele é 401/422 da própria ZenRows — e porque o diagnóstico não deve
    consumir o teto por execução do módulo.
    """
    prov = thttp._unblocker_ativo()
    if prov is None:
        # A distinção importa: "não configurado" não é "falhou".
        return "não configurado"
    semanticos = thttp._headers_semanticos(headers)
    pedido = prov.montar(url, semanticos)
    try:
        r = requests.get(pedido, headers=semanticos, timeout=90)
    except Exception as exc:  # noqa: BLE001 — o erro é o dado que queremos
        return _resultado(None, type(exc).__name__)
    if r.status_code == 200:
        return "OK 200"
    trecho = thttp._redigir((r.text or "")[:60].replace("\n", " "), prov.chave)
    return f"HTTP {r.status_code} {trecho}".rstrip()


def main() -> int:
    if cffi is None:
        # `pip3` e `python3` podem ser instalações diferentes do Python na
        # mesma máquina -- é o motivo mais comum de o pip dizer "Requirement
        # already satisfied" e o script seguinte não achar o pacote. Por isso
        # o caminho do interpretador aparece aqui: é o dado que resolve essa
        # confusão, e `python3 -m pip` instala no Python certo por construção.
        print("AVISO: o curl_cffi não pôde ser carregado — os testes 2, 3 e 5\n"
              f"       vão aparecer como ausentes.\n\n"
              f"       Motivo: {CFFI_ERRO}\n"
              f"       Python em uso: {sys.executable}\n\n"
              "       Se o pip disse que já estava instalado, ele instalou em\n"
              "       OUTRO Python. Instale no que este script usa:\n\n"
              "           python3 -m pip install curl_cffi\n")

    prov = thttp._unblocker_ativo()
    if prov is None:
        print(f"AVISO: o desbloqueador não está configurado — a coluna 4 vai\n"
              f"       aparecer como ausente. Para medi-la (gasta crédito!):\n\n"
              f"           export {thttp.UNBLOCKER_ENV}=zenrows\n"
              f"           export ZENROWS_API_KEY=...\n")

    largura = max(len(nome) for nome, _, _ in ALVOS)
    cab = (f"{'fonte'.ljust(largura)}  {'1 requests'.ljust(18)}"
           f"{'2 cffi+nossos hdrs'.ljust(20)}{'3 cffi limpo'.ljust(18)}"
           f"{'4 desbloqueador'.ljust(24)}")
    print(cab)
    print("-" * len(cab))

    precisam_de_navegador = []
    for nome, url, headers in ALVOS:
        r1 = _requests(url, headers)
        r2 = _cffi(url, headers, sujo=True, alvo="chrome")
        r3 = _cffi(url, headers, sujo=False, alvo="chrome")
        # Só gasta crédito onde os transportes grátis não bastaram — é a mesma
        # ordem de escalada que o http.py segue em produção.
        if r1.startswith("OK") or r3.startswith("OK"):
            r4 = "não precisou"
        else:
            r4 = _unblocker(url, headers)
        print(f"{nome.ljust(largura)}  {r1.ljust(18)}{r2.ljust(20)}"
              f"{r3.ljust(18)}{r4.ljust(24)}")
        if not r1.startswith("OK") and not r3.startswith("OK"):
            precisam_de_navegador.append((nome, url, headers))

    if precisam_de_navegador and cffi is not None:
        print("\nTeste 5 — variando o navegador imitado, só para os que "
              "continuaram fechados:\n")
        largura2 = max(len(n) for n, _, _ in precisam_de_navegador)
        print(f"{'fonte'.ljust(largura2)}  " +
              "".join(a.ljust(14) for a in NAVEGADORES))
        print("-" * (largura2 + 2 + 14 * len(NAVEGADORES)))
        for nome, url, headers in precisam_de_navegador:
            linha = "".join(
                _cffi(url, headers, sujo=False, alvo=a).ljust(14)
                for a in NAVEGADORES
            )
            print(f"{nome.ljust(largura2)}  {linha}")

    print("\nPronto. Cole esta saída inteira na conversa — ela diz, por fonte,\n"
          "se o problema é impressão digital de TLS (corrigível no código) ou\n"
          "o IP / uma sessão de navegador de verdade (não é).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
