"""Розбір заголовка ринку в нормалізоване твердження EventClaim.

Це найбрудніша частина будь-якого такого бота: назви ринків пишуть люди.
Тому підхід тришаровий:
  1) правила (regex) — покривають 90% крипто-цінових ринків;
  2) ручний реєстр перевизначень для важливих ринків (overrides.yaml);
  3) все, що не розпізналось — у карантин, з логом, а не «на око».
НІКОЛИ не торгуємо ринок, чиє правило резолву не розпізнане однозначно.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Optional

from ..models import ClaimKind, EventClaim

ASSET_ALIASES = {
    "ETH": ["eth", "ethereum", "ether"],
    "BTC": ["btc", "bitcoin"],
    "SOL": ["sol", "solana"],
    "XRP": ["xrp", "ripple"],
    "DOGE": ["doge", "dogecoin"],
}

TOUCH_WORDS = r"(hit|reach|touch|test|tap|trade at|get to|dip to|fall to|drop to)"
EXPIRY_WORDS = r"(above|below|over|under|close|end|be)"

_MONTHS = {
    m.lower(): i
    for i, m in enumerate(
        [
            "January", "February", "March", "April", "May", "June",
            "July", "August", "September", "October", "November", "December",
        ],
        start=1,
    )
}
_MONTHS.update({k[:3]: v for k, v in list(_MONTHS.items())})


def _find_asset(text: str) -> Optional[str]:
    low = text.lower()
    for asset, aliases in ASSET_ALIASES.items():
        for a in aliases:
            if re.search(rf"\b{re.escape(a)}\b", low):
                return asset
    return None


def _find_price(text: str) -> Optional[float]:
    """Ціна-поріг. Пріоритет: число з $ або з суфіксом k/m — щоб не сплутати
    поріг із числом дати («June 30»)."""
    patterns = (
        r"\$\s*([0-9][0-9,]*\.?[0-9]*)\s*([kKmM])?",   # $4,000 / $4k
        r"\b([0-9][0-9,]*\.?[0-9]*)\s*([kKmM])\b",     # 4k
        r"\b([0-9][0-9,]{2,}\.?[0-9]*)\b",             # 4000 (>=4 знаки)
    )
    for pat in patterns:
        m = re.search(pat, text)
        if not m:
            continue
        val = float(m.group(1).replace(",", ""))
        suffix = (m.group(2) if m.lastindex and m.lastindex >= 2 else "") or ""
        if suffix.lower() == "k":
            val *= 1_000
        elif suffix.lower() == "m":
            val *= 1_000_000
        return val
    return None


def _mask_dates(text: str) -> str:
    """Прибрати з рядка все, що схоже на дату, щоб не сплутати рік/число з ціною."""
    masked = re.sub(r"\b(19|20)\d{2}\b", " ", text)
    masked = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", " ", masked)
    months = "|".join(sorted(_MONTHS, key=len, reverse=True))
    masked = re.sub(rf"\b({months})\b\.?\s*\d{{0,2}}(st|nd|rd|th)?", " ", masked, flags=re.I)
    return masked


def _find_date(text: str) -> Optional[dt.datetime]:
    low = text.lower()
    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", low)
    if m:
        y, mo, d = (int(x) for x in m.groups())
        return dt.datetime(y, mo, d, 12, tzinfo=dt.timezone.utc)
    m = re.search(r"\b([a-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b", low)
    if m and m.group(1) in _MONTHS:
        return dt.datetime(int(m.group(3)), _MONTHS[m.group(1)], int(m.group(2)), 12, tzinfo=dt.timezone.utc)
    m = re.search(r"\b([a-z]+)\s+(\d{4})\b", low)
    if m and m.group(1) in _MONTHS:
        mo, y = _MONTHS[m.group(1)], int(m.group(2))
        nxt = dt.datetime(y + (mo == 12), (mo % 12) + 1, 1, tzinfo=dt.timezone.utc)
        return nxt - dt.timedelta(days=1)
    return None


def parse_title(
    title: str,
    end_date: Optional[dt.datetime] = None,
    resolution_source: str = "unknown",
) -> Optional[EventClaim]:
    """Повертає EventClaim або None, якщо впевненого розбору немає."""
    asset = _find_asset(title)
    price = _find_price(_mask_dates(title))
    if asset is None or price is None:
        return None
    deadline = end_date or _find_date(title)
    if deadline is None:
        return None
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=dt.timezone.utc)

    low = title.lower()
    down_hint = bool(re.search(r"\b(dip|fall|drop|below|under|down to)\b", low))
    if re.search(TOUCH_WORDS, low):
        kind = ClaimKind.TOUCH_BELOW if down_hint else ClaimKind.TOUCH_ABOVE
    elif re.search(rf"\b{EXPIRY_WORDS}\b", low):
        kind = ClaimKind.BELOW_AT_EXPIRY if down_hint else ClaimKind.ABOVE_AT_EXPIRY
    else:
        return None

    return EventClaim(
        asset=asset,
        kind=kind,
        threshold=price,
        deadline=deadline,
        resolution_source=resolution_source,
        raw_title=title,
    )
