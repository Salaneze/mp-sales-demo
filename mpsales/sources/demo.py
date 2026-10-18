"""Тестовый магазин: правдоподобные заказы и остатки без чужого кабинета.

Каждый запуск «доживает» магазин до текущего момента: новые заказы
появляются с той же частотой, что у живого продавца, а остатки тают на
проданное. Состояние берётся из базы, поэтому повторный запуск через минуту
не выдумает гору заказов, а через час — выдаст новые.
"""
import random
from datetime import datetime, timedelta
from decimal import Decimal

from ..models import Order, Stock

CATALOG = [
    ("RUN-42", "Кроссовки беговые, 42", Decimal("3490"), 120),
    ("RUN-43", "Кроссовки беговые, 43", Decimal("3490"), 90),
    ("TEE-M", "Футболка базовая, M", Decimal("890"), 22),
    ("TEE-L", "Футболка базовая, L", Decimal("890"), 80),
    ("BAG-01", "Рюкзак городской 20 л", Decimal("2190"), 60),
    ("BTL-07", "Бутылка спортивная 0,7 л", Decimal("490"), 200),
    ("CAP-01", "Кепка с прямым козырьком", Decimal("790"), 14),
]
# Запасы подобраны так, чтобы футболка M и кепка кончались на 3-4 день демо:
# видно и «заканчивается», и «закончилось»
WAREHOUSES = ["Коледино", "Казань", "Электросталь"]
ORDERS_PER_HOUR = 1.2


def orders(mp: str, since: datetime, now: datetime, seed: int = 42) -> list[Order]:
    """Заказы в окне (since, now].

    Генератор детерминированный по часу: один и тот же час всегда даёт одни
    и те же заказы, так что перезапуск не плодит дубли — как настоящий API.
    """
    out = []
    hour = since.replace(minute=0, second=0, microsecond=0)
    while hour <= now:
        rnd = random.Random(f"{seed}-{mp}-{hour.isoformat()}")
        for i in range(_poisson(rnd, ORDERS_PER_HOUR)):
            # Все случайные значения тянем до фильтра по окну: иначе пропуск
            # сдвигает генератор, и тот же номер заказа в другом запуске
            # получает другой товар
            at = hour + timedelta(minutes=rnd.randint(0, 59), seconds=rnd.randint(0, 59))
            sku, name, price, _ = rnd.choices(CATALOG, weights=[5, 4, 6, 4, 2, 7, 3])[0]
            discount = Decimal(rnd.choice([0, 5, 10, 15])) / 100
            warehouse, cancelled = rnd.choice(WAREHOUSES), rnd.random() < 0.06
            if since < at <= now:
                out.append(Order(
                    mp=mp, order_id=f"{mp}-{hour:%Y%m%d%H}-{i}", date=at, sku=sku, name=name,
                    price=(price * (1 - discount)).quantize(Decimal("1")),
                    warehouse=warehouse, cancelled=cancelled,
                ))
        hour += timedelta(hours=1)
    return out


def stocks(mp: str, sold: dict[str, int]) -> list[Stock]:
    """Остатки = стартовый запас минус всё проданное (sold считает база)."""
    stocks = []
    for sku, name, _, start in CATALOG:
        left = max(start - sold.get(sku, 0), 0)
        # Делим остаток по складам примерно как в жизни: львиная доля в Коледино
        parts = [left * 6 // 10, left * 3 // 10]
        parts.append(left - sum(parts))
        stocks += [Stock(mp, sku, name, w, q) for w, q in zip(WAREHOUSES, parts)]
    return stocks


def _poisson(rnd: random.Random, lam: float) -> int:
    # Простое пуассоновское: без numpy ради одной функции
    k, p, limit = 0, 1.0, pow(2.718281828, -lam)
    while True:
        p *= rnd.random()
        if p <= limit:
            return k
        k += 1
