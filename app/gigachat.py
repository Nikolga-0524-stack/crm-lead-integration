"""Клиент GigaChat API.

Используется актуальная схема (проверено по документации developers.sber.ru):
1. POST https://ngw.devices.sberbank.ru:9443/api/v2/oauth
   Заголовки: RqUID (uuid4), Authorization: Basic <ключ авторизации>.
2. POST https://api.giga.chat/v1/chat/completions
   Заголовок: Authorization: Bearer <access token>.
3. Ответ модели запрашивается по JSON-схеме (response_format + strict).

Ключ читается только из переменных окружения (.env) и никогда не логируется.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import ssl
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import certifi
import httpx
from dotenv import load_dotenv

from app.anonymizer import Anonymizer
from app.prompt import RESULT_SCHEMA, SYSTEM_PROMPT, build_user_message
from app.schema import ResultValidationError, normalize_result

load_dotenv()

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CERT_PATH = BASE_DIR / "certs" / "russian_trusted_root_ca_pem.crt"

DEFAULT_OAUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
DEFAULT_BASE_URL = "https://api.giga.chat/v1"
DEFAULT_MODEL = "GigaChat-2-Pro"
DEFAULT_SCOPE = "GIGACHAT_API_PERS"

MAX_TOKENS = 1500
TEMPERATURE = 0.2


@dataclass(slots=True)
class AnonymizationTrace:
    """Текст, который реально уходит в модель, и статистика маскирования.

    Заполняется только если вызывающий код передал объект в ``analyze_lead``.
    Содержит исключительно замаскированный текст: реальные значения остаются
    в памяти анонимайзера на время запроса.
    """

    text: str = ""
    stats: dict[str, int] = field(default_factory=dict)
    unresolved: int = 0


class GigaChatError(Exception):
    """Базовая ошибка с готовым текстом для пользователя."""

    status_code = 502
    message = "Не удалось получить ответ от GigaChat."
    hint = ""

    def __init__(self, message: str | None = None, hint: str | None = None):
        super().__init__(message or self.message)
        if message:
            self.message = message
        if hint is not None:
            self.hint = hint


class NotConfiguredError(GigaChatError):
    status_code = 503
    message = "GigaChat не настроен: в файле .env не заполнен GIGACHAT_CREDENTIALS."
    hint = (
        "Откройте файл .env в папке проекта и вставьте ключ авторизации "
        "(Authorization key) из личного кабинета GigaChat. Затем перезапустите сервер."
    )


class AuthError(GigaChatError):
    status_code = 502
    message = "GigaChat отклонил ключ авторизации."
    hint = (
        "Проверьте GIGACHAT_CREDENTIALS в .env: ключ должен быть скопирован целиком, "
        "без пробелов и слова Basic. При необходимости создайте новый ключ в кабинете."
    )


class CertificateError(GigaChatError):
    status_code = 502
    message = "Не удалось проверить SSL-сертификат GigaChat."
    hint = (
        "Скачайте корневой сертификат Минцифры командой «python download_cert.py» "
        "или вручную с gosuslugi.ru/crt и положите его в папку certs. "
        "Путь также можно указать в GIGACHAT_CA_BUNDLE_FILE. Если используется "
        "антивирус или прокси с проверкой HTTPS, добавьте его корневой сертификат "
        "в доверенные системы или в GIGACHAT_CA_BUNDLE_FILE."
    )


class GigaChatTimeout(GigaChatError):
    status_code = 504
    message = "GigaChat не ответил за отведённое время."
    hint = "Проверьте интернет-соединение и попробуйте отправить заявку ещё раз."


class UpstreamError(GigaChatError):
    status_code = 502
    message = "GigaChat вернул ошибку."
    hint = "Попробуйте ещё раз через минуту. Если ошибка повторяется, проверьте лимиты в личном кабинете."


class BadResponseError(GigaChatError):
    status_code = 502
    message = "GigaChat вернул ответ в неожиданном формате."
    hint = "Попробуйте обработать заявку ещё раз. Если ошибка повторяется, смените модель в .env."


# --- настройки из окружения -------------------------------------------------


def _env(name: str) -> str:
    return (os.getenv(name) or "").strip()


def credentials() -> str:
    return _env("GIGACHAT_CREDENTIALS")


def scope() -> str:
    return _env("GIGACHAT_SCOPE") or DEFAULT_SCOPE


def model_name() -> str:
    return _env("GIGACHAT_MODEL") or DEFAULT_MODEL


def base_url() -> str:
    return (_env("GIGACHAT_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")


def oauth_url() -> str:
    return _env("GIGACHAT_OAUTH_URL") or DEFAULT_OAUTH_URL


def is_configured() -> bool:
    return bool(credentials())


def cert_path() -> Path | None:
    """Путь к сертификату Минцифры: из .env или стандартная папка certs."""
    raw = _env("GIGACHAT_CA_BUNDLE_FILE")
    if raw:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = BASE_DIR / path
        return path
    if DEFAULT_CERT_PATH.exists():
        return DEFAULT_CERT_PATH
    return None


# --- HTTP-клиент и токен ----------------------------------------------------


_client: httpx.AsyncClient | None = None
_token: str | None = None
_token_expires_at: float = 0.0
_token_lock = asyncio.Lock()


def _ssl_context() -> ssl.SSLContext:
    # Системное хранилище нужно для корпоративных прокси и антивирусов,
    # которые проверяют HTTPS и подменяют цепочку сертификатов.
    # certifi добавляет привычный набор публичных корневых сертификатов.
    try:
        context = ssl.create_default_context()
    except ssl.SSLError:  # pragma: no cover - зависит от системы
        try:
            context = ssl.create_default_context(cafile=certifi.where())
        except ssl.SSLError as exc:  # pragma: no cover - зависит от системы
            raise CertificateError(
                hint=f"Не удалось прочитать хранилище сертификатов: {exc}"
            ) from exc

    try:
        context.load_verify_locations(cafile=certifi.where())
    except (ssl.SSLError, OSError) as exc:  # pragma: no cover - зависит от системы
        logger.warning("Не удалось добавить сертификаты certifi: %s", exc.__class__.__name__)

    path = cert_path()
    if path is not None:
        if not path.exists():
            raise CertificateError(
                hint=(
                    f"Файл сертификата не найден: {path}. "
                    "Скачайте его командой «python download_cert.py» или очистите "
                    "GIGACHAT_CA_BUNDLE_FILE в .env."
                )
            )
        try:
            context.load_verify_locations(cafile=str(path))
        except (ssl.SSLError, OSError) as exc:
            raise CertificateError(
                hint=f"Файл сертификата повреждён или имеет неверный формат: {path} ({exc})."
            ) from exc
    return context


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            verify=_ssl_context(),
            timeout=httpx.Timeout(60.0, connect=15.0),
            follow_redirects=True,
        )
    return _client


async def close_client() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


def _is_cert_error(exc: Exception) -> bool:
    text = f"{exc!r} {exc}".lower()
    return "certificate" in text or "ssl" in text


async def _request_token(force: bool = False) -> str:
    """Возвращает access token, кэшируя его до истечения срока."""
    global _token, _token_expires_at

    if not force and _token and time.time() < _token_expires_at - 60:
        return _token

    async with _token_lock:
        if not force and _token and time.time() < _token_expires_at - 60:
            return _token

        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "RqUID": str(uuid.uuid4()),
            "Authorization": f"Basic {credentials()}",
        }
        try:
            response = await _get_client().post(
                oauth_url(), headers=headers, data={"scope": scope()}
            )
        except httpx.TimeoutException as exc:
            raise GigaChatTimeout(hint="Сервер авторизации GigaChat не ответил вовремя.") from exc
        except httpx.HTTPError as exc:
            if _is_cert_error(exc):
                raise CertificateError() from exc
            raise UpstreamError(
                hint=f"Не удалось подключиться к серверу авторизации GigaChat: {exc.__class__.__name__}."
            ) from exc

        if response.status_code in (401, 403):
            logger.warning("GigaChat OAuth: ключ отклонён (HTTP %s)", response.status_code)
            raise AuthError()
        if response.status_code != 200:
            logger.warning("GigaChat OAuth: HTTP %s", response.status_code)
            raise UpstreamError(
                hint=f"Сервер авторизации GigaChat ответил кодом {response.status_code}."
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise UpstreamError(hint="Сервер авторизации вернул не JSON.") from exc

        token = payload.get("access_token")
        if not token:
            raise UpstreamError(hint="В ответе сервера авторизации нет access_token.")

        expires_at = payload.get("expires_at")
        try:
            expires_at = float(expires_at)
        except (TypeError, ValueError):
            expires_at = time.time() + 1800
        if expires_at > 10_000_000_000:  # пришло в миллисекундах
            expires_at /= 1000

        _token = token
        _token_expires_at = expires_at
        logger.info("GigaChat: получен access token, действует ~%d мин.", max(1, int((expires_at - time.time()) / 60)))
        return token


def _strip_code_fences(text: str) -> str:
    text = text.strip()
    match = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, flags=re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return text


def _extract_json(payload: dict[str, Any]) -> Any:
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise BadResponseError(hint="В ответе GigaChat нет текста сообщения.") from exc

    if isinstance(content, list):
        content = " ".join(
            part.get("text", "") if isinstance(part, dict) else str(part) for part in content
        )
    if not isinstance(content, str) or not content.strip():
        raise BadResponseError(hint="GigaChat вернул пустой ответ.")

    cleaned = _strip_code_fences(content)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        # Иногда модель добавляет текст до или после JSON — вырезаем объект.
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                pass
        raise BadResponseError(hint="Не удалось разобрать JSON в ответе модели.")


def _structured_format_rejected(response: httpx.Response) -> bool:
    """Похоже ли, что модель не поддерживает response_format / JSON-схему."""
    if response.status_code not in (400, 422):
        return False
    text = response.text.lower()
    return any(
        marker in text
        for marker in ("response_format", "json_schema", "schema", "structured")
    )


async def _chat(lead_text: str, retry_note: bool = False) -> Any:
    token = await _request_token()

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_user_message(lead_text)},
    ]
    if retry_note:
        messages.append(
            {
                "role": "user",
                "content": (
                    "Предыдущий ответ не удалось прочитать как JSON. "
                    "Верни только корректный JSON-объект строго по схеме, без текста вокруг."
                ),
            }
        )

    body = {
        "model": model_name(),
        "messages": messages,
        "temperature": TEMPERATURE,
        "max_tokens": MAX_TOKENS,
    }
    structured_body = {
        **body,
        "response_format": {
            "type": "json_schema",
            "schema": RESULT_SCHEMA,
            "strict": True,
        },
    }

    async def send(access_token: str, payload: dict[str, Any]) -> httpx.Response:
        return await _get_client().post(
            f"{base_url()}/chat/completions",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            json=payload,
        )

    payload = structured_body
    try:
        response = await send(token, payload)
        if response.status_code == 401:
            logger.info("GigaChat: токен истёк, обновляем.")
            token = await _request_token(force=True)
            response = await send(token, payload)
        if _structured_format_rejected(response):
            # Не все модели/версии API принимают response_format.
            # Тогда полагаемся на промпт: он всё равно требует только JSON.
            logger.warning("GigaChat: модель не приняла JSON-схему, повторяем без response_format.")
            payload = body
            response = await send(token, payload)
        if response.status_code in (429, 500, 502, 503, 504):
            logger.warning("GigaChat chat: HTTP %s, повтор через 2 сек.", response.status_code)
            await asyncio.sleep(2)
            response = await send(token, payload)
    except httpx.TimeoutException as exc:
        raise GigaChatTimeout() from exc
    except httpx.HTTPError as exc:
        if _is_cert_error(exc):
            raise CertificateError() from exc
        raise UpstreamError(
            hint=f"Сетевая ошибка при обращении к GigaChat: {exc.__class__.__name__}."
        ) from exc

    if response.status_code == 401:
        raise AuthError()
    if response.status_code == 429:
        raise UpstreamError(
            message="Превышен лимит запросов к GigaChat.",
            hint="Подождите минуту и попробуйте снова. Лимиты видны в личном кабинете GigaChat.",
        )
    if response.status_code != 200:
        logger.warning("GigaChat chat: HTTP %s", response.status_code)
        raise UpstreamError(
            hint=f"Модель GigaChat ответила кодом {response.status_code}. Попробуйте позже."
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise BadResponseError(hint="GigaChat вернул ответ не в формате JSON.") from exc

    return _extract_json(payload)


async def analyze_lead(
    lead_text: str,
    known_values: dict[str, str] | None = None,
    *,
    trace: AnonymizationTrace | None = None,
) -> dict[str, Any]:
    """Полный цикл: анонимизация → запрос к модели → проверка → восстановление.

    Если передан ``trace``, в него записывается точный замаскированный текст,
    который был отправлен в GigaChat. Это нужно только для отображения
    прозрачности на странице; реальные значения в trace не попадают.
    """
    if not is_configured():
        raise NotConfiguredError()

    # Mapping живёт только внутри этого вызова и нигде не сохраняется.
    anonymizer = Anonymizer(known_values)
    masked_text = anonymizer.mask(lead_text)
    stats = anonymizer.stats()
    if trace is not None:
        trace.text = masked_text
        trace.stats = stats
    if any(stats.values()):
        logger.info(
            "Анонимизация перед запросом: %s",
            ", ".join(
                f"{label}={value}" for label, value in stats.items() if value
            ),
        )

    started = time.perf_counter()
    try:
        raw = await _chat(masked_text)
        try:
            # Бизнес-правила проверяют оригинальный текст: модель видит только токены,
            # но контакт в заявке всё равно должен считаться указанным.
            result = normalize_result(raw, lead_text)
        except ResultValidationError:
            logger.info("GigaChat: ответ не прошёл валидацию, повторяем запрос.")
            raw = await _chat(masked_text, retry_note=True)
            result = normalize_result(raw, lead_text)
    except ResultValidationError as exc:
        raise BadResponseError() from exc

    result = anonymizer.restore_result(result)
    if trace is not None:
        trace.unresolved = anonymizer.unresolved_count
    if anonymizer.unresolved_count:
        logger.warning(
            "Не удалось восстановить токенов: %d. Показаны нейтральные формулировки.",
            anonymizer.unresolved_count,
        )

    elapsed = time.perf_counter() - started
    logger.info(
        "Заявка обработана за %.1f сек., модель %s, тип лида: %s",
        elapsed,
        model_name(),
        result["lead_type"],
    )
    return result
