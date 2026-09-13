import argparse
import csv

from leadgen import db
from leadgen.config import settings
from leadgen.pipeline import run_dgis_pipeline, run_telegram_pipeline


def cmd_init(_args):
    db.init_db(settings.db_path)
    print(f"БД инициализирована: {settings.db_path}")


def cmd_telegram(args):
    channels = [c.strip() for c in args.channels.split(",") if c.strip()]
    run_telegram_pipeline(channels, limit_per_channel=args.limit, min_confidence=args.min_confidence)


def cmd_dgis(args):
    queries = [q.strip() for q in args.queries.split(",")] if args.queries else None
    run_dgis_pipeline(queries=queries, pages_per_query=args.pages, min_confidence=args.min_confidence)


def cmd_stats(_args):
    db.init_db(settings.db_path)
    with db.get_conn(settings.db_path) as conn:
        total = db.count_leads(conn)
        by_source = conn.execute("SELECT source, COUNT(*) FROM leads GROUP BY source").fetchall()
    print(f"Всего лидов: {total} (цель: {settings.target_leads})")
    for row in by_source:
        print(f"  {row[0]}: {row[1]}")


def cmd_export(args):
    db.init_db(settings.db_path)
    with db.get_conn(settings.db_path) as conn:
        rows = conn.execute("SELECT * FROM leads ORDER BY confidence DESC").fetchall()
    if not rows:
        print("Лидов пока нет.")
        return
    with open(args.out, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(rows[0].keys())
        for row in rows:
            writer.writerow([row[k] for k in row.keys()])
    print(f"Экспортировано {len(rows)} лидов -> {args.out}")


def main():
    parser = argparse.ArgumentParser(description="Прототип сбора холодных лидов на покупку складов (Москва/МО)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db").set_defaults(func=cmd_init)

    p_tg = sub.add_parser("telegram", help="Сканировать публичные Telegram-каналы на buy-intent сигналы")
    p_tg.add_argument("--channels", required=True, help="Список @username через запятую")
    p_tg.add_argument("--limit", type=int, default=500, help="Сколько последних сообщений смотреть на канал")
    p_tg.add_argument("--min-confidence", type=float, default=0.5)
    p_tg.set_defaults(func=cmd_telegram)

    p_dg = sub.add_parser("dgis", help="Собрать компании-таргеты через 2GIS Catalog API")
    p_dg.add_argument("--queries", default=None, help="Поисковые фразы через запятую (по умолчанию — набор из sources/dgis_source.py)")
    p_dg.add_argument("--pages", type=int, default=5, help="Сколько страниц выдачи на каждый запрос")
    p_dg.add_argument("--min-confidence", type=float, default=0.4)
    p_dg.set_defaults(func=cmd_dgis)

    sub.add_parser("stats").set_defaults(func=cmd_stats)

    p_exp = sub.add_parser("export")
    p_exp.add_argument("--out", default="leads.csv")
    p_exp.set_defaults(func=cmd_export)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
