"""Telegram ping when a run turns up something new.

Silent by default: no new matches means no message, so the channel stays worth
reading. Failure to notify never fails the run.
"""

from __future__ import annotations

import logging

from . import http
from .config import env

log = logging.getLogger("terreno.notify")

API = "https://api.telegram.org/bot{token}/sendMessage"


def _fmt_brl(value) -> str:
    if not value:
        return "preço não informado"
    return f"R$ {value:,.0f}".replace(",", ".")


def _esc(text) -> str:
    """Telegram's HTML parse mode is a small tag subset, not a browser -- a
    listing title with a stray '<' or '&' (both common: 'área <2km', 'água &
    energia') makes Telegram reject the whole message rather than degrade
    gracefully. Every scraped field going into the message text is untrusted
    for this reason and must be escaped, same as the page template."""
    return (str(text or "")
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def telegram(listings: list, page_url: str, top_n: int = 8,
              alertas: list[str] | None = None) -> bool:
    token, chat_id = env("TELEGRAM_BOT_TOKEN"), env("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        log.info("telegram not configured — no ping sent")
        return False
    if not listings and not alertas:
        return False

    lines: list[str] = []

    if alertas:
        lines.append("⚠️ <b>Fontes sem resultado</b>")
        lines.extend(f"• {_esc(a)}" for a in alertas)
        lines.append("")

    if listings:
        lines.append(f"🌱 <b>{len(listings)} novo(s) terreno(s)</b>")
        lines.append("")
        for item in listings[:top_n]:
            area = f"{item.area_ha:g} ha" if item.area_ha else "área n/d"
            ppha = f" · {_fmt_brl(item.price_per_ha)}/ha" if item.price_per_ha else ""
            where = _esc(f"{item.municipality}/{item.uf}".strip("/"))
            titulo = _esc((item.title or "sem título")[:70])
            url = _esc(item.url)
            lines.append(
                f'• <a href="{url}">{titulo}</a>\n'
                f"  {_fmt_brl(item.price)} · {area}{ppha} · {where} · {_esc(item.source)}"
            )
        if len(listings) > top_n:
            lines.append(f"\n…e mais {len(listings) - top_n}.")

    if page_url:
        lines.append(f'\n<a href="{_esc(page_url)}">Ver todos</a>')

    resp = http.get(
        API.format(token=token),
        params={
            "chat_id": chat_id,
            "text": "\n".join(lines)[:4000],
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        },
        retries=2,
    )
    ok = resp is not None
    if not ok:
        log.warning("telegram: send failed")
    return ok
