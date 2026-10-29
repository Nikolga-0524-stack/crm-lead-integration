"""Веб-приложение: страница и API обработки заявок."""

from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import anonymization_log, bitrix24, gigachat
from app.gigachat import GigaChatError
from app.schema import ResultValidationError

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"

MAX_TEXT_LENGTH = 8000
MAX_NAME_LENGTH = 120
MAX_PHONE_LENGTH = 40
MAX_EMAIL_LENGTH = 120

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    logger.info("Сервер запущен. Модель: %s", gigachat.model_name())
    if not gigachat.is_configured():
        logger.warning("GIGACHAT_CREDENTIALS не заполнен: анализ заявок будет недоступен.")
    if not bitrix24.is_configured():
        logger.warning("B24_URL не заполнен: сохранение заявок в Битрикс24 будет недоступно.")
    if anonymization_log.is_enabled():
        if anonymization_log.ensure_file():
            logger.info("Журнал анонимизации: %s", anonymization_log.log_path())
        else:
            logger.warning(
                "Журнал анонимизации недоступен: проверьте путь ANONYMIZATION_LOG_FILE."
            )
    yield
    await gigachat.close_client()
    await bitrix24.close_client()


app = FastAPI(
    title="AI-помощник по заявкам",
    description="MVP первичной обработки входящих заявок на GigaChat API.",
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class AnalyzeRequest(BaseModel):
    name: str = Field(default="")
    phone: str = Field(default="")
    email: str = Field(default="")
    text: str = Field(default="")


def _clean_field(value: str) -> str:
    """Убирает переносы строк и лишние пробелы из контактных полей."""
    return " ".join((value or "").split())


def _compose_lead_text(payload: AnalyzeRequest) -> str:
    """Собирает текст заявки из полей формы.

    Контактные данные добавляются отдельными подписанными строками, чтобы модель
    и правила нормализации (проверка телефона/email) видели их как часть заявки.
    """
    parts: list[str] = []
    if payload.name:
        parts.append(f"Имя клиента: {payload.name}")
    if payload.phone:
        parts.append(f"Телефон: {payload.phone}")
    if payload.email:
        parts.append(f"Почта: {payload.email}")
    if payload.text:
        parts.append(f"Текст заявки:\n{payload.text}")
    return "\n".join(parts).strip()


def _error(
    status: int,
    message: str,
    hint: str = "",
    *,
    extra: dict[str, Any] | None = None,
) -> JSONResponse:
    content: dict[str, Any] = {"error": {"message": message, "hint": hint}}
    if extra:
        content.update(extra)
    return JSONResponse(status_code=status, content=content)


async def _sync_bitrix24(
    result: dict[str, Any],
    *,
    name: str,
    phone: str,
    email: str,
    lead_text: str,
) -> bitrix24.CrmStatus:
    """Сохраняет заявку в CRM, не ломая основной ответ при ошибке."""
    try:
        return await bitrix24.sync_lead(
            result=result,
            client={"name": name, "phone": phone, "email": email},
            lead_text=lead_text,
            model=gigachat.model_name(),
        )
    except Exception as exc:  # noqa: BLE001 - CRM не должна ломать анализ
        # Не пишем traceback: в исключениях httpx может оказаться URL с токеном.
        logger.warning("Битрикс24: непредвиденная ошибка (%s)", exc.__class__.__name__)
        return bitrix24.CrmStatus(
            status="error",
            message="Заявка проанализирована, но сохранить её в Битрикс24 не удалось.",
            warnings=["Битрикс24: непредвиденная ошибка."],
        )


@app.exception_handler(RequestValidationError)
async def validation_handler(_: Request, __: RequestValidationError) -> JSONResponse:
    return _error(400, "Некорректный запрос.", "Обновите страницу и попробуйте снова.")


@app.exception_handler(ResultValidationError)
async def result_validation_handler(_: Request, __: ResultValidationError) -> JSONResponse:
    return _error(
        502,
        "Не удалось разобрать ответ модели.",
        "Попробуйте обработать заявку ещё раз.",
    )


@app.exception_handler(Exception)
async def unexpected_handler(_: Request, exc: Exception) -> JSONResponse:
    logger.exception("Непредвиденная ошибка: %s", exc.__class__.__name__)
    return _error(
        500,
        "Внутренняя ошибка сервера.",
        "Посмотрите логи в терминале, где запущен сервер, и попробуйте снова.",
    )


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html")


@app.get("/api/health")
async def health() -> dict[str, Any]:
    path = gigachat.cert_path()
    return {
        "status": "ok",
        "configured": gigachat.is_configured(),
        "model": gigachat.model_name(),
        "certificate": str(path) if path else None,
        "certificate_found": bool(path and path.exists()),
        "bitrix24": {
            "configured": bitrix24.is_configured(),
        },
    }

@app.get("/health")
async def fake_health():
    return {"status": "ok"}

@app.post("/api/analyze")
async def analyze(payload: AnalyzeRequest) -> JSONResponse:
    name = _clean_field(payload.name)
    phone = _clean_field(payload.phone)
    email = _clean_field(payload.email)
    text = (payload.text or "").strip()

    for value, limit, label in (
        (name, MAX_NAME_LENGTH, "Имя"),
        (phone, MAX_PHONE_LENGTH, "Телефон"),
        (email, MAX_EMAIL_LENGTH, "Почта"),
    ):
        if len(value) > limit:
            return _error(
                400,
                f"Поле «{label}» слишком длинное: {len(value)} символов.",
                f"Сократите его до {limit} символов.",
            )

    if not text:
        return _error(400, "Впишите текст заявки.", "Поле не должно быть пустым.")
    if len(text) > MAX_TEXT_LENGTH:
        return _error(
            400,
            f"Заявка слишком длинная: {len(text)} символов.",
            f"Сократите текст до {MAX_TEXT_LENGTH} символов.",
        )

    lead_text = _compose_lead_text(
        AnalyzeRequest(name=name, phone=phone, email=email, text=text)
    )

    request_id = uuid.uuid4().hex
    trace = gigachat.AnonymizationTrace()
    try:
        result = await gigachat.analyze_lead(
            lead_text,
            known_values={"name": name, "phone": phone, "email": email},
            trace=trace,
        )
    except GigaChatError as exc:
        logger.warning("Ошибка обработки заявки: %s", exc.message)
        extra: dict[str, Any] = {"request_id": request_id}
        if trace.text:
            # Текст уже был подготовлен и, возможно, отправлен в модель.
            # Показываем его на странице, чтобы запрос оставался прозрачным.
            extra.update(
                {
                    "anonymized_text": trace.text,
                    "anonymization_stats": trace.stats,
                }
            )
            anonymization_log.write_entry(
                trace,
                request_id=request_id,
                status="error",
                error_type=exc.__class__.__name__,
                model=gigachat.model_name(),
                original_chars=len(lead_text),
            )
        return _error(exc.status_code, exc.message, exc.hint, extra=extra)

    if trace.text:
        anonymization_log.write_entry(
            trace,
            request_id=request_id,
            status="success",
            model=gigachat.model_name(),
            original_chars=len(lead_text),
        )

    crm_status = await _sync_bitrix24(
        result,
        name=name,
        phone=phone,
        email=email,
        lead_text=lead_text,
    )

    return JSONResponse(
        content={
            "result": result,
            "model": gigachat.model_name(),
            "client": {"name": name, "phone": phone, "email": email},
            "crm": crm_status.as_dict(),
            "anonymized_text": trace.text,
            "anonymization_stats": trace.stats,
            "request_id": request_id,
        }
    )
