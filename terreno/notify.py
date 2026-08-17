"""Telegram ping when a run turns up something new.

A run with nothing to report still gets a one-line "Nenhum resultado hoje"
ping (see NENHUM_RESULTADO) -- found 2026-08-17: the old "silent means no
message" design was indistinguishable, from the owner's side, from the run
never having happened at all (workflow failure, cron not firing, secrets
missing). One short line costs nothing and turns that silence into a
confirmed "ran, found nothing" instead of an open question. Failure to
notify never fails the run.

Message building (``build_messages``) is deliberately separated from sending
(``telegram``) so the exact rendered text can be inspected without touching the
network.

This module sends whatever list it is handed: no filtering, no re-sorting. The
caller (pipeline) decides which listings deserve a ping and in what order.
"""

from __future__ import annotations

import logging

from . import http
from .config import env

log = logging.getLogger("terreno.notify")

API = "https://api.telegram.org/bot{token}/sendMessage"

# Telegram's hard limit is 4096 characters per message. We stop adding whole
# cards before crossing it -- never slicing mid-card, and above all never
# mid-tag: an unclosed <a> is a parse error that makes Telegram reject the
# entire message, so a naive text[:4000] trades a truncated list for nothing
# at all.
LIMIT = 4096

# Cards are ~6 lines each, so roughly 10-12 fit per message. Rather than
# choosing between "one message, few cards" and "unbounded spam", cards are
# packed into whole messages up to this cap: the owner gets a useful number of
# full cards, the channel never gets flooded, and anything past the cap is
# reported honestly at the end with a pointer to the full list.
MAX_MESSAGES = 3

HEADER = "🌿 <b>NOVO IMÓVEL ENCONTRADO - VALE DO PARAÍBA</b> 🌿"
HEADER_CONT = "🌿 <b>NOVO IMÓVEL ENCONTRADO - VALE DO PARAÍBA</b> 🌿 (continuação)"

# Sent instead of nothing when a run has no new listings and no health
# alerts -- a heartbeat, not a card, so it stays a single short line rather
# than reusing HEADER (which would read as "new property" when there isn't
# one).
NENHUM_RESULTADO = "🌿 Terreno: nenhum resultado novo hoje."

# Score bands -> Portuguese qualitative label. Listing.score is a 0..1 float;
# it is shown as an integer out of 100, so the bands are stated in the same
# 0..100 terms:
#   >= 85  Excelente   -- ticks essentially every criterion
#   70-84  Muito bom   -- strong candidate, minor gaps
#   55-69  Bom         -- worth a look, real trade-offs
#   < 55   Regular     -- borderline, listed for completeness
SCORE_BANDS = ((85, "Excelente"), (70, "Muito bom"), (55, "Bom"))
SCORE_FALLBACK = "Regular"

# terreno.scoring fills Listing.destaques with these optional keys; a key is
# absent when there is no evidence for it, and absent means the line is simply
# not printed (never "n/d", never filler).
DESTAQUES = (
    ("agua", "💧", "Água"),
    ("benfeitorias", "🏠", "Benfeitorias"),
    ("acesso", "🚗", "Acesso"),
    ("documentacao", "📜", "Doc"),
    ("solo", "🌲", "Solo"),
)

# Only these alqueire variants have a determined name. Anything else (including
# "") means the variant was not determined, and no variant name is printed.
# Must stay in step with _ALQUEIRE_TIPO in terreno/units.py -- a variant the
# units module can produce but this dict lacks would silently print the neutral
# word "alqueires" instead of the determined name, which reads as "we could not
# tell" when in fact we could.
ALQUEIRE_NOMES = {
    "paulista": "Alqueire Paulista",
    "mineiro": "Alqueire Mineiro",
    "norte": "Alqueire do Norte",
    "amazonense": "Alqueire Amazonense",
}


def _fmt_brl(value) -> str:
    if not value:
        return "preço não informado"
    return f"R$ {value:,.0f}".replace(",", ".")


def _fmt_num(value, decimals: int = 1) -> str:
    """Brazilian number formatting: dot thousands, comma decimal.

    4.5 -> "4,5"; 12.0 -> "12"; 1200.5 -> "1.200,5".
    """
    txt = f"{float(value):,.{decimals}f}"
    txt = txt.replace(",", "\x00").replace(".", ",").replace("\x00", ".")
    if "," in txt:
        txt = txt.rstrip("0").rstrip(",")
    return txt


def _esc(text) -> str:
    """Telegram's HTML parse mode is a small tag subset, not a browser -- a
    listing title with a stray '<' or '&' (both common: 'área <2km', 'água &
    energia') makes Telegram reject the whole message rather than degrade
    gracefully. Every scraped field going into the message text is untrusted
    for this reason and must be escaped, same as the page template."""
    return (str(text or "")
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _score_label(score01: float) -> str:
    pts = int(round((score01 or 0) * 100))
    for floor, label in SCORE_BANDS:
        if pts >= floor:
            return f"{pts}/100 ({label})"
    return f"{pts}/100 ({SCORE_FALLBACK})"


def _alqueire_unidade(alq: float, tipo: str) -> str:
    nome = ALQUEIRE_NOMES.get((tipo or "").strip().lower(), "")
    if nome:
        return nome
    # Variant undetermined: show the figure, name no variant.
    return "alqueire" if abs(alq - 1) < 1e-9 else "alqueires"


def _area_text(item) -> str:
    # getattr keeps this working whether or not the alqueire fields have landed
    # on Listing yet.
    ha = getattr(item, "area_ha", None)
    alq = getattr(item, "area_alqueires", None)
    tipo = getattr(item, "area_alqueire_tipo", "") or ""
    if ha:
        txt = f"{_fmt_num(ha)} ha"
        if alq:
            txt += f" (approx. {_fmt_num(alq)} {_alqueire_unidade(alq, tipo)})"
        return txt
    if alq:
        # No hectare figure to convert from, and we never fabricate one.
        return f"{_fmt_num(alq)} {_alqueire_unidade(alq, tipo)}"
    return "área não informada"


def _where(item) -> str:
    muni = (getattr(item, "municipality", "") or "").strip()
    uf = (getattr(item, "uf", "") or "").strip()
    if muni and uf:
        return f"{muni} - {uf}"
    return muni or uf or "não informada"


def render_card(item, index: int) -> str:
    """One property card, fully escaped and self-contained (no dangling tags)."""
    titulo = _esc((getattr(item, "title", "") or "sem título").strip()[:80])
    lines = [
        f"  # {index} - <b>{titulo}</b>",
        f"📍 <b>Localização:</b> {_esc(_where(item))}",
        f"💰 <b>Preço:</b> {_fmt_brl(getattr(item, 'price', None))}",
        f"📐 <b>Área:</b> {_esc(_area_text(item))}",
        f"⭐ <b>Pontuação:</b> {_score_label(getattr(item, 'score', 0.0))}",
    ]

    # Starred features (Listing.estrelas) get their own line right under the
    # score -- above the Destaques block, so a cachoeira is impossible to miss
    # even when the card is long. Not mandatory, so absent means no line.
    estrelas = [e for e in (getattr(item, "estrelas", None) or []) if str(e).strip()]
    if estrelas:
        nomes = ", ".join(_esc(str(e).strip().capitalize()) for e in estrelas)
        lines.append(f"🌟 <b>Em destaque:</b> {nomes}")

    destaques = getattr(item, "destaques", None) or {}
    presentes = [(emoji, rotulo, destaques[chave])
                 for chave, emoji, rotulo in DESTAQUES
                 if str(destaques.get(chave, "") or "").strip()]
    if presentes:
        lines.append("")
        lines.append("<b>Destaques:</b>")
        lines.extend(f"{emoji} <b>{rotulo}:</b> {_esc(str(valor).strip())}"
                     for emoji, rotulo, valor in presentes)

    url = _esc(getattr(item, "url", "") or "")
    if url:
        lines.append("")
        lines.append(f'🔗 <b>Ver Anúncio Original:</b> <a href="{url}">'
                     "Clique aqui para abrir</a>")
    return "\n".join(lines)


def build_messages(listings: list, page_url: str = "", top_n: int = 8,
                   alertas: list[str] | None = None,
                   limit: int = LIMIT,
                   max_messages: int = MAX_MESSAGES) -> list[str]:
    """Render the ping as a list of ready-to-send message bodies.

    Each body is <= ``limit`` characters and contains only whole cards, so no
    HTML tag is ever cut in half. Returns [] when there is nothing to say.
    """
    listings = list(listings or [])
    alertas = [a for a in (alertas or []) if str(a).strip()]
    if not listings and not alertas:
        return []

    cards = [render_card(item, i)
             for i, item in enumerate(listings[:max(top_n, 0)], 1)]

    def footer(dropped: int) -> str:
        tail: list[str] = []
        if dropped > 0:
            tail.append(f"…e mais {dropped} — ver a lista completa.")
        if page_url:
            tail.append(f'<a href="{_esc(page_url)}">Ver todos</a>')
        return ("\n\n" + "\n".join(tail)) if tail else ""

    # Worst-case footer, reserved in every message so the tail always fits.
    reserve = len(footer(len(listings)))

    head: list[str] = []
    if alertas:
        head.append("⚠️ <b>Fontes sem resultado</b>")
        head.extend(f"• {_esc(a)}" for a in alertas)
        head.append("")
    if cards:
        head.append(HEADER)

    messages: list[str] = []
    current = "\n".join(head) if head else ""
    used = 0

    for card in cards:
        candidate = f"{current}\n\n{card}" if current else card
        if len(candidate) + reserve <= limit:
            current, used = candidate, used + 1
            continue
        # Card does not fit: close this message and open a new one, unless the
        # message budget is spent (then the rest is reported as dropped).
        if len(messages) + 1 >= max_messages:
            break
        messages.append(current)
        current = f"{HEADER_CONT}\n\n{card}"
        if len(current) + reserve > limit:
            # A single card larger than a whole message: skip it rather than
            # emit a truncated one. Practically unreachable, cheap to guard.
            current = HEADER_CONT
            continue
        used += 1

    dropped = len(listings) - used
    messages.append(current + footer(dropped))
    return [m for m in messages if m.strip()]


def telegram(listings: list, page_url: str, top_n: int = 8,
             alertas: list[str] | None = None) -> bool:
    """Send the ping. True only if every message went out."""
    token, chat_id = env("TELEGRAM_BOT_TOKEN"), env("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        log.info("telegram not configured — no ping sent")
        return False

    messages = build_messages(listings, page_url, top_n, alertas) or [NENHUM_RESULTADO]

    ok = True
    for i, text in enumerate(messages, 1):
        resp = http.get(
            API.format(token=token),
            params={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            },
            retries=2,
        )
        if resp is None:
            # Keep going: a later card is still worth delivering.
            ok = False
            log.warning("telegram: send failed (%d/%d)", i, len(messages))
    return ok
