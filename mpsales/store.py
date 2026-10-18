"""SQLite: заказы без дублей и последний снимок остатков."""
import sqlite3
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from .models import Order, Stock

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    mp TEXT NOT NULL, order_id TEXT NOT NULL, date TEXT NOT NULL,
    sku TEXT NOT NULL, name TEXT NOT NULL, price TEXT NOT NULL,
    warehouse TEXT NOT NULL, cancelled INTEGER NOT NULL,
    PRIMARY KEY (mp, order_id)
);
CREATE TABLE IF NOT EXISTS stocks (
    mp TEXT NOT NULL, sku TEXT NOT NULL, name TEXT NOT NULL,
    warehouse TEXT NOT NULL, qty INTEGER NOT NULL,
    PRIMARY KEY (mp, sku, warehouse)
);
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


def get_state(db, key: str, default: str | None = None) -> str | None:
    row = db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_state(db, key: str, value: str) -> None:
    db.execute("INSERT INTO state VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def save_orders(db, orders: list[Order]) -> tuple[list[Order], list[Order]]:
    """Сохраняет заказы, возвращает (новые, отменённые с прошлого раза).

    WB отдаёт заказ повторно, когда он меняется (например, отмена) — это
    обновление строки, а не новый заказ.
    """
    new, cancelled = [], []
    for o in orders:
        prev = db.execute("SELECT cancelled FROM orders WHERE mp=? AND order_id=?", (o.mp, o.order_id)).fetchone()
        if prev is None:
            new.append(o)
        elif o.cancelled and not prev["cancelled"]:
            cancelled.append(o)
        db.execute(
            """INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(mp, order_id) DO UPDATE SET cancelled=excluded.cancelled,
                 price=excluded.price, warehouse=excluded.warehouse""",
            (o.mp, o.order_id, o.date.isoformat(timespec="seconds"), o.sku, o.name,
             str(o.price), o.warehouse, int(o.cancelled)),
        )
    db.commit()
    return new, cancelled


def sold_by_sku(db, mp: str) -> dict[str, int]:
    return {r["sku"]: r["n"] for r in db.execute(
        "SELECT sku, count(*) n FROM orders WHERE mp=? AND cancelled=0 GROUP BY sku", (mp,))}


def save_stocks(db, mp: str, stocks: list[Stock]) -> dict[str, int]:
    """Заменяет снимок остатков, возвращает прежние суммы по артикулу."""
    before = totals(db, mp)
    db.execute("DELETE FROM stocks WHERE mp=?", (mp,))
    db.executemany("INSERT INTO stocks VALUES (?, ?, ?, ?, ?)",
                   [(s.mp, s.sku, s.name, s.warehouse, s.qty) for s in stocks])
    db.commit()
    return before


def totals(db, mp: str) -> dict[str, int]:
    return {r["sku"]: r["q"] for r in db.execute(
        "SELECT sku, sum(qty) q FROM stocks WHERE mp=? GROUP BY sku", (mp,))}


def day_summary(db, day: datetime) -> dict:
    d = day.strftime("%Y-%m-%d")
    # Суммируем в Python: SUM в SQLite считает цены как float
    by_mp: dict[str, tuple[int, Decimal]] = {}
    for r in db.execute("SELECT mp, price FROM orders WHERE substr(date, 1, 10)=? AND cancelled=0", (d,)):
        n, s = by_mp.get(r["mp"], (0, Decimal(0)))
        by_mp[r["mp"]] = (n + 1, s + Decimal(r["price"]))
    return {
        "day": d,
        "orders": sum(n for n, _ in by_mp.values()),
        "revenue": sum((s for _, s in by_mp.values()), Decimal(0)),
        "by_mp": by_mp,
    }
