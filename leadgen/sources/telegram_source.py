from typing import Iterator, List

from ..config import settings

# Cheap keyword prefilter so we don't burn an LLM call on every single message
# in a busy channel - only texts that plausibly mention buying a warehouse
# get sent to the model for real classification.
KEYWORDS = [
    "куплю склад",
    "куплю складск",
    "ищу склад для покуп",
    "приобрет склад",
    "приобрету склад",
    "рассмотрю покупку склад",
    "склад в собственность",
    "куплю производственно-складск",
    "куплю пск",
    "покупка склада",
    "интересует покупка склад",
    "нужен склад в собственность",
    "куплю помещение склад",
]


def _looks_relevant(text: str) -> bool:
    low = text.lower()
    return any(kw in low for kw in KEYWORDS)


def iter_telegram_messages(channels: List[str], limit_per_channel: int = 500) -> Iterator[dict]:
    """Yields dicts with source_id/url/raw_text/telegram_username for messages
    in public channels that pass the keyword prefilter.

    First run will prompt interactively for your Telegram phone number + login
    code (Telethon standard flow); after that a .session file caches the login.
    """
    from telethon.sync import TelegramClient

    if not settings.telegram_api_id or not settings.telegram_api_hash:
        raise RuntimeError(
            "TELEGRAM_API_ID / TELEGRAM_API_HASH не заданы в .env. "
            "Получить за 1 минуту: https://my.telegram.org -> API development tools"
        )

    with TelegramClient(
        settings.telegram_session_name, int(settings.telegram_api_id), settings.telegram_api_hash
    ) as client:
        for channel in channels:
            channel = channel.strip().lstrip("@")
            if not channel:
                continue
            print(f"[telegram] сканирую @{channel} (до {limit_per_channel} сообщений)...")
            try:
                for msg in client.iter_messages(channel, limit=limit_per_channel):
                    if not msg.text or not _looks_relevant(msg.text):
                        continue
                    sender = None
                    try:
                        sender = msg.sender
                    except Exception:
                        sender = None
                    tg_username = getattr(sender, "username", None) if sender else None
                    yield {
                        "source_id": f"{channel}:{msg.id}",
                        "url": f"https://t.me/{channel}/{msg.id}",
                        "raw_text": msg.text,
                        "telegram_username": tg_username,
                    }
            except Exception as exc:
                print(f"[telegram] не удалось обработать @{channel}: {exc}")
                continue
