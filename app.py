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
    "new": "Новый",
    "contacted": "Связались",
    "in_work": "В работе",
    "won": "Сделка",
    "rejected": "Не подошёл",
}
STATUS_BY_LABEL = {v: k for k, v in STATUSES.items()}
BUYER_TYPES = {"direct": "Покупатель", "broker": "Брокер", "unknown": "Неясно", None: "Неясно"}

st.set_page_config(page_title="Kennimarket", page_icon="macos/AppIcon.png", layout="wide")
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


# ---------- оформление ----------

st.markdown(
    """
    <style>
      header[data-testid="stHeader"] {background: transparent; height: 0;}
      .block-container {padding-top: 2.2rem; padding-bottom: 3rem; max-width: 1400px;}
      [data-testid="stSidebar"] .stCaption, [data-testid="stSidebar"] small {color: #8b96ad !important;}
      [data-testid="stMetric"] {background: #fff;}
      [data-testid="stMetricValue"] {font-weight: 700;}
      div[data-testid="stVerticalBlockBorderWrapper"]:has(> div > div > div.lead-card) {background: #fff;}
      .lead-text {font-size: 0.95rem; line-height: 1.5; color: #16181d; margin: 0.35rem 0 0.6rem;
                  display: -webkit-box; -webkit-line-clamp: 4; -webkit-box-orient: vertical; overflow: hidden;}
      .lead-meta {color: #6b7280; font-size: 0.82rem;}
      .page-sub {color: #6b7280; margin-top: -0.6rem; margin-bottom: 1.2rem;}
      .dot {display:inline-block; width:8px; height:8px; border-radius:50%; margin-right:8px;}
    </style>
    """,
    unsafe_allow_html=True,
)

BUYER_BADGE = {"Покупатель": ("green", ":material/storefront:"), "Брокер": ("violet", ":material/handshake:"),
               "Неясно": ("gray", ":material/help:")}
STATUS_BADGE = {"new": "blue", "contacted": "orange", "in_work": "violet", "won": "green", "rejected": "gray"}
MONTHS = ["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]


def page_header(title: str, subtitle: str) -> None:
    st.title(title)
    st.markdown(f"<div class='page-sub'>{subtitle}</div>", unsafe_allow_html=True)


def human_date(ts) -> str:
    if pd.isna(ts):
        return "дата неизвестна"
    days = (pd.Timestamp.now(tz="UTC") - ts).days
    ago = "сегодня" if days == 0 else "вчера" if days == 1 else f"{days} дн. назад"
    return f"{ts.day} {MONTHS[ts.month - 1]} {ts.year} · {ago}"


@st.cache_data(ttl=15)
def llm_online() -> bool:
    return LMStudioClient().ping()


def with_labels(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["buyer_label"] = df["buyer_type"].map(lambda t: BUYER_TYPES.get(t, "Неясно"))
    df["status_label"] = df["status"].map(lambda s: STATUSES.get(s, STATUSES["new"]))
    return df


# ---------- Лиды ----------

def set_status(lead_id: int, key: str) -> None:
    update_lead(lead_id, status=STATUS_BY_LABEL[st.session_state[key]])
    st.toast("Статус сохранён", icon=":material/check:")


def set_comment(lead_id: int, key: str) -> None:
    update_lead(lead_id, comment=st.session_state[key] or None)
    st.toast("Комментарий сохранён", icon=":material/check:")


def lead_card(lead_id: int, r: pd.Series) -> None:
    with st.container(border=True):
        st.markdown("<div class='lead-card'></div>", unsafe_allow_html=True)
        top_l, top_r = st.columns([3, 2], vertical_alignment="center")
        with top_l:
            color, icon = BUYER_BADGE[r["buyer_label"]]
            st.badge(r["buyer_label"], color=color, icon=icon)
            st.markdown(f"<span class='lead-meta'>{human_date(r['posted_at'])}"
                        f"{' · @' + r['chat'] if pd.notna(r['chat']) else ''}</span>", unsafe_allow_html=True)
        with top_r:
            key = f"status_{lead_id}"
            st.selectbox("Статус", list(STATUSES.values()), index=list(STATUSES).index(r["status"]) if r["status"] in STATUSES else 0,
                         key=key, label_visibility="collapsed", on_change=set_status, args=(lead_id, key))

        text = str(r["raw_text"] or "").replace("<", "&lt;").replace("\n", " ")
        st.markdown(f"<div class='lead-text'>{text}</div>", unsafe_allow_html=True)

        chips = [(":material/location_on:", r["location"]), (":material/square_foot:", r["area_sqm"]),
                 (":material/payments:", r["budget"]), (":material/call:", r["phone"])]
        chips = [f"{icon} {val}" for icon, val in chips if pd.notna(val) and str(val).strip()]
        if chips:
            st.markdown(" &nbsp;·&nbsp; ".join(chips))

        b1, b2, b3 = st.columns(3)
        if pd.notna(r["telegram_username"]):
            b1.link_button("Написать", f"https://t.me/{r['telegram_username']}", icon=":material/send:",
                           type="primary", width="stretch")
        else:
            b1.button("Нет контакта", disabled=True, key=f"nc_{lead_id}", width="stretch")
        b2.link_button("Сообщение", r["source_ref"] or "#", icon=":material/open_in_new:", width="stretch")
        with b3.popover("Детали", icon=":material/more_horiz:", width="stretch"):
            st.markdown("**Полный текст**")
            st.text(r["raw_text"] or "—")
            if pd.notna(r["notes"]) and r["notes"]:
                st.caption(f"Модель: {r['notes']}")
            if pd.notna(r["confidence"]):
                st.caption(f"Уверенность модели: {r['confidence']:.0%}")
            ckey = f"comment_{lead_id}"
            st.text_area("Комментарий", value=r["comment"] if pd.notna(r["comment"]) else "", key=ckey,
                         on_change=set_comment, args=(lead_id, ckey), placeholder="Позвонил, ждёт варианты до пятницы…")


def page_leads():
    page_header("Лиды", "Запросы на покупку складов из Telegram — свежие сверху")
    leads = load_leads()
    if leads.empty:
        st.info("Лидов пока нет. Запустите сбор на странице «Сбор».", icon=":material/info:")
        return

    f1, f2, f3 = st.columns([1, 1.7, 2.9], vertical_alignment="bottom")
    f4 = st
    period = f1.selectbox("Период", ["7 дней", "30 дней", "90 дней", "Год", "Всё время"], index=2)
    types = f2.segmented_control("Кто ищет", ["Покупатель", "Брокер", "Неясно"], selection_mode="multi",
                                 default=["Покупатель", "Брокер", "Неясно"])
    statuses = f3.segmented_control("Статус", list(STATUSES.values()), selection_mode="multi",
                                    default=[s for k, s in STATUSES.items() if k != "rejected"])
    search = f4.text_input("Поиск", placeholder="Химки, 1000 м², класс А…", icon=":material/search:")

    days = {"7 дней": 7, "30 дней": 30, "90 дней": 90, "Год": 365}.get(period)
    view = with_labels(leads)
    if days:
        view = view[view["posted_at"] >= pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=days)]
    view = view[view["buyer_label"].isin(types or []) & view["status_label"].isin(statuses or [])]
    if search:
        view = view[view["raw_text"].fillna("").str.contains(search, case=False, regex=False)]

    c1, c2, c3 = st.columns([3, 1.3, 1], vertical_alignment="center")
    c1.caption(f"Найдено **{len(view)}** из {len(leads)}")
    mode = c2.segmented_control("Вид", ["Карточки", "Таблица"], default="Карточки", label_visibility="collapsed")
    c3.download_button("CSV", view.to_csv().encode("utf-8-sig"), file_name=f"leads_{datetime.now():%Y%m%d}.csv",
                       mime="text/csv", icon=":material/download:", width="stretch")

    if view.empty:
        st.info("Под фильтры ничего не попало.", icon=":material/filter_alt_off:")
        return

    if mode == "Таблица":
        leads_table(view, key=f"leads_{period}_{types}_{statuses}_{search}")
        return

    shown = st.session_state.setdefault("cards_shown", 12)
    items = list(view.iterrows())[:shown]
    for i in range(0, len(items), 2):
        cols = st.columns(2)
        for col, (lead_id, r) in zip(cols, items[i:i + 2]):
            with col:
                lead_card(int(lead_id), r)
    if len(view) > shown:
        if st.button(f"Показать ещё ({len(view) - shown})", icon=":material/expand_more:", width="stretch"):
            st.session_state["cards_shown"] = shown + 12
            st.rerun()


def leads_table(view: pd.DataFrame, key: str) -> None:
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
    text_cols = ["Телефон", "Где", "Площадь", "Бюджет", "Комментарий"]
    table[text_cols] = table[text_cols].astype(object).where(table[text_cols].notna(), "")

    edited = st.data_editor(
        table,
        # ключ зависит от фильтров: правки привязаны к позиции строки, и при смене
        # фильтра старые правки не должны попасть на другой лид
        key=key,
        hide_index=True,
        width="stretch",
        height=min(38 * len(table) + 40, 700),
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
    for lead_id in edited.index:
        new, old = edited.loc[lead_id], table.loc[lead_id]
        if new["Статус"] != old["Статус"]:
            update_lead(int(lead_id), status=STATUS_BY_LABEL[new["Статус"]])
        if (new["Комментарий"] or "") != old["Комментарий"]:
            update_lead(int(lead_id), comment=new["Комментарий"] or None)


# ---------- Обзор ----------

def page_overview():
    page_header("Обзор", "Сколько лидов, откуда и на каком они этапе")
    leads = load_leads()
    if leads.empty:
        st.info("Пока нечего показывать.", icon=":material/info:")
        return
    now = pd.Timestamp.now(tz="UTC")
    fresh = lambda d: int((leads["posted_at"] >= now - pd.Timedelta(days=d)).sum())
    m = st.columns(5)
    m[0].metric("Всего лидов", len(leads), border=True)
    m[1].metric("За 7 дней", fresh(7), border=True)
    m[2].metric("За 30 дней", fresh(30), border=True)
    m[3].metric("С контактом", f"{leads['has_contact'].mean():.0%}", border=True)
    m[4].metric("Покупателей", int((leads["buyer_type"] == "direct").sum()), border=True,
                help="Прямые покупатели. Остальные — брокеры, которые ищут объект для клиента, или неясно")

    g1, g2 = st.columns(2)
    with g1.container(border=True):
        st.markdown("**Лиды по месяцам**")
        st.caption("По дате сообщения, последние 24 месяца")
        monthly = (
            leads.dropna(subset=["posted_at"])
            .assign(Месяц=lambda d: d["posted_at"].dt.tz_convert(None).dt.to_period("M").dt.to_timestamp())
            .groupby("Месяц").size().rename("Лидов")
        )
        monthly = monthly[monthly.index >= now.tz_convert(None) - pd.DateOffset(months=24)]
        st.bar_chart(monthly, color=ACCENT, height=260)
    with g2.container(border=True):
        st.markdown("**Воронка**")
        st.caption("Сколько лидов в каждом статусе")
        funnel = leads["status"].map(STATUSES).value_counts().reindex(list(STATUSES.values()), fill_value=0)
        st.bar_chart(funnel.rename("Лидов"), color=ACCENT, height=260, horizontal=True, sort=False)

    g3, g4 = st.columns(2)
    with g3.container(border=True):
        st.markdown("**Самые полезные чаты**")
        st.caption("Лидов за всё время, топ-10")
        st.bar_chart(leads["chat"].value_counts().head(10).rename("Лидов"), color=ACCENT, height=300, horizontal=True,
                     sort="-Лидов")
    with g4.container(border=True):
        st.markdown("**Кто ищет**")
        st.caption("Прямые покупатели и брокеры с клиентом")
        who = leads["buyer_type"].map(lambda t: BUYER_TYPES.get(t, "Неясно")).value_counts().rename("Лидов")
        st.bar_chart(who, color=ACCENT, height=300, horizontal=True)


# ---------- Сбор ----------

@st.fragment(run_every=3)
def job_panel():
    job = current_job()
    if not job or not job["running"]:
        if job:
            with st.expander(f"Последняя задача: {job['title']} · завершена", icon=":material/history:"):
                st.code(tail(job["log"], 200) or "—", language=None)
        return

    started = datetime.fromisoformat(job["started"])
    minutes = int((datetime.now(timezone.utc) - started).total_seconds() // 60)
    with st.container(border=True):
        h1, h2 = st.columns([4, 1], vertical_alignment="center")
        h1.markdown(f"**:material/progress_activity: {job['title']}**")
        if h2.button("Остановить", icon=":material/stop_circle:", width="stretch"):
            stop_job(job)
            st.toast("Останавливаю… Всё собранное уже сохранено.", icon=":material/check:")

        if job["args"][0] == "telegram":
            log = tail(job["log"], 100000)
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
            c[2].metric("Идёт", f"{minutes} мин")
        else:
            st.caption(f"Идёт {minutes} мин")
        with st.expander("Лог", icon=":material/terminal:"):
            st.code(tail(job["log"], 25) or "запускается…", language=None)


def page_run():
    page_header("Сбор", "Поиск новых запросов и новых чатов. Работает в фоне — окно можно закрыть.")
    job = current_job()
    busy = bool(job and job["running"])
    online = llm_online()
    blocked = busy or not SESSION_FILE.exists()

    job_panel()

    left, right = st.columns(2)
    with left.container(border=True):
        st.subheader(":material/radar: Собрать лиды")
        st.caption("Ищет «куплю склад / ищу для клиента» во включённых чатах и проверяет каждое сообщение моделью.")
        chats = read_channels()
        enabled = chats[chats["enabled"]] if not chats.empty else chats
        n_groups = int((enabled["kind"] == "группа").sum()) if not enabled.empty else 0
        scope = st.segmented_control("Где искать", [f"Группы · {n_groups}", f"Все чаты · {len(enabled)}"],
                                     default=f"Группы · {n_groups}",
                                     help="В группах почти все запросы покупателей, каналы — в основном продавцы")
        days = st.select_slider("Свежесть сообщений", options=[7, 14, 30, 60, 90, 180, 365], value=90,
                                format_func=lambda d: f"до {d} дн.")
        with st.expander("Дополнительно", icon=":material/tune:"):
            limit = st.number_input("Результатов на поисковую фразу в чате", 20, 1000, 200, step=20)
            min_conf = st.slider("Минимальная уверенность модели", 0.0, 1.0, 0.5, 0.05)
        if not online:
            st.warning("Сначала запустите сервер в LM Studio.", icon=":material/memory:")
        if st.button("Запустить сбор", type="primary", icon=":material/play_arrow:", width="stretch",
                     disabled=blocked or not online or enabled.empty):
            selected = enabled if (scope or "").startswith("Все") else enabled[enabled["kind"] == "группа"]
            LOG_DIR.mkdir(exist_ok=True)
            run_list = LOG_DIR / "run_channels.txt"
            run_list.write_text("\n".join(selected["username"]) + "\n", encoding="utf-8")
            start_job(
                f"Сбор лидов · {len(selected)} чатов · до {days} дн.",
                ["telegram", "--channels-file", str(run_list), "--max-age-days", str(days),
                 "--limit", str(int(limit)), "--min-confidence", str(min_conf)],
            )
            st.rerun()

    with right.container(border=True):
        st.subheader(":material/explore: Найти новые чаты")
        st.caption("Ищет публичные чаты и каналы о недвижимости и складах. Выключенные вами чаты останутся выключенными.")
        depth = st.segmented_control("Раскрутка через «похожие каналы»", [0, 1, 2], default=1,
                                     format_func=lambda d: {0: "Нет", 1: "1 уровень", 2: "2 уровня"}[d])
        min_members = st.number_input("Минимум участников", 0, 100000, 300, step=100)
        if st.button("Искать чаты", icon=":material/travel_explore:", width="stretch", disabled=blocked):
            start_job("Поиск чатов", ["tg-discover", "--depth", str(depth or 0), "--min-members",
                                      str(int(min_members)), "--out", str(CHANNELS_FILE)])
            st.rerun()

    if busy:
        st.caption("Пока идёт задача, новую запустить нельзя — Telegram-сессия одна.")


# ---------- Чаты ----------

def page_chats():
    page_header("Чаты", "Где ищем запросы. Снимите галочку, чтобы не искать в чате.")
    chats = read_channels()
    if chats.empty:
        st.info("Список пуст. Нажмите «Искать чаты» на странице «Сбор» или добавьте вручную.", icon=":material/info:")
    chats["leads"] = chats["username"].map(load_leads()["chat"].value_counts()).fillna(0).astype(int)

    a, b, c, d = st.columns([1.2, 1.6, 2.4, 2], vertical_alignment="bottom")
    kind_filter = a.selectbox("Тип", ["Все", "группа", "канал"])
    only_enabled = b.segmented_control("Показать", ["Все", "Вкл", "Выкл"], default="Все")
    name_filter = c.text_input("Поиск", placeholder="недвижимость, склад…", icon=":material/search:")
    with d.popover("Добавить чат", icon=":material/add:", width="stretch"):
        with st.form("add_chat", clear_on_submit=True, border=False):
            new_chat = st.text_input("Ссылка или @username", placeholder="t.me/mossdelka")
            if st.form_submit_button("Добавить", type="primary") and new_chat.strip():
                username = new_chat.strip().rstrip("/").split("/")[-1].lstrip("@")
                if username in set(chats["username"]):
                    st.warning(f"@{username} уже в списке")
                else:
                    write_channels(pd.concat([pd.DataFrame([{
                        "enabled": True, "username": username, "kind": "—", "members": None,
                        "title": "добавлен вручную"}]), chats.drop(columns=["leads"])], ignore_index=True))
                    st.rerun()

    shown = chats
    if kind_filter != "Все":
        shown = shown[shown["kind"] == kind_filter]
    if only_enabled in ("Вкл", "Выкл"):
        shown = shown[shown["enabled"] == (only_enabled == "Вкл")]
    if name_filter:
        shown = shown[shown["title"].str.contains(name_filter, case=False, regex=False)
                      | shown["username"].str.contains(name_filter, case=False, regex=False)]

    st.caption(f"Включено **{int(chats['enabled'].sum())}** из {len(chats)}")
    edited = st.data_editor(
        shown.assign(link=shown["username"].map(lambda u: f"https://t.me/{u}")),
        key=f"chats_{kind_filter}_{only_enabled}_{name_filter}",
        hide_index=True, width="stretch", height=560,
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
    if not edited["enabled"].equals(shown["enabled"]):
        chats.loc[edited.index, "enabled"] = edited["enabled"]
        write_channels(chats.drop(columns=["leads"]))
        st.toast("Список чатов сохранён", icon=":material/check:")


# ---------- навигация и состояние ----------

nav = st.navigation([
    st.Page(page_leads, title="Лиды", icon=":material/inbox:", default=True),
    st.Page(page_overview, title="Обзор", icon=":material/insights:", url_path="overview"),
    st.Page(page_run, title="Сбор", icon=":material/radar:", url_path="run"),
    st.Page(page_chats, title="Чаты", icon=":material/forum:", url_path="chats"),
])

with st.sidebar:
    st.logo("macos/AppIcon.png", size="large")
    st.markdown("### Kennimarket")
    st.caption("Покупатели складов · Москва и МО")
    st.divider()

    def status_line(ok: bool, text: str, warn: bool = False) -> None:
        color = "#22c55e" if ok else "#f59e0b" if warn else "#ef4444"
        st.markdown(f"<div style='font-size:0.88rem;margin:4px 0'><span class='dot' style='background:{color}'></span>{text}</div>",
                    unsafe_allow_html=True)

    status_line(llm_online(), f"Модель · {settings.lm_studio_model}" if llm_online() else "LM Studio не запущена")
    status_line(SESSION_FILE.exists(), "Telegram подключён" if SESSION_FILE.exists() else "Telegram: нужен вход", warn=True)
    job = current_job()
    if job and job["running"]:
        status_line(True, f"Идёт: {job['title']}")
    counts = query("SELECT (SELECT COUNT(*) FROM leads) leads, (SELECT COUNT(*) FROM raw_items) raw").iloc[0]
    st.caption(f"{counts.leads} лидов · {counts.raw} сообщений проверено")
    if not SESSION_FILE.exists():
        st.caption("Вход в Telegram — один раз в терминале: `python3 cli.py telegram --channels mossdelka`")

nav.run()
