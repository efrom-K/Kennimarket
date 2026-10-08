import json
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Iterator, List, Optional

from ..config import settings

# Server-side search phrases run against the FULL history of each chat
# (Telegram's own index, works on public groups/channels without joining).
# Telegram search is fuzzy/morphological, so results are noisy - the regex
# prefilter below and then the LLM do the real filtering.
SEARCH_QUERIES = [
    # Отдельные слова, а не фразы: Telegram ищет сообщения со всеми словами запроса, и фразы
    # ("куплю склад") теряли "#запрос ПСК 3000 м² Подольск, покупка". Шум режут префильтр и модель.
    "склад", "склады", "ПСК", "производство", "производственное", "промышленная", "промзона", "промбаза",
    "ангар", "земля", "участок", "куплю", "купим", "покупка", "приобрету", "запрос", "ГАБ", "арендный бизнес",
]

# Cheap prefilter so we don't burn an LLM call on every seller ad that
# Telegram's fuzzy search drags in: need a buy-ish word AND a warehouse-ish object.
_BUY_RE = re.compile(r"купл|купим|купит|покупк|приобр|ищу|ищем|запрос|в собственность", re.I)
_OBJECT_RE = re.compile(r"склад|пск|производствен|под производств|\bгаб|арендн\w* бизнес|промк|промназнач|ангар|логистич|индустри|industrial", re.I)
# Seller/rent/job posts are most of what search returns; drop them unless the
# text also has an explicit buy formula ("куплю", "ищу для клиента", "#запрос"...).
_STRONG_BUY_RE = re.compile(
    r"\bкуплю\b|\bкупим\b|хочу купить|хочет купить|(ищу|ищем) (для|под) (клиент|покупател|инвестор)|"
    r"(ищу|ищем) клиенту|запрос на покупк|на покупку|под покупку|для покупки|рассмотр\w* (покупк|приобрет)|"
    r"#покупка|#ищу|#запрос|#куплю|#габ|запрос (на )?(покупк\w* )?габ|(ищу|ищем) (склад|земельн|участ|зу\b|промк)|нужен склад|приобрету|приобретем|приобретём",
    re.I,
)
_SELL_RE = re.compile(
    r"прода[её]тся|продаю|прода[её]м|продам|продажа|в продаже|сда[её]тся|сдам|сдаю|сда[её]м|в аренду|аренда|предлага|"
    r"вакан|требуются|ищем (сотрудник|кладовщ|работник)",
    re.I,
)

# Чужие регионы и аренда без покупки — частый мусор; режем до модели.
_OTHER_REGION_RE = re.compile(
    r"санкт|петербург|\bспб\b|ленобл|ленинградск|краснодар|калининград|казан[ьи]|екатеринбург|новосибирск|"
    r"\bсочи\b|крым|кавказ|дуба[йи]|\bоаэ\b|пхукет|таиланд|батуми|абхаз",
    re.I,
)
_MOSCOW_RE = re.compile(r"москв|подмоск|московск|\bмо\b|мкад|цкад|\bттк\b|шоссе|\bш\.", re.I)
_RENT_RE = re.compile(r"аренд|сниму|снять", re.I)
# У ГАБ "с арендатором"/"арендный поток" — это доход покупателя, а не аренда помещения.
_TENANT_RE = re.compile(r"арендатор\w*|арендн\w* (бизнес|поток|доход)\w*", re.I)
_PURCHASE_RE = re.compile(r"купл|куп(им|ит|ить|ят)\b|покуп|приобр|в собственност|\bдкп\b", re.I)

# contacts.Search queries for discovering chats. Returns ~10 public chats each.
# Это только кандидаты: в список попадёт лишь тот чат, где проба нашла запросы на покупку.
DISCOVERY_QUERIES = [
    "склад", "склады москва", "склады мо", "складская недвижимость", "куплю склад", "покупка склада",
    "коммерческая недвижимость", "коммерческая недвижимость москва", "коммерческая недвижимость мо",
    "коммерческая недвижимость подмосковье", "коммерция москва", "недвижимость москва", "недвижимость подмосковье",
    "брокеры недвижимости", "брокеры коммерческой", "брокеры коммерческой недвижимости", "риэлторы москва",
    "агенты недвижимости", "сделки недвижимость", "московские сделки", "инвестиции в недвижимость",
    "производственная база", "производственные помещения", "промзона", "промышленная недвижимость",
    "индустриальная недвижимость", "light industrial", "ангар", "земля под склад", "земельные участки мо",
    "земля промназначения", "земля коммерческая", "девелопмент", "ГАБ", "арендный бизнес",
    "чат брокеров", "чат риэлторов", "чат агентов недвижимости", "чат недвижимость", "чат коммерческая недвижимость",
    "сделки чат", "куплю продам недвижимость", "покупка недвижимости", "база объектов", "запросы покупателей",
    "запрос недвижимость", "ищу для клиента", "off market", "закрытые продажи", "объявления москва",
    "продажа склада", "склад продажа", "складской комплекс", "логистический комплекс", "промбаза", "ПСК",
    "ответственное хранение", "производство москва", "земля мо", "участки подмосковье", "клуб брокеров",
    "брокеры москва", "риэлторы мо", "агентство недвижимости москва", "инвесторы недвижимость",
    "бизнес недвижимость", "предприниматели москва", "бизнес чат москва", "торги недвижимость",
    "сделки москва", "сделки мо", "недвижимость рф", "коммерческая недвижимость рф", "куплю здание",
] + [
    f"{prefix} {city}"
    for prefix in ("недвижимость", "склад", "коммерческая недвижимость", "объявления", "куплю продам", "бизнес")
    for city in [
        "подольск", "химки", "домодедово", "балашиха", "мытищи", "люберцы", "одинцово", "красногорск",
        "королев", "щелково", "пушкино", "раменское", "ногинск", "дмитров", "видное", "чехов", "истра",
        "солнечногорск", "серпухов", "новая москва",
    ]
] + [
    f"{prefix} {road}"
    for prefix in ("склад", "недвижимость", "участки")
    for road in ["новорижское", "киевское", "каширское", "ярославское", "дмитровское", "ленинградское",
                 "симферопольское", "горьковское", "егорьевское", "новорязанское", "минское", "калужское"]
] + [
    f"{prefix} {area}"
    for prefix in ("сделки", "брокеры", "коммерческая недвижимость")
    for area in ["мо", "подмосковье", "цфо", "россия", "рф"]
] + [  # username латиницей: поиск Telegram матчит и по нему
    "sklad", "sklady", "skladmsk", "sklad_msk", "sklady_moskva", "nedvizhimost", "nedvizhimost_msk",
    "nedvizhimost_moskva", "kommercheskaya", "kommercheskaya_nedvizhimost", "kommerc", "commerce_msk",
    "cre_moscow", "cre_msk", "realty_msk", "realty_moscow", "industrial_msk", "prom_msk", "promzona",
    "zemlya_mo", "uchastki_mo", "sdelki", "sdelka_msk", "brokery", "broker_msk", "rieltor_msk",
    "zapros_nedvizhimost", "gab_msk", "invest_nedvizhimost", "biznes_msk", "obyavleniya_msk", "msk_obyavleniya",
]
# Job/delivery/cargo chats match "склад"/"логистика" by title but are noise.
_TITLE_EXCLUDE_RE = re.compile(r"работ|вахт|ваканс|подработ|халтур|шабаш|грузчик|доставк|карго|такси", re.I)
# Проба чата: по этим запросам за PROBE_DAYS считаем явные запросы на покупку.
PROBE_QUERIES = ["склад", "куплю", "покупка"]  # первый — ворота: нет «склад» за полгода — чат не наш
PROBE_DAYS = 180
MIN_CHANNEL_HITS = 3
# Пересылки/связанные чаты затягивают новостные каналы и их комментарии: в потоке тысяч сообщений
# "куплю"+"склад" совпадают случайно. Канал без темы в названии не берём, группу — только при сильном сигнале.
_TOPIC_RE = re.compile(
    r"недвиж|склад|коммерч|промзон|промышл|индустри|industrial|\bгаб\b|сделк|брокер|\bcre\b|земл|участ|"
    r"девелоп|логист|инвест|риэлт|риелт|объявлен|запрос|аренд|продаж|покупк|realty|estate|бизнес|ангары?\b|производств",
    re.I,
)
MIN_OFFTOPIC_GROUP_HITS = 5
_ADDLIST_RE = re.compile(r"t\.me/addlist/([\w-]+)")
_MENTION_RE = re.compile(r"(?:t\.me/|@)([A-Za-z]\w{4,31})\b")
_NOT_CHAT = {"addlist", "joinchat", "share", "proxy", "socks", "iv", "c", "s"}
MAX_MENTION_LOOKUPS = 150  # на уровень раскрутки; каждое имя — один contacts.Search
# Результаты проб между запусками: повторный tg-discover пробует только новых кандидатов.
PROBE_CACHE = "logs/probe_cache.json"
PROBE_CACHE_DAYS = 7


def _looks_relevant(text: str) -> bool:
    if not (_BUY_RE.search(text) and _OBJECT_RE.search(text)):
        return False
    if _OTHER_REGION_RE.search(text) and not _MOSCOW_RE.search(text):
        return False
    text = _TENANT_RE.sub(" ", text)
    if _RENT_RE.search(text) and not _PURCHASE_RE.search(text):
        return False
    return bool(_STRONG_BUY_RE.search(text) or not _SELL_RE.search(text))


def _is_request(text: str) -> bool:
    """Явный запрос на покупку (для оценки чата — строже, чем префильтр перед моделью)."""
    return _looks_relevant(text) and bool(_STRONG_BUY_RE.search(text))


def _client():
    from telethon.sync import TelegramClient

    if not settings.telegram_api_id or not settings.telegram_api_hash:
        raise RuntimeError(
            "TELEGRAM_API_ID / TELEGRAM_API_HASH не заданы в .env. "
            "Получить за 1 минуту: https://my.telegram.org -> API development tools"
        )
    # Sleep through FloodWait instead of crashing a long scan.
    return TelegramClient(
        settings.telegram_session_name,
        int(settings.telegram_api_id),
        settings.telegram_api_hash,
        flood_sleep_threshold=600,
    )


def _username(chat) -> Optional[str]:
    """У чатов с несколькими именами (как mossdelka) .username пустой — берём первое активное."""
    if getattr(chat, "username", None):
        return chat.username
    return next((u.username for u in getattr(chat, "usernames", None) or [] if u.active), None)


def _probe(client, chat, cutoff) -> int:
    """Сколько разных явных запросов на покупку в чате с даты cutoff. Тянет 1-3 запроса к Telegram."""
    texts = set()  # одно объявление постят много раз
    for i, query in enumerate(PROBE_QUERIES):
        seen = 0
        for msg in client.iter_messages(chat, search=query, limit=100):
            if msg.date < cutoff:
                break
            seen += 1
            if msg.text and _is_request(msg.text):
                texts.add(msg.text)
        if i == 0 and not seen:
            break  # о складах за полгода ни слова — дальше не проверяем
    return len(texts)


def _find_by_username(client, name: str):
    """Чат по username через contacts.Search, а не ResolveUsername — тот Telegram банит на сутки
    уже после пары десятков вызовов. None — не нашёлся или это не чат (личный аккаунт, бот)."""
    from telethon.tl.functions.contacts import SearchRequest

    try:
        chats = client(SearchRequest(q=name, limit=10)).chats
    except Exception as exc:
        print(f"[tg-discover] поиск @{name}: {exc}")
        return None
    return next((c for c in chats if (_username(c) or "").lower() == name.lower()), None)


def _neighbours(client, chat, tried: set, mentions) -> list:
    """Кандидаты рядом с хорошим чатом: «похожие каналы», пересылки, папки t.me/addlist
    из последних сообщений. @упоминания и t.me-ссылки копятся в `mentions` (Counter) —
    их ищут пачкой после обхода уровня."""
    from telethon.tl.functions.channels import GetChannelRecommendationsRequest, GetFullChannelRequest
    from telethon.tl.functions.chatlists import CheckChatlistInviteRequest
    from telethon.tl.types import MessageEntityTextUrl

    out, slugs = [], set()
    try:  # связанный чат: у канала — группа обсуждений (там пишут покупатели), у группы — её канал
        out += client(GetFullChannelRequest(chat)).chats
    except Exception as exc:
        print(f"[tg-discover] связанный чат @{chat.username}: {exc}")
    if not getattr(chat, "megagroup", False):  # рекомендации Telegram отдаёт только для каналов
        try:
            out += client(GetChannelRecommendationsRequest(channel=chat)).chats
        except Exception as exc:
            print(f"[tg-discover] похожие для @{chat.username}: {exc}")
    for msg in client.iter_messages(chat, limit=300):
        fwd_chat = getattr(msg.forward, "chat", None) if msg.forward else None
        if fwd_chat is not None:
            out.append(fwd_chat)
        texts = [msg.text or ""] + [e.url for e in msg.entities or [] if isinstance(e, MessageEntityTextUrl)]
        for text in texts:
            slugs.update(_ADDLIST_RE.findall(text))
            mentions.update(m.lower() for m in _MENTION_RE.findall(text)
                            if m.lower() not in _NOT_CHAT and not m.lower().endswith("bot"))
    for slug in slugs - tried:
        tried.add(slug)
        try:
            out += client(CheckChatlistInviteRequest(slug=slug)).chats
        except Exception as exc:
            print(f"[tg-discover] папка {slug}: {exc}")
    return out


def discover_chats(
    seeds: List[str] = (), queries: List[str] = None, min_members: int = 300, depth: int = 1
) -> List[dict]:
    """Ищет чаты, где реально пишут запросы на покупку складов. Кандидаты: глобальный поиск
    Telegram по DISCOVERY_QUERIES + seeds (уже известные чаты). Каждый кандидат проверяется
    пробой (_probe) — в результат попадают только чаты с запросами. Затем `depth` раз
    расширяемся от найденных хороших чатов (_neighbours) и снова пробуем."""
    from telethon.tl.functions.contacts import SearchRequest

    cutoff = datetime.now(timezone.utc) - timedelta(days=PROBE_DAYS)
    found, tried = {}, set()
    try:
        with open(PROBE_CACHE, encoding="utf-8") as f:
            cache = json.load(f)
    except (OSError, ValueError):
        cache = {}
    cache_cutoff = (datetime.now(timezone.utc) - timedelta(days=PROBE_CACHE_DAYS)).isoformat()

    with _client() as client:
        def consider(chats) -> list:
            good = []
            for chat in chats:
                username, title = _username(chat), getattr(chat, "title", None)
                if not username or title is None or username.lower() in tried:
                    continue  # нет username (приватный) или это пользователь, а не чат
                tried.add(username.lower())
                members = getattr(chat, "participants_count", None)
                if (members is not None and members < min_members) or _TITLE_EXCLUDE_RE.search(title):
                    continue
                cached = cache.get(username.lower())
                if cached and cached["at"] > cache_cutoff:
                    hits = cached["hits"]
                else:
                    try:
                        hits = _probe(client, chat, cutoff)
                    except Exception as exc:
                        print(f"[tg-discover] @{username}: {exc}")
                        continue
                    cache[username.lower()] = {"hits": hits, "at": datetime.now(timezone.utc).isoformat()}
                    with open(PROBE_CACHE, "w", encoding="utf-8") as f:  # после каждой пробы — прерывание не теряет работу
                        json.dump(cache, f)
                kind = "группа" if getattr(chat, "megagroup", False) else "канал"
                # в каналах (новости, аналитика) случайные совпадения часты, запросы пишут в группах
                on_topic = bool(_TOPIC_RE.search(title))
                if kind == "канал" and (not on_topic or hits < MIN_CHANNEL_HITS):
                    continue
                if kind == "группа" and hits < (1 if on_topic else MIN_OFFTOPIC_GROUP_HITS):
                    continue
                found[username] = {"username": username, "title": title, "kind": kind,
                                   "members": members or 0, "hits": hits}
                print(f"[tg-discover] + @{username} ({kind}): запросов за {PROBE_DAYS} дн. — {hits}")
                good.append(chat)
            return good

        candidates = []
        for seed in seeds:
            try:
                try:
                    peer = client.session.get_input_entity(seed)  # из кэша сессии, без сети
                except ValueError:
                    chat = _find_by_username(client, seed)
                    if chat is not None:
                        candidates.append(chat)
                    continue
                candidates.append(client.get_entity(peer))
            except Exception as exc:
                print(f"[tg-discover] @{seed}: {exc}")
        for q in queries or DISCOVERY_QUERIES:
            try:
                candidates += client(SearchRequest(q=q, limit=100)).chats
            except Exception as exc:
                print(f"[tg-discover] '{q}': {exc}")
        print(f"[tg-discover] кандидатов: {len(candidates)}, проверяю каждый...")
        frontier = consider(candidates)
        print(f"[tg-discover] с запросами: {len(found)}")

        for level in range(depth):
            candidates, mentions = [], Counter()
            for chat in frontier:
                candidates += _neighbours(client, chat, tried, mentions)
            # упомянутые чаще — первыми; большинство упоминаний — личные аккаунты, их Search отсеет
            names = [n for n, _ in mentions.most_common() if n not in tried][:MAX_MENTION_LOOKUPS]
            for name in names:
                found_chat = _find_by_username(client, name)
                if found_chat is not None:
                    candidates.append(found_chat)
                else:
                    tried.add(name)
            frontier = consider(candidates)
            print(f"[tg-discover] уровень {level + 1}: +{len(frontier)}, всего с запросами {len(found)}")
    return sorted(found.values(), key=lambda c: -c["hits"])


def iter_telegram_messages(
    channels: List[str], limit_per_query: int = 200, max_age_days: Optional[int] = None
) -> Iterator[dict]:
    """Yields dicts with source_id/url/raw_text/telegram_username/posted_at for messages
    in public channels/groups found by server-side search (SEARCH_QUERIES)
    that pass the regex prefilter.

    First run will prompt interactively for your Telegram phone number + login
    code (Telethon standard flow); after that a .session file caches the login.
    """
    from telethon.tl.types import User

    seen_texts = set()  # одно и то же объявление постят десятки раз и в разные чаты
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days) if max_age_days else None
    period = f"за {max_age_days} дн." if max_age_days else "по всей истории"
    with _client() as client:
        for channel in channels:
            channel = channel.strip().lstrip("@")
            if not channel:
                continue
            print(f"[telegram] ищу в @{channel} ({len(SEARCH_QUERIES)} запросов {period})...")
            try:
                for query in SEARCH_QUERIES:
                    for msg in client.iter_messages(channel, search=query, limit=limit_per_query):
                        if cutoff and msg.date < cutoff:
                            break  # поиск отдаёт от новых к старым, дальше только старее
                        if not msg.text or msg.text in seen_texts or not _looks_relevant(msg.text):
                            continue
                        seen_texts.add(msg.text)
                        # В каналах отправитель — сам канал, контактом он не является;
                        # юзернейм берём только у живого пользователя (посты в группах).
                        try:
                            sender = msg.get_sender()
                        except Exception:
                            sender = None
                        # боты-агрегаторы (…_bot) переписку не ведут — это не контакт
                        person = isinstance(sender, User) and not sender.bot
                        tg_username = sender.username if person else None
                        # номер виден, только если человек открыл его в настройках приватности
                        tg_phone = f"+{sender.phone}" if person and sender.phone else None
                        yield {
                            "source_id": f"{channel}:{msg.id}",
                            "url": f"https://t.me/{channel}/{msg.id}",
                            "raw_text": msg.text,
                            "telegram_username": tg_username,
                            "telegram_phone": tg_phone,
                            "posted_at": msg.date.isoformat(),
                        }
            except Exception as exc:
                print(f"[telegram] не удалось обработать @{channel}: {exc}")
                continue


if __name__ == "__main__":
    # python3 -m leadgen.sources.telegram_source — самопроверка префильтра
    assert _looks_relevant("Куплю склад от 120 кв.м в районе м. Свиблово.")
    assert _looks_relevant("Ищу клиенту склады класса В на покупку, продажа не интересует")
    assert _looks_relevant("#запрос Купим участок под склад, юг МО")
    assert not _looks_relevant("Продается складской комплекс, рассмотрим аренду")
    assert not _looks_relevant("Ищем кладовщика на склад")
    assert not _looks_relevant("Куплю квартиру в Химках")
    assert not _looks_relevant("Срочный запрос! Склады Калининград 10-15 тыс м2, хочет купить")
    assert _looks_relevant("Ищу клиенту склады на покупку в МО и Ленинградской области")
    assert not _looks_relevant("#запрос #аренда Ищем под клиента склад от 200 м2")
    assert _looks_relevant("Сниму/куплю производственное помещение от 1000 м2")
    assert _is_request("#ищуклиенту Купим ЗУ промка до 15 км от МКАД")
    assert _is_request("Ищу земельный участок под склад в Московской области")
    assert not _is_request("Продаётся склад 3400 м2 в Щелково, ищем покупателя")
    assert _looks_relevant("Коллеги, добрый день! Запрос на ГАБ до 100 млн. Только с арендатором, Москва")
    assert _looks_relevant("#габ Московская область. Запрос покупка работающий ГАБ с федеральным арендатором")
    assert not _looks_relevant("Продаётся ГАБ с арендатором Пятёрочка, окупаемость 9 лет")
    assert not _looks_relevant("ПРЯМОЙ ВЫХОД НА СОБСТВЕННИКА. В ПРОДАЖЕ участки промышленного назначения, ищем покупателя")
    print("ok")
