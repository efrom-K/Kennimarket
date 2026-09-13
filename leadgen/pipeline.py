from typing import List, Optional

from . import db
from .config import settings
from .llm import DGIS_SYSTEM_PROMPT, TELEGRAM_SYSTEM_PROMPT, LMStudioClient


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


def run_telegram_pipeline(channels: List[str], limit_per_channel: int = 500, min_confidence: float = 0.5) -> None:
    from .sources.telegram_source import iter_telegram_messages

    llm = LMStudioClient()
    if not _check_llm(llm):
        return

    db.init_db(settings.db_path)
    saved = 0
    with db.get_conn(settings.db_path) as conn:
        for item in iter_telegram_messages(channels, limit_per_channel):
            if db.raw_item_seen(conn, "telegram", item["source_id"]):
                continue
            db.save_raw_item(conn, "telegram", item["source_id"], item["url"], item["raw_text"])

            result = llm.chat_json(TELEGRAM_SYSTEM_PROMPT, item["raw_text"])
            if not result or not result.get("is_relevant"):
                continue
            if result.get("intent") not in ("buy", "rent_with_buyout"):
                continue
            if float(result.get("confidence") or 0) < min_confidence:
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
            db.save_raw_item(conn, "2gis", str(item["source_id"]), item["url"], item["raw_text"])

            if not item.get("phone"):
                continue  # без контакта лид бесполезен для холодного обзвона

            result = llm.chat_json(DGIS_SYSTEM_PROMPT, item["raw_text"])
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
