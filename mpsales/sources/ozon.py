"""Ozon: Seller API по ключу из личного кабинета.

Эндпоинты и поля сверены по официальной спецификации
docs.ozon.ru/api/seller/swagger.json 18.10.2026. Списки отправлений
/v3/posting/fbs/list и /v2/posting/fbo/list там уже помечены устаревшими,
отчёт /v2/analytics/stock_on_warehouses — «в будущем отключим», поэтому
здесь их преемники.
"""
import logging
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx

from ..models import Order, Stock

log = logging.getLogger(__name__)

API = "https://api-seller.ozon.ru"

# Ozon отбирает отправления по дате создания, а не по дате изменения, как WB.
# Заказ, сделанный вчера и отменённый сегодня, в окно «последний час» не
# попадёт — поэтому каждый проход перечитывает несколько дней, дубли всё
# равно отсекает ключ заказа
LOOKBACK = timedelta(days=3)

# Статус отмены у FBS не один: есть ещё cancelled_from_split_pending
# (отменено из-за разделения отправления)
def _is_cancelled(status: str) -> bool:
    return status.startswith("cancelled")


class Ozon:
    def __init__(self, client_id: str, api_key: str, client: httpx.Client | None = None):
        self.http = client or httpx.Client(
            base_url=API, headers={"Client-Id": client_id, "Api-Key": api_key}, timeout=60)

    def _post(self, path: str, body: dict) -> dict:
        # Общий лимит Ozon — 50 запросов в секунду, нам до него далеко.
        # Но на 429 документация обещает Retry-After — уважаем его
        for _ in range(5):
            r = self.http.post(path, json=body)
            if r.status_code != 429:
                # Неверный ключ Ozon отдаёт как 400, а суть — в теле ответа:
                # без него «Bad Request» ничего не говорит
                if r.is_error:
                    raise RuntimeError(f"Ozon {path}: {r.status_code} {r.text[:300]}")
                return r.json()
            wait = int(r.headers.get("Retry-After") or 5)
            log.warning("Ozon просит подождать %s с", wait)
            time.sleep(wait)
        r.raise_for_status()
        return r.json()

    def _postings(self, path: str, since: datetime, now: datetime, with_: dict) -> list[dict]:
        out, cursor = [], ""
        body = {
            "filter": {"since": _utc(since), "to": _utc(now)},
            "limit": 100,  # максимум по спецификации
            "with": with_,
        }
        while True:
            if cursor:
                body["cursor"] = cursor
            data = self._post(path, body)
            out += data.get("postings") or []
            cursor = data.get("cursor") or ""
            if not data.get("has_next") or not cursor:
                return out

    def orders(self, since: datetime, now: datetime | None = None) -> list[Order]:
        now = now or datetime.now()
        since = min(since, now - LOOKBACK)
        fbs = self._postings("/v4/posting/fbs/list", since, now, {})
        # Имя склада FBO лежит только в analytics_data — без флага его нет
        fbo = self._postings("/v3/posting/fbo/list", since, now, {"analytics_data": True})
        return ([o for p in fbs for o in posting_orders(p, "fbs")]
                + [o for p in fbo for o in posting_orders(p, "fbo")])

    def stocks(self) -> list[Stock]:
        # Свой склад (FBS/rFBS) — из общего метода остатков: там наличие и
        # резерв, но нет названий складов и товаров. Склады Ozon (FBO) по
        # отдельности отдаёт только аналитика, и только по списку SKU
        items, cursor = [], ""
        while True:
            body = {"filter": {"visibility": "ALL"}, "limit": 1000}
            if cursor:
                body["cursor"] = cursor
            data = self._post("/v4/product/info/stocks", body)
            items += data.get("items") or []
            cursor = data.get("cursor") or ""
            if not data.get("items") or not cursor:
                break

        own, fbo_skus = parse_own_stocks(items)
        fbo_rows = []
        for i in range(0, len(fbo_skus), 100):  # skus — не больше 100 за запрос
            fbo_rows += self._post("/v1/analytics/stocks", {"skus": fbo_skus[i:i + 100]}).get("items") or []
        return own + parse_fbo_stocks(fbo_rows)


def posting_orders(p: dict, scheme: str) -> list[Order]:
    """Отправление -> по строке на каждую единицу товара.

    У WB одна строка отчёта — одна единица. Ozon кладёт в отправление
    несколько товаров и количество у каждого; раскладываем так же, чтобы
    таблица, «новый заказ» и итоги дня считали одинаково для обоих.
    """
    if scheme == "fbo":
        warehouse = (p.get("analytics_data") or {}).get("warehouse_name") or "Ozon"
    else:
        warehouse = (p.get("delivery_method") or {}).get("warehouse") or "Свой склад"
    raw = p.get("in_process_at") or p.get("created_at")
    # Ozon отдаёт UTC, а всё остальное в скрипте — местное время машины
    date = datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone().replace(tzinfo=None)
    cancelled = _is_cancelled(p.get("status", ""))
    out = []
    for prod in p.get("products") or []:
        price = Decimal(str((prod.get("price") or {}).get("amount") or 0))
        sku = prod.get("offer_id") or str(prod["sku"])
        for i in range(int(prod.get("quantity") or 1)):
            out.append(Order("ozon", f"{p['posting_number']}/{sku}/{i}", date, sku,
                             prod.get("name", sku), price, f"{warehouse} ({scheme.upper()})", cancelled))
    return out


def parse_own_stocks(items: list[dict]) -> tuple[list[Stock], list[int]]:
    own, fbo_skus = [], []
    for it in items:
        sku = it.get("offer_id") or str(it.get("product_id"))
        free = 0
        for s in it.get("stocks") or []:
            if s.get("type") == "fbo":
                fbo_skus.append(int(s["sku"]))
            elif s.get("type") in ("fbs", "rfbs"):
                # Зарезервированное уже продано — к продаже его считать нельзя
                free += max(int(s.get("present") or 0) - int(s.get("reserved") or 0), 0)
        if any(s.get("type") in ("fbs", "rfbs") for s in it.get("stocks") or []):
            own.append(Stock("ozon", sku, sku, "Свой склад (FBS)", free))
    return own, sorted(set(fbo_skus))


def parse_fbo_stocks(rows: list[dict]) -> list[Stock]:
    return [
        Stock("ozon", r.get("offer_id") or str(r["sku"]), r.get("name") or "",
              f"{r.get('warehouse_name') or r.get('cluster_name') or 'Ozon'} (FBO)",
              int(r.get("available_stock_count") or 0))
        for r in rows
    ]


def _utc(d: datetime) -> str:
    # astimezone() считает наивное время местным — так и есть: datetime.now()
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
