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


def telegram(listings: list, page_url: str, top_n: int = 8) -> bool:
    token, chat_id = env("TELEGRAM_BOT_TOKEN"), env("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        log.info("telegram not configured — no ping sent")
        return False
    if not listings:
        return False

    lines = [f"🌱 <b>{len(listings)} novo(s) terreno(s)</b>", ""]
    for item in listings[:top_n]:
        area = f"{item.area_ha:g} ha" if item.area_ha else "área n/d"
        ppha = f" · {_fmt_brl(item.price_per_ha)}/ha" if item.price_per_ha else ""
        where = f"{item.municipality}/{item.uf}".strip("/")
        lines.append(
            f'• <a href="{item.url}">{(item.title or "sem título")[:70]}</a>\n'
            f"  {_fmt_brl(item.price)} · {area}{ppha} · {where} · {item.source}"
        )
    if len(listings) > top_n:
        lines.append(f"\n…e mais {len(listings) - top_n}.")
    if page_url:
        lines.append(f'\n<a href="{page_url}">Ver todos</a>')

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
