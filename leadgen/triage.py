"""Первое сообщение лиду: анкета запроса -> подбор вашего готового склада -> черновик.
Пишем только с конкретным объектом на продажу; если ни один ваш склад не подходит под
запрос — черновика нет (объект можно выбрать вручную). Дальше лида ведёт человек.

Модель вызывается один раз на лид (анкета + вводная фраза для письма). Подбор
и сам текст собирает код: цифры в письме берутся только из базы
объектов, поэтому модель не может их выдумать. Подбор и черновики можно
пересобирать без модели (rematch), например после обновления базы объектов.
"""
import json
import re
from datetime import datetime, timezone
from typing import List, Optional

from . import db
from .llm import LMStudioClient

PROFILE_PROMPT = """Ты помогаешь агентству, которое продаёт склады в Москве и МО. Тебе дают запрос из Telegram
от человека, который хочет КУПИТЬ объект. Разбери запрос в анкету. Бери ТОЛЬКО то, что написано в тексте;
если чего-то нет — null. Числа — просто числа без пробелов и единиц.

Верни СТРОГО один JSON-объект без markdown:
{
  "object_type": "склад"|"пск"|"производство"|"земля"|"другое",  // ПСК = производственно-складской комплекс
  "area_min": число|null,        // площадь помещения в м², нижняя граница ("от 1000" -> 1000)
  "area_max": число|null,        // верхняя граница ("1000-1500" -> 1500; одно число "1000 м2" -> 1000 и 1000)
  "directions": [строки],        // шоссе, районы, города, стороны света: ["Каширское шоссе", "Видное", "юг"]
  "mkad_km_max": число|null,     // "до 30 км от МКАД" -> 30
  "classes": [строки],           // классы склада: ["A", "B"]
  "budget_rub": число|null,      // ВЕРХНЯЯ граница бюджета в рублях ("до 600 млн" -> 600000000).
                                 // "от 50 млн", "рассмотрим и дороже" — это не потолок -> null
  "budget_per_sqm": число|null,  // цена за м² в рублях ("30 000 руб./м2" -> 30000)
  "requirements": строка|null,   // особые требования коротко: "тёплый, потолки от 8 м, пандус, 200 кВт"
  "urgent": true/false           // "срочно", "до 1 октября", "быстро выйти на сделку"
}"""

# Слова, которые есть почти в любом адресе — по ним совпадение ничего не значит.
# «Между МКАД и ЦКАД», «Московская область и регионы» — не направление, поиск не сужают.
_GENERIC = {"шоссе", "район", "области", "область", "москва", "москве", "москвы", "московская", "московской",
            "мкад", "мкада", "цкад", "цкада", "город", "километр", "направление", "внутри", "пределах", "рядом",
            "ближайшее", "подмосковье", "между", "регионы", "регионах", "любой", "любое", "рассмотрим", "варианты"}


# ---------- модель ----------

def extract_profile(llm: LMStudioClient, text: str) -> Optional[dict]:
    """None — модель ответила мусором; LLMUnavailable — модель недоступна (летит наружу)."""
    return llm.chat_json(PROFILE_PROMPT, text)


# ---------- подбор ----------

def _num(v) -> Optional[float]:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _stems(text: str) -> set:
    words = re.findall(r"[а-яёa-z0-9-]{4,}", (text or "").lower())
    return {w[:5] for w in words if w not in _GENERIC}


def match_objects(profile: dict, objects: List[dict], limit: int = 3) -> List[dict]:
    """Объекты, подходящие под анкету, лучшие первыми: [{object, score, reasons}]."""
    if profile.get("object_type") in ("земля", "другое"):
        return []  # в базе склады; под землю подбирать нечего
    a_min, a_max = _num(profile.get("area_min")), _num(profile.get("area_max"))
    budget, per_sqm = _num(profile.get("budget_rub")), _num(profile.get("budget_per_sqm"))
    km_max = _num(profile.get("mkad_km_max"))
    want_dirs = _stems(" ".join(clean_directions(profile.get("directions"))))
    want_classes = {c.strip().upper()[:1] for c in profile.get("classes") or [] if c and c.strip()}

    result = []
    for o in objects:
        if not o.get("active", 1):
            continue
        area, price, km = _num(o.get("area_sqm")), _num(o.get("price_rub")), _num(o.get("mkad_km"))
        score, reasons = 0, []

        # Жёсткие условия: площадь (±20%), бюджет (+15%), удалённость (+20%), направление.
        if area is not None and (a_min or a_max):
            lo, hi = (a_min or 0) * 0.8, (a_max * 1.2 if a_max else float("inf"))
            if not lo <= area <= hi:
                continue
            score += 3
            reasons.append(f"{area:,.0f} м²".replace(",", " "))
        if price is not None and budget and price > budget * 1.15:
            continue
        if price is not None and per_sqm and area and price / area > per_sqm * 1.15:
            continue
        if price is not None and (budget or per_sqm):
            score += 2
            reasons.append("в бюджете")
        if km is not None and km_max and km > km_max * 1.2:
            continue
        if want_dirs:
            if not want_dirs & _stems(f"{o.get('direction', '')} {o.get('address', '')}"):
                continue
            score += 3
            reasons.append(o.get("direction") or "нужное направление")
        if want_classes and o.get("class"):
            if o["class"].strip().upper()[:1] in want_classes:
                score += 1
                reasons.append(f"класс {o['class']}")
            else:
                score -= 1
        if not score - (2 if (budget or per_sqm) and price is not None else 0) > 0:
            continue  # одного бюджета мало: нужно совпадение по площади или направлению
        result.append({"object": o, "score": score, "reasons": reasons})
    result.sort(key=lambda m: -m["score"])
    return result[:limit]


def mismatch_reasons(profile: dict, o: dict) -> List[str]:
    """Почему объект не подходит под запрос — для карточки лида («нужно до 15 м², у вас 900 м²»)."""
    if profile.get("object_type") in ("земля", "другое"):
        return [f"ищут {'землю' if profile['object_type'] == 'земля' else 'не склад'}"]
    out = []
    a_min, a_max = _num(profile.get("area_min")), _num(profile.get("area_max"))
    area, price, km = _num(o.get("area_sqm")), _num(o.get("price_rub")), _num(o.get("mkad_km"))
    if area is not None and (a_min or a_max):
        if not (a_min or 0) * 0.8 <= area <= (a_max * 1.2 if a_max else float("inf")):
            need = f"{_sqm(a_min)[:-3]}–{_sqm(a_max)}" if a_min and a_max else \
                f"от {_sqm(a_min)}" if a_min else f"до {_sqm(a_max)}"
            out.append(f"нужно {need}, у вас {_sqm(area)}")
    budget = _num(profile.get("budget_rub"))
    if price is not None and budget and price > budget * 1.15:
        out.append(f"бюджет до {_money(budget)}, у вас {_money(price)}")
    km_max = _num(profile.get("mkad_km_max"))
    if km is not None and km_max and km > km_max * 1.2:
        out.append(f"нужно до {km_max:g} км от МКАД, у вас {km:g} км")
    want = clean_directions(profile.get("directions"))
    if want and not _stems(" ".join(want)) & _stems(f"{o.get('direction', '')} {o.get('address', '')}"):
        out.append(f"ищут {', '.join(want[:3])}")
    return out or ["мало совпадений с запросом"]


# ---------- черновик ----------
# Пишем как живой брокер в Telegram: представился, одна фраза про их запрос, объект одной
# фразой, короткий вопрос. У каждой фразы несколько вариантов — у разных лидов разный текст,
# у одного лида всегда один и тот же (выбор по id лида).

def clean_directions(dirs) -> List[str]:
    """Без общих фраз («между МКАД и ЦКАД») и без мусора модели: цифр и смеси латиницы с кириллицей."""
    out = []
    for d in dirs or []:
        d = str(d).strip()
        words = set(re.findall(r"[а-яёa-z]+", d.lower()))
        if not d or re.search(r"\d", d) or (re.search(r"[a-z]", d, re.I) and re.search(r"[а-яё]", d, re.I)):
            continue
        if words and words <= _GENERIC | {"и", "в", "до", "от", "за", "по"}:
            continue
        out.append(d)
    return out


def _pick(options: List[str], seed: int, salt: int) -> str:
    return options[(seed * 31 + salt) % len(options)]


def _money(v: Optional[float]) -> str:
    if not v:
        return ""
    if v >= 1e9:
        return f"{v / 1e9:.1f}".rstrip("0").rstrip(".").replace(".", ",") + " млрд"
    if v >= 1e6:
        return f"{v / 1e6:.1f}".rstrip("0").rstrip(".").replace(".", ",") + " млн"
    return f"{v:,.0f} ₽".replace(",", " ")


def _sqm(v: float) -> str:
    return f"{v:,.0f}".replace(",", " ") + " м²"


def _along(direction: str) -> str:
    """«Каширское шоссе» -> «по Каширскому шоссе»; остальное — «, Видное»."""
    words = direction.split()
    if len(words) == 2 and words[1].lower() in ("шоссе", "ш.") and words[0].lower().endswith("ое"):
        return f"по {words[0][:-2]}ому шоссе"
    return direction


def object_phrase(o: dict) -> str:
    """«склад 900 м² по Новорязанскому шоссе, 3 км от МКАД, потолки 8 м, 48 млн»"""
    area, km, ceil = _num(o.get("area_sqm")), _num(o.get("mkad_km")), _num(o.get("ceiling_m"))
    head = "склад" + (f" {_sqm(area)}" if area else "")
    where = o.get("direction") or o.get("address")
    if where:
        along = _along(where)
        head += f" {along}" if along.startswith("по ") else f", {along}"
    parts = [head]
    if km is not None:
        parts.append("в черте МКАД" if km == 0 else f"{km:g} км от МКАД")
    if ceil:
        parts.append(f"потолки {ceil:g} м")
    if _money(_num(o.get("price_rub"))):
        parts.append(_money(_num(o.get("price_rub"))))
    return ", ".join(parts)


_OBJECT_DATIVE = {"склад": "складу", "пск": "ПСК", "производство": "производственному помещению",
                  "земля": "участку"}


def opener(lead: dict, profile: dict, seed: int = 0) -> str:
    """Одна живая фраза про их запрос: тип и площадь (место — в описании объекта, без повтора)."""
    what = _OBJECT_DATIVE.get(profile.get("object_type"), "объекту")
    a_min, a_max = _num(profile.get("area_min")), _num(profile.get("area_max"))
    area = ""
    if profile.get("object_type") != "земля":  # площадь земли модель путает (сотки/га)
        if a_min and a_max and a_min != a_max:
            area = f" {_sqm(a_min)[:-3]}–{_sqm(a_max)}"
        elif a_min:
            area = f" от {_sqm(a_min)}"
        elif a_max:
            area = f" до {_sqm(a_max)}"
    client = " для клиента" if lead.get("buyer_type") == "broker" else ""
    verb = _pick(["Увидел ваш запрос", "Видел ваш запрос", "Наткнулся на ваш запрос"], seed, 1)
    return f"{verb} по {what}{area}{client}."


# Приветствуем по имени, только если это действительно имя: модель иногда пишет в contact_name
# «Прямой покупатель», «Покупка», название компании. Лучше «Добрый день!», чем «Добрый день, Покупка!».
_NAMES = set("""
александр саша алексей лёша леша андрей анатолий антон аркадий арсений артём артем артур богдан борис вадим
валентин валерий василий вася виктор виталий владимир володя владислав влад всеволод вячеслав слава геннадий
георгий герман глеб григорий давид данил даниил денис дмитрий дима евгений женя егор захар иван ваня игорь илья
кирилл константин костя лев леонид максим макс марат марк матвей михаил миша никита николай коля олег павел паша
пётр петр роман рома руслан сергей серёжа сережа станислав стас степан тимофей тимур фёдор федор филипп эдуард
юрий юра ярослав рустам ринат ильдар азат айдар тагир камиль
александра алина алла алёна алена алиса анастасия настя ангелина анна аня валентина валерия варвара вера
вероника виктория вика галина дарья даша диана ева евгения екатерина катя елена лена елизавета лиза жанна зарина
зоя инна ирина ира карина кристина ксения лариса лилия любовь люба людмила марина мария маша милана надежда
надя наталья наталия наташа нина оксана олеся ольга оля полина раиса регина светлана света снежана софия софья
таисия тамара татьяна таня ульяна эльвира юлия юля яна ясмина
""".split())


def first_name(contact_name: Optional[str]) -> str:
    """Имя для приветствия — только если это известное имя («Данил Пузака» -> «Данил»)."""
    name = (contact_name or "").strip().split(" ")[0].strip(".,!")
    return name.capitalize() if name.lower() in _NAMES else ""


def compose_draft(lead: dict, profile: dict, objects: List[dict], sign: dict) -> str:
    """Первое сообщение с предложением конкретного готового склада (1–2 объекта)."""
    seed = int(lead.get("id") or 0)
    name = first_name(lead.get("contact_name"))  # «Данил Пузака» -> «Данил»
    hello = _pick(["Добрый день", "Здравствуйте"], seed, 0)
    hello += f", {name}!" if name else "!"
    agency = f"агентство «{sign['agency']}»" if sign.get("agency") else ""
    if sign.get("manager"):
        intro = _pick(["Меня зовут {m}", "Я {m}"], seed, 8).format(m=sign["manager"]) + (f", {agency}" if agency else "")
    else:
        intro = agency[:1].upper() + agency[1:] if agency else ""
    first = f"{hello} {intro}." if intro else hello

    body = opener(lead, profile, seed)
    objs = objects[:2]
    if len(objs) == 1:
        body += " " + _pick(["Как раз продаём готовый", "У нас в продаже готовый", "Можем предложить готовый"],
                            seed, 2) + f" {object_phrase(objs[0])}."
    else:
        a, b = (object_phrase(o).removeprefix("склад ") for o in objs)
        body += f" У нас в продаже два готовых склада: {a}; и {b}."
    if lead.get("buyer_type") == "broker":
        body += " " + _pick(["С брокерами работаем, по условиям договоримся.",
                             "С коллегами работаем, условия обсудим."], seed, 3)

    ask = _pick(["Скинуть презентацию?", "Прислать презентацию с фото?", "Могу скинуть презентацию."], seed, 4)
    if not lead.get("phone"):  # номер — главное, что нужно получить из первого касания
        ask += " " + _pick(["Оставьте номер, наберу.", "Удобно созвониться? Подскажите номер.",
                            "Можно ваш номер? Так быстрее обсудим."], seed, 6)
    if sign.get("phone"):
        ask += f" Мой номер: {sign['phone']}"
    return "\n".join((first, body, ask))


# ---------- сборка ----------

def _signature(conn) -> dict:
    return {k: db.get_setting(conn, k) for k in ("agency", "manager", "phone")}


def _objects(conn) -> List[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM objects WHERE active = 1")]


def _save(conn, lead: dict, profile: dict, objects: List[dict], sign: dict, keep_edit: bool = True) -> None:
    matches = match_objects(profile, objects)
    draft = compose_draft(lead, profile, [m["object"] for m in matches], sign) if matches else None
    stored = [{"object_id": m["object"]["id"], "score": m["score"], "reasons": m["reasons"]} for m in matches]
    conn.execute(
        """INSERT INTO lead_profiles (lead_id, profile_json, matches_json, draft, processed_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(lead_id) DO UPDATE SET profile_json=excluded.profile_json, matches_json=excluded.matches_json,
             draft=excluded.draft, processed_at=excluded.processed_at"""
        + ("" if keep_edit else ", draft_edited=NULL"),
        (lead["id"], json.dumps(profile, ensure_ascii=False), json.dumps(stored, ensure_ascii=False),
         draft, db.now_iso()),
    )


def process_lead(conn, llm: LMStudioClient, lead_id: int) -> bool:
    """Полная обработка одного лида (с моделью). True — анкета получена."""
    row = conn.execute("""SELECT l.*, r.raw_text FROM leads l JOIN raw_items r ON r.url = l.source_ref
                          WHERE l.id = ?""", (lead_id,)).fetchone()
    if not row:
        return False
    profile = extract_profile(llm, row["raw_text"])
    if profile is None:
        return False
    _save(conn, dict(row), profile, _objects(conn), _signature(conn), keep_edit=False)
    return True


def rematch_all(conn) -> int:
    """Пересобрать подбор и черновики без модели (после правки базы объектов или подписи)."""
    rows = conn.execute("""SELECT l.*, p.profile_json FROM leads l JOIN lead_profiles p ON p.lead_id = l.id""").fetchall()
    objects, sign = _objects(conn), _signature(conn)
    for r in rows:
        _save(conn, dict(r), json.loads(r["profile_json"]), objects, sign)
    return len(rows)


def draft_with_object(conn, lead_id: int, object_id: int) -> Optional[str]:
    """Ручной выбор: собрать черновик под указанный объект, даже если подбор его не предложил."""
    row = conn.execute("""SELECT l.*, p.profile_json FROM leads l JOIN lead_profiles p ON p.lead_id = l.id
                          WHERE l.id = ?""", (lead_id,)).fetchone()
    obj = conn.execute("SELECT * FROM objects WHERE id = ?", (object_id,)).fetchone()
    if not row or not obj:
        return None
    draft = compose_draft(dict(row), json.loads(row["profile_json"]), [dict(obj)], _signature(conn))
    # в draft_edited: ручной выбор не затрётся пересборкой подбора (rematch_all)
    conn.execute("UPDATE lead_profiles SET draft_edited = ? WHERE lead_id = ?", (draft, lead_id))
    return draft


def unprocessed_lead_ids(conn) -> List[int]:
    return [r[0] for r in conn.execute(
        """SELECT l.id FROM leads l JOIN raw_items r ON r.url = l.source_ref
           WHERE l.source = 'telegram' AND l.id NOT IN (SELECT lead_id FROM lead_profiles)
           ORDER BY l.posted_at DESC""")]


if __name__ == "__main__":
    # python3 -m leadgen.triage — самопроверка подбора и черновика (без модели)
    objs = [
        {"id": 1, "active": 1, "class": "B", "area_sqm": 1200, "direction": "Каширское шоссе", "address": "Видное",
         "mkad_km": 12, "price_rub": 95e6, "ceiling_m": 9},
        {"id": 2, "active": 1, "class": "A", "area_sqm": 5000, "direction": "Новорижское шоссе", "mkad_km": 25,
         "price_rub": 600e6},
        {"id": 3, "active": 0, "class": "B", "area_sqm": 1100, "direction": "Каширское шоссе", "price_rub": 80e6},
    ]
    p = {"object_type": "склад", "area_min": 1000, "area_max": 1500, "directions": ["Каширское шоссе"],
         "budget_rub": 100e6, "classes": ["B"], "urgent": True}
    m = match_objects(p, objs)
    assert [x["object"]["id"] for x in m] == [1], m                                  # 2 — не та площадь, 3 — снят
    assert not match_objects({**p, "budget_rub": 50e6}, objs)                        # дорого
    assert not match_objects({**p, "directions": ["Ярославское шоссе"]}, objs)       # не то направление
    assert not match_objects({**p, "object_type": "земля"}, objs)
    assert match_objects({**p, "directions": ["Между МКАД и ЦКАД"]}, objs)                # не сужает
    assert not match_objects({"object_type": "склад", "budget_rub": 1e9}, objs)           # только бюджет — мало
    assert match_objects({"object_type": "склад", "area_min": 4500}, objs)[0]["object"]["id"] == 2
    assert clean_directions(["юг", "ЗАО", "новая Рiga", "район 1 гектар", "Между МКАД и ЦКАД"]) == ["юг", "ЗАО"]
    assert object_phrase(objs[0]) == "склад 1 200 м² по Каширскому шоссе, 12 км от МКАД, потолки 9 м, 95 млн"
    assert opener({"buyer_type": "broker"}, p).endswith("по складу 1 000–1 500 м² для клиента.")
    assert opener({}, {"object_type": "земля", "area_max": 2000, "directions": ["Шереметьево"]}).endswith("по участку.")
    lead = {"id": 7, "buyer_type": "broker", "telegram_username": "x"}
    sign = {"agency": "Склады МО", "manager": "Иван"}
    mo = [x["object"] for x in m]
    d = compose_draft(lead, p, mo, sign)
    assert "Иван, агентство «Склады МО»." in d and "готовый склад 1 200 м²" in d and "95 млн" in d, d
    assert "брокерами" in d or "коллегами" in d, d
    assert "номер" in d.lower() and "номер" not in compose_draft({**lead, "phone": "+7 900"}, p, mo, sign).lower()
    assert compose_draft({"contact_name": "Данил Пузака"}, p, mo, {}).split("!")[0].endswith("Данил")
    d2 = compose_draft({"id": 3, "buyer_type": "direct", "phone": "+7"}, p, [objs[0], objs[1]], sign)
    assert "два готовых склада: 1 200 м²" in d2 and "брокер" not in d2 and "—" not in d + d2, d2
    assert "базе" not in d + d2 and "Подскажите площадь" not in d + d2
    assert len({compose_draft({**lead, "id": i}, p, mo, sign) for i in range(6)}) > 1     # тексты разные
    assert [first_name(n) for n in ("Данил Пузака", "Прямой покупатель", "Покупка", "андрей", "ООО Ромашка",
                                    "nikita", None)] == ["Данил", "", "", "Андрей", "", "", ""]
    assert mismatch_reasons({"object_type": "склад", "area_max": 15}, objs[0]) == ["нужно до 15 м², у вас 1 200 м²"]
    assert mismatch_reasons({**p, "budget_rub": 50e6}, objs[0]) == ["бюджет до 50 млн, у вас 95 млн"]
    print(d, "\n---\n", d2, "\nok", sep="")
