"""Kennimarket — веб-интерфейс к пайплайну лидов.

Запуск: streamlit run app.py   (или двойной клик по Kennimarket.command)

Сбор и поиск чатов запускаются как фоновые процессы `cli.py` (лог в logs/),
поэтому интерфейс можно закрывать и открывать — работа не прерывается.
"""
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import streamlit as st

from leadgen import db
from leadgen.config import settings
from leadgen.llm import LMStudioClient

ROOT = Path(__file__).parent
LOG_DIR = ROOT / "logs"
JOB_FILE = LOG_DIR / "job.json"
CHANNELS_FILE = ROOT / "channels.txt"
SESSION_FILE = ROOT / f"{settings.telegram_session_name}.session"
ACCENT = "#2a78d6"

STATUSES = {
    "new": "🆕 Новый",
    "contacted": "📞 Связались",
    "in_work": "🤝 В работе",
    "won": "✅ Сделка",
    "rejected": "❌ Не подошёл",
}
STATUS_BY_LABEL = {v: k for k, v in STATUSES.items()}
BUYER_TYPES = {"direct": "Покупатель", "broker": "Брокер", "unknown": "Неясно", None: "Неясно"}

st.set_page_config(page_title="Kennimarket — лиды на склады", page_icon="🏭", layout="wide")
db.init_db(settings.db_path)


# ---------- данные ----------

def query(sql: str, params=()) -> pd.DataFrame:
    with db.get_conn(settings.db_path) as conn:
        return pd.read_sql_query(sql, conn, params=params)


def load_leads() -> pd.DataFrame:
    df = query(
        """
        SELECT l.id, l.posted_at, l.status, l.buyer_type, l.confidence, l.telegram_username, l.phone,
               l.contact_name, l.company_name, l.location, l.area_sqm, l.budget, l.source, l.source_ref,
               l.notes, l.comment, l.created_at, r.raw_text
        FROM leads l LEFT JOIN raw_items r ON r.url = l.source_ref
        """
    )
    df["posted_at"] = pd.to_datetime(df["posted_at"], utc=True, errors="coerce")
    df["created_at"] = pd.to_datetime(df["created_at"], utc=True, errors="coerce")
    df["status"] = df["status"].fillna("new")
    df["chat"] = df["source_ref"].str.extract(r"t\.me/([^/]+)/")[0]
    df["contact_url"] = df["telegram_username"].map(lambda u: f"https://t.me/{u}" if u else None)
    df["has_contact"] = df["telegram_username"].notna() | df["phone"].notna()
    return df.sort_values("posted_at", ascending=False, na_position="last").set_index("id")


def update_lead(lead_id: int, **fields) -> None:
    cols = ", ".join(f"{k} = ?" for k in fields)
    with db.get_conn(settings.db_path) as conn:
        conn.execute(f"UPDATE leads SET {cols} WHERE id = ?", (*fields.values(), lead_id))


def read_channels() -> pd.DataFrame:
    rows = []
    if CHANNELS_FILE.exists():
        for line in CHANNELS_FILE.read_text(encoding="utf-8").splitlines():
            body = line.lstrip("# ").strip()
            if not body:
                continue
            name, _, meta = body.partition("#")
            parts = [p.strip() for p in meta.split(",", 2)] + ["", "", ""]
            members = "".join(ch for ch in parts[1] if ch.isdigit())
            rows.append({
                "enabled": not line.startswith("#"),
                "username": name.strip(),
                "kind": parts[0] or "—",
                "members": int(members) if members else None,
                "title": parts[2],
            })
    return pd.DataFrame(rows, columns=["enabled", "username", "kind", "members", "title"])


def write_channels(df: pd.DataFrame) -> None:
    lines = []
    for r in df.itertuples():
        if not r.username:
            continue
        prefix = "" if r.enabled else "# "
        members = f"{int(r.members)} уч." if pd.notna(r.members) else ""
        lines.append(f"{prefix}{r.username.strip().lstrip('@')}  # {r.kind or '—'}, {members}, {r.title or ''}")
    CHANNELS_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------- фоновые задачи ----------

def _alive(pid: int) -> bool:
    try:
        done, _ = os.waitpid(pid, os.WNOHANG)  # наш потомок: заодно убираем зомби
        return done == 0
    except ChildProcessError:  # процесс запущен прошлым экземпляром интерфейса
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def current_job():
    if not JOB_FILE.exists():
        return None
    job = json.loads(JOB_FILE.read_text())
    job["running"] = _alive(job["pid"])
    return job


def start_job(title: str, args: list) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    log_path = LOG_DIR / f"{datetime.now():%Y%m%d-%H%M%S}.log"
    log = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-u", "cli.py", *args],
        cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        start_new_session=True,  # переживает перезапуск интерфейса
    )
    JOB_FILE.write_text(json.dumps({
        "title": title, "pid": proc.pid, "log": str(log_path),
        "started": datetime.now(timezone.utc).isoformat(), "args": args,
    }))


def stop_job(job) -> None:
    try:
        os.killpg(job["pid"], signal.SIGINT)  # как Ctrl+C: Telethon закрывается чисто, собранное уже в БД
    except OSError:
        pass


def tail(path: str, n: int = 60) -> str:
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(l for l in lines[-n:] if "NotOpenSSLWarning" not in l and "warnings.warn" not in l)


# ---------- боковая панель: состояние системы ----------

@st.cache_data(ttl=15)
def llm_online() -> bool:
    return LMStudioClient().ping()


with st.sidebar:
    st.title("🏭 Kennimarket")
    st.caption("Покупатели складов в Москве и МО из Telegram")

    st.subheader("Состояние")
    if llm_online():
        st.success(f"Модель на связи · `{settings.lm_studio_model}`", icon="🧠")
    else:
        st.error("LM Studio не отвечает. Откройте LM Studio → Developer → Start Server.", icon="🧠")
    if SESSION_FILE.exists():
        st.success("Telegram: вход выполнен", icon="✈️")
    else:
        st.warning(
            "Telegram: нужен вход. Один раз в терминале:\n\n"
            "`python3 cli.py telegram --channels mossdelka`\n\nи введите номер и код.",
            icon="✈️",
        )
    job = current_job()
    if job and job["running"]:
        st.info(f"Идёт: {job['title']}", icon="⏳")

    counts = query("SELECT (SELECT COUNT(*) FROM leads) leads, (SELECT COUNT(*) FROM raw_items) raw").iloc[0]
    st.caption(f"В базе: {counts.leads} лидов · {counts.raw} проверенных сообщений")


tab_leads, tab_overview, tab_run, tab_chats = st.tabs(["📋 Лиды", "📊 Обзор", "⚙️ Сбор", "💬 Чаты"])


# ---------- Лиды ----------

with tab_leads:
    leads = load_leads()
    if leads.empty:
        st.info("Лидов пока нет. Запустите сбор на вкладке «⚙️ Сбор».")
    else:
        f1, f2, f3, f4 = st.columns([1, 1.3, 1.6, 2])
        period = f1.selectbox("Период", ["7 дней", "30 дней", "90 дней", "Год", "Всё время"], index=2)
        types = f2.multiselect("Кто ищет", ["Покупатель", "Брокер", "Неясно"], default=["Покупатель", "Брокер", "Неясно"])
        statuses = f3.multiselect("Статус", list(STATUSES.values()), default=[s for k, s in STATUSES.items() if k != "rejected"])
        search = f4.text_input("Поиск по тексту", placeholder="Химки, 1000 м², класс А…")

        days = {"7 дней": 7, "30 дней": 30, "90 дней": 90, "Год": 365}.get(period)
        view = leads.copy()
        view["buyer_label"] = view["buyer_type"].map(lambda t: BUYER_TYPES.get(t, "Неясно"))
        view["status_label"] = view["status"].map(lambda s: STATUSES.get(s, STATUSES["new"]))
        if days:
            view = view[view["posted_at"] >= pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=days)]
        view = view[view["buyer_label"].isin(types) & view["status_label"].isin(statuses)]
        if search:
            view = view[view["raw_text"].fillna("").str.contains(search, case=False, regex=False)]

        st.caption(f"Показано {len(view)} из {len(leads)}. Статус и комментарий можно менять прямо в таблице.")

        table = pd.DataFrame({
            "Дата": view["posted_at"].dt.tz_convert(None).dt.date,
            "Статус": view["status_label"],
            "Кто": view["buyer_label"],
            "Контакт": view["contact_url"],
            "Телефон": view["phone"],
            "Запрос": view["raw_text"].fillna("").str.replace(r"\s+", " ", regex=True).str.slice(0, 160),
            "Где": view["location"],
            "Площадь": view["area_sqm"],
            "Бюджет": view["budget"],
            "Сообщение": view["source_ref"],
            "Комментарий": view["comment"],
        }, index=view.index)
        # пустые ячейки — пустые, а не «None»
        text_cols = ["Телефон", "Где", "Площадь", "Бюджет", "Комментарий"]
        table[text_cols] = table[text_cols].astype(object).where(table[text_cols].notna(), "")

        edited = st.data_editor(
            table,
            # ключ зависит от фильтров: правки привязаны к позиции строки, и при смене
            # фильтра старые правки не должны попасть на другой лид
            key=f"leads_{period}_{types}_{statuses}_{search}",
            hide_index=True,
            width="stretch",
            height=min(38 * len(table) + 40, 640),
            disabled=[c for c in table.columns if c not in ("Статус", "Комментарий")],
            column_config={
                "Статус": st.column_config.SelectboxColumn(options=list(STATUSES.values()), required=True, width="small"),
                "Кто": st.column_config.TextColumn(width=90),
                "Контакт": st.column_config.LinkColumn(display_text=r"https://t\.me/(.+)", width="small"),
                "Запрос": st.column_config.TextColumn(width="large"),
                "Сообщение": st.column_config.LinkColumn(display_text="открыть ↗", width="small"),
                "Комментарий": st.column_config.TextColumn(width="medium"),
            },
        )
        # Сохраняем только реально изменённые ячейки.
        for lead_id in edited.index:
            new, old = edited.loc[lead_id], table.loc[lead_id]
            if new["Статус"] != old["Статус"]:
                update_lead(int(lead_id), status=STATUS_BY_LABEL[new["Статус"]])
            if (new["Комментарий"] or "") != old["Комментарий"]:
                update_lead(int(lead_id), comment=new["Комментарий"] or None)

        c1, c2 = st.columns([3, 1])
        c2.download_button(
            "⬇️ Скачать CSV",
            view.to_csv().encode("utf-8-sig"),
            file_name=f"leads_{datetime.now():%Y%m%d}.csv",
            mime="text/csv",
            width="stretch",
        )

        st.divider()
        st.subheader("Карточка лида")
        options = list(view.index)
        if options:
            def label(i):
                r = view.loc[i]
                who = (f"@{r['telegram_username']}" if pd.notna(r["telegram_username"])
                       else r["phone"] if pd.notna(r["phone"]) else "без контакта")
                date = r["posted_at"].strftime("%d.%m.%Y") if pd.notna(r["posted_at"]) else "—"
                return f"{date} · {who} · {str(r['raw_text'] or '')[:70]}"

            lead_id = st.selectbox("Выберите лид", options, format_func=label, label_visibility="collapsed")
            r = view.loc[lead_id]
            left, right = st.columns([2, 1])
            with left:
                st.markdown(f"**Текст сообщения** · [открыть в Telegram ↗]({r['source_ref']})")
                st.text(r["raw_text"] or "—")
            with right:
                if pd.notna(r["telegram_username"]):
                    st.link_button(f"✈️ Написать @{r['telegram_username']}", f"https://t.me/{r['telegram_username']}", width="stretch")
                if pd.notna(r["phone"]):
                    st.markdown(f"📞 **{r['phone']}**")
                st.markdown(
                    f"**Кто:** {r['buyer_label']}  \n"
                    f"**Где:** {r['location'] or '—'}  \n"
                    f"**Площадь:** {r['area_sqm'] or '—'}  \n"
                    f"**Бюджет:** {r['budget'] or '—'}  \n"
                    f"**Чат:** @{r['chat'] or '—'}  \n"
                    + (f"**Уверенность модели:** {r['confidence']:.0%}" if pd.notna(r["confidence"]) else "")
                )
                if pd.notna(r["notes"]) and r["notes"]:
                    st.caption(f"🧠 {r['notes']}")
        else:
            st.caption("Нет лидов под выбранные фильтры.")


# ---------- Обзор ----------

with tab_overview:
    leads = load_leads()
    if leads.empty:
        st.info("Пока нечего показывать.")
    else:
        now = pd.Timestamp.now(tz="UTC")
        fresh = lambda d: int((leads["posted_at"] >= now - pd.Timedelta(days=d)).sum())
        m = st.columns(5)
        m[0].metric("Всего лидов", len(leads))
        m[1].metric("За 7 дней", fresh(7))
        m[2].metric("За 30 дней", fresh(30))
        m[3].metric("С контактом", f"{leads['has_contact'].mean():.0%}")
        m[4].metric("Прямых покупателей", int((leads["buyer_type"] == "direct").sum()),
                    help="Остальные — брокеры, которые ищут объект для клиента, или неясно")

        g1, g2 = st.columns(2)
        with g1:
            st.markdown("**Лиды по месяцам** · по дате сообщения, последние 24 месяца")
            monthly = (
                leads.dropna(subset=["posted_at"])
                .assign(Месяц=lambda d: d["posted_at"].dt.tz_convert(None).dt.to_period("M").dt.to_timestamp())
                .groupby("Месяц").size().rename("Лидов")
            )
            monthly = monthly[monthly.index >= now.tz_convert(None) - pd.DateOffset(months=24)]
            st.bar_chart(monthly, color=ACCENT, height=280)
        with g2:
            st.markdown("**Воронка** · сколько лидов в каждом статусе")
            funnel = leads["status"].map(STATUSES).value_counts().reindex(list(STATUSES.values()), fill_value=0)
            st.bar_chart(funnel.rename("Лидов"), color=ACCENT, height=280, horizontal=True)

        g3, g4 = st.columns(2)
        with g3:
            st.markdown("**Самые полезные чаты** · лидов за всё время")
            top = leads["chat"].value_counts().head(10).rename("Лидов")
            st.bar_chart(top, color=ACCENT, height=300, horizontal=True)
        with g4:
            st.markdown("**Кто ищет**")
            who = leads["buyer_type"].map(lambda t: BUYER_TYPES.get(t, "Неясно")).value_counts().rename("Лидов")
            st.bar_chart(who, color=ACCENT, height=300, horizontal=True)


# ---------- Сбор ----------

@st.fragment(run_every=3)
def job_panel():
    job = current_job()
    if not job or not job["running"]:
        if job:
            st.caption(f"Последняя задача: {job['title']} · завершена")
            with st.expander("Лог последней задачи"):
                st.code(tail(job["log"], 200) or "—", language=None)
        return

    started = datetime.fromisoformat(job["started"])
    elapsed = datetime.now(timezone.utc) - started
    log = tail(job["log"], 100000)
    st.subheader(f"⏳ {job['title']}")

    if job["args"][0] == "telegram":
        total = next((int(l.rsplit(":", 1)[1]) for l in log.splitlines() if "чатов в списке:" in l), 0)
        done = log.count("[telegram] ищу в @")
        new = query(
            "SELECT (SELECT COUNT(*) FROM raw_items WHERE fetched_at >= ?) checked,"
            " (SELECT COUNT(*) FROM leads WHERE created_at >= ?) leads",
            (job["started"], job["started"]),
        ).iloc[0]
        if total:
            st.progress(min(done / total, 1.0), text=f"Чат {done} из {total}")
        c = st.columns(3)
        c[0].metric("Проверено сообщений", int(new.checked))
        c[1].metric("Новых лидов", int(new.leads))
        c[2].metric("Идёт", f"{int(elapsed.total_seconds() // 60)} мин")
    else:
        st.caption(f"Идёт {int(elapsed.total_seconds() // 60)} мин")

    if st.button("⏹ Остановить", type="secondary"):
        stop_job(job)
        st.toast("Останавливаю… Всё уже собранное сохранено.")
    st.code(tail(job["log"], 25) or "запускается…", language=None)


with tab_run:
    job = current_job()
    busy = bool(job and job["running"])
    blocked = busy or not SESSION_FILE.exists()

    job_panel()

    left, right = st.columns(2)
    with left:
        st.subheader("🔎 Собрать лиды")
        st.caption("Ищет запросы «куплю склад / ищу для клиента» во всех включённых чатах и проверяет их моделью.")
        chats = read_channels()
        enabled = chats[chats["enabled"]] if not chats.empty else chats
        n_groups = int((enabled["kind"] == "группа").sum()) if not enabled.empty else 0
        scope = st.radio(
            "Где искать",
            [f"Только группы ({n_groups}) — быстрее, там почти все запросы", f"Все включённые чаты ({len(enabled)})"],
        )
        days = st.select_slider("Свежесть сообщений", options=[7, 14, 30, 60, 90, 180, 365], value=90,
                                format_func=lambda d: f"до {d} дней")
        with st.expander("Дополнительно"):
            limit = st.number_input("Результатов на поисковую фразу в чате", 20, 1000, 200, step=20)
            min_conf = st.slider("Минимальная уверенность модели", 0.0, 1.0, 0.5, 0.05)
        if not llm_online():
            st.warning("Сначала запустите сервер в LM Studio.")
        if st.button("▶️ Запустить сбор", type="primary", disabled=blocked or not llm_online() or enabled.empty,
                     width="stretch"):
            selected = enabled if scope.startswith("Все") else enabled[enabled["kind"] == "группа"]
            LOG_DIR.mkdir(exist_ok=True)
            run_list = LOG_DIR / "run_channels.txt"
            run_list.write_text("\n".join(selected["username"]) + "\n", encoding="utf-8")
            start_job(
                f"Сбор лидов · {len(selected)} чатов · до {days} дней",
                ["telegram", "--channels-file", str(run_list), "--max-age-days", str(days),
                 "--limit", str(int(limit)), "--min-confidence", str(min_conf)],
            )
            st.rerun()

    with right:
        st.subheader("🧭 Найти новые чаты")
        st.caption("Ищет публичные чаты и каналы о недвижимости и складах. Выключенные вами чаты останутся выключенными.")
        depth = st.select_slider("Глубина поиска по «похожим каналам»", options=[0, 1, 2], value=1,
                                 format_func=lambda d: {0: "без раскрутки", 1: "1 уровень", 2: "2 уровня (долго)"}[d])
        min_members = st.number_input("Минимум участников", 0, 100000, 300, step=100)
        if st.button("🧭 Искать чаты", disabled=blocked, width="stretch"):
            start_job("Поиск чатов", ["tg-discover", "--depth", str(depth), "--min-members", str(int(min_members)),
                                      "--out", str(CHANNELS_FILE)])
            st.rerun()

    if busy:
        st.caption("Пока идёт задача, новые запускать нельзя — Telegram-сессия одна.")


# ---------- Чаты ----------

with tab_chats:
    chats = read_channels()
    if chats.empty:
        st.info("Список чатов пуст. Нажмите «🧭 Искать чаты» на вкладке «⚙️ Сбор» или добавьте вручную ниже.")
    leads_per_chat = load_leads()["chat"].value_counts()
    chats["leads"] = chats["username"].map(leads_per_chat).fillna(0).astype(int)

    a, b, c = st.columns([1.2, 1.2, 3])
    kind_filter = a.selectbox("Тип", ["Все", "группа", "канал"])
    only_enabled = b.selectbox("Показать", ["Все", "Включённые", "Выключенные"])
    name_filter = c.text_input("Поиск по названию", placeholder="недвижимость, склад…")
    shown = chats
    if kind_filter != "Все":
        shown = shown[shown["kind"] == kind_filter]
    if only_enabled != "Все":
        shown = shown[shown["enabled"] == (only_enabled == "Включённые")]
    if name_filter:
        mask = shown["title"].str.contains(name_filter, case=False, regex=False) | shown["username"].str.contains(name_filter, case=False, regex=False)
        shown = shown[mask]

    st.caption(f"Включено {int(chats['enabled'].sum())} из {len(chats)}. Снимите галочку, чтобы не искать в чате.")
    edited_chats = st.data_editor(
        shown.assign(link=shown["username"].map(lambda u: f"https://t.me/{u}")),
        key=f"chats_{kind_filter}_{only_enabled}_{name_filter}",
        hide_index=True,
        width="stretch",
        height=520,
        disabled=["username", "kind", "members", "title", "leads", "link"],
        column_order=["enabled", "title", "kind", "members", "leads", "link"],
        column_config={
            "enabled": st.column_config.CheckboxColumn("Искать", width="small"),
            "title": st.column_config.TextColumn("Название", width="large"),
            "kind": st.column_config.TextColumn("Тип", width="small"),
            "members": st.column_config.NumberColumn("Участников", format="%d", width="small"),
            "leads": st.column_config.NumberColumn("Лидов", width="small"),
            "link": st.column_config.LinkColumn("Чат", display_text=r"https://t\.me/(.+)", width="medium"),
        },
    )
    if not edited_chats["enabled"].equals(shown["enabled"]):
        chats.loc[edited_chats.index, "enabled"] = edited_chats["enabled"]
        write_channels(chats)
        st.toast("Список чатов сохранён")

    with st.form("add_chat", clear_on_submit=True):
        new_chat = st.text_input("Добавить чат вручную", placeholder="@username или ссылка t.me/…")
        if st.form_submit_button("➕ Добавить") and new_chat.strip():
            username = new_chat.strip().rstrip("/").split("/")[-1].lstrip("@")
            if username in set(chats["username"]):
                st.warning(f"@{username} уже в списке")
            else:
                write_channels(pd.concat([pd.DataFrame([{
                    "enabled": True, "username": username, "kind": "—", "members": None, "title": "добавлен вручную",
                }]), chats.drop(columns=["leads"])], ignore_index=True))
                st.success(f"@{username} добавлен")
                st.rerun()
