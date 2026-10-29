"""Запуск приложения одной командой: python run.py."""

from __future__ import annotations

import logging
import os

import uvicorn
from dotenv import load_dotenv

load_dotenv()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s %(message)s",
        datefmt="%H:%M:%S",
    )
    host = os.getenv("APP_HOST", "127.0.0.1")
    port = int(os.getenv("APP_PORT", "8000"))

    print()
    print("=" * 58)
    print("  AI-помощник по входящим заявкам")
    print(f"  Откройте в браузере: http://{host}:{port}")
    print("  Остановить сервер: Ctrl + C")
    print("=" * 58)
    print()

    uvicorn.run("app.main:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
