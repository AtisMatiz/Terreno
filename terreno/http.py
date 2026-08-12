"""Shared HTTP client. Every source goes through this — polite rate limiting,
retries, and a per-host backoff live here once rather than in five scrapers.

Três transportes, em escalada: `requests` -> `curl_cffi` (grátis, imita a
impressão digital de um navegador) -> serviço de desbloqueio pago, este último
desligado por padrão e limitado por um teto de requisições **por processo**
(um contador de módulo, deliberadamente não o `budget_ledger` do SQLite: este
módulo não tem handle de `store` e não deve passar a ter).
"""

from __future__ import annotations

import logging
import os
import random
import re
import threading
import time
from collections import defaultdict
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import requests

log = logging.getLogger("terreno.http")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

DEFAULT_HEADERS = {
    "User-Agent": UA,
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
}

# Seconds to wait between requests to the same host.
MIN_INTERVAL = 1.5

# Hosts that need more room. comprei.pgfn.gov.br resets the connection under
# even a modest burst, so it gets a much slower cadence than the portals.
HOST_INTERVAL = {
    "comprei.pgfn.gov.br": 6.0,
    "nominatim.openstreetmap.org": 1.1,
}

# Layer B now visits candidate pages from several threads at once, so the
# per-host throttle has to be safe under concurrency, not just correct for a
# single caller. A lock guards only the bookkeeping (reserving the next
# allowed slot for a host), never the sleep itself -- holding it during
# time.sleep() would serialize every request onto one host's clock, exactly
# the throughput parallelism is meant to buy back.
_throttle_lock = threading.Lock()
_last_hit: dict[str, float] = defaultdict(float)
_blocked: set[str] = set()

_session = requests.Session()
_session.headers.update(DEFAULT_HEADERS)

# Optional second transport. Several Brazilian portals (Caixa/Radware, OLX,
# Imovelweb/Wimoveis) fingerprint the TLS handshake rather than the headers, so
# they answer 403 to `requests` while letting a real browser through. curl_cffi
# reproduces a browser's handshake and often clears exactly those walls.
#
# It is optional on purpose: `pip install curl_cffi` enables it, its absence
# changes nothing. It was NOT verifiable during development — the sandbox this
# was written in routes through a TLS-terminating proxy, which replaces any
# fingerprint we send, so whether it clears a given portal has to be measured
# where the code actually runs.
# Deliberately catches more than ImportError: curl_cffi ships a compiled
# extension, and when that fails to load (wrong architecture, missing system
# library, half-finished install) the exception is an OSError, not an
# ImportError. Treating only ImportError as "absent" would report a broken
# install as a missing one -- two problems whose fixes have nothing in common.
# The reason is kept so it can be shown instead of a bare "not installed".
try:
    from curl_cffi import requests as _cffi
    _cffi_erro = ""
except Exception as _exc:  # noqa: BLE001 — see above
    _cffi = None
    _cffi_erro = f"{type(_exc).__name__}: {_exc}"

# Which browser curl_cffi imitates. Overridable without a code change because
# anti-bot vendors ship fingerprint databases at their own pace: when one
# target stops working, the next one along often still does, and finding that
# out is a one-command experiment (TERRENO_IMPERSONATE=chrome131 ...) rather
# than an edit-commit-rerun cycle.
IMPERSONATE = os.getenv("TERRENO_IMPERSONATE", "chrome")

# ...and the rest of the ladder, tried in order when the first one is refused.
#
# Not speculative: measured on a GitHub Actions runner, 2026-08-12. OLX answers
# 403 to `chrome` and `chrome124`, and 200 to `safari` and `firefox`. A single
# fixed target therefore reports "blocked" for a host that is not blocked at
# all -- the wall is fingerprint-specific, so the choice of imitation *is* the
# result, and one guess cannot be the answer. Anti-bot vendors update their
# databases per browser at their own pace, so which target works rotates over
# time; walking the ladder finds today's answer without anyone editing code.
IMPERSONATE_ESCADA = [
    alvo.strip() for alvo in
    os.getenv("TERRENO_IMPERSONATE_ESCADA", "chrome,safari,firefox,chrome124").split(",")
    if alvo.strip()
]
if IMPERSONATE not in IMPERSONATE_ESCADA:
    IMPERSONATE_ESCADA.insert(0, IMPERSONATE)

_cffi_ok: set[str] = set()      # hosts the browser transport got through
_cffi_falhou: set[str] = set()  # ...and hosts where every rung was refused
# host -> the rung that actually worked, so later requests to it skip straight
# there instead of re-walking the ladder and re-paying for the refusals.
_cffi_alvo: dict[str, str] = {}

# Headers that describe *who is asking* rather than *what is being asked for*.
# curl_cffi's `impersonate` sets a complete, self-consistent set of these to
# match the TLS and HTTP/2 handshake it performs. Overriding any of them
# re-creates exactly the mismatch the impersonation exists to remove: a
# "Chrome 124 on Windows" User-Agent arriving over a current-Chrome handshake
# is not a neutral detail, it is itself a bot signal to Cloudflare/DataDome,
# which cross-check the two. Passing DEFAULT_HEADERS into curl_cffi -- which
# this module did until now -- therefore sabotaged the very fallback it was
# calling, and did it silently.
_HEADERS_DE_IMPRESSAO = {
    "user-agent", "accept", "accept-language", "accept-encoding",
    "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform",
    "sec-fetch-dest", "sec-fetch-mode", "sec-fetch-site", "sec-fetch-user",
}


def _headers_semanticos(headers: dict | None) -> dict:
    """Só os cabeçalhos que dizem o que estamos pedindo (x-domain, Origin,
    Referer, tokens de API) -- nunca os que dizem quem somos."""
    return {k: v for k, v in (headers or {}).items()
            if k.lower() not in _HEADERS_DE_IMPRESSAO}


# ---------------------------------------------------------------------------
# Terceiro transporte: serviço de desbloqueio (pago), só como último recurso.
#
# Por que existe: `olx.com.br` e `imovelweb.com.br` devolvem 403 e o
# `comprei.pgfn.gov.br` recusa no handshake TLS *tanto do CI quanto de um IP
# residencial brasileiro* — medido. Isso elimina o IP como causa, e com ele
# elimina o proxy comum como solução: trocar de IP muda justamente a única
# coisa já provada irrelevante. O que ainda pode ajudar é um serviço que traz
# a própria impressão digital, navegador sem cabeça e resolução de desafio. O
# `curl_cffi` acima é a tentativa gratuita da mesma ideia; isto é o pago, para
# quando ela não basta.
#
# Desligado por padrão, e por padrão significa "não muda nada": só liga quando
# TERRENO_UNBLOCKER nomeia um provedor E a chave dele está no ambiente.
UNBLOCKER_ENV = "TERRENO_UNBLOCKER"
UNBLOCKER_MAX_ENV = "TERRENO_UNBLOCKER_MAX_POR_RUN"
UNBLOCKER_MAX_PADRAO = 15

# O tempo limite normal (25s) é curto para um serviço que abre um navegador
# sem cabeça e pode ter de resolver um desafio antes de responder.
UNBLOCKER_TIMEOUT_MIN = 60

ZENROWS_ENDPOINT = "https://api.zenrows.com/v1/"


class _ZenRows:
    """ZenRows Universal Scraper API.

    A URL de destino vai como UM valor percent-encoded do parâmetro `url`.
    Isso não é detalhe de estilo: vários chamadores passam `params=`
    (`sources/olx.py`, `sources/htmlportal.py`, `sources/pgfn.py`), e repassar
    esses params para o endpoint da ZenRows os transformaria em *opções da
    ZenRows*, não em params da URL de destino — a requisição buscaria
    silenciosamente a página errada. Por isso `_mesclar_params` dobra tudo
    dentro do destino antes de codificar.
    """

    nome = "zenrows"
    env_chave = "ZENROWS_API_KEY"

    def __init__(self, chave: str) -> None:
        self.chave = chave

    def montar(self, destino: str, headers: dict | None = None, *,
              js_render: bool = False) -> str:
        opcoes = {
            "apikey": self.chave,
            "url": destino,
            "premium_proxy": "true",
            "proxy_country": "br",
        }
        if js_render:
            opcoes["js_render"] = "true"
        if headers:
            # Sem isto a ZenRows ignora cabeçalhos enviados na requisição —
            # e um `Authorization` ou `x-domain` silenciosamente descartado
            # vira um 401 do destino que parece bloqueio.
            opcoes["custom_headers"] = "true"
        return ZENROWS_ENDPOINT + "?" + urlencode(opcoes, quote_via=quote)


# Registro de provedores: acrescentar um segundo é acrescentar uma classe e uma
# entrada aqui, sem reorganizar nada. Só o ZenRows está implementado.
_UNBLOCKER_PROVEDORES = {_ZenRows.nome: _ZenRows}

_unblocker_ok: set[str] = set()      # hosts que o desbloqueador liberou
_unblocker_falhou: set[str] = set()  # ...e hosts onde foi tentado e não passou

# Teto de crédito por execução. É obrigatório, não opcional: o
# `brave_visit.py` percorre a fila pendente inteira (hoje ~577 URLs) e uma
# execução recente registrou 22 hosts bloqueados distintos. Sem teto, uma
# execução poderia queimar a cota de um mês (uma requisição premium na ZenRows
# custa 10 créditos; a faixa gratuita são 5.000/mês, ~500 requisições).
#
# É de propósito um contador de processo, NÃO o `budget_ledger` do SQLite:
# este módulo não tem (e não deve passar a ter) um handle de `store`. A
# consequência honesta é que o teto vale por processo — duas execuções
# simultâneas têm um teto cada.
_unblocker_lock = threading.Lock()
_unblocker_gastos = 0
_unblocker_cap_avisado = False
_unblocker_config_avisada = False


def _unblocker_cap() -> int:
    bruto = os.getenv(UNBLOCKER_MAX_ENV, "")
    try:
        return int(bruto)
    except ValueError:
        if bruto:
            log.warning("%s=%r não é um número inteiro — usando o padrão %d",
                        UNBLOCKER_MAX_ENV, bruto, UNBLOCKER_MAX_PADRAO)
        return UNBLOCKER_MAX_PADRAO


def _unblocker_ativo():
    """O provedor configurado, ou None. Lido do ambiente a cada chamada para
    que ligar/desligar seja uma variável de ambiente, não um reimport."""
    global _unblocker_config_avisada
    nome = (os.getenv(UNBLOCKER_ENV) or "").strip().lower()
    if nome in ("", "0", "false", "no", "off", "nao", "não"):
        return None
    classe = _UNBLOCKER_PROVEDORES.get(nome)
    if classe is None:
        if not _unblocker_config_avisada:
            _unblocker_config_avisada = True
            log.warning("%s=%r não é um provedor conhecido (conhecidos: %s) — "
                        "desbloqueador desligado", UNBLOCKER_ENV, nome,
                        ", ".join(sorted(_UNBLOCKER_PROVEDORES)))
        return None
    chave = (os.getenv(classe.env_chave) or "").strip()
    if not chave:
        if not _unblocker_config_avisada:
            _unblocker_config_avisada = True
            log.warning("%s=%s pedido mas %s não está no ambiente — "
                        "desbloqueador desligado", UNBLOCKER_ENV, nome,
                        classe.env_chave)
        return None
    return classe(chave)


def _redigir(texto: str, chave: str = "") -> str:
    """Nunca deixar a chave de API sair no log — nem embutida numa URL que a
    própria `requests` põe na mensagem da exceção."""
    limpo = texto or ""
    if chave:
        limpo = limpo.replace(chave, "REDACTED")
    return re.sub(r"(?i)(apikey=)[^&\s\"'>]+", r"\1REDACTED", limpo)


def _mesclar_params(url: str, params: dict | None) -> str:
    """Dobra `params=` dentro da própria URL de destino, preservando os que já
    estavam na query."""
    if not params:
        return url
    partes = urlsplit(url)
    pares = parse_qsl(partes.query, keep_blank_values=True)
    pares += [(k, v) for k, v in params.items() if v is not None]
    return urlunsplit(partes._replace(query=urlencode(pares, doseq=True)))


def _unblocker_cap_atingido() -> bool:
    global _unblocker_cap_avisado
    cap = _unblocker_cap()
    with _unblocker_lock:
        if _unblocker_gastos < cap:
            return False
        avisar = not _unblocker_cap_avisado
        _unblocker_cap_avisado = True
        gastos = _unblocker_gastos
    if avisar:
        log.warning("desbloqueador: teto de %d requisições por execução "
                    "atingido (%d usadas) — nenhuma outra será feita nesta "
                    "execução; ajuste com %s", cap, gastos, UNBLOCKER_MAX_ENV)
    return True


def _throttle(host: str) -> None:
    """Reserve this thread's slot for `host`, then sleep outside the lock.

    Two threads hitting *different* hosts never wait on each other at all --
    each reservation only depends on that host's own last slot. Two threads
    hitting the *same* host get staggered by the reservation itself, so the
    politeness guarantee holds even under concurrency.
    """
    interval = HOST_INTERVAL.get(host, MIN_INTERVAL) + random.uniform(0, 0.4)
    with _throttle_lock:
        now = time.monotonic()
        proxima_vez = max(now, _last_hit[host] + interval)
        _last_hit[host] = proxima_vez
    espera = proxima_vez - time.monotonic()
    if espera > 0:
        time.sleep(espera)


def get(url: str, *, params: dict | None = None, headers: dict | None = None,
        timeout: int = 25, retries: int = 3, json: bool = False):
    """GET with backoff. Returns Response, parsed JSON, or None.

    Never raises for a dead source: a failing portal degrades that source to
    zero results and is logged, rather than taking the whole run down.
    """
    host = urlsplit(url).netloc
    if host in _blocked:
        return None

    for attempt in range(retries):
        # A host where the browser transport already proved necessary goes
        # straight to it: repeating a plain attempt we have measured to fail
        # only spends another throttle slot to learn the same thing again.
        if host in _cffi_ok:
            _throttle(host)
            # Must pass the rung that actually worked for this host. Without
            # it this fast path silently retried the *default* imitation --
            # the very one already measured to be refused -- so a host cleared
            # by `safari` paid a guaranteed 403 on every single request before
            # falling through and finding safari again.
            pronto = _via_cffi(url, params, headers, timeout, json,
                               alvo=_cffi_alvo.get(host))
            if pronto is not None:
                return pronto

        # Mesma economia para o desbloqueador, mas só quando o transporte
        # gratuito já foi medido como insuficiente neste host: ir direto ao
        # pago sem isso gastaria crédito onde o curl_cffi resolveria.
        if host in _unblocker_ok and host in _cffi_falhou:
            pronto = _tentar_unblocker(url, params, headers, timeout, json,
                                       host, motivo=" (host já sabido)")
            if pronto is not None:
                return pronto

        _throttle(host)
        try:
            r = _session.get(url, params=params, headers=headers, timeout=timeout)
        except requests.RequestException as exc:
            log.warning("%s: %s (attempt %d)", host, exc, attempt + 1)
            # A connection torn down at the TLS layer (SSLZeroReturnError,
            # connection reset) is a *rejection*, not an outage -- and it is
            # precisely the shape a fingerprint block takes when the wall sits
            # in front of the server rather than in it. Until now curl_cffi was
            # only ever reached from the 403/429 branch below, so this whole
            # class of block -- PGFN's, measured repeatedly -- was recorded as
            # "curl_cffi doesn't help here" without curl_cffi ever being tried.
            alt = _tentar_cffi(url, params, headers, timeout, json, host,
                               motivo=f" (após {type(exc).__name__})")
            if alt is not None:
                return alt
            # Escalada: requests -> curl_cffi -> desbloqueador. O grátis
            # primeiro, sempre; o pago só quando ele não bastou.
            alt = _tentar_unblocker(url, params, headers, timeout, json, host,
                                    motivo=f" (após {type(exc).__name__})")
            if alt is not None:
                return alt
            time.sleep(2 ** attempt)
            continue

        if r.status_code == 200:
            if json:
                try:
                    return r.json()
                except ValueError:
                    log.warning("%s: response was not JSON", host)
                    return None
            return r

        if r.status_code in (403, 429):
            # Try the browser-fingerprint transport before backing off.
            alt = _tentar_cffi(url, params, headers, timeout, json, host,
                               motivo=f" (após HTTP {r.status_code})")
            if alt is not None:
                return alt
            alt = _tentar_unblocker(url, params, headers, timeout, json, host,
                                    motivo=f" (após HTTP {r.status_code})")
            if alt is not None:
                return alt
            # The body goes in the log for the same reason the generic-4xx
            # branch below does it: "token inválido", "scope insuficiente" and
            # "your IP is blocked" all arrive as a bare 403, and without the
            # body they are indistinguishable -- which is precisely the
            # confusion that had Mercado Livre's auth failure filed as an IP
            # block for weeks.
            trecho = (r.text or "")[:200].replace("\n", " ")
            log.warning("%s: HTTP %s — backing off%s", host, r.status_code,
                        f" — {trecho}" if trecho else "")
            time.sleep(3 * (attempt + 1))
            if attempt == retries - 1:
                # Persistent wall: stop hammering this host for the rest of the
                # run, and make it visible rather than reporting "0 results".
                _blocked.add(host)
                log.error("%s: blocked after %d attempts", host, retries)
            continue

        if 500 <= r.status_code < 600:
            time.sleep(2 ** attempt)
            continue

        # A 4xx here means the request itself was rejected — a bad or
        # unsupported parameter, not a wall to back off from. The body usually
        # says exactly what was wrong (Brave's 422s do), and without it a bad
        # parameter looks identical to a dead source until someone downloads
        # the workflow log and greps for it by hand.
        trecho = (r.text or "")[:400].replace("\n", " ")
        log.warning("%s: HTTP %s — %s", host, r.status_code, trecho)
        return None
    return None


def _tentar_cffi(url, params, headers, timeout, want_json, host, motivo=""):
    """Uma tentativa com o transporte de navegador por host por execução,
    dizendo no log o que aconteceu.

    O silêncio era o problema real da versão anterior: a falha do curl_cffi
    só aparecia em DEBUG, então uma execução normal não distinguia "o
    curl_cffi foi tentado e não passou" de "o curl_cffi nem existe aqui" --
    e as duas pedem providências opostas.
    """
    if _cffi is None:
        if host not in _cffi_falhou:
            _cffi_falhou.add(host)
            log.warning("%s: bloqueado e o curl_cffi não pôde ser carregado "
                        "(%s) — `python3 -m pip install curl_cffi` (com o -m, "
                        "para instalar no mesmo Python que roda isto aqui) "
                        "habilita a tentativa com impressão digital de navegador",
                        host, _cffi_erro or "não instalado")
        return None
    if host in _cffi_falhou:
        return None

    # A rung already known to work for this host goes first and alone; the
    # ladder is only walked while the answer is still unknown.
    conhecido = _cffi_alvo.get(host)
    alvos = [conhecido] if conhecido else IMPERSONATE_ESCADA

    for alvo in alvos:
        resultado = _via_cffi(url, params, headers, timeout, want_json,
                              motivo=motivo, alvo=alvo)
        if resultado is not None:
            _cffi_ok.add(host)
            _cffi_alvo[host] = alvo
            log.info("%s: liberado via curl_cffi (impersonate=%s)%s",
                     host, alvo, motivo)
            return resultado

    _cffi_falhou.add(host)
    if len(alvos) > 1:
        log.warning("%s: curl_cffi recusado em todas as imitações (%s)",
                    host, ", ".join(alvos))
    return None


def _via_cffi(url, params, headers, timeout, want_json, *, motivo="", alvo=None):
    """One attempt with a browser TLS fingerprint. Never raises.

    Deliberately does not forward DEFAULT_HEADERS -- see
    `_HEADERS_DE_IMPRESSAO` for why sending our own User-Agent here defeats
    the impersonation instead of helping it.

    `alvo` names the browser to imitate; None means the configured default.
    Callers that walk `IMPERSONATE_ESCADA` pass one rung at a time.
    """
    if _cffi is None:
        return None
    alvo = alvo or IMPERSONATE
    host = urlsplit(url).netloc
    try:
        r = _cffi.get(
            url, params=params,
            headers=_headers_semanticos(headers),
            timeout=timeout, impersonate=alvo,
        )
    except Exception as exc:  # noqa: BLE001 — optional path, must not break a run
        log.warning("%s: curl_cffi (impersonate=%s) falhou%s: %s: %s",
                    host, alvo, motivo, type(exc).__name__, exc)
        return None
    if r.status_code != 200:
        log.warning("%s: curl_cffi (impersonate=%s) devolveu HTTP %s%s",
                    host, alvo, r.status_code, motivo)
        return None
    if want_json:
        try:
            return r.json()
        except ValueError:
            log.warning("%s: curl_cffi passou mas a resposta não era JSON", host)
            return None
    return r


def _tentar_unblocker(url, params, headers, timeout, want_json, host, motivo=""):
    """Último recurso, uma tentativa por host por execução, com o resultado
    dito no log.

    Mesma disciplina do `_tentar_cffi`: um host já sabido como "precisa do
    desbloqueador" ou "nem com ele passa" não repete tentativas condenadas a
    cada retry — e aqui isso não é só tempo perdido, é crédito pago.
    """
    prov = _unblocker_ativo()
    if prov is None:
        return None
    if host in _unblocker_falhou:
        return None
    if _unblocker_cap_atingido():
        # Não marca o host como fracassado: o que faltou foi orçamento, não
        # capacidade — e confundir os dois esconderia um host que o
        # desbloqueador resolveria.
        return None

    resultado = _via_unblocker(url, params, headers, timeout, want_json,
                               motivo=motivo, prov=prov)
    if resultado is None:
        _unblocker_falhou.add(host)
    else:
        _unblocker_ok.add(host)
        log.info("%s: liberado via desbloqueador %s%s", host, prov.nome, motivo)
    return resultado


def _via_unblocker(url, params, headers, timeout, want_json, *, motivo="",
                   prov=None):
    """Uma tentativa pelo serviço de desbloqueio. Nunca levanta exceção.

    Devolve exatamente as mesmas formas que o `get()` já devolve — um objeto
    `Response` (com `.text`, `.headers["content-type"]` e `.content`, que os
    chamadores usam: ver `extract/imagem.py` e `sources/caixa.py`) ou o JSON já
    convertido quando `json=True` — para que nenhum chamador precise mudar.
    """
    if prov is None:
        prov = _unblocker_ativo()
        if prov is None:
            return None
        if _unblocker_cap_atingido():
            return None
    host = urlsplit(url).netloc
    destino = _mesclar_params(url, params)
    # `_headers_semanticos` existe para o curl_cffi, onde Accept/User-Agent são
    # parte do que a imitação de navegador já define de forma coerente com o
    # handshake TLS -- mandar o nosso por cima quebraria essa coerência. O
    # ZenRows não é isso: é um proxy remoto que repassa exatamente os
    # cabeçalhos que dermos (via `custom_headers=true`), então aqui Accept é
    # negociação de conteúdo real, não sinal de impressão digital. Medido
    # 2026-08-12: filtrar o Accept aqui fez o PGFN devolver XML em vez do JSON
    # que `?Accept: application/json` pede, e `desbloqueador zenrows passou
    # mas a resposta não era JSON` escondeu isso atrás de um erro genérico.
    semanticos = dict(headers or {})

    def _uma_tentativa(js_render: bool):
        global _unblocker_gastos
        with _unblocker_lock:
            _unblocker_gastos += 1
            usados = _unblocker_gastos
        log.info("%s: tentando o desbloqueador %s%s (%d/%d nesta execução)%s",
                 host, prov.nome, " com js_render" if js_render else "",
                 usados, _unblocker_cap(), motivo)
        pedido = prov.montar(destino, semanticos, js_render=js_render)
        try:
            return _session.get(pedido, headers=semanticos,
                                timeout=max(timeout, UNBLOCKER_TIMEOUT_MIN))
        except requests.RequestException as exc:
            log.warning("%s: desbloqueador %s falhou%s: %s: %s", host, prov.nome,
                        motivo, type(exc).__name__,
                        _redigir(str(exc), prov.chave))
            return None

    r = _uma_tentativa(js_render=False)

    # RESP001 ("Could not get content") e RESP002 ("Page not found") são a
    # ZenRows dizendo que não conseguiu buscar a página de verdade com o
    # transporte simples -- medido 2026-08-12: olx/imovelweb/mercadolivre-api
    # devolveram RESP001; wimoveis devolveu RESP002 (404) para a mesma URL que
    # funciona direto de uma conexão residencial, o que é a assinatura de um
    # desafio JS do Cloudflare que o modo simples da ZenRows não resolve, não
    # de a página realmente não existir. A documentação da própria ZenRows
    # aponta `js_render=true` como o próximo passo para RESP001; a mesma causa
    # provável (desafio JS) vale para RESP002. Essa segunda tentativa (mais
    # cara: ~25 créditos contra ~10) só acontece para esses dois códigos
    # específicos, nunca para qualquer outro erro -- não vale gastar o dobro
    # em cima de um 401/403 que js_render não resolve.
    if (r is not None and r.status_code in (422, 404)
            and any(c in (r.text or "") for c in ("RESP001", "RESP002"))
            and not _unblocker_cap_atingido()):
        codigo = "RESP001" if "RESP001" in (r.text or "") else "RESP002"
        log.info("%s: %s do desbloqueador -- tentando de novo com js_render",
                 host, codigo)
        r2 = _uma_tentativa(js_render=True)
        if r2 is not None:
            r = r2

    if r is None:
        return None

    if r.status_code != 200:
        # A ZenRows devolve erros explícitos em JSON (plano errado, chave
        # inválida, créditos esgotados). Mostrar o corpo é a diferença entre
        # "a parede venceu" e "sua conta está mal configurada" — e este módulo
        # foi consertado justamente porque um fallback que falhava em silêncio
        # era indistinguível de uma dependência ausente.
        trecho = _redigir((r.text or "")[:400].replace("\n", " "), prov.chave)
        log.warning("%s: desbloqueador %s devolveu HTTP %s%s%s", host,
                    prov.nome, r.status_code, motivo,
                    f" — {trecho}" if trecho else "")
        return None

    if want_json:
        try:
            return r.json()
        except ValueError:
            trecho = _redigir((r.text or "")[:200].replace("\n", " "),
                              prov.chave)
            log.warning("%s: desbloqueador %s passou mas a resposta não era "
                        "JSON — %s", host, prov.nome, trecho)
            return None
    return r


def get_json(url: str, **kw):
    return get(url, json=True, **kw)


def blocked_hosts() -> list[str]:
    """Hosts that walled us off this run — surfaced in the run report."""
    return sorted(_blocked)
