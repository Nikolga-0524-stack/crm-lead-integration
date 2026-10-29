"""CSV-журнал событий анонимизации.

Журнал предназначен для аудита и не хранит исходный или замаскированный текст
заявки: только метаданные, счётчики найденных сущностей и SHA-256
замаскированного текста. Реальные персональные данные в файл не попадают.

Запись append-only, доступ к файлу защищён блокировкой в пределах процесса.
Ошибка записи журнала не должна прерывать обработку заявки.
"""

from __future__ import annotations

import csv
import hashlib
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.gigachat import AnonymizationTrace

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_LOG_FILE = BASE_DIR / "logs" / "anonymization.csv"

CSV_FIELDS = (
    "timestamp_utc",
    "request_id",
    "status",
    "error_type",
    "model",
    "original_chars",
    "masked_chars",
    "total_masked",
    "name",
    "phone",
    "email",
    "address",
    "passport",
    "snils",
    "inn",
    "card",
    "birth_date",
    "company",
    "unresolved",
    "masked_sha256",
)

STAT_KEYS = (
    "name",
    "phone",
    "email",
    "address",
    "passport",
    "snils",
    "inn",
    "card",
    "birth_date",
    "company",
)

_lock = threading.Lock()


def is_enabled() -> bool:
    """Журнал включён, если переменная не задана или не равна false."""
    raw = (os.getenv("ANONYMIZATION_LOG_ENABLED") or "true").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def log_path() -> Path:
    """Путь к CSV: из ANONYMIZATION_LOG_FILE или logs/anonymization.csv."""
    raw = (os.getenv("ANONYMIZATION_LOG_FILE") or "").strip()
    if not raw:
        return DEFAULT_LOG_FILE
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = BASE_DIR / path
    return path


def _write_header(handle: Any) -> None:
    writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, lineterminator="\n")
    writer.writeheader()


def ensure_file() -> bool:
    """Создаёт CSV с заголовком, если файла ещё нет. Не бросает исключений."""
    if not is_enabled():
        return False

    path = log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _lock:
            if not path.exists() or path.stat().st_size == 0:
                with path.open("a", newline="", encoding="utf-8") as handle:
                    _write_header(handle)
        return True
    except Exception as exc:  # noqa: BLE001 - журнал не должен ломать приложение
        logger.warning(
            "Не удалось подготовить журнал анонимизации (%s).",
            exc.__class__.__name__,
        )
        return False


def write_entry(
    trace: AnonymizationTrace,
    *,
    request_id: str,
    status: str,
    model: str,
    original_chars: int,
    error_type: str = "",
) -> bool:
    """Добавляет строку в CSV. Возвращает False, если запись не удалась."""
    if not is_enabled() or not trace.text:
        return False

    stats = trace.stats or {}
    masked_text = trace.text
    row = {
        "timestamp_utc": datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "request_id": request_id,
        "status": status,
        "error_type": error_type,
        "model": model,
        "original_chars": max(0, int(original_chars)),
        "masked_chars": len(masked_text),
        "total_masked": sum(int(value or 0) for value in stats.values()),
        **{key: int(stats.get(key, 0) or 0) for key in STAT_KEYS},
        "unresolved": max(0, int(trace.unresolved)),
        "masked_sha256": hashlib.sha256(masked_text.encode("utf-8")).hexdigest(),
    }

    path = log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _lock:
            new_file = not path.exists() or path.stat().st_size == 0
            with path.open("a", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=CSV_FIELDS,
                    lineterminator="\n",
                )
                if new_file:
                    writer.writeheader()
                writer.writerow(row)
        return True
    except Exception as exc:  # noqa: BLE001 - журнал не должен ломать приложение
        logger.warning(
            "Не удалось записать журнал анонимизации (%s).",
            exc.__class__.__name__,
        )
        return False
