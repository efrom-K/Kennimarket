import json
import re
from typing import Optional

import requests

from .config import settings

_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)
_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def _extract_json(text: str) -> Optional[dict]:
    """Local models often wrap JSON in prose or markdown fences. Reasoning
    models (DeepSeek-R1 and similar) additionally prepend a <think>...</think>
    block that can itself contain stray braces, so strip that first, then pull
    out the first balanced-looking {...} block and parse it, tolerating minor
    noise."""
    text = _THINK_BLOCK_RE.sub("", text)
    match = _JSON_BLOCK_RE.search(text)
    if not match:
        return None
    candidate = match.group(0)
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        # Try trimming trailing commas, a common small-model mistake.
        cleaned = re.sub(r",\s*([}\]])", r"\1", candidate)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            return None


class LMStudioClient:
    def __init__(self, base_url: Optional[str] = None, model: Optional[str] = None, timeout: int = 120):
        self.base_url = (base_url or settings.lm_studio_base_url).rstrip("/")
        self.model = model or settings.lm_studio_model
        self.timeout = timeout

    def ping(self) -> bool:
        try:
            r = requests.get(f"{self.base_url}/models", timeout=5)
            return r.status_code == 200
        except requests.RequestException:
            return False

    def chat_json(self, system_prompt: str, user_prompt: str) -> Optional[dict]:
        """Send a chat completion request and parse a JSON object out of the reply."""
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.1,
            "max_tokens": 2000,
        }
        try:
            resp = requests.post(
                f"{self.base_url}/chat/completions", json=payload, timeout=self.timeout
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            print(f"  [llm] request failed: {exc}")
            return None

        try:
            content = resp.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, ValueError):
            print(f"  [llm] unexpected response shape: {resp.text[:200]}")
            return None

        parsed = _extract_json(content)
        if parsed is None:
            print(f"  [llm] could not parse JSON from model output: {content[:200]}")
        return parsed


TELEGRAM_SYSTEM_PROMPT = """Ты — ассистент по квалификации лидов для агентства коммерческой недвижимости
в Москве и Московской области, специализация — склады (складские, производственно-складские помещения).
Тебе дают текст сообщения из публичного Telegram-канала о коммерческой недвижимости.
Определи, является ли это реальным сигналом ПОКУПАТЕЛЬСКОГО интереса к складу (покупка, а не аренда).
Верни СТРОГО один JSON-объект без markdown и пояснений, со следующими полями:
{
  "is_relevant": true/false,           // сообщение вообще про коммерческую/складскую недвижимость
  "intent": "buy"|"rent"|"rent_with_buyout"|"sell"|"other",
  "confidence": 0.0-1.0,               // уверенность, что это реальный лид на ПОКУПКУ склада
  "company_name": string|null,
  "contact_name": string|null,
  "phone": string|null,                // только если явно указан в тексте
  "location": string|null,
  "area_sqm": string|null,
  "budget": string|null,
  "notes": string|null                 // краткое обоснование в одном предложении
}
Если сообщение не про покупку склада — is_relevant=false, confidence=0."""


DGIS_SYSTEM_PROMPT = """Ты — ассистент по холодному B2B-лидогену для агентства, продающего склады
в Москве и Московской области. Тебе дают карточку компании из бизнес-справочника (название, рубрика,
адрес, краткое описание). Оцени, насколько эта компания похожа на потенциального ПОКУПАТЕЛЯ склада
в собственность (растущий бизнес в логистике/опте/e-commerce/производстве, которому имеет смысл
предложить покупку складского помещения).
Верни СТРОГО один JSON-объект без markdown и пояснений:
{
  "is_relevant": true/false,
  "confidence": 0.0-1.0,      // fit-score как потенциального покупателя склада
  "notes": string             // одно предложение с обоснованием и рекомендованным заходом для звонка
}"""
