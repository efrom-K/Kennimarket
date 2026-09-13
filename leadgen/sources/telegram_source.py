import re
import time
from typing import Iterator, List, Optional

import requests
from bs4 import BeautifulSoup

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

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


def _looks_relevant(text: str) -> bool:
    low = text.lower()
    return any(kw in low for kw in KEYWORDS)


def _fetch_page(channel: str, before: Optional[int] = None) -> Optional[str]:
    """Telegram publishes a static, unauthenticated HTML preview of any public
    channel at t.me/s/<channel> (no robots.txt exists for t.me at all, and this
    is Telegram's own official rendering for logged-out users/search engines -
    not a bypass of any access control). This avoids needing my.telegram.org
    API credentials entirely."""
    url = f"https://t.me/s/{channel}"
    params = {"before": before} if before else {}
    try:
        resp = requests.get(url, params=params, headers={"User-Agent": USER_AGENT}, timeout=15)
        resp.raise_for_status()
        return resp.text
    except requests.RequestException as exc:
        print(f"  [telegram] запрос не удался ({url}, before={before}): {exc}")
        return None


def _parse_messages(html: str) -> List[dict]:
    soup = BeautifulSoup(html, "html.parser")
    results = []
    for wrap in soup.select("div.tgme_widget_message_wrap"):
        post_div = wrap.select_one("div.tgme_widget_message[data-post]")
        if not post_div:
            continue
        data_post = post_div.get("data-post", "")
        if "/" not in data_post:
            continue
        channel, msg_id_str = data_post.split("/", 1)
        try:
            msg_id = int(msg_id_str)
        except ValueError:
            continue

        text_div = post_div.select_one("div.tgme_widget_message_text")
        text = text_div.get_text("\n", strip=True) if text_div else ""

        results.append({"channel": channel, "msg_id": msg_id, "text": text})
    return results


def iter_telegram_messages(channels: List[str], limit_per_channel: int = 500, delay: float = 1.0) -> Iterator[dict]:
    """Yields dicts with source_id/url/raw_text for messages in public channels
    that pass the keyword prefilter, scraping the public t.me/s/ web preview
    (no login, no API credentials, no Telethon session needed).

    Paginates backward via the `before=<msg_id>` query param that Telegram's
    own "load more" link uses. Stops per channel once `limit_per_channel`
    messages have been seen or a page returns no new (older) messages.
    """
    for channel in channels:
        channel = channel.strip().lstrip("@")
        if not channel:
            continue
        print(f"[telegram] сканирую @{channel} (до {limit_per_channel} сообщений)...")

        seen_total = 0
        before = None
        min_seen_id = None

        while seen_total < limit_per_channel:
            html = _fetch_page(channel, before)
            if html is None:
                break

            messages = _parse_messages(html)
            if not messages:
                break

            # Page renders oldest-to-newest; walk oldest-first so `before`
            # pagination always moves strictly backward in time.
            messages.sort(key=lambda m: m["msg_id"])
            page_min_id = messages[0]["msg_id"]

            if min_seen_id is not None and page_min_id >= min_seen_id:
                break  # no older messages returned, avoid infinite loop

            for m in messages:
                if not m["text"] or not _looks_relevant(m["text"]):
                    continue
                yield {
                    "source_id": f"{channel}:{m['msg_id']}",
                    "url": f"https://t.me/{channel}/{m['msg_id']}",
                    "raw_text": m["text"],
                    "telegram_username": None,
                }

            seen_total += len(messages)
            min_seen_id = page_min_id
            before = page_min_id
            time.sleep(delay)  # be polite to t.me, this is a shared public endpoint
