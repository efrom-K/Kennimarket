"""Kennimarket — веб-интерфейс к пайплайну лидов.

Запуск: streamlit run app.py   (или двойной клик по Kennimarket.command)

Сбор и поиск чатов запускаются как фоновые процессы `cli.py` (лог в logs/),
поэтому интерфейс можно закрывать и открывать — работа не прерывается.
"""
import io
import json
import os
import re
from typing import Optional
from urllib.parse import quote
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
from leadgen import triage
from leadgen.llm import LLMUnavailable, LMStudioClient, ensure_model_running

ROOT = Path(__file__).parent
LOG_DIR = ROOT / "logs"
JOB_FILE = LOG_DIR / "job.json"
CHANNELS_FILE = ROOT / "channels.txt"
SESSION_FILE = ROOT / f"{settings.telegram_session_name}.session"
ACCENT = "#2a78d6"

STATUSES = {
    "new": "Новый",
    "contacted": "Написали",
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
               l.notes, l.comment, l.created_at, l.sent_at, r.raw_text,
               p.lead_id IS NOT NULL AS has_profile, p.profile_json, p.draft, p.draft_edited
        FROM leads l LEFT JOIN raw_items r ON r.url = l.source_ref
                     LEFT JOIN lead_profiles p ON p.lead_id = l.id
        """
    )
    df["message"] = df["draft_edited"].where(df["draft_edited"].notna(), df["draft"])
    df["msg_state"] = "Не обработан"
    df.loc[df["has_profile"] == 1, "msg_state"] = "Не подходит"
    df.loc[df["message"].notna(), "msg_state"] = "Готово"
    df.loc[df["sent_at"].notna(), "msg_state"] = "Отправлено"
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


def chat_url(r: pd.Series, text: str = "") -> Optional[str]:
    """Ссылка на чат с лидом; в приложении для Mac откроет Telegram и положит текст в буфер."""
    q = f"?text={quote(text)}" if text else ""
    if pd.notna(r["telegram_username"]):
        return f"https://t.me/{r['telegram_username']}{q}"
    if pd.notna(r["phone"]) and str(r["phone"]).startswith("+"):
        return f"https://t.me/{r['phone']}{q}"
    return None


def save_message(lead_id: int, key: str) -> None:
    with db.get_conn(settings.db_path) as conn:
        conn.execute("UPDATE lead_profiles SET draft_edited = ? WHERE lead_id = ?", (st.session_state[key], lead_id))


def mark_sent(lead_id: int, key: str) -> None:
    with db.get_conn(settings.db_path) as conn:
        conn.execute("UPDATE lead_profiles SET draft_edited = ? WHERE lead_id = ?", (st.session_state[key], lead_id))
        conn.execute("UPDATE leads SET status = 'contacted', sent_at = ? WHERE id = ?", (db.now_iso(), lead_id))
    st.toast("Отмечено: написали", icon=":material/check:")


def pick_object(lead_id: int, key: str) -> None:
    if st.session_state[key] is not None:
        with db.get_conn(settings.db_path) as conn:
            triage.draft_with_object(conn, lead_id, int(st.session_state[key]))


def prepare_message(lead_id: int) -> None:
    with db.get_conn(settings.db_path) as conn:
        try:
            ok = triage.process_lead(conn, LMStudioClient(), lead_id)
        except LLMUnavailable:
            ok = False
    if not ok:
        st.toast("Не получилось: проверьте, что LM Studio запущена", icon=":material/error:")


@st.cache_data(ttl=5)
def active_objects() -> pd.DataFrame:
    return query("SELECT * FROM objects WHERE active = 1 ORDER BY id")


def message_block(lead_id: int, r: pd.Series) -> None:
    state = r["msg_state"]
    if state == "Отправлено":
        sent = pd.to_datetime(r["sent_at"], utc=True)
        with st.expander(f"Написали {sent.day} {MONTHS[sent.month - 1]} · что отправили", icon=":material/mark_chat_read:"):
            st.text(r["message"] or "—")
        return
    if state == "Готово":
        key = f"msg_{lead_id}"
        st.text_area("Первое сообщение", value=r["message"], key=key, height=170, on_change=save_message,
                     args=(lead_id, key), help="Можно править — изменения сохраняются")
        text = st.session_state.get(key, r["message"])
        url = chat_url(r, text)
        b1, b2 = st.columns([1.4, 1])
        if url:
            b1.link_button("Открыть чат с текстом", url, icon=":material/send:", type="primary", width="stretch",
                           help="В приложении для Mac текст будет в буфере обмена — вставьте Cmd+V и отправьте")
        else:
            b1.button("Нет контакта в Telegram", disabled=True, key=f"nc_{lead_id}", width="stretch")
        b2.button("Отправил", icon=":material/done:", key=f"sent_{lead_id}", width="stretch",
                  on_click=mark_sent, args=(lead_id, key))
        return
    objs = active_objects()
    if state == "Не подходит":
        if objs.empty:
            st.caption(":material/inventory_2: База складов пуста — добавьте объекты на странице «Объекты».")
            return
        profile = json.loads(r["profile_json"])
        reasons = min((triage.mismatch_reasons(profile, o) for o in objs.to_dict("records")), key=len)
        st.caption(f":material/block: Ваши склады не подходят: {'; '.join(reasons)}")
        key = f"pick_{lead_id}"
        st.selectbox("Всё равно предложить", objs["id"].tolist(), index=None, key=key, on_change=pick_object,
                     args=(lead_id, key), placeholder="Всё равно предложить объект…", label_visibility="collapsed",
                     format_func=lambda i: objs.set_index("id").loc[i, "title"] or f"Объект #{i}")
        return
    st.button("Подготовить сообщение", icon=":material/edit_note:", key=f"prep_{lead_id}", width="stretch",
              on_click=prepare_message, args=(lead_id,), disabled=not llm_online(),
              help="Модель разберёт запрос и подберёт ваш склад (≈10 с)")


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
        contact = [f"@{r['telegram_username']}" if pd.notna(r["telegram_username"]) else None,
                   r["phone"] if pd.notna(r["phone"]) else None]
        contact = [c for c in contact if c]
        st.caption(":material/person: " + (" · ".join(contact) if contact else "контакта нет — только через пост"))

        message_block(lead_id, r)

        b1, b2 = st.columns(2)
        b1.link_button("Пост в чате", r["source_ref"] or "#", icon=":material/open_in_new:", width="stretch")
        with b2.popover("Детали", icon=":material/more_horiz:", width="stretch"):
            st.markdown("**Полный текст**")
            st.text(r["raw_text"] or "—")
            if pd.notna(r["profile_json"]):
                pr = json.loads(r["profile_json"])
                if pr.get("requirements"):
                    st.caption(f"Требования: {pr['requirements']}")
            if pd.notna(r["notes"]) and r["notes"]:
                st.caption(f"Модель: {r['notes']}")
            ckey = f"comment_{lead_id}"
            st.text_area("Комментарий", value=r["comment"] if pd.notna(r["comment"]) else "", key=ckey,
                         on_change=set_comment, args=(lead_id, ckey), placeholder="Позвонил, ждёт презентацию…")


def page_leads():
    page_header("Лиды", "Запросы на покупку складов из Telegram и готовые первые сообщения")
    leads = load_leads()
    if leads.empty:
        st.info("Лидов пока нет. Запустите сбор на странице «Сбор».", icon=":material/info:")
        return

    f1, f2, f3 = st.columns([1, 1.7, 2.9], vertical_alignment="bottom")
    f5, f4 = st.columns([2.4, 3.2], vertical_alignment="bottom")
    msg_filter = f5.segmented_control("Первое сообщение", ["Все", "Готово", "Не подходит", "Отправлено"], default="Все")
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
    if msg_filter and msg_filter != "Все":
        view = view[view["msg_state"] == msg_filter]
    # сначала те, кому пора писать; внутри — свежие сверху (сортировка устойчивая)
    order = {"Готово": 0, "Не обработан": 1, "Не подходит": 2, "Отправлено": 3}
    view = view.sort_values("msg_state", key=lambda s: s.map(order), kind="stable")

    c1, c2, c3 = st.columns([3, 1.3, 1], vertical_alignment="center")
    c1.caption(f"Найдено **{len(view)}** из {len(leads)}")
    mode = c2.segmented_control("Вид", ["Карточки", "Таблица"], default="Карточки", label_visibility="collapsed")
    c3.download_button("CSV", view.to_csv().encode("utf-8-sig"), file_name=f"leads_{datetime.now():%Y%m%d}.csv",
                       mime="text/csv", icon=":material/download:", width="stretch")

    if view.empty:
        st.info("Под фильтры ничего не попало.", icon=":material/filter_alt_off:")
        return

    if mode == "Таблица":
        leads_table(view, key=f"leads_{period}_{types}_{statuses}_{search}_{msg_filter}")
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
    m[2].metric("Готово к отправке", int((leads["msg_state"] == "Готово").sum()), border=True,
                help="Есть черновик первого сообщения с вашим складом")
    m[3].metric("Написали", int((leads["msg_state"] == "Отправлено").sum()), border=True)
    m[4].metric("С контактом", f"{leads['has_contact'].mean():.0%}", border=True)

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
            if "Модель так и не ответила" in tail(job["log"], 5):
                st.error("Сбор остановлен: модель в LM Studio перестала отвечать. Найденное сохранено, "
                         "непроверенные сообщения проверятся при следующем запуске. Проверьте, что в LM Studio "
                         "сервер запущен и модель загружена, и запустите сбор снова.", icon=":material/memory:")
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

        if "модель не отвечает" in tail(job["log"], 2):
            st.warning("Модель в LM Studio не отвечает — жду до 5 минут и пробую снова. Проверьте LM Studio: "
                       "сервер запущен, модель загружена.", icon=":material/hourglass_top:")
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

    with st.container(border=True):
        n_raw = int(query("""SELECT COUNT(*) n FROM leads l JOIN raw_items r ON r.url = l.source_ref
                             WHERE l.source = 'telegram' AND l.id NOT IN (SELECT lead_id FROM lead_profiles)""").n[0])
        c1, c2 = st.columns([3, 1], vertical_alignment="center")
        c1.markdown("**:material/edit_note: Подготовить первые сообщения**")
        c1.caption(f"Лидов без черновика: {n_raw}. Новые лиды обрабатываются сами во время сбора; "
                   "эта кнопка — для старых и для тех, на ком модель не ответила.")
        if c2.button("Подготовить", width="stretch", disabled=busy or not online or n_raw == 0):
            start_job(f"Подготовка сообщений · {n_raw} лидов", ["triage"])
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


# ---------- Объекты ----------

OBJECT_COLUMNS = {  # колонка в базе -> заголовок в Excel
    "title": "Название", "address": "Адрес", "direction": "Направление (шоссе)", "mkad_km": "От МКАД, км",
    "area_sqm": "Площадь, м²", "class": "Класс", "price_rub": "Цена, ₽", "ceiling_m": "Потолки, м",
    "gates": "Ворота/пандусы", "heating": "Отопление", "presentation_url": "Презентация (ссылка)",
    "notes": "Комментарий", "active": "В продаже",
}
NUMERIC = {"mkad_km", "area_sqm", "price_rub", "ceiling_m"}
EXAMPLE = {"title": "Склад Видное", "address": "МО, Видное, ул. Промышленная, 5", "direction": "Каширское шоссе",
           "mkad_km": 12, "area_sqm": 1200, "class": "B", "price_rub": 95000000, "ceiling_m": 9,
           "gates": "2 ворот, пандус", "heating": "тёплый", "presentation_url": "https://…",
           "notes": "свободен", "active": "да"}


def to_number(v) -> Optional[float]:
    """«95 млн», «1,4 млрд», «95 000 000», 95000000.0 -> число."""
    if v is None or (isinstance(v, float) and pd.isna(v)) or str(v).strip() == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    t = str(v).lower().replace("\xa0", "").replace(" ", "").replace(",", ".")
    mult = 1e9 if "млрд" in t else 1e6 if "млн" in t else 1e3 if "тыс" in t else 1
    m = re.search(r"\d+(\.\d+)?", t)
    return float(m.group(0)) * mult if m else None


def _norm(header: str) -> str:
    return re.sub(r"[^а-яёa-z0-9]", "", header.lower().replace("²", "2"))


def _column_for(header: str) -> Optional[str]:
    """Заголовок из файла -> колонка базы: «Площадь, м2», «площадь», «Цена» тоже подходят."""
    h = _norm(header)
    for k, v in OBJECT_COLUMNS.items():
        if h == _norm(v):
            return k
    for k, v in OBJECT_COLUMNS.items():
        first = _norm(v.split()[0] if k != "mkad_km" else "мкад")
        if h.startswith(first) or (k == "mkad_km" and "мкад" in h):
            return k
    return None


def template_xlsx() -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        pd.DataFrame(columns=list(OBJECT_COLUMNS.values())).to_excel(xw, sheet_name="Склады", index=False)
        pd.DataFrame([{OBJECT_COLUMNS[k]: v for k, v in EXAMPLE.items()}]).to_excel(
            xw, sheet_name="Пример заполнения", index=False)
        for ws in xw.book.worksheets:
            for col in ws.columns:
                ws.column_dimensions[col[0].column_letter].width = 22
    return buf.getvalue()


def parse_objects(upload) -> pd.DataFrame:
    if upload.name.lower().endswith((".xlsx", ".xls")):
        raw = pd.read_excel(upload, sheet_name=0)
    else:
        data = upload.getvalue()
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("cp1251")
        first = text.splitlines()[0] if text else ""
        sep = max([";", "\t", ","], key=first.count)  # русский Excel сохраняет CSV через «;»
        raw = pd.read_csv(io.StringIO(text), sep=sep)
    raw = raw.rename(columns=lambda c: _column_for(str(c)) or c)
    df = pd.DataFrame({k: raw[k] if k in raw else None for k in OBJECT_COLUMNS})
    for k in NUMERIC:
        df[k] = df[k].map(to_number)
    df["active"] = df["active"].map(lambda v: 0 if str(v).strip().lower() in ("нет", "0", "false", "снят") else 1)
    df = df[df["title"].notna() | df["area_sqm"].notna()]
    return df.astype(object).where(df.notna(), None)


def save_objects(df: pd.DataFrame, replace: bool) -> None:
    rows = df[list(OBJECT_COLUMNS)].astype(object).where(df[list(OBJECT_COLUMNS)].notna(), None).values.tolist()
    with db.get_conn(settings.db_path) as conn:
        if replace:
            conn.execute("DELETE FROM objects")
        conn.executemany(f"INSERT INTO objects ({', '.join(OBJECT_COLUMNS)}) VALUES ({', '.join('?' * len(OBJECT_COLUMNS))})", rows)
        n = triage.rematch_all(conn)  # подбор и черновики — под новую базу, без модели
    active_objects.clear()
    st.toast(f"База сохранена, черновики пересобраны для {n} лидов", icon=":material/check:")


def page_objects():
    page_header("Объекты", "Готовые склады, которые вы продаёте. Под них подбираются лиды и пишутся сообщения.")
    objs = query(f"SELECT id, {', '.join(OBJECT_COLUMNS)} FROM objects ORDER BY id")

    with st.container(border=True):
        st.markdown("**Загрузить из Excel**")
        c1, c2 = st.columns([1, 2], vertical_alignment="center")
        c1.download_button("Скачать шаблон", template_xlsx(), file_name="kennimarket_sklady.xlsx",
                           icon=":material/download:", width="stretch",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        c2.caption("Заполните лист «Склады» (на втором листе — пример). Обязательно: площадь; "
                   "желательно направление, удалённость от МКАД и цена — по ним идёт подбор.")
        upload = st.file_uploader("Файл со складами", type=["xlsx", "xls", "csv"], label_visibility="collapsed")
        if upload:
            try:
                parsed = parse_objects(upload)
            except Exception as exc:
                st.error(f"Не получилось прочитать файл: {exc}", icon=":material/error:")
                parsed = None
            if parsed is not None:
                st.caption(f"В файле {len(parsed)} объектов:")
                st.dataframe(parsed.rename(columns=OBJECT_COLUMNS), hide_index=True, width="stretch")
                u1, u2 = st.columns(2)
                if u1.button("Заменить всю базу", type="primary", width="stretch", disabled=parsed.empty):
                    save_objects(parsed, replace=True)
                    st.rerun()
                if u2.button("Добавить к базе", width="stretch", disabled=parsed.empty):
                    save_objects(parsed, replace=False)
                    st.rerun()

    st.markdown(f"**В базе: {len(objs)}** · в продаже {int((objs['active'] == 1).sum()) if not objs.empty else 0}")
    edit = objs.drop(columns=["id"]).assign(active=objs["active"].astype(bool))
    edited = st.data_editor(
        edit, key="objects_editor", num_rows="dynamic", hide_index=True, width="stretch",
        column_config={k: (st.column_config.CheckboxColumn(v) if k == "active" else
                           st.column_config.NumberColumn(v, format="localized") if k in NUMERIC
                           else st.column_config.LinkColumn(v) if k == "presentation_url"
                           else st.column_config.TextColumn(v)) for k, v in OBJECT_COLUMNS.items()},
    )
    if st.button("Сохранить изменения", icon=":material/save:", type="primary"):
        save_objects(edited.assign(active=edited["active"].fillna(True).astype(int)), replace=True)
        st.rerun()


# ---------- Настройки ----------

def page_settings():
    page_header("Настройки", "Как вы представляетесь в первом сообщении")
    with db.get_conn(settings.db_path) as conn:
        cur = {k: db.get_setting(conn, k) for k in ("agency", "manager", "phone")}
    with st.form("sign"):
        agency = st.text_input("Агентство", value=cur["agency"], placeholder="Склады МО")
        manager = st.text_input("Имя менеджера", value=cur["manager"], placeholder="Иван")
        phone = st.text_input("Ваш телефон (необязательно)", value=cur["phone"], placeholder="+7 900 000-00-00",
                              help="Если указать — добавится в конец сообщения")
        if st.form_submit_button("Сохранить", type="primary"):
            with db.get_conn(settings.db_path) as conn:
                for k, v in (("agency", agency), ("manager", manager), ("phone", phone)):
                    db.set_setting(conn, k, v.strip())
                n = triage.rematch_all(conn)
            st.toast(f"Сохранено, черновики обновлены ({n})", icon=":material/check:")
    st.caption("Так начнётся сообщение:")
    st.code(f"Добрый день! Меня зовут {manager or '…'}, агентство «{agency or '…'}».", language=None)
    st.caption("Черновики, которые вы уже правили вручную, не перезаписываются.")


# ---------- навигация и состояние ----------

nav = st.navigation([
    st.Page(page_leads, title="Лиды", icon=":material/inbox:", default=True),
    st.Page(page_overview, title="Обзор", icon=":material/insights:", url_path="overview"),
    st.Page(page_run, title="Сбор", icon=":material/radar:", url_path="run"),
    st.Page(page_chats, title="Чаты", icon=":material/forum:", url_path="chats"),
    st.Page(page_objects, title="Объекты", icon=":material/warehouse:", url_path="objects"),
    st.Page(page_settings, title="Настройки", icon=":material/settings:", url_path="settings"),
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

    status_line(llm_online(), f"Модель · {settings.lm_studio_model}" if llm_online() else "Модель не на связи")
    if not llm_online():
        if st.button("Запустить модель", icon=":material/power_settings_new:", width="stretch"):
            with st.spinner("Запускаю LM Studio и загружаю модель…"):
                ensure_model_running()
            llm_online.clear()
            st.rerun()
    status_line(SESSION_FILE.exists(), "Telegram подключён" if SESSION_FILE.exists() else "Telegram: нужен вход", warn=True)
    job = current_job()
    if job and job["running"]:
        status_line(True, f"Идёт: {job['title']}")
    counts = query("SELECT (SELECT COUNT(*) FROM leads) leads, (SELECT COUNT(*) FROM raw_items) raw").iloc[0]
    st.caption(f"{counts.leads} лидов · {counts.raw} сообщений проверено")
    if not SESSION_FILE.exists():
        st.caption("Вход в Telegram — один раз в терминале: `python3 cli.py telegram --channels mossdelka`")

nav.run()
