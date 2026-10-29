"""Интеграция с Битрикс24.

Логика рассчитана на простой режим CRM (без лидов):
- контакт — карточка клиента;
- сделка — конкретная заявка;
- в контакте хранится снимок последней заявки (UF_CRM_AI_LAST_*);
- в сделке — полная AI-карточка (UF_CRM_AI_*);
- история доступна в связанных сделках и таймлайне контакта.

Секрет вебхука B24_URL нигде не логируется. Ошибки CRM не должны ломать
основной сценарий анализа заявки, поэтому sync_lead() возвращает статус,
а не выбрасывает исключение наружу.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# httpx на уровне INFO пишет полный URL, а в нём находится токен вебхука.
logging.getLogger("httpx").setLevel(logging.WARNING)

MIN_INTERVAL = 0.55  # ~2 запроса в секунду
MAX_ATTEMPTS = 3
DEFAULT_TIMEOUT = 10.0

CONTACT_ENTITY_TYPE_ID = 3
DEAL_ENTITY_TYPE_ID = 2


# --- ошибки -----------------------------------------------------------------


class Bitrix24Error(Exception):
    """Базовая ошибка интеграции с готовым безопасным текстом."""

    status_code = 502
    message = "Не удалось сохранить заявку в Битрикс24."
    hint = ""

    def __init__(self, message: str | None = None, hint: str | None = None):
        super().__init__(message or self.message)
        if message:
            self.message = message
        if hint is not None:
            self.hint = hint


class Bitrix24NotConfiguredError(Bitrix24Error):
    status_code = 503
    message = "Битрикс24 не настроен: в .env не заполнен B24_URL."
    hint = (
        "Создайте входящий вебхук в Битрикс24 с правами crm, task и im "
        "и вставьте его URL в B24_URL. Затем перезапустите сервер."
    )


class Bitrix24Timeout(Bitrix24Error):
    status_code = 504
    message = "Битрикс24 не ответил за отведённое время."
    hint = "Заявка проанализирована, но в CRM могла не сохраниться. Проверьте карточку вручную."


class Bitrix24NetworkError(Bitrix24Error):
    message = "Не удалось подключиться к Битрикс24."
    hint = "Проверьте интернет и доступность портала. Заявка при этом не потеряна."


class Bitrix24BadResponse(Bitrix24Error):
    message = "Битрикс24 вернул ответ в неожиданном формате."


class Bitrix24APIError(Bitrix24Error):
    """Ошибка конкретного REST-метода."""

    def __init__(
        self,
        method: str,
        code: str = "",
        description: str = "",
        status: int = 0,
    ):
        self.method = method
        self.code = code
        self.description = description
        self.status = status
        super().__init__(f"Битрикс24: ошибка метода {method}.")


# --- настройки --------------------------------------------------------------


def _env(name: str) -> str:
    return (os.getenv(name) or "").strip()


def _env_bool(name: str, default: bool) -> bool:
    value = _env(name).lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "y", "on", "да"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name))
    except ValueError:
        return default


def webhook_url() -> str:
    raw = _env("B24_URL")
    if not raw:
        return ""
    return raw.rstrip("/") + "/"


def is_configured() -> bool:
    return bool(webhook_url())


def portal_url() -> str:
    """Возвращает адрес портала без пути и токена вебхука."""
    raw = webhook_url()
    if not raw:
        return ""
    from urllib.parse import urlsplit

    parsed = urlsplit(raw)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return ""


def webhook_user_id() -> int | None:
    """ID владельца вебхука из URL вида /rest/1/token/."""
    from urllib.parse import urlsplit

    parts = [part for part in urlsplit(webhook_url()).path.split("/") if part]
    try:
        index = parts.index("rest")
        return int(parts[index + 1])
    except (ValueError, IndexError):
        return None


def responsible_id() -> int:
    return _env_int("B24_RESPONSIBLE_ID", 0) or webhook_user_id() or 1


def source_id() -> str:
    return _env("B24_SOURCE_ID") or "WEB"


def category_id() -> int:
    return _env_int("B24_CATEGORY_ID", 0)


def stage_id() -> str:
    return _env("B24_STAGE_ID")


def timeout() -> float:
    return max(3.0, _env_float("B24_TIMEOUT", DEFAULT_TIMEOUT))


def task_mode() -> str:
    value = _env("B24_TASK_MODE").lower() or "needs_human"
    return value if value in {"needs_human", "always", "never"} else "needs_human"


def notify_mode() -> str:
    value = _env("B24_NOTIFY_MODE").lower() or "all"
    return value if value in {"all", "needs_human", "never"} else "all"


def timeline_comment_enabled() -> bool:
    return _env_bool("B24_TIMELINE_COMMENT", True)


# --- HTTP-клиент ------------------------------------------------------------


_client: httpx.AsyncClient | None = None
_limiter_lock = asyncio.Lock()
_next_slot = 0.0


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout(), connect=min(5.0, timeout())),
            follow_redirects=True,
            headers={"Accept": "application/json"},
        )
    return _client


async def close_client() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


async def _wait_for_slot() -> None:
    """Мягкий лимитер: не чаще ~2 запросов в секунду."""
    global _next_slot
    async with _limiter_lock:
        now = time.monotonic()
        wait = max(0.0, _next_slot - now)
        _next_slot = max(now, _next_slot) + MIN_INTERVAL
    if wait > 0:
        await asyncio.sleep(wait)


def _json_error(data: Any) -> tuple[str, str]:
    if not isinstance(data, dict):
        return "", ""
    return str(data.get("error") or ""), str(data.get("error_description") or "")


async def call(method: str, params: dict[str, Any] | None = None) -> Any:
    """Вызывает REST-метод Битрикс24 и возвращает поле result.

    Повторяем только 429/503. После таймаута не повторяем автоматически,
    чтобы не создать дубликат сделки.
    """
    if not is_configured():
        raise Bitrix24NotConfiguredError()

    url = f"{webhook_url()}{method}"
    payload = params or {}

    for attempt in range(1, MAX_ATTEMPTS + 1):
        await _wait_for_slot()
        try:
            response = await _get_client().post(url, json=payload)
        except httpx.TimeoutException as exc:
            logger.warning("Битрикс24: %s — таймаут", method)
            raise Bitrix24Timeout() from exc
        except httpx.HTTPError as exc:
            logger.warning("Битрикс24: %s — сетевая ошибка (%s)", method, exc.__class__.__name__)
            raise Bitrix24NetworkError() from exc

        if response.status_code in (429, 503) and attempt < MAX_ATTEMPTS:
            delay = 0.8 * (2 ** (attempt - 1))
            logger.warning(
                "Битрикс24: %s — HTTP %s, повтор через %.1f сек.",
                method,
                response.status_code,
                delay,
            )
            await asyncio.sleep(delay)
            continue

        if response.status_code >= 400:
            try:
                data = response.json()
            except ValueError:
                data = None
            code, description = _json_error(data)
            logger.warning(
                "Битрикс24: %s — HTTP %s, код %s",
                method,
                response.status_code,
                code or "без кода",
            )
            raise Bitrix24APIError(
                method,
                code=code or f"HTTP_{response.status_code}",
                description=description,
                status=response.status_code,
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise Bitrix24BadResponse(f"Битрикс24: {method} вернул не JSON.") from exc

        code, description = _json_error(data)
        if code:
            logger.warning("Битрикс24: %s — ошибка %s", method, code)
            raise Bitrix24APIError(
                method,
                code=code,
                description=description,
                status=response.status_code,
            )

        return data.get("result") if isinstance(data, dict) else None

    raise Bitrix24APIError(method, code="RETRY_EXHAUSTED", status=503)


# --- пользовательские поля --------------------------------------------------


@dataclass(frozen=True)
class FieldSpec:
    code: str
    label: str
    user_type: str = "string"
    rows: int = 1
    sort: int = 100
    show_filter: bool = False
    show_in_list: bool = False
    searchable: bool = False

    def payload(self) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "FIELD_NAME": self.code,
            "LABEL": self.label,
            "USER_TYPE_ID": self.user_type,
            "MULTIPLE": "N",
            "MANDATORY": "N",
            "SHOW_FILTER": "Y" if self.show_filter else "N",
            "SHOW_IN_LIST": "Y" if self.show_in_list else "N",
            "EDIT_IN_LIST": "Y",
            "IS_SEARCHABLE": "Y" if self.searchable else "N",
            "SORT": self.sort,
            "EDIT_FORM_LABEL": {"ru": self.label, "en": self.label},
            "LIST_COLUMN_LABEL": {"ru": self.label, "en": self.label},
            "LIST_FILTER_LABEL": {"ru": self.label, "en": self.label},
        }
        if self.user_type == "string":
            fields["SETTINGS"] = {"ROWS": max(1, self.rows)}
        elif self.user_type == "boolean":
            fields["SETTINGS"] = {"DEFAULT_VALUE": 0, "DISPLAY": "CHECKBOX"}
        elif self.user_type == "datetime":
            fields["SETTINGS"] = {"DEFAULT_VALUE": {"TYPE": "NONE", "VALUE": ""}}
        return fields


def _deal_field_specs() -> list[FieldSpec]:
    return [
        FieldSpec("UF_CRM_AI_COMPANY", "AI: компания", sort=100, show_filter=True, show_in_list=True, searchable=True),
        FieldSpec("UF_CRM_AI_SUMMARY", "AI: суть запроса", rows=4, sort=110, show_in_list=True, searchable=True),
        FieldSpec("UF_CRM_AI_LEAD_TYPE", "AI: тип заявки", sort=120, show_filter=True, show_in_list=True),
        FieldSpec("UF_CRM_AI_KEY_DATA", "AI: известные данные", rows=4, sort=130),
        FieldSpec("UF_CRM_AI_MISSING_DATA", "AI: чего не хватает", rows=3, sort=140),
        FieldSpec("UF_CRM_AI_NEXT_STEP", "AI: следующий шаг", rows=3, sort=150),
        FieldSpec("UF_CRM_AI_REPLY_DRAFT", "AI: черновик ответа", rows=6, sort=160),
        FieldSpec("UF_CRM_AI_NEEDS_HUMAN", "AI: нужен человек", user_type="boolean", sort=170, show_filter=True, show_in_list=True),
        FieldSpec("UF_CRM_AI_HUMAN_REASON", "AI: причина передачи человеку", rows=3, sort=180),
        FieldSpec("UF_CRM_AI_SOURCE_TEXT", "AI: исходный текст заявки", rows=8, sort=190),
        FieldSpec("UF_CRM_AI_MODEL", "AI: модель", sort=200),
        FieldSpec("UF_CRM_AI_REQUEST_ID", "AI: ID запроса", sort=210, show_filter=True, searchable=True),
    ]


def _contact_field_specs() -> list[FieldSpec]:
    return [
        FieldSpec("UF_CRM_AI_LAST_COMPANY", "AI (последняя заявка): компания", sort=100, show_filter=True, show_in_list=True, searchable=True),
        FieldSpec("UF_CRM_AI_LAST_SUMMARY", "AI (последняя заявка): суть запроса", rows=4, sort=110, show_in_list=True, searchable=True),
        FieldSpec("UF_CRM_AI_LAST_LEAD_TYPE", "AI (последняя заявка): тип заявки", sort=120, show_filter=True, show_in_list=True),
        FieldSpec("UF_CRM_AI_LAST_KEY_DATA", "AI (последняя заявка): известные данные", rows=4, sort=130),
        FieldSpec("UF_CRM_AI_LAST_MISSING_DATA", "AI (последняя заявка): чего не хватает", rows=3, sort=140),
        FieldSpec("UF_CRM_AI_LAST_NEXT_STEP", "AI (последняя заявка): следующий шаг", rows=3, sort=150),
        FieldSpec("UF_CRM_AI_LAST_REPLY_DRAFT", "AI (последняя заявка): черновик ответа", rows=6, sort=160),
        FieldSpec("UF_CRM_AI_LAST_NEEDS_HUMAN", "AI (последняя заявка): нужен человек", user_type="boolean", sort=170, show_filter=True, show_in_list=True),
        FieldSpec("UF_CRM_AI_LAST_HUMAN_REASON", "AI (последняя заявка): причина передачи человеку", rows=3, sort=180),
        FieldSpec("UF_CRM_AI_LAST_SOURCE_TEXT", "AI (последняя заявка): исходный текст", rows=8, sort=190),
        FieldSpec("UF_CRM_AI_LAST_MODEL", "AI (последняя заявка): модель", sort=200),
        FieldSpec("UF_CRM_AI_LAST_REQUEST_ID", "AI (последняя заявка): ID запроса", sort=210, show_filter=True, searchable=True),
        FieldSpec("UF_CRM_AI_LAST_DEAL_ID", "AI (последняя заявка): ID сделки", sort=220, show_filter=True),
        FieldSpec("UF_CRM_AI_LAST_REQUEST_AT", "AI (последняя заявка): дата и время", user_type="datetime", sort=230, show_in_list=True),
    ]


def _field_methods(entity_type: str) -> tuple[str, str]:
    if entity_type == "contact":
        return "crm.contact.userfield.list", "crm.contact.userfield.add"
    return "crm.deal.userfield.list", "crm.deal.userfield.add"


def _field_specs(entity_type: str) -> list[FieldSpec]:
    return _contact_field_specs() if entity_type == "contact" else _deal_field_specs()


def _collect_field_names(value: Any) -> set[str]:
    names: set[str] = set()
    if isinstance(value, list):
        for item in value:
            names.update(_collect_field_names(item))
    elif isinstance(value, dict):
        field_name = value.get("FIELD_NAME")
        if field_name:
            names.add(str(field_name).upper())
        for item in value.values():
            if isinstance(item, (list, dict)):
                names.update(_collect_field_names(item))
    return names


async def list_user_fields(entity_type: str) -> Any:
    method, _ = _field_methods(entity_type)
    return await call(method)


async def setup_user_fields() -> dict[str, dict[str, list[str]]]:
    """Идемпотентно создаёт недостающие пользовательские поля.

    Возвращает отчёт: для контакта и сделки — списки created/existing.
    """
    report: dict[str, dict[str, list[str]]] = {}
    for entity_type in ("contact", "deal"):
        _, add_method = _field_methods(entity_type)
        existing = _collect_field_names(await list_user_fields(entity_type))
        created: list[str] = []
        skipped: list[str] = []
        for spec in _field_specs(entity_type):
            code = spec.code.upper()
            if code in existing:
                skipped.append(code)
                continue
            await call(add_method, {"fields": spec.payload()})
            created.append(code)
            existing.add(code)
        report[entity_type] = {"created": created, "existing": skipped}
    return report


_fields_cache: dict[str, bool] = {}
_fields_lock = asyncio.Lock()


async def fields_ready(entity_type: str) -> bool:
    """Проверяет наличие всех UF-полей (результат кэшируется на процесс)."""
    if entity_type in _fields_cache:
        return _fields_cache[entity_type]

    async with _fields_lock:
        if entity_type in _fields_cache:
            return _fields_cache[entity_type]
        try:
            existing = _collect_field_names(await list_user_fields(entity_type))
        except Bitrix24Error:
            # Если проверка недоступна, пробуем писать поля: возможно, они уже есть.
            return True
        required = {spec.code.upper() for spec in _field_specs(entity_type)}
        ready = required.issubset(existing)
        _fields_cache[entity_type] = ready
        return ready


# --- статус CRM -------------------------------------------------------------


@dataclass
class CrmStatus:
    status: str = "disabled"
    message: str = ""
    request_id: str = ""
    contact_id: int | None = None
    deal_id: int | None = None
    timeline_comment_id: int | None = None
    task_id: int | None = None
    notified: bool = False
    contact_created: bool = False
    contact_updated: bool = False
    deal_created: bool = False
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        base = portal_url()
        data: dict[str, Any] = {
            "status": self.status,
            "message": self.message,
            "request_id": self.request_id,
            "contact_id": self.contact_id,
            "deal_id": self.deal_id,
            "timeline_comment_id": self.timeline_comment_id,
            "task_id": self.task_id,
            "notified": self.notified,
            "warnings": list(self.warnings),
        }
        if base and self.contact_id:
            data["contact_url"] = f"{base}/crm/contact/details/{self.contact_id}/"
        if base and self.deal_id:
            data["deal_url"] = f"{base}/crm/deal/details/{self.deal_id}/"
        return data


# --- вспомогательные функции ------------------------------------------------


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _truncate(value: str, limit: int = 255) -> str:
    value = _text(value)
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def _multifield(phone: str, email: str) -> list[dict[str, str]]:
    """Телефон и email для crm.item.add (там fm — массив)."""
    fields: list[dict[str, str]] = []
    if phone:
        fields.append({"typeId": "PHONE", "valueType": "WORK", "value": phone})
    if email:
        fields.append({"typeId": "EMAIL", "valueType": "WORK", "value": email})
    return fields


def _item_id(result: Any) -> int | None:
    if isinstance(result, dict):
        item = result.get("item")
        if isinstance(item, dict) and item.get("id") is not None:
            try:
                return int(item["id"])
            except (TypeError, ValueError):
                return None
        if result.get("id") is not None:
            try:
                return int(result["id"])
            except (TypeError, ValueError):
                return None
    return None


def _task_id(result: Any) -> int | None:
    if isinstance(result, dict):
        task = result.get("task")
        if isinstance(task, dict) and task.get("id") is not None:
            try:
                return int(task["id"])
            except (TypeError, ValueError):
                return None
    return None


def _error_label(exc: Bitrix24Error) -> str:
    if isinstance(exc, Bitrix24APIError) and exc.code:
        return exc.code
    if isinstance(exc, Bitrix24Timeout):
        return "таймаут"
    if isinstance(exc, Bitrix24NetworkError):
        return "нет связи"
    return "ошибка"


# --- карточка и поля --------------------------------------------------------


def _deal_title(client: dict[str, str], result: dict[str, Any]) -> str:
    company = _text(result.get("company"))
    name = _text(client.get("name"))
    parts: list[str] = []
    if company and company.lower() not in {"не указано", "нет"}:
        parts.append(company)
    if name:
        parts.append(name)
    return _truncate("Заявка: " + " — ".join(parts) if parts else "Заявка с сайта", 255)


def _deal_ai_fields(
    result: dict[str, Any],
    lead_text: str,
    model: str,
    request_id: str,
) -> dict[str, Any]:
    return {
        "UF_CRM_AI_COMPANY": _text(result.get("company")),
        "UF_CRM_AI_SUMMARY": _text(result.get("summary")),
        "UF_CRM_AI_LEAD_TYPE": _text(result.get("lead_type")),
        "UF_CRM_AI_KEY_DATA": _text(result.get("key_data")),
        "UF_CRM_AI_MISSING_DATA": _text(result.get("missing_data")),
        "UF_CRM_AI_NEXT_STEP": _text(result.get("next_step")),
        "UF_CRM_AI_REPLY_DRAFT": _text(result.get("reply_draft")),
        "UF_CRM_AI_NEEDS_HUMAN": "Y" if result.get("needs_human") else "N",
        "UF_CRM_AI_HUMAN_REASON": _text(result.get("human_reason")),
        "UF_CRM_AI_SOURCE_TEXT": lead_text,
        "UF_CRM_AI_MODEL": model,
        "UF_CRM_AI_REQUEST_ID": request_id,
    }


def _contact_ai_fields(
    result: dict[str, Any],
    lead_text: str,
    model: str,
    request_id: str,
    deal_id: int | None,
) -> dict[str, Any]:
    return {
        "UF_CRM_AI_LAST_COMPANY": _text(result.get("company")),
        "UF_CRM_AI_LAST_SUMMARY": _text(result.get("summary")),
        "UF_CRM_AI_LAST_LEAD_TYPE": _text(result.get("lead_type")),
        "UF_CRM_AI_LAST_KEY_DATA": _text(result.get("key_data")),
        "UF_CRM_AI_LAST_MISSING_DATA": _text(result.get("missing_data")),
        "UF_CRM_AI_LAST_NEXT_STEP": _text(result.get("next_step")),
        "UF_CRM_AI_LAST_REPLY_DRAFT": _text(result.get("reply_draft")),
        "UF_CRM_AI_LAST_NEEDS_HUMAN": "Y" if result.get("needs_human") else "N",
        "UF_CRM_AI_LAST_HUMAN_REASON": _text(result.get("human_reason")),
        "UF_CRM_AI_LAST_SOURCE_TEXT": lead_text,
        "UF_CRM_AI_LAST_MODEL": model,
        "UF_CRM_AI_LAST_REQUEST_ID": request_id,
        "UF_CRM_AI_LAST_DEAL_ID": str(deal_id) if deal_id else "",
        "UF_CRM_AI_LAST_REQUEST_AT": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def _format_card(
    result: dict[str, Any],
    client: dict[str, str],
    lead_text: str,
    model: str,
    request_id: str,
    deal_url: str = "",
    contact_url: str = "",
) -> str:
    needs_human = "да" if result.get("needs_human") else "нет"
    lines = [
        "AI-карточка заявки",
        f"Клиент: {_text(client.get('name')) or 'не указано'}",
        f"Телефон: {_text(client.get('phone')) or 'не указано'}",
        f"Email: {_text(client.get('email')) or 'не указано'}",
        f"Компания: {_text(result.get('company')) or 'не указано'}",
        f"Тип заявки: {_text(result.get('lead_type')) or 'не указано'}",
        f"Суть: {_text(result.get('summary')) or 'не указано'}",
        f"Известные данные: {_text(result.get('key_data')) or 'не указано'}",
        f"Чего не хватает: {_text(result.get('missing_data')) or 'не указано'}",
        f"Следующий шаг: {_text(result.get('next_step')) or 'не указано'}",
        f"Черновик ответа: {_text(result.get('reply_draft')) or 'не указано'}",
        f"Нужен человек: {needs_human}",
    ]
    if result.get("needs_human") and result.get("human_reason"):
        lines.append(f"Причина: {_text(result.get('human_reason'))}")
    if deal_url:
        lines.append(f"Сделка: {deal_url}")
    if contact_url:
        lines.append(f"Контакт: {contact_url}")
    lines.extend(
        [
            f"Модель: {_text(model) or 'не указано'}",
            f"ID запроса: {_text(request_id) or 'не указано'}",
            "",
            "Исходный текст заявки:",
            lead_text.strip(),
        ]
    )
    return "\n".join(lines).strip()


def _format_timeline(
    result: dict[str, Any],
    client: dict[str, str],
    lead_text: str,
    model: str,
    request_id: str,
    deal_url: str = "",
) -> str:
    needs_human = "да" if result.get("needs_human") else "нет"
    lines = [
        "Новая заявка с сайта",
        f"Клиент: {_text(client.get('name')) or 'не указано'}",
        f"Компания: {_text(result.get('company')) or 'не указано'}",
        f"Тип: {_text(result.get('lead_type')) or 'не указано'}",
        f"Суть: {_text(result.get('summary')) or 'не указано'}",
        f"Следующий шаг: {_text(result.get('next_step')) or 'не указано'}",
        f"Нужен человек: {needs_human}",
    ]
    if result.get("needs_human") and result.get("human_reason"):
        lines.append(f"Причина: {_text(result.get('human_reason'))}")
    if deal_url:
        lines.append(f"Сделка: {deal_url}")
    lines.extend(
        [
            f"Модель: {_text(model) or 'не указано'}",
            f"ID запроса: {_text(request_id) or 'не указано'}",
            "",
            "Исходный текст:",
            lead_text.strip(),
        ]
    )
    return "\n".join(lines).strip()


# --- операции CRM -----------------------------------------------------------


async def _find_contact(phone: str, email: str) -> int | None:
    checks: list[tuple[str, str]] = []
    if phone:
        checks.append(("PHONE", phone))
    if email:
        checks.append(("EMAIL", email))

    for comm_type, value in checks:
        result = await call(
            "crm.duplicate.findbycomm",
            {
                "entity_type": "CONTACT",
                "type": comm_type,
                "values": [value],
            },
        )
        ids = result.get("CONTACT") if isinstance(result, dict) else None
        if ids:
            try:
                return int(ids[0])
            except (TypeError, ValueError, IndexError):
                return None
    return None


async def _create_contact(client: dict[str, str]) -> int:
    name = _text(client.get("name")) or _text(client.get("phone")) or _text(client.get("email"))
    fields: dict[str, Any] = {
        "name": _truncate(name or "Без имени", 120),
        "sourceId": source_id(),
        "assignedById": responsible_id(),
        "opened": "Y",
        "comments": "Создано автоматически из заявки на сайте.",
    }
    fm = _multifield(_text(client.get("phone")), _text(client.get("email")))
    if fm:
        fields["fm"] = fm

    result = await call(
        "crm.item.add",
        {
            "entityTypeId": CONTACT_ENTITY_TYPE_ID,
            "fields": fields,
            "useOriginalUfNames": "Y",
        },
    )
    contact_id = _item_id(result)
    if contact_id is None:
        raise Bitrix24BadResponse("Битрикс24: не вернул ID созданного контакта.")
    return contact_id


async def _create_deal(
    result: dict[str, Any],
    client: dict[str, str],
    lead_text: str,
    model: str,
    request_id: str,
    contact_id: int | None,
    use_ai_fields: bool,
) -> int:
    base_url = portal_url()
    contact_url = f"{base_url}/crm/contact/details/{contact_id}/" if base_url and contact_id else ""
    fields: dict[str, Any] = {
        "title": _deal_title(client, result),
        "sourceId": source_id(),
        "assignedById": responsible_id(),
        "opened": "Y",
        "categoryId": category_id(),
        "comments": _format_card(
            result,
            client,
            lead_text,
            model,
            request_id,
            contact_url=contact_url,
        ),
    }
    if contact_id:
        fields["contactId"] = contact_id
    if stage_id():
        fields["stageId"] = stage_id()
    fm = _multifield(_text(client.get("phone")), _text(client.get("email")))
    if fm:
        fields["fm"] = fm
    if use_ai_fields:
        fields.update(_deal_ai_fields(result, lead_text, model, request_id))

    created = await call(
        "crm.item.add",
        {
            "entityTypeId": DEAL_ENTITY_TYPE_ID,
            "fields": fields,
            "useOriginalUfNames": "Y",
        },
    )
    deal_id = _item_id(created)
    if deal_id is None:
        raise Bitrix24BadResponse("Битрикс24: не вернул ID созданной сделки.")
    return deal_id


async def _update_contact(
    contact_id: int,
    result: dict[str, Any],
    client: dict[str, str],
    lead_text: str,
    model: str,
    request_id: str,
    deal_id: int | None,
    use_ai_fields: bool,
) -> None:
    fields: dict[str, Any] = {}
    if use_ai_fields:
        fields.update(_contact_ai_fields(result, lead_text, model, request_id, deal_id))
    else:
        fields["comments"] = _format_card(
            result,
            client,
            lead_text,
            model,
            request_id,
            deal_url=f"{portal_url()}/crm/deal/details/{deal_id}/" if portal_url() and deal_id else "",
        )

    await call(
        "crm.item.update",
        {
            "entityTypeId": CONTACT_ENTITY_TYPE_ID,
            "id": contact_id,
            "fields": fields,
            "useOriginalUfNames": "Y",
        },
    )


async def _add_timeline_comment(
    contact_id: int,
    result: dict[str, Any],
    client: dict[str, str],
    lead_text: str,
    model: str,
    request_id: str,
    deal_id: int | None,
) -> int | None:
    base_url = portal_url()
    deal_url = f"{base_url}/crm/deal/details/{deal_id}/" if base_url and deal_id else ""
    comment = _format_timeline(result, client, lead_text, model, request_id, deal_url=deal_url)
    created = await call(
        "crm.timeline.comment.add",
        {
            "fields": {
                "ENTITY_ID": contact_id,
                "ENTITY_TYPE": "contact",
                "COMMENT": comment,
            }
        },
    )
    try:
        return int(created)
    except (TypeError, ValueError):
        return None


async def _create_task(
    result: dict[str, Any],
    client: dict[str, str],
    lead_text: str,
    model: str,
    request_id: str,
    contact_id: int | None,
    deal_id: int | None,
) -> int | None:
    base_url = portal_url()
    deal_url = f"{base_url}/crm/deal/details/{deal_id}/" if base_url and deal_id else ""
    bindings: list[str] = []
    if deal_id:
        bindings.append(f"D_{deal_id}")
    if contact_id:
        bindings.append(f"C_{contact_id}")

    fields: dict[str, Any] = {
        "TITLE": _truncate(f"Обработать заявку: {_text(client.get('name')) or 'без имени'}", 255),
        "DESCRIPTION": _format_card(
            result,
            client,
            lead_text,
            model,
            request_id,
            deal_url=deal_url,
        ),
        "RESPONSIBLE_ID": responsible_id(),
        "CREATED_BY": responsible_id(),
        "PRIORITY": "2" if result.get("needs_human") else "1",
    }
    if bindings:
        fields["UF_CRM_TASK"] = bindings

    created = await call("tasks.task.add", {"fields": fields})
    return _task_id(created)


async def _notify(
    result: dict[str, Any],
    client: dict[str, str],
    deal_id: int | None,
) -> bool:
    base_url = portal_url()
    deal_url = f"{base_url}/crm/deal/details/{deal_id}/" if base_url and deal_id else ""
    lines = [
        "Новая заявка с сайта",
        f"Клиент: {_text(client.get('name')) or 'не указано'}",
        f"Тип: {_text(result.get('lead_type')) or 'не указано'}",
        f"Суть: {_text(result.get('summary')) or 'не указано'}",
    ]
    if result.get("needs_human") and result.get("human_reason"):
        lines.append(f"Нужен человек: {_text(result.get('human_reason'))}")
    if deal_url:
        lines.append(deal_url)

    created = await call(
        "im.notify",
        {
            "USER_ID": responsible_id(),
            "TYPE": "SYSTEM",
            "MESSAGE": "\n".join(lines),
        },
    )
    return bool(created)


async def _crm_mode() -> int:
    global _crm_mode_cache
    if _crm_mode_cache is not None:
        return _crm_mode_cache
    async with _crm_mode_lock:
        if _crm_mode_cache is not None:
            return _crm_mode_cache
        result = await call("crm.settings.mode.get")
        try:
            _crm_mode_cache = int(result)
        except (TypeError, ValueError) as exc:
            raise Bitrix24BadResponse("Битрикс24: не удалось определить режим CRM.") from exc
        return _crm_mode_cache


_crm_mode_cache: int | None = None
_crm_mode_lock = asyncio.Lock()


async def sync_lead(
    *,
    result: dict[str, Any],
    client: dict[str, str],
    lead_text: str,
    model: str = "",
    request_id: str | None = None,
) -> CrmStatus:
    """Создаёт/обновляет контакт, создаёт сделку и связанные сущности."""
    if not is_configured():
        return CrmStatus(
            status="disabled",
            message="Интеграция с Битрикс24 выключена: не заполнен B24_URL.",
        )

    request_id = request_id or uuid.uuid4().hex
    status = CrmStatus(status="error", request_id=request_id)

    try:
        mode = await _crm_mode()
    except Bitrix24Error as exc:
        status.message = "Не удалось проверить режим CRM."
        status.warnings.append(f"Битрикс24: {_error_label(exc)}")
        return status

    if mode != 2:
        status.status = "unsupported_mode"
        status.message = (
            "Битрикс24 работает в классическом режиме с лидами. "
            "Интеграция рассчитана на простой режим CRM без лидов."
        )
        return status

    status.message = "Заявка сохраняется в Битрикс24."

    deal_fields_ready = await fields_ready("deal")
    contact_fields_ready = await fields_ready("contact")
    if not deal_fields_ready:
        status.warnings.append(
            "Поля сделки не созданы. Запустите python setup_bitrix24.py и перезапустите сервер."
        )
    if not contact_fields_ready:
        status.warnings.append(
            "Поля контакта не созданы. Данные последней заявки записаны в комментарий."
        )

    contact_id: int | None = None
    contact_created = False
    try:
        contact_id = await _find_contact(_text(client.get("phone")), _text(client.get("email")))
        if contact_id is None:
            contact_id = await _create_contact(client)
            contact_created = True
    except Bitrix24Error as exc:
        status.warnings.append(f"Не удалось создать контакт: {_error_label(exc)}.")

    deal_id: int | None = None
    try:
        deal_id = await _create_deal(
            result,
            client,
            lead_text,
            model,
            request_id,
            contact_id,
            use_ai_fields=deal_fields_ready,
        )
    except Bitrix24Error as exc:
        status.warnings.append(f"Не удалось создать сделку: {_error_label(exc)}.")

    contact_updated = False
    if contact_id:
        try:
            await _update_contact(
                contact_id,
                result,
                client,
                lead_text,
                model,
                request_id,
                deal_id,
                use_ai_fields=contact_fields_ready,
            )
            contact_updated = True
        except Bitrix24Error as exc:
            status.warnings.append(f"Не удалось обновить карточку клиента: {_error_label(exc)}.")

    timeline_comment_id: int | None = None
    if contact_id and timeline_comment_enabled():
        try:
            timeline_comment_id = await _add_timeline_comment(
                contact_id,
                result,
                client,
                lead_text,
                model,
                request_id,
                deal_id,
            )
        except Bitrix24Error as exc:
            status.warnings.append(f"Не удалось добавить комментарий в таймлайн: {_error_label(exc)}.")

    task_id: int | None = None
    should_task = task_mode() == "always" or (
        task_mode() == "needs_human" and bool(result.get("needs_human"))
    )
    if should_task and (deal_id or contact_id):
        try:
            task_id = await _create_task(
                result,
                client,
                lead_text,
                model,
                request_id,
                contact_id,
                deal_id,
            )
        except Bitrix24Error as exc:
            status.warnings.append(f"Не удалось создать задачу: {_error_label(exc)}.")

    notified = False
    should_notify = notify_mode() == "all" or (
        notify_mode() == "needs_human" and bool(result.get("needs_human"))
    )
    if should_notify:
        try:
            notified = await _notify(result, client, deal_id)
        except Bitrix24Error as exc:
            status.warnings.append(f"Не удалось отправить уведомление: {_error_label(exc)}.")

    status.contact_id = contact_id
    status.deal_id = deal_id
    status.timeline_comment_id = timeline_comment_id
    status.task_id = task_id
    status.notified = notified
    status.contact_created = contact_created
    status.contact_updated = contact_updated
    status.deal_created = deal_id is not None

    if not contact_id and not deal_id:
        status.status = "error"
        status.message = "Не удалось сохранить заявку в Битрикс24."
    elif contact_id and contact_updated and deal_id and not status.warnings:
        status.status = "created"
        status.message = "Заявка сохранена в Битрикс24: создана сделка и обновлена карточка клиента."
    else:
        status.status = "partial"
        status.message = "Заявка сохранена в Битрикс24 частично. Проверьте предупреждения."

    return status
