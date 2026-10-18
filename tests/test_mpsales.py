"""python -m unittest"""
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
from openpyxl import load_workbook

from mpsales import notify, report, store
from mpsales.models import Order, Stock
from mpsales.sources import demo
from mpsales.sources import ozon
from mpsales.sources.wb import WB, parse_remains

T0 = datetime(2026, 10, 18, 12, 0)


def db():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(store.SCHEMA)
    return con


def order(i, cancelled=False, price="1000", day=T0):
    return Order("wb", f"srid-{i}", day, f"SKU{i}", f"Товар {i}", Decimal(price), "Коледино", cancelled)


class WbParseTest(unittest.TestCase):
    # Строка ответа из документации WB (dev.wildberries.ru, 18.10.2026)
    DOC_ORDER = {
        "date": "2022-03-04T18:08:31", "lastChangeDate": "2022-03-06T10:11:07",
        "warehouseName": "Склад WB РФ", "supplierArticle": "12345", "nmId": 1234567,
        "subject": "Мультистайлеры", "brand": "Тест", "techSize": "0",
        "totalPrice": 1887, "discountPercent": 18, "finishedPrice": 1145, "priceWithDisc": 1547,
        "isCancel": True, "srid": "11.rf9ef11fce1684117b0nhj96222982382.3.0",
    }

    def test_order(self):
        o = WB._order(self.DOC_ORDER)
        self.assertEqual(o.order_id, self.DOC_ORDER["srid"])
        self.assertEqual(o.sku, "12345")
        self.assertEqual(o.name, "Мультистайлеры Тест")  # размер «0» не показываем
        self.assertEqual(o.price, Decimal("1547"))
        self.assertTrue(o.cancelled)

    def test_remains_skip_pseudo_warehouses(self):
        rows = [{"brand": "Wonderful", "subjectName": "Фотоальбомы", "vendorCode": "41058", "nmId": 1,
                 "warehouses": [
                     {"warehouseName": "В пути до получателей", "quantity": 14},
                     {"warehouseName": "В пути возвраты на склад WB", "quantity": 4},
                     {"warehouseName": "Всего находится на складах", "quantity": 267},
                     {"warehouseName": "Склад WB РФ", "quantity": 134},
                     {"warehouseName": "Минск", "quantity": 123},
                 ]}]
        stocks = parse_remains(rows)
        self.assertEqual({s.warehouse for s in stocks}, {"Склад WB РФ", "Минск"})
        self.assertEqual(sum(s.qty for s in stocks), 257)


class OzonParseTest(unittest.TestCase):
    # Поля — как в docs.ozon.ru/api/seller/swagger.json (18.10.2026)
    FBS = {
        "posting_number": "05708065-0029-1", "status": "cancelled_from_split_pending",
        "in_process_at": "2026-10-18T09:00:00Z",
        "delivery_method": {"warehouse": "Склад на Ленина"},
        "products": [
            {"offer_id": "RUN-42", "name": "Кроссовки", "sku": 1, "quantity": 2,
             "price": {"amount": "3490.00", "currency": "RUB"}},
            {"offer_id": "CAP-01", "name": "Кепка", "sku": 2, "quantity": 1,
             "price": {"amount": "790.50", "currency": "RUB"}},
        ],
    }
    FBO = {
        "posting_number": "1-2-3", "status": "delivered", "created_at": "2026-10-18T08:00:00Z",
        "analytics_data": {"warehouse_name": "ХОРУГВИНО_РФЦ"},
        "products": [{"offer_id": "TEE-M", "name": "Футболка", "sku": 3, "quantity": 1,
                      "price": {"amount": "890", "currency": "RUB"}}],
    }

    def test_posting_split_per_unit(self):
        orders = ozon.posting_orders(self.FBS, "fbs")
        # 2 кроссовки + 1 кепка = три единицы с разными ключами
        self.assertEqual(len(orders), 3)
        self.assertEqual(len({o.order_id for o in orders}), 3)
        self.assertTrue(all(o.cancelled for o in orders))  # отмена из-за разделения — тоже отмена
        self.assertEqual(sum(o.price for o in orders), Decimal("7770.50"))
        self.assertEqual(orders[0].warehouse, "Склад на Ленина (FBS)")

    def test_fbo_warehouse_from_analytics(self):
        o = ozon.posting_orders(self.FBO, "fbo")[0]
        self.assertEqual(o.warehouse, "ХОРУГВИНО_РФЦ (FBO)")
        self.assertFalse(o.cancelled)

    def test_own_stock_minus_reserved(self):
        items = [{"offer_id": "RUN-42", "product_id": 10, "stocks": [
                     {"type": "fbs", "present": 10, "reserved": 3, "sku": 1},
                     {"type": "fbo", "present": 50, "reserved": 0, "sku": 1}]},
                 {"offer_id": "CAP-01", "product_id": 11, "stocks": [
                     {"type": "rfbs", "present": 1, "reserved": 4, "sku": 2}]}]
        own, skus = ozon.parse_own_stocks(items)
        self.assertEqual({s.sku: s.qty for s in own}, {"RUN-42": 7, "CAP-01": 0})
        self.assertEqual(skus, [1])  # FBO-остаток берём из аналитики, не отсюда

    def test_orders_paginate_and_look_back(self):
        calls = []

        def handler(req: httpx.Request) -> httpx.Response:
            body = json.loads(req.content)
            calls.append((req.url.path, body))
            if req.url.path == "/v4/posting/fbs/list" and not body.get("cursor"):
                return httpx.Response(200, json={"postings": [self.FBS], "has_next": True, "cursor": "p2"})
            if req.url.path == "/v4/posting/fbs/list":
                return httpx.Response(200, json={"postings": [], "has_next": False, "cursor": ""})
            return httpx.Response(200, json={"postings": [self.FBO], "has_next": False})

        api = ozon.Ozon("1", "k", httpx.Client(base_url=ozon.API, transport=httpx.MockTransport(handler)))
        orders = api.orders(T0 - timedelta(hours=1), T0)
        self.assertEqual(len(orders), 4)
        self.assertEqual([c[0] for c in calls], ["/v4/posting/fbs/list"] * 2 + ["/v3/posting/fbo/list"])
        # Окно в час растянуто до трёх дней — иначе вчерашний заказ, отменённый сегодня, не увидим
        since = datetime.fromisoformat(calls[0][1]["filter"]["since"].replace("Z", "+00:00"))
        self.assertLessEqual(since.astimezone().replace(tzinfo=None), T0 - ozon.LOOKBACK)
        self.assertTrue(calls[2][1]["with"]["analytics_data"])


class StoreTest(unittest.TestCase):
    def test_orders_dedup_and_cancel(self):
        con = db()
        new, canc = store.save_orders(con, [order(1), order(2)])
        self.assertEqual((len(new), len(canc)), (2, 0))
        # WB отдал тот же заказ повторно, уже отменённым
        new, canc = store.save_orders(con, [order(1, cancelled=True), order(2), order(3)])
        self.assertEqual([o.order_id for o in new], ["srid-3"])
        self.assertEqual([o.order_id for o in canc], ["srid-1"])
        self.assertEqual(con.execute("SELECT count(*) FROM orders").fetchone()[0], 3)

    def test_day_summary_exact_money(self):
        con = db()
        store.save_orders(con, [order(1, price="0.10"), order(2, price="0.20"), order(3, cancelled=True)])
        s = store.day_summary(con, T0)
        self.assertEqual(s["orders"], 2)
        self.assertEqual(s["revenue"], Decimal("0.30"))


class DemoTest(unittest.TestCase):
    def test_deterministic_and_window_independent(self):
        a = demo.orders("demo", T0 - timedelta(hours=10), T0)
        b = demo.orders("demo", T0 - timedelta(hours=10), T0 - timedelta(hours=5)) + \
            demo.orders("demo", T0 - timedelta(hours=5), T0)
        self.assertEqual([(o.order_id, o.sku, o.price) for o in a], [(o.order_id, o.sku, o.price) for o in b])

    def test_stock_melts(self):
        full = sum(s.qty for s in demo.stocks("demo", {}) if s.sku == "TEE-M")
        after = sum(s.qty for s in demo.stocks("demo", {"TEE-M": 5}) if s.sku == "TEE-M")
        self.assertEqual(full - after, 5)
        self.assertEqual(sum(s.qty for s in demo.stocks("demo", {"TEE-M": 999}) if s.sku == "TEE-M"), 0)


class ReportTest(unittest.TestCase):
    def test_xlsx_sheets(self):
        con = db()
        store.save_orders(con, [order(1), order(2, day=T0 - timedelta(days=1)), order(3, cancelled=True)])
        store.save_stocks(con, "wb", [Stock("wb", "SKU1", "Товар 1", "Коледино", 7)])
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.xlsx"
            report.to_xlsx(con, p)
            wb = load_workbook(p)
            self.assertEqual(wb.sheetnames, ["Заказы", "Остатки", "По дням"])
            days = list(wb["По дням"].iter_rows(min_row=2, values_only=True))
            self.assertEqual(days[0][2:], (1, 1000.0, 1))  # сегодня: 1 заказ, 1 отмена


class NotifyTest(unittest.TestCase):
    def test_pack(self):
        msgs = notify.pack([notify.new_order(order(i)) for i in range(200)])
        self.assertGreater(len(msgs), 1)
        self.assertTrue(all(len(m) <= notify.CHUNK for m in msgs))
        self.assertEqual(sum(m.count("Новый заказ") for m in msgs), 200)

    def test_money(self):
        self.assertEqual(notify.money(Decimal("84300")), "84 300 ₽")


if __name__ == "__main__":
    unittest.main()
