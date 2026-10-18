"""python -m mpsales --demo          один проход на тестовом магазине
python -m mpsales                 один проход по кабинету WB (нужен WB_TOKEN)
python -m mpsales --mp ozon       то же по Ozon (OZON_CLIENT_ID, OZON_API_KEY)
python -m mpsales --every 1h      по расписанию
python -m mpsales --summary       итоги дня в Telegram (для cron вечером)
"""
import argparse
import logging
import re
import sys
import time
from datetime import datetime, timedelta

from . import notify, report, store
from .config import settings
from .sources import demo

log = logging.getLogger("mpsales")

# WB обновляет заказы раз в 30 минут и отдаёт их по lastChangeDate, поэтому
# окно берём с нахлёстом: дубли отсечёт ключ заказа, а пропуски — нет
OVERLAP = timedelta(minutes=40)


def source(mp: str):
    if mp == "ozon":
        from .sources.ozon import Ozon
        if not (settings.ozon_client_id and settings.ozon_api_key):
            raise SystemExit("Нет OZON_CLIENT_ID / OZON_API_KEY в .env — или запустите с --demo")
        return Ozon(settings.ozon_client_id, settings.ozon_api_key)
    from .sources.wb import WB
    if not settings.wb_token:
        raise SystemExit("Нет WB_TOKEN в .env — или запустите с --demo")
    return WB(settings.wb_token)


def run_once(use_demo: bool, now: datetime | None = None, market: str | None = None) -> list[str]:
    now = now or datetime.now()
    db = store.connect(settings.db_path)
    mp = "demo" if use_demo else (market or settings.mp)
    try:
        last = store.get_state(db, f"last_run:{mp}")
        first_run = last is None
        since = datetime.fromisoformat(last) - OVERLAP if last else now - timedelta(days=3)

        if use_demo:
            orders = demo.orders(mp, since, now)
        else:
            api = source(mp)
            orders = api.orders(since)
        new, cancelled = store.save_orders(db, orders)

        stocks = demo.stocks(mp, store.sold_by_sku(db, mp)) if use_demo else api.stocks()
        before = store.save_stocks(db, mp, stocks)
        after = store.totals(db, mp)
        names = {s.sku: s.name for s in stocks}

        texts: list[str] = []
        # Первый запуск подтягивает историю за три дня — присылать её
        # пачкой «новых заказов» незачем, это не новости
        if not first_run:
            texts += [notify.new_order(o) for o in sorted(new, key=lambda o: o.date)]
            texts += [notify.cancelled(o) for o in cancelled]
            for sku, qty in sorted(after.items()):
                was = before.get(sku)
                # Сообщаем один раз — в момент, когда остаток перешёл порог,
                # а не каждый час, пока товар лежит на нуле
                if was is not None and qty <= settings.low_stock < was:
                    texts.append(notify.low_stock(mp, sku, names.get(sku, sku), qty))

        report.to_xlsx(db, settings.xlsx_path)
        if settings.gsheet_id and settings.gsheet_credentials:
            report.to_gsheet(db, settings.gsheet_id, settings.gsheet_credentials)
        store.set_state(db, f"last_run:{mp}", now.isoformat(timespec="seconds"))
        db.commit()
    finally:
        db.close()

    notify.send(settings.tg_token, settings.tg_chat_id, texts)
    log.info("заказов в окне %d, новых %d, отмен %d, уведомлений %d", len(orders), len(new), len(cancelled), len(texts))
    for t in texts:
        log.info("  %s", t.replace("\n", " | "))
    return texts


def send_summary(now: datetime | None = None) -> str:
    db = store.connect(settings.db_path)
    try:
        text = notify.summary(store.day_summary(db, now or datetime.now()))
    finally:
        db.close()
    notify.send(settings.tg_token, settings.tg_chat_id, [text])
    log.info(text.replace("\n", " | "))
    return text


def parse_interval(text: str) -> int:
    m = re.fullmatch(r"(\d+)\s*([mhd])", text.strip().lower())
    if not m:
        raise argparse.ArgumentTypeError("формат: 30m, 1h, 1d")
    return int(m.group(1)) * {"m": 60, "h": 3600, "d": 86400}[m.group(2)]


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    ap = argparse.ArgumentParser(prog="python -m mpsales")
    ap.add_argument("--demo", action="store_true", help="тестовый магазин вместо кабинета")
    ap.add_argument("--mp", choices=["wb", "ozon"], help="кабинет: wb или ozon (по умолчанию MP из .env)")
    ap.add_argument("--every", type=parse_interval, help="повторять: 30m, 1h")
    ap.add_argument("--summary", action="store_true", help="итоги дня в Telegram")
    # Только для показа: перемотать тестовый магазин вперёд и увидеть,
    # как приходят новые заказы, не дожидаясь их час
    ap.add_argument("--now", type=datetime.fromisoformat, help="(демо) считать текущим это время")
    args = ap.parse_args()
    if args.now and not args.demo:
        ap.error("--now работает только с --demo")

    if args.summary:
        send_summary(args.now)
        return 0
    if not args.every:
        run_once(args.demo, args.now, args.mp)
        return 0
    while True:
        try:
            run_once(args.demo, market=args.mp)
        except Exception:
            log.exception("проход упал, следующий по расписанию")
        time.sleep(args.every)


if __name__ == "__main__":
    sys.exit(main())
