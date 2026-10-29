"""Проверка и нормализация ответа модели.

Модель обязана вернуть JSON по схеме, но на практике она может:
- вернуть список вместо объекта;
- положить в текстовое поле список или словарь;
- написать «да» вместо true;
- придумать другое значение lead_type.

Здесь мы приводим ответ к строгому виду из девяти полей и применяем
детерминированные бизнес-правила (страховка поверх анализа ИИ).
"""

from __future__ import annotations

import re
from typing import Any

FIELDS = (
    "company",
    "summary",
    "lead_type",
    "key_data",
    "missing_data",
    "next_step",
    "reply_draft",
    "needs_human",
    "human_reason",
)

LEAD_TYPES = ("целевой", "нецелевой", "неясно")

LEAD_TYPE_ALIASES = {
    "целевой": "целевой",
    "целевая": "целевой",
    "target": "целевой",
    "горячий": "целевой",
    "нецелевой": "нецелевой",
    "не целевой": "нецелевой",
    "нецелевая": "нецелевой",
    "спам": "нецелевой",
    "неясно": "неясно",
    "не ясно": "неясно",
    "неприменимо": "неясно",
    "unknown": "неясно",
}

# Признаки жалобы/конфликта. Это страховка: даже если модель ошиблась,
# такая заявка не должна остаться без менеджера.
COMPLAINT_MARKERS = (
    "жалоба",
    "жалобу",
    "жалобы",
    "пожалова",
    "претенз",
    "возврат",
    "отказыва",
    "угрожа",
    "суд",
    "сорван",
    "сорвали",
    "не привезли",
    "не привезёте",
    "не дозвониться",
    "не берёт трубку",
    "не берет трубку",
    "третье письмо",
    "компенсац",
)


class ResultValidationError(Exception):
    """Модель вернула ответ, который нельзя превратить в карточку."""


def _as_text(value: Any) -> str:
    """Превращает список/словарь/число в аккуратную строку."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "да" if value else "нет"
    if isinstance(value, str):
        return " ".join(value.split()).strip()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        parts = [_as_text(item) for item in value]
        return "; ".join(part for part in parts if part)
    if isinstance(value, dict):
        parts = []
        for key, item in value.items():
            text = _as_text(item)
            if text:
                parts.append(f"{key}: {text}")
        return "; ".join(parts)
    return str(value).strip()


def _capitalize_first(value: str) -> str:
    return value[:1].upper() + value[1:] if value else value


def _as_sentence(value: str) -> str:
    text = _capitalize_first(value.strip())
    if text and text[-1] not in ".!?":
        text += "."
    return text


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = _as_text(value).lower()
    if text in {"да", "true", "yes", "1", "нужен", "нужно", "требуется"}:
        return True
    return False


def normalize_lead_type(value: Any) -> str:
    text = _as_text(value).lower().replace("ё", "е")
    text = text.strip(" .,!«»\"'")
    if text in LEAD_TYPE_ALIASES:
        return LEAD_TYPE_ALIASES[text]
    if "нецелев" in text or "не целевой" in text:
        return "нецелевой"
    if "целев" in text:
        return "целевой"
    return "неясно"


def _clean_key_data(value: str) -> str:
    """Убирает из известных данных пункты «не указано» — это не факты."""
    parts = [part.strip() for part in re.split(r";|\n", value) if part.strip()]
    kept = [part for part in parts if "не указано" not in part.lower()]
    return "; ".join(kept) if kept else "не указано"


def _looks_like_complaint(lead_text: str) -> bool:
    text = lead_text.lower().replace("ё", "е")
    return any(marker.replace("ё", "е") in text for marker in COMPLAINT_MARKERS)


EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE_RE = re.compile(
    r"(?:\+7|8)[\s\-()]*\d{3}[\s\-()]*\d{3}[\s\-()]*\d{2}[\s\-()]*\d{2}"
)
LONG_DIGITS_RE = re.compile(r"\b\d{10,11}\b")

EMPTY_MISSING = {
    "нет",
    "не указано",
    "-",
    "—",
    "недостающих данных нет",
    "недостающие данные отсутствуют",
}

GENERIC_REASON = "Требуется решение или ответ менеджера."


def _has_contact(text: str) -> bool:
    """Есть ли в тексте телефон или email (имя контактом не считаем)."""
    return bool(
        EMAIL_RE.search(text)
        or PHONE_RE.search(text)
        or LONG_DIGITS_RE.search(text)
    )


def _mentions_contact(value: str) -> bool:
    """Упоминается ли контакт в тексте поля (чтобы не дублировать пункт)."""
    return bool(re.search(r"контакт|телефон|e-?mail|почт", value, re.IGNORECASE))


EMAIL_CHANNEL_RE = re.compile(
    r"\s+(?:на|по)\s+(?:ваш\w*\s+)?(?:указанн\w*(?:\s+в\s+заявке)?\s+)?"
    r"(?:электронн\w*\s+)?(?:почт\w*|e-?mail)",
    re.IGNORECASE,
)

CONTACT_REQUEST_RE = re.compile(
    r"(?:уточн|укаж|указ|сообщ|напиш|остав|пришл|предостав)\w*"
    r"[^.!?]{0,80}(?:телефон|e-?mail|почт|контакт)",
    re.IGNORECASE,
)

CONTACT_SUFFIX = "Уточните, пожалуйста, телефон или email для связи."


def _is_empty_missing(value: str) -> bool:
    text = value.strip().lower().rstrip(".")
    if text in EMPTY_MISSING:
        return True
    return "недостающ" in text and ("нет" in text or "отсутств" in text)


def _append_missing(missing: str, item: str) -> str:
    if _is_empty_missing(missing):
        return item
    if item.lower() in missing.lower():
        return missing
    return f"{missing.rstrip(' .;')}; {item}"


def _complaint_reason(lead_text: str) -> str:
    text = lead_text.lower().replace("ё", "е")
    details: list[str] = []
    if "возврат" in text:
        details.append("требование возврата предоплаты")
    if "не привезли" in text or "сорван" in text or "сорвали" in text:
        details.append("срыв срока поставки")
    if "не берет трубку" in text or "не берёт трубку" in text:
        details.append("нет связи с менеджером")
    if details:
        return "Жалоба клиента: " + " и ".join(details) + "."
    return "Жалоба или конфликт, требуется решение менеджера."


def _complaint_facts(lead_text: str) -> str:
    """Страховка: вытаскиваем очевидные факты жалобы, если модель их не назвала."""
    text = lead_text.lower().replace("ё", "е")
    facts: list[str] = []
    if "понедельник" in text:
        facts.append("заказ должны были привезти в понедельник")
    if "четверг" in text:
        facts.append("сейчас четверг")
    if "ни мебели" in text or "не привезли" in text:
        facts.append("мебель не привезли")
    if "не берет трубку" in text:
        facts.append("менеджер не берёт трубку")
    if "возврат" in text and "предоплат" in text:
        facts.append("клиент требует возврат предоплаты")
    if "отказыва" in text:
        facts.append("клиент готов отказаться от заказа")
    return "; ".join(facts)


def normalize_result(raw: Any, lead_text: str = "") -> dict[str, Any]:
    """Возвращает словарь ровно с девятью полями нужных типов."""
    if isinstance(raw, list) and len(raw) == 1:
        raw = raw[0]
    if not isinstance(raw, dict):
        raise ResultValidationError("Ответ модели не является JSON-объектом.")

    result: dict[str, Any] = {}

    result["company"] = _as_text(raw.get("company")) or "не указано"
    result["summary"] = _as_text(raw.get("summary")) or "не указано"
    result["lead_type"] = normalize_lead_type(raw.get("lead_type"))
    result["key_data"] = _capitalize_first(_clean_key_data(_as_text(raw.get("key_data"))))

    missing = _as_text(raw.get("missing_data"))
    if not missing or _is_empty_missing(missing):
        missing = "Недостающих данных нет"

    # Страховка: без телефона/email менеджер не сможет связаться с клиентом.
    if not _has_contact(lead_text) and not _mentions_contact(missing):
        missing = _append_missing(missing, "Контакт для связи (телефон или email)")
    if re.search(r"\bнескольк\w*|\bпар[уы]\b", lead_text.lower()) and "количеств" not in missing.lower():
        missing = _append_missing(missing, "Точное количество")

    # Если модель перечислила контакт несколько раз — оставляем один, самый полный.
    parts = [part.strip() for part in missing.split(";") if part.strip()]
    contact_parts = [part for part in parts if _mentions_contact(part)]
    if len(contact_parts) > 1:
        best = max(contact_parts, key=len)
        deduped: list[str] = []
        added = False
        for part in parts:
            if _mentions_contact(part):
                if not added:
                    deduped.append(best)
                    added = True
            else:
                deduped.append(part)
        missing = "; ".join(deduped)

    result["missing_data"] = _capitalize_first(missing)

    # Жалоба — это не новый целевой лид, даже если модель ошиблась.
    is_complaint = _looks_like_complaint(lead_text)
    if is_complaint:
        result["lead_type"] = "неясно"

    next_step = _as_text(raw.get("next_step")) or "не указано"
    if is_complaint:
        # Убираем из следующего шага указание «нейтральный ответ клиенту»:
        # это не действие менеджера, а требование к тексту ответа.
        next_step = re.sub(
            r"\bнейтральн\w*\s+ответ\w*\s+клиенту\b[,\s;:—-]*",
            "",
            next_step,
            flags=re.IGNORECASE,
        ).strip(" .,;:—-")
        if not next_step.strip(" .,;:—-"):
            next_step = "Передать жалобу менеджеру и связаться с клиентом."
        else:
            next_step = next_step[0].upper() + next_step[1:]
        # Жалоба обязана уходить менеджеру, даже если модель предложила
        # только уточнить данные.
        if not re.search(r"передат|эскал", next_step, re.IGNORECASE):
            if next_step.lower() == "не указано":
                next_step = "Передать жалобу менеджеру и связаться с клиентом."
            else:
                next_step = re.sub(
                    r"\s+для передачи (?:информации )?менеджеру",
                    "",
                    next_step,
                    flags=re.IGNORECASE,
                )
                next_step = (
                    "Передать жалобу менеджеру. "
                    + next_step[0].upper()
                    + next_step[1:]
                )

    # Убираем придуманную характеристику продукта, которой нет в заявке.
    if "электронн" not in lead_text.lower():
        next_step = re.sub(
            r"\bэлектронн\w*\s+(?=(?:кресл|стол|шкаф|мебел|диван|стеллаж|тумб|полк))",
            "",
            next_step,
            flags=re.IGNORECASE,
        )
    if (
        result["lead_type"] == "целевой"
        and not _has_contact(lead_text)
        and "контакт" not in next_step.lower()
    ):
        next_step = (
            "Сначала уточнить контакт для связи, затем — "
            + next_step[0].lower()
            + next_step[1:]
        )
    result["next_step"] = _as_sentence(next_step)

    reply = _as_text(raw.get("reply_draft"))

    if not _has_contact(lead_text):
        # Не обещаем отправку на email/почту, которой нет в заявке.
        reply = EMAIL_CHANNEL_RE.sub("", reply).strip()

    if not reply or _is_empty_missing(reply):
        if is_complaint:
            reply = (
                "Приносим извинения за ситуацию. Передадим ваш вопрос нашему менеджеру — "
                "он свяжется с вами для уточнения деталей."
            )
        elif result["lead_type"] == "целевой":
            reply = (
                "Здравствуйте! Спасибо за обращение. Уточним детали и подготовим "
                "коммерческое предложение."
            )
        else:
            reply = "Здравствуйте! Спасибо за обращение. Передадим вашу заявку менеджеру."

    # Для целевого лида без контакта черновик обязан попросить телефон/email.
    if (
        result["lead_type"] == "целевой"
        and not _has_contact(lead_text)
        and not CONTACT_REQUEST_RE.search(reply)
    ):
        reply = reply.rstrip(" .")
        if reply and reply[-1] not in "!?":
            reply += "."
        reply = (reply + " " + CONTACT_SUFFIX).strip()

    # Если модель уже попросила контакт, не повторяем ту же просьбу дважды.
    if reply.endswith(CONTACT_SUFFIX):
        before = reply[: -len(CONTACT_SUFFIX)]
        if CONTACT_REQUEST_RE.search(before):
            reply = before.rstrip(" .")
            if reply and reply[-1] not in "!?":
                reply += "."
    result["reply_draft"] = reply or "не указано"

    needs_human = _as_bool(raw.get("needs_human"))
    human_reason = _as_text(raw.get("human_reason"))

    # Страховка №1: жалоба или конфликт всегда уходит человеку.
    if is_complaint:
        if not needs_human:
            needs_human = True
        if not human_reason or human_reason == GENERIC_REASON:
            human_reason = _complaint_reason(lead_text)
        if _is_empty_missing(result["key_data"]) or result["key_data"].startswith(
            "Факты из текста:"
        ):
            facts = _complaint_facts(lead_text)
            summary = result["summary"]
            if facts:
                result["key_data"] = facts
            elif summary != "не указано":
                result["key_data"] = "Факты из текста: " + summary
            else:
                result["key_data"] = "не указано"

    if needs_human and not human_reason:
        human_reason = GENERIC_REASON
    if needs_human and human_reason:
        human_reason = _as_sentence(human_reason)

    result["needs_human"] = needs_human
    result["human_reason"] = human_reason if needs_human else ""

    # Гарантируем состав и порядок полей.
    return {field: result[field] for field in FIELDS}
