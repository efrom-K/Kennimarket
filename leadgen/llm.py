import json
import re
import subprocess
from pathlib import Path
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


LMS_CLI = Path.home() / ".lmstudio" / "bin" / "lms"


def ensure_model_running(model: Optional[str] = None) -> bool:
    """Поднять сервер LM Studio и загрузить модель, если они не запущены (через CLI `lms`,
    который ставится вместе с LM Studio). Безопасно вызывать сколько угодно раз: модель
    не грузится повторно. True — модель на месте и сервер отвечает."""
    client = LMStudioClient()
    model = model or client.model
    if not LMS_CLI.exists():  # LM Studio не установлена — остаётся надеяться на ручной запуск
        return client.ping()
    try:
        if not client.ping():
            subprocess.run([str(LMS_CLI), "server", "start"], capture_output=True, timeout=90)
        out = subprocess.run([str(LMS_CLI), "ps", "--json"], capture_output=True, text=True, timeout=60).stdout
        try:
            loaded = {m.get("identifier") for m in json.loads(out or "[]")} | {m.get("modelKey") for m in json.loads(out or "[]")}
        except ValueError:
            loaded = set()
        if model not in loaded:
            subprocess.run([str(LMS_CLI), "load", model, "-y"], capture_output=True, timeout=300)
    except (OSError, subprocess.SubprocessError):
        pass
    return client.ping()


class LLMUnavailable(Exception):
    """Сервер модели не ответил (упал, выгрузил модель, таймаут) — это не «отказ»,
    сообщение нужно проверить позже."""


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

    def chat_json(self, system_prompt: str, user_prompt: str, retry: bool = True) -> Optional[dict]:
        """Send a chat completion request and parse a JSON object out of the reply.
        Returns None if the model answered but no JSON could be parsed;
        raises LLMUnavailable if the server didn't answer at all."""
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
            raise LLMUnavailable(str(exc)) from exc

        try:
            content = resp.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, ValueError):
            print(f"  [llm] unexpected response shape: {resp.text[:200]}")
            return None

        parsed = _extract_json(content)
        if parsed is None and retry:
            # маленькие модели изредка ломают JSON; повтор обычно помогает
            return self.chat_json(system_prompt, user_prompt, retry=False)
        if parsed is None:
            print(f"  [llm] could not parse JSON from model output: {content[:200]}")
        return parsed


TELEGRAM_SYSTEM_PROMPT = """Ты — ассистент по квалификации лидов для агентства, которое ПРОДАЁТ склады
в Москве и Московской области. Тебе дают текст сообщения из публичного Telegram-чата или канала.
Нужно найти людей, которые хотят КУПИТЬ склад. Ответь на три вопроса отдельно:

1. wants_to_buy — автор (или его клиент/инвестор/заказчик) хочет КУПИТЬ объект?
   true: "куплю", "купим", "ищу для клиента на покупку", "запрос на покупку", "хотим приобрести".
   Поиск ЗЕМЛИ/участка/ЗУ ("ищу участок", "ищу клиенту ЗУ") — это тоже покупка → true.
   Если рассматривают и аренду, и покупку ("#аренда #покупка", "сниму/куплю") → true.
   false: продаёт ("продаётся", "продам", описание объекта с ценой), сдаёт, ищет только АРЕНДУ,
   новость/аналитика рынка, реклама услуг ("подбор объектов", "поможем"), вакансия.
2. object_fits — объект: склад, ПСК, производственное помещение, ангар, промбаза, логистический комплекс,
   или земля под склад/производство/промку? false: офис, торговое помещение, жильё, земля под жильё/ИЖС/ЖК,
   ГАБ без склада, дата-центр.
3. other_region — в тексте ЯВНО назван регион ВНЕ Москвы и Московской области (СПб, Ленобласть, Краснодар,
   Кавказ, Крым, другой город России или страна) → true. Шоссе, районы, города Подмосковья (Химки, Мытищи,
   Ногинск, Дмитровское ш., ЦКАД, МКАД и т.п.) и отсутствие локации → false.

Верни СТРОГО один JSON-объект без markdown и пояснений:
{
  "wants_to_buy": true/false,
  "object_fits": true/false,
  "other_region": true/false,
  "intent": "buy"|"rent"|"rent_with_buyout"|"sell"|"other",
  "buyer_type": "direct"|"broker"|"unknown", // direct — покупает для себя/своей компании;
                                       // broker — брокер/агент/риэлтор ищет "для клиента/покупателя/инвестора"
  "confidence": 0.0-1.0,               // уверенность, что это реальный запрос на ПОКУПКУ склада
  "company_name": string|null,
  "contact_name": string|null,
  "phone": string|null,                // только если явно указан в тексте
  "location": string|null,
  "area_sqm": string|null,
  "budget": string|null,
  "notes": string|null                 // краткое обоснование в одном предложении
}"""


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
