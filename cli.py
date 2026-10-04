import argparse
import csv
import os
from datetime import datetime, timedelta, timezone

from leadgen import db
from leadgen.config import settings
from leadgen.pipeline import run_dgis_pipeline, run_telegram_pipeline


def cmd_init(_args):
    db.init_db(settings.db_path)
    print(f"БД инициализирована: {settings.db_path}")


def cmd_telegram(args):
    if args.channels_file:
        with open(args.channels_file, encoding="utf-8") as f:
            raw = [line.split("#")[0] for line in f]
    else:
        raw = args.channels.split(",")
    channels = [c.strip() for c in raw if c.strip()]
    run_telegram_pipeline(
        channels, limit_per_query=args.limit, min_confidence=args.min_confidence, max_age_days=args.max_age_days
    )


def cmd_tg_discover(args):
    from leadgen.sources.telegram_source import discover_chats

    # Выключенные вручную чаты (строка начинается с "#") остаются выключенными.
    disabled = set()
    if os.path.exists(args.out):
        with open(args.out, encoding="utf-8") as f:
            disabled = {line.lstrip("# ").split()[0] for line in f if line.startswith("#") and line.strip("# \n")}
    chats = discover_chats(min_members=args.min_members, depth=args.depth)
    with open(args.out, "w", encoding="utf-8") as f:
        for c in chats:
            prefix = "# " if c["username"] in disabled else ""
            f.write(f"{prefix}{c['username']}  # {c['kind']}, {c['members']} уч., {c['title']}\n")
    print(f"Найдено {len(chats)} чатов -> {args.out} (лишние можно удалить/закомментировать вручную)")


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
        query = "SELECT * FROM leads"
        params = ()
        if args.max_age_days:
            # лиды без даты (2GIS) не отсекаем — у них нет понятия "свежести"
            query += " WHERE posted_at IS NULL OR posted_at >= ?"
            params = ((datetime.now(timezone.utc) - timedelta(days=args.max_age_days)).isoformat(),)
        query += " ORDER BY posted_at IS NULL, posted_at DESC, confidence DESC"
        rows = conn.execute(query, params).fetchall()
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

    p_disc = sub.add_parser("tg-discover", help="Найти публичные Telegram-чаты/каналы по тематическим запросам")
    p_disc.add_argument("--out", default="channels.txt")
    p_disc.add_argument("--min-members", type=int, default=300)
    p_disc.add_argument("--depth", type=int, default=1, help="Сколько раз раскручивать через «похожие каналы»")
    p_disc.set_defaults(func=cmd_tg_discover)

    p_tg = sub.add_parser("telegram", help="Искать buy-intent сигналы в публичных Telegram-каналах и группах")
    src = p_tg.add_mutually_exclusive_group(required=True)
    src.add_argument("--channels", help="Список @username через запятую")
    src.add_argument("--channels-file", help="Файл со списком username (по одному в строке, # — комментарий)")
    p_tg.add_argument("--limit", type=int, default=200, help="Максимум результатов на один поисковый запрос в чате")
    p_tg.add_argument("--max-age-days", type=int, default=90, help="Брать сообщения не старше N дней (0 — вся история)")
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
    p_exp.add_argument("--max-age-days", type=int, default=0, help="Только сообщения не старше N дней (0 — все)")
    p_exp.set_defaults(func=cmd_export)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
