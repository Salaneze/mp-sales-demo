import logging

import httpx

from .models import Order

log = logging.getLogger(__name__)

MP_NAMES = {"wb": "WB", "ozon": "Ozon", "demo": "Демо"}
CHUNK = 3500  # лимит Telegram 4096, запас под длинные названия


def money(x) -> str:
    return f"{int(x):,}".replace(",", " ") + " ₽"


def new_order(o: Order) -> str:
    return f"🛒 Новый заказ · {MP_NAMES.get(o.mp, o.mp)}\n{o.name}\n{money(o.price)} · склад {o.warehouse}"


def cancelled(o: Order) -> str:
    return f"↩️ Отмена · {MP_NAMES.get(o.mp, o.mp)}\n{o.name}, {money(o.price)}"


def low_stock(mp: str, sku: str, name: str, qty: int) -> str:
    head = "⛔ Закончилось" if qty == 0 else "⚠️ Заканчивается"
    return f"{head} · {MP_NAMES.get(mp, mp)}\n{name} ({sku})\nосталось {qty} шт."


def summary(s: dict) -> str:
    parts = " · ".join(f"{MP_NAMES.get(mp, mp)} {n}" for mp, (n, _) in sorted(s["by_mp"].items()))
    return f"📊 Итоги {s['day']}\n{s['orders']} заказов · {money(s['revenue'])}" + (f"\n{parts}" if parts else "")


def pack(texts: list[str]) -> list[str]:
    """Склеивает уведомления в сообщения до CHUNK символов.

    Двадцать заказов за час — это одно сообщение, а не двадцать пушей подряд.
    """
    out, cur = [], ""
    for t in texts:
        if cur and len(cur) + len(t) + 2 > CHUNK:
            out.append(cur.rstrip())
            cur = ""
        cur += t + "\n\n"
    if cur:
        out.append(cur.rstrip())
    return out


def send(token: str, chat_id: str, texts: list[str]) -> None:
    if not (token and chat_id and texts):
        return
    for text in pack(texts):
        r = httpx.post(f"https://api.telegram.org/bot{token}/sendMessage",
                       json={"chat_id": chat_id, "text": text}, timeout=20)
        # Ошибку Telegram присылает телом с ok=false, исключения не будет
        if r.status_code != 200 or not r.json().get("ok"):
            log.error("Telegram не принял сообщение: %s", r.text[:300])
