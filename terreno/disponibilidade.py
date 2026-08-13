"""Está o anúncio ainda no ar? — verificação de disponibilidade antes de notificar.

Motivo: vários links enviados ao Telegram já não abriam nada. O anúncio havia
sido vendido, encerrado ou removido entre a coleta e o envio, e o cartão
continuava sendo mandado. Este módulo abre a URL uma vez e classifica o que
encontra em três estados — `DISPONIVEL`, `INDISPONIVEL`, `DESCONHECIDO`.

O terceiro estado é o ponto central do desenho. Uma falha de rede, um 403 de
anti-bot ou uma resposta que não é HTML **não** são prova de que o imóvel
acabou; tratá-los como "vendido" apagaria silenciosamente anúncios bons a cada
instabilidade. Só há duas maneiras de um anúncio ser declarado indisponível:
o servidor dizer que a página não existe mais (404/410, ou redirecionamento
para a home) ou a própria página dizer, em português, que aquele anúncio saiu
do ar. Qualquer outra coisa é `DESCONHECIDO`, e a decisão sobre o que fazer com
o desconhecido fica com quem chama (o `pipeline`), não aqui.

Custo: **uma requisição HTTP GET por URL** (`retries=1`, sem nova tentativa
imediata; em host que já exigiu o transporte de navegador, o `http.get` pode
gastar uma segunda tentativa via curl_cffi — logo, no pior caso 2). Rodando
sobre os candidatos que já passaram por filtro e nota, isso é da ordem de
dezenas de requisições por execução, não de milhares. A verificação é feita em
paralelo (mesmo padrão de `sources/brave_visit.py`), e o `http.get` continua
respeitando o intervalo mínimo por host, então vários anúncios do mesmo portal
não viram rajada.
"""

from __future__ import annotations

import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit

from . import http
from .sources.base import strip_tags

log = logging.getLogger("terreno.disponibilidade")


class Estado(StrEnum):
    """Os três veredictos possíveis. `StrEnum` para serializar sem cerimônia:
    `veredito.estado == "vendido"`? não -- `veredito.estado.value` já é a string
    que vai para o SQLite ou para o JSON do site, sem conversão."""

    DISPONIVEL = "disponivel"
    INDISPONIVEL = "indisponivel"
    DESCONHECIDO = "desconhecido"


DISPONIVEL = Estado.DISPONIVEL
INDISPONIVEL = Estado.INDISPONIVEL
DESCONHECIDO = Estado.DESCONHECIDO


@dataclass(frozen=True)
class Veredito:
    """O resultado de uma verificação, com o porquê à mão para o log/relatório."""

    url: str
    estado: Estado
    motivo: str = ""
    http_status: int | None = None
    url_final: str = ""

    @property
    def indisponivel(self) -> bool:
        return self.estado is Estado.INDISPONIVEL


# --------------------------------------------------------------- vocabulário
#
# Redigido a partir do que os portais brasileiros de fato imprimem quando um
# anúncio sai do ar: OLX ("Esse anúncio não está mais disponível", "Anúncio
# não encontrado"), VivaReal/ZAP ("Este imóvel não está mais disponível",
# "Imóvel não encontrado"), Mercado Livre ("Anúncio pausado", "Anúncio
# encerrado"), Imovelweb/Wimoveis ("O imóvel que você procura não está mais
# disponível"), Chaves na Mão ("Imóvel indisponível") e o selo "VENDIDO" que
# imobiliárias pequenas simplesmente estampam sobre a foto.
#
# Todo padrão abaixo exige que a palavra fale *deste* anúncio: um substantivo
# de imóvel ou a palavra "anúncio" ao lado, ou uma frase inteira de status.
# É o que separa "imóvel vendido" de "já vendemos mais de 100 imóveis" no
# rodapé de uma imobiliária — este segundo caso não casa com nada aqui, e há
# teste para isso.

_IMOVEL = r"(?:im[óo]ve(?:l|is)|an[úu]ncio|publica[çc][ãa]o|oferta|s[íi]tio|" \
          r"ch[áa]cara|fazenda|terreno|lote|propriedade|casa)"

_PADROES: tuple[tuple[str, str], ...] = (
    # "Este anúncio não está mais disponível" / "não se encontra mais disponível"
    ("nao_mais_disponivel",
     r"n[ãa]o\s+(?:est[áa]|se\s+encontra|encontra-se)\s+mais\s+dispon[íi]ve"),
    # "O imóvel que você procura não está mais disponível" (variante sem "mais")
    ("nao_disponivel",
     rf"{_IMOVEL}\s+(?:est[áa]|se\s+encontra)\s+(?:no\s+momento\s+)?indispon[íi]ve"
     rf"|{_IMOVEL}\s+indispon[íi]ve|indispon[íi]vel\s+para\s+venda"),
    # "Anúncio encerrado / removido / expirado / pausado / desativado"
    ("anuncio_encerrado",
     rf"{_IMOVEL}\s+(?:j[áa]\s+)?(?:foi\s+|est[áa]\s+)?"
     r"(?:encerrad[oa]|removid[oa]|exclu[íi]d[oa]|finalizad[oa]|desativad[oa]|"
     r"inativ[oa]|expirad[oa]|cancelad[oa]|pausad[oa]|suspens[oa]|arquivad[oa])"),
    ("anuncio_expirou",
     rf"{_IMOVEL}\s+(?:j[áa]\s+)?(?:expirou|venceu|saiu\s+do\s+ar|foi\s+ao\s+ch[ãa]o)"
     r"|(?:este|esse)\s+an[úu]ncio\s+saiu\s+do\s+ar"),
    # "Imóvel vendido", "sítio já vendido", "este imóvel foi vendido/alugado"
    ("vendido",
     rf"{_IMOVEL}\s+(?:j[áa]\s+)?(?:foi\s+|est[áa]\s+)?(?:vendid[oa]|alugad[oa]|"
     r"negociad[oa])\b"),
    ("venda_concluida",
     r"venda\s+(?:j[áa]\s+)?(?:foi\s+)?(?:conclu[íi]da|realizada|efetivada|"
     r"finalizada)|neg[óo]cio\s+(?:j[áa]\s+)?fechado"),
    # "Imóvel reservado" — nunca "todos os direitos reservados", que é rodapé.
    ("reservado",
     rf"{_IMOVEL}\s+(?:j[áa]\s+)?(?:foi\s+|est[áa]\s+)?reservad[oa]\b"),
    # 404 servido com HTTP 200, que é o comum nos portais brasileiros.
    ("pagina_inexistente",
     rf"{_IMOVEL}\s+n[ãa]o\s+(?:foi\s+)?(?:encontrad[oa]|localizad[oa])"
     r"|p[áa]gina\s+n[ãa]o\s+(?:foi\s+)?encontrada"
     r"|erro\s*404|404\s*[-–—:]\s*(?:p[áa]gina|n[ãa]o)"
     r"|n[ãa]o\s+encontramos\s+(?:essa|esta)\s+p[áa]gina"),
)

_COMPILADOS = tuple((nome, re.compile(padrao, re.I)) for nome, padrao in _PADROES)

# O selo. Sozinha, "VENDIDO" só vale como status quando está no título da
# página, no og:title ou num h1 — ou seja, onde a página diz do que ela trata.
# No meio do corpo, "vendido" solto é ruído de rodapé e é ignorado.
_SELO = re.compile(
    r"(?<!direitos\s)\b(vendid[oa]|alugad[oa]|reservad[oa]|indispon[íi]vel|"
    r"encerrad[oa]|expirad[oa]|inativ[oa]|removid[oa])\b",
    re.I,
)
_H1 = re.compile(r"<h1[^>]*>(.*?)</h1>", re.S | re.I)
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)
_OG_TITLE = re.compile(
    r'<meta[^>]+(?:property|name)=["\']og:title["\'][^>]+content=["\']([^"\']*)["\']',
    re.I,
)

# Quanto do texto visível é examinado. O selo de vendido às vezes vem depois
# da galeria de fotos, então não dá para olhar só o começo; 60 mil caracteres
# cobrem a página inteira dos portais sem transformar isto em processamento de
# texto caro.
_LIMITE_TEXTO = 60_000

# Caminhos que, quando são o destino final de um redirecionamento, significam
# "essa página não existe mais, leia o resto do site": a home e os índices.
_DESTINO_DE_DESCARTE = re.compile(
    r"^/?$|^/(?:index\.\w+|home|busca|buscar|search|pesquisa|imoveis|"
    r"imoveis-a-venda|anuncios|404|erro|nao-encontrado)/?$",
    re.I,
)


def avaliar_html(html: str, url: str, *, url_final: str = "",
                 http_status: int | None = 200) -> Veredito:
    """Classifica uma página já baixada. Função pura — é o que os testes usam.

    Separada de `verificar` de propósito: a parte que erra é a leitura do
    texto, e ela precisa ser exercitável sem rede.
    """
    # 404/410 decidem sozinhos, antes de olhar o conteúdo. `verificar` já trata
    # esse caso pelo caminho de `resp is None`, então aqui é redundante para o
    # fluxo real -- mas sem isto o parâmetro `http_status` seria recebido e
    # ignorado na decisão, só repassado ao Veredito. Um argumento que parece
    # governar o resultado e não governa é a assinatura mentindo, e foi
    # exatamente o que confundiu a primeira leitura desta função.
    if http_status in (404, 410):
        return Veredito(url, Estado.INDISPONIVEL, f"http_{http_status}",
                        http_status, url_final)

    if url_final and _redirecionou_para_indice(url, url_final):
        return Veredito(url, Estado.INDISPONIVEL, "redirecionado_para_indice",
                        http_status, url_final)

    titulo = " ".join(
        strip_tags(m) for m in (
            _primeiro(_TITLE, html), _primeiro(_OG_TITLE, html),
            *(_H1.findall(html)[:3]),
        ) if m
    )
    if _SELO.search(titulo):
        return Veredito(url, Estado.INDISPONIVEL, "selo_no_titulo",
                        http_status, url_final)

    texto = strip_tags(html)[:_LIMITE_TEXTO]
    alvo = f"{titulo} {texto}"
    for nome, regex in _COMPILADOS:
        m = regex.search(alvo)
        if m:
            log.debug("indisponível (%s: %r): %s", nome, m.group(0)[:80], url)
            return Veredito(url, Estado.INDISPONIVEL, nome, http_status, url_final)

    return Veredito(url, Estado.DISPONIVEL, "", http_status, url_final)


def verificar(url: str, *, timeout: int = 20) -> Veredito:
    """Uma requisição, um veredito. Nunca levanta exceção.

    Sempre via `terreno.http.get`, que é onde moram o intervalo por host, o
    backoff e o transporte curl_cffi para os portais que farejam TLS.
    """
    if not url:
        return Veredito(url, Estado.DESCONHECIDO, "sem_url")

    _status_visto.limpar()
    try:
        resp = http.get(url, timeout=timeout, retries=1)
    except Exception as exc:  # noqa: BLE001 — verificar nunca derruba a execução
        log.debug("falha ao verificar %s: %s: %s", url, type(exc).__name__, exc)
        return Veredito(url, Estado.DESCONHECIDO, "erro_de_rede")

    if resp is None:
        # `http.get` devolve None tanto para 404 quanto para timeout, então o
        # código, quando existe, vem do log que ele mesmo emitiu nesta thread.
        status = _status_visto.ler()
        if status in (404, 410):
            return Veredito(url, Estado.INDISPONIVEL, f"http_{status}", status)
        return Veredito(url, Estado.DESCONHECIDO,
                        f"http_{status}" if status else "sem_resposta", status)

    tipo = (getattr(resp, "headers", {}) or {}).get("content-type", "")
    if "text/html" not in tipo.lower():
        return Veredito(url, Estado.DESCONHECIDO, "nao_e_html", 200)

    return avaliar_html(resp.text, url,
                        url_final=str(getattr(resp, "url", "") or ""),
                        http_status=200)


def verificar_lote(urls, *, paralelismo: int = 8,
                   timeout: int = 20) -> dict[str, Veredito]:
    """Verifica muitas URLs em paralelo. Devolve {url: Veredito}.

    Paralelo pelo mesmo motivo de `brave_visit.visit_all`: o tempo é todo de
    espera de rede, em hosts diferentes. O padrão (8) é baixo de propósito —
    isto roda sobre os poucos anúncios que estão a ponto de ser notificados,
    não sobre o backlog inteiro, e um número modesto mantém a visita discreta.
    """
    unicas = list(dict.fromkeys(u for u in urls if u))
    if not unicas:
        return {}

    resultados: dict[str, Veredito] = {}
    trabalhadores = max(1, min(int(paralelismo), len(unicas)))
    with ThreadPoolExecutor(max_workers=trabalhadores) as pool:
        futuros = {pool.submit(verificar, u, timeout=timeout): u for u in unicas}
        for futuro in as_completed(futuros):
            url = futuros[futuro]
            try:
                resultados[url] = futuro.result()
            except Exception as exc:  # noqa: BLE001 — uma URL ruim não para o lote
                log.debug("verificação falhou em %s: %s", url, exc)
                resultados[url] = Veredito(url, Estado.DESCONHECIDO, "erro_interno")

    indisp = sum(1 for v in resultados.values() if v.indisponivel)
    desconh = sum(1 for v in resultados.values() if v.estado is Estado.DESCONHECIDO)
    log.info("disponibilidade: %d verificadas — %d indisponíveis, %d desconhecidas "
             "(mantidas)", len(resultados), indisp, desconh)
    return resultados


def urls_indisponiveis(urls, *, paralelismo: int = 8,
                       timeout: int = 20) -> set[str]:
    """Atalho para o pipeline: só o conjunto de URLs a não notificar.

    Só entra aqui o que foi *provado* fora do ar; desconhecido fica de fora do
    conjunto, isto é, continua sendo notificado.
    """
    return {
        url for url, veredito in
        verificar_lote(urls, paralelismo=paralelismo, timeout=timeout).items()
        if veredito.indisponivel
    }


# ------------------------------------------------------------------ internos


def _primeiro(regex: re.Pattern, html: str) -> str:
    m = regex.search(html or "")
    return m.group(1) if m else ""


def _redirecionou_para_indice(url: str, url_final: str) -> bool:
    """A URL pedida era de um anúncio e caímos na home/num índice.

    Portais fazem isso em vez de 404 quando o anúncio sai: o link responde 200,
    mas o que abre é outra página. Comparar os caminhos é o único jeito de ver.
    """
    origem, destino = urlsplit(url), urlsplit(url_final)
    if origem.path.rstrip("/") == destino.path.rstrip("/"):
        return False
    if len(origem.path.rstrip("/")) <= 1:
        return False  # a origem já era a home; nada a concluir
    return bool(_DESTINO_DE_DESCARTE.match(destino.path))


class _StatusPorThread(logging.Handler):
    """Recupera o código HTTP que o `http.get` viu e não devolve.

    `http.get` é deliberadamente tolerante: qualquer resposta que não seja 200
    vira `None`, para que um portal quebrado não derrube a execução. Só que
    aqui a diferença entre 404 e timeout é exatamente a informação que
    importa — um é "vendido", o outro é "não sei". Em vez de duplicar a
    requisição com `requests` (o que furaria o throttle e o curl_cffi), este
    handler escuta o logger `terreno.http`, que já registra o código, e o
    guarda por thread — o log é emitido na mesma thread que chamou `get`, de
    modo que duas verificações simultâneas não se confundem.

    Acoplamento assumido e localizado: se `http.py` deixar de logar o código,
    isto degrada para `DESCONHECIDO`, ou seja, para o lado seguro.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self._por_thread: dict[int, int] = {}

    def emit(self, record: logging.LogRecord) -> None:
        try:
            for arg in record.args or ():
                if isinstance(arg, int) and 100 <= arg <= 599:
                    self._por_thread[threading.get_ident()] = arg
        except Exception:  # noqa: BLE001 — logging jamais pode quebrar a chamada
            pass

    def limpar(self) -> None:
        self._por_thread.pop(threading.get_ident(), None)

    def ler(self) -> int | None:
        return self._por_thread.get(threading.get_ident())


_status_visto = _StatusPorThread()
_logger_http = logging.getLogger("terreno.http")
_logger_http.addHandler(_status_visto)
# Deliberadamente NÃO se chama `_logger_http.setLevel(...)` aqui. Fazer isso
# neste ponto do import (antes de `run.py:main()` chamar `logging.basicConfig`)
# travaria o nível de `terreno.http` permanentemente em WARNING pelo resto do
# processo -- `--verbose` (DEBUG) nunca mais conseguiria reverter isso, porque
# um nível explícito em um logger sempre vence o nível efetivo herdado da
# raiz. Foi exatamente isso que aconteceu (2026-08-12): toda mensagem INFO/
# DEBUG de `http.py` -- inclusive "liberado via curl_cffi", a única evidência
# de sucesso do transporte -- ficou invisível em toda execução, sempre, e foi
# o que tornou o comportamento do OLX indecifrável por várias sessões. A
# premissa do guard também não procede: sem NENHUMA configuração de logging,
# a raiz já vem em WARNING por padrão no próprio `logging` -- não há WARNING
# "perdido" a proteger. `_status_visto` só depende de registros WARNING/ERROR
# de qualquer forma (`emit()` só lê status HTTP dos casos de falha), então
# tirar este `setLevel` não muda nada do que esta classe observa.
