from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass
class Order:
    mp: str            # wb | ozon | demo
    order_id: str      # у WB это srid — он один на единицу товара и не меняется
    date: datetime
    sku: str           # артикул продавца
    name: str
    price: Decimal     # сколько заплатил покупатель, после всех скидок
    warehouse: str
    cancelled: bool = False


@dataclass
class Stock:
    mp: str
    sku: str
    name: str
    warehouse: str
    qty: int
