"""Wildberries: API продавца, а не витрина.

Витрина wildberries.ru закрыта собственной защитой WB (ответ 498, сервер
wbaas, JS-проверка), поэтому парсить её бессмысленно. API продавца работает
по токену из личного кабинета и защиты не имеет — только лимиты.

Эндпоинты и поля сверены по dev.wildberries.ru/docs/openapi/reports 18.10.2026.
"""
import logging
import time
from datetime import datetime, timedelta
from decimal import Decimal

import httpx

from ..models import Order, Stock

log = logging.getLogger(__name__)

STATS = "https://statistics-api.wildberries.ru"
ANALYTICS = "https://seller-analytics-api.wildberries.ru"

# В отчёте об остатках WB кладёт рядом с настоящими складами сводные строки.
# Сложить всё подряд — получить остаток в полтора-два раза больше реального.
PSEUDO_WAREHOUSES = {
    "В пути до получателей",
    "В пути возвраты на склад WB",
    "Всего находится на складах",
}


class WB:
    def __init__(self, token: str, client: httpx.Client | None = None):
        self.http = client or httpx.Client(headers={"Authorization": token}, timeout=60)

    def _get(self, url: str, params: dict | None = None) -> httpx.Response:
        # Лимит у отчётов — 1 запрос в минуту на продавца. 429 не ошибка,
        # а просьба подождать: ждём и пробуем ещё, но не бесконечно.
        for _ in range(5):
            r = self.http.get(url, params=params)
            if r.status_code != 429:
                r.raise_for_status()
                return r
            # Имя заголовка с паузой на странице отчётов не описано — берём
            # стандартный Retry-After, а без него ждём минуту, как велит лимит
            # TODO: сверить с разделом лимитов в общей части документации WB
            wait = int(r.headers.get("X-Ratelimit-Retry") or r.headers.get("Retry-After") or 60)
            log.warning("WB просит подождать %s с", wait)
            time.sleep(wait)
        r.raise_for_status()
        return r

    def orders(self, since: datetime) -> list[Order]:
        out: list[Order] = []
        date_from = since.strftime("%Y-%m-%dT%H:%M:%S")
        while True:
            rows = self._get(f"{STATS}/api/v1/supplier/orders", {"dateFrom": date_from}).json()
            if not rows:
                break
            out += [self._order(r) for r in rows]
            # Больше ~80 000 строк за раз WB не отдаёт: следующую порцию
            # просим от lastChangeDate последней строки, как велит документация
            if len(rows) < 80_000:
                break
            date_from = rows[-1]["lastChangeDate"]
        return out

    @staticmethod
    def _order(r: dict) -> Order:
        return Order(
            mp="wb",
            order_id=r["srid"],
            date=datetime.fromisoformat(r["date"]),
            sku=r.get("supplierArticle") or str(r["nmId"]),
            name=" ".join(x for x in (r.get("subject"), r.get("brand"), r.get("techSize")) if x and x != "0"),
            price=Decimal(str(r.get("priceWithDisc") or r.get("finishedPrice") or 0)),
            warehouse=r.get("warehouseName", ""),
            cancelled=bool(r.get("isCancel")),
        )

    def stocks(self, poll_every: int = 60, give_up_after: int = 15 * 60) -> list[Stock]:
        task = self._get(f"{ANALYTICS}/api/v1/warehouse_remains",
                         {"groupBySa": "true", "groupByNm": "true"}).json()["data"]["taskId"]
        deadline = time.monotonic() + give_up_after
        while time.monotonic() < deadline:
            time.sleep(poll_every)
            status = self._get(f"{ANALYTICS}/api/v1/warehouse_remains/tasks/{task}/status").json()
            if status["data"]["status"] == "done":
                break
        else:
            raise TimeoutError(f"WB не собрал отчёт об остатках за {give_up_after} с")
        rows = self._get(f"{ANALYTICS}/api/v1/warehouse_remains/tasks/{task}/download").json()
        return parse_remains(rows)


def parse_remains(rows: list[dict]) -> list[Stock]:
    out = []
    for r in rows:
        name = " ".join(x for x in (r.get("subjectName"), r.get("brand")) if x)
        for w in r.get("warehouses", []):
            if w["warehouseName"] in PSEUDO_WAREHOUSES:
                continue
            out.append(Stock("wb", r.get("vendorCode") or str(r["nmId"]), name, w["warehouseName"], int(w["quantity"])))
    return out


def default_since() -> datetime:
    # Данные заказов хранятся у WB 90 дней; при первом запуске берём неделю
    return datetime.now() - timedelta(days=7)
