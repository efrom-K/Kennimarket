import re
import time
from typing import List, Optional

from . import db
from .config import settings
from .llm import DGIS_SYSTEM_PROMPT, TELEGRAM_SYSTEM_PROMPT, LLMUnavailable, LMStudioClient


# Брокеры сами себя выдают; маленькая модель эти маркеры часто пропускает,
# поэтому правило важнее её ответа.
_BROKER_RE = re.compile(
    r"клиент|для инвестор|для покупател|под заказчик|заказчик|коллеги|комисси|#ищуклиенту|#куплюклиенту|агент|брокер|риэлтор|риелтор",
    re.I,
)


def _buyer_type(text: str, llm_answer: Optional[str]) -> Optional[str]:
    return "broker" if _BROKER_RE.search(text) else llm_answer


def is_telegram_lead(result: Optional[dict], min_confidence: float) -> bool:
    """Лид = все три ответа модели "да" и достаточная уверенность. Решение в коде,
    а не в одном confidence: на прямые вопросы маленькая модель отвечает надёжнее."""
    if not result:
        return False
    if not (result.get("wants_to_buy") and result.get("object_fits")) or result.get("other_region"):
        return False
    try:
        return float(result.get("confidence") or 0) >= min_confidence
    except (TypeError, ValueError):
        return False


def _dedup_key(fields: dict, source: str, source_id: str) -> str:
    for key in ("phone", "email", "telegram_username"):
        val = fields.get(key)
        if val:
            return f"{key}:{str(val).strip().lower()}"
    company = fields.get("company_name")
    if company:
        return f"company:{company.strip().lower()}"
    return f"{source}:{source_id}"


def _check_llm(llm: LMStudioClient) -> bool:
    if llm.ping():
        return True
    print(
        f"[!] LM Studio недоступна на {llm.base_url}. "
        "Открой LM Studio -> вкладка Developer/Local Server -> Start Server, "
        "и убедись, что модель загружена."
    )
    return False


def _ask(llm: LMStudioClient, prompt: str, text: str, retries: int = 10, wait: float = 30) -> Optional[dict]:
    """Спросить модель, пережидая её падение/перезагрузку до ~5 минут.
    Если так и не ответила — LLMUnavailable летит дальше и останавливает сбор."""
    for attempt in range(retries + 1):
        try:
            return llm.chat_json(prompt, text)
        except LLMUnavailable as exc:
            if attempt == retries:
                raise
            print(f"  [llm] модель не отвечает ({exc}), жду {int(wait)} с и пробую снова "
                  f"({attempt + 1}/{retries})...")
            time.sleep(wait)


LLM_DOWN_MESSAGE = (
    "[!] Модель так и не ответила — сбор остановлен. Всё найденное сохранено, непроверенные "
    "сообщения проверятся при следующем запуске. Проверьте LM Studio (сервер запущен, модель загружена)."
)


def run_telegram_pipeline(
    channels: List[str], limit_per_query: int = 200, min_confidence: float = 0.5, max_age_days: Optional[int] = None
) -> None:
    from .sources.telegram_source import iter_telegram_messages

    llm = LMStudioClient()
    if not _check_llm(llm):
        return

    db.init_db(settings.db_path)
    saved = 0
    print(f"[telegram] чатов в списке: {len(channels)}")
    with db.get_conn(settings.db_path) as conn:
        for item in iter_telegram_messages(channels, limit_per_query, max_age_days):
            if db.raw_item_seen(conn, "telegram", item["source_id"]):
                continue
            try:
                result = _ask(llm, TELEGRAM_SYSTEM_PROMPT, item["raw_text"])
            except LLMUnavailable:
                print(LLM_DOWN_MESSAGE)
                break
            # «проверено» только после ответа модели, иначе сбой модели = потерянный лид
            db.save_raw_item(conn, "telegram", item["source_id"], item["url"], item["raw_text"])
            if not is_telegram_lead(result, min_confidence):
                continue

            fields = {
                "intent": result.get("intent"),
                "company_name": result.get("company_name"),
                "contact_name": result.get("contact_name"),
                "phone": result.get("phone"),
                "telegram_username": item.get("telegram_username"),
                "location": result.get("location"),
                "area_sqm": result.get("area_sqm"),
                "budget": result.get("budget"),
                "confidence": result.get("confidence"),
                "notes": result.get("notes"),
                "posted_at": item.get("posted_at"),
                "buyer_type": _buyer_type(item["raw_text"], result.get("buyer_type")),
            }
            key = _dedup_key(fields, "telegram", item["source_id"])
            if db.save_lead(conn, key, "telegram", item["url"], fields):
                saved += 1
                label = fields.get("company_name") or fields.get("contact_name") or key
                print(f"  [+] lead #{saved}: {label}")

            if db.count_leads(conn) >= settings.target_leads:
                print("[i] достигнут TARGET_LEADS, останавливаюсь")
                break
    print(f"[telegram] новых лидов сохранено: {saved}")


def run_dgis_pipeline(
    queries: Optional[List[str]] = None,
    regions: Optional[List[str]] = None,
    pages_per_query: int = 5,
    min_confidence: float = 0.4,
) -> None:
    from .sources.dgis_source import iter_dgis_companies

    llm = LMStudioClient()
    if not _check_llm(llm):
        return

    db.init_db(settings.db_path)
    saved = 0
    with db.get_conn(settings.db_path) as conn:
        for item in iter_dgis_companies(queries, regions, pages_per_query):
            if db.raw_item_seen(conn, "2gis", str(item["source_id"])):
                continue
            if not item.get("phone"):
                db.save_raw_item(conn, "2gis", str(item["source_id"]), item["url"], item["raw_text"])
                continue  # без контакта лид бесполезен для холодного обзвона

            try:
                result = _ask(llm, DGIS_SYSTEM_PROMPT, item["raw_text"])
            except LLMUnavailable:
                print(LLM_DOWN_MESSAGE)
                break
            db.save_raw_item(conn, "2gis", str(item["source_id"]), item["url"], item["raw_text"])
            if not result or not result.get("is_relevant"):
                continue
            if float(result.get("confidence") or 0) < min_confidence:
                continue

            fields = {
                "intent": "target_account",
                "company_name": item.get("company_name"),
                "phone": item.get("phone"),
                "location": item.get("location"),
                "confidence": result.get("confidence"),
                "notes": result.get("notes"),
            }
            key = _dedup_key(fields, "2gis", str(item["source_id"]))
            if db.save_lead(conn, key, "2gis", item["url"], fields):
                saved += 1
                print(f"  [+] lead #{saved}: {fields.get('company_name')}")

            if db.count_leads(conn) >= settings.target_leads:
                print("[i] достигнут TARGET_LEADS, останавливаюсь")
                break
    print(f"[2gis] новых лидов сохранено: {saved}")
