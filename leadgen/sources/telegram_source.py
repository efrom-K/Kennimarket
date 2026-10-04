import re
from datetime import datetime, timedelta, timezone
from typing import Iterator, List, Optional

from ..config import settings

# Server-side search phrases run against the FULL history of each chat
# (Telegram's own index, works on public groups/channels without joining).
# Telegram search is fuzzy/morphological, so results are noisy - the regex
# prefilter below and then the LLM do the real filtering.
SEARCH_QUERIES = [
    "куплю склад",
    "купим склад",
    "покупка склада",
    "приобрету склад",
    "ищу склад",
    "ищу клиенту склад",
    "запрос склад",
    "склад в собственность",
    "куплю ПСК",
    "куплю производственное помещение",
    "купим промку",
    "куплю ангар",
    "куплю базу",
    "участок под склад",
]

# Cheap prefilter so we don't burn an LLM call on every seller ad that
# Telegram's fuzzy search drags in: need a buy-ish word AND a warehouse-ish object.
_BUY_RE = re.compile(r"купл|купим|купит|покупк|приобр|ищу|ищем|запрос|в собственность", re.I)
_OBJECT_RE = re.compile(r"склад|пск|производствен|промк|промназнач|ангар|логистич|индустри|industrial", re.I)
# Seller/rent/job posts are most of what search returns; drop them unless the
# text also has an explicit buy formula ("куплю", "ищу для клиента", "#запрос"...).
_STRONG_BUY_RE = re.compile(
    r"\bкуплю\b|\bкупим\b|хочу купить|хочет купить|(ищу|ищем) (для |под )?(клиент|покупател|инвестор)|"
    r"(ищу|ищем) клиенту|запрос на покупк|на покупку|под покупку|для покупки|рассмотр\w* (покупк|приобрет)|"
    r"#покупка|#ищу|#запрос|#куплю|(ищу|ищем) склад|нужен склад|приобрету|приобретем|приобретём",
    re.I,
)
_SELL_RE = re.compile(
    r"прода[её]тся|продаю|прода[её]м|продам|продажа|сда[её]тся|сдам|сдаю|сда[её]м|в аренду|аренда|предлага|"
    r"вакан|требуются|ищем (сотрудник|кладовщ|работник)",
    re.I,
)

# contacts.Search queries for discovering chats. Returns ~10 public chats each.
DISCOVERY_QUERIES = [
    "склад", "склады москва", "склады мо", "складская недвижимость", "куплю склад",
    "коммерческая недвижимость", "коммерческая недвижимость москва", "коммерческая недвижимость мо",
    "коммерческая недвижимость подмосковье", "недвижимость москва", "недвижимость подмосковье",
    "недвижимость мо", "брокеры недвижимости", "брокеры коммерческой", "риэлторы москва",
    "агенты недвижимости", "сделки недвижимость", "московские сделки", "инвестиции в недвижимость",
    "производственная база", "производственные помещения", "промзона", "промышленная недвижимость",
    "индустриальная недвижимость", "light industrial", "ангар", "земля под склад", "земельные участки мо",
    "готовый бизнес", "арендный бизнес", "ГАБ", "продажа бизнеса", "покупка бизнеса",
    "логистика москва", "фулфилмент", "селлеры wb", "селлеры ozon", "оптовики москва", "дистрибуция",
    # чаты (группы), где и пишут запросы "куплю/ищу для клиента"
    "чат брокеров", "чат риэлторов", "чат агентов недвижимости", "чат недвижимость", "чат коммерческая недвижимость",
    "сделки чат", "объявления недвижимость", "куплю продам недвижимость", "покупка недвижимости",
    "база объектов", "запросы покупателей", "ищу для клиента", "off market", "закрытые продажи",
] + [
    f"{prefix} {city}"
    for city in [
        "москва", "подмосковье", "подольск", "химки", "домодедово", "балашиха", "мытищи", "люберцы", "одинцово",
        "красногорск", "королев", "щелково", "пушкино", "раменское", "сергиев посад", "коломна", "чехов",
        "ногинск", "дмитров", "видное", "наро-фоминск", "истра", "солнечногорск", "клин", "электросталь",
        "серпухов", "лобня", "долгопрудный", "реутов", "жуковский", "новая москва", "тинао",
    ]
    for prefix in ("недвижимость", "объявления")
]
# Job/delivery/cargo chats match "склад"/"логистика" by title but are noise.
_TITLE_EXCLUDE_RE = re.compile(r"работ|вахт|ваканс|подработ|халтур|шабаш|грузчик|доставк|карго|такси", re.I)
_TITLE_INCLUDE_RE = re.compile(
    r"недвиж|склад|коммерч|промзон|промышл|индустри|industrial|габ|сделк|брокер|cre|земл|девелоп|логист|инвест",
    re.I,
)


def _looks_relevant(text: str) -> bool:
    if not (_BUY_RE.search(text) and _OBJECT_RE.search(text)):
        return False
    return bool(_STRONG_BUY_RE.search(text) or not _SELL_RE.search(text))


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


def discover_chats(queries: List[str] = None, min_members: int = 300, depth: int = 1) -> List[dict]:
    """Finds public groups/channels via Telegram's global chat search, then
    expands `depth` times via Telegram's "similar channels" recommendations
    (works for broadcast channels only; recommended chats must match
    _TITLE_INCLUDE_RE so the crawl doesn't drift off-topic)."""
    from telethon.tl.functions.channels import GetChannelRecommendationsRequest
    from telethon.tl.functions.contacts import SearchRequest

    found = {}

    def add(chats, require_topic=False) -> List[str]:
        new = []
        for chat in chats:
            username = getattr(chat, "username", None)
            members = getattr(chat, "participants_count", None) or 0
            title = chat.title or ""
            if not username or username in found or members < min_members:
                continue
            if _TITLE_EXCLUDE_RE.search(title) or (require_topic and not _TITLE_INCLUDE_RE.search(title)):
                continue
            kind = "группа" if getattr(chat, "megagroup", False) else "канал"
            found[username] = {"username": username, "title": title, "kind": kind, "members": members}
            new.append(username)
        return new

    with _client() as client:
        frontier = []
        for q in queries or DISCOVERY_QUERIES:
            try:
                frontier += add(client(SearchRequest(q=q, limit=100)).chats)
            except Exception as exc:
                print(f"[tg-discover] '{q}': {exc}")
            print(f"[tg-discover] '{q}': всего найдено {len(found)}")

        for level in range(depth):
            next_frontier = []
            for username in frontier:
                if found[username]["kind"] != "канал":
                    continue
                try:
                    result = client(GetChannelRecommendationsRequest(channel=username))
                except Exception as exc:
                    print(f"[tg-discover] похожие для @{username}: {exc}")
                    continue
                next_frontier += add(result.chats, require_topic=True)
            frontier = next_frontier
            print(f"[tg-discover] похожие каналы, уровень {level + 1}: +{len(frontier)}, всего {len(found)}")
    # Группы первыми: именно в чатах пишут запросы, каналы — в основном продавцы и новости.
    return sorted(found.values(), key=lambda c: (c["kind"] != "группа", -c["members"]))


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
    print("ok")
