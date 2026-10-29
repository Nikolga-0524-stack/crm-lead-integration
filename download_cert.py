"""Скачивание корневого сертификата Минцифры для работы с GigaChat API.

Запуск: python download_cert.py

Сертификат публичный, секретов в нём нет. Он нужен, потому что GigaChat
использует цепочку сертификатов Национального удостоверяющего центра,
которой по умолчанию нет в Python и в системе.
"""

from __future__ import annotations

import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path

URL = "https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt"
DEST = Path(__file__).resolve().parent / "certs" / "russian_trusted_root_ca_pem.crt"


def main() -> int:
    DEST.parent.mkdir(parents=True, exist_ok=True)

    print(f"Скачиваю сертификат:\n  {URL}\n")
    context = ssl.create_default_context()
    # Проверка сертификата самого сайта госуслуг здесь не нужна: мы скачиваем
    # публичный корневой сертификат, а не передаём никаких данных.
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE

    try:
        with urllib.request.urlopen(URL, context=context, timeout=30) as response:
            data = response.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print("Не удалось скачать автоматически:", exc)
        print()
        print("Скачайте сертификат вручную:")
        print("  1. Откройте https://www.gosuslugi.ru/crt")
        print("  2. Скачайте корневой сертификат для Windows/Linux/macOS (PEM).")
        print(f"  3. Положите файл сюда: {DEST}")
        print("  4. Запустите сервер заново.")
        return 1

    if b"BEGIN CERTIFICATE" not in data:
        print("Файл скачался, но это не PEM-сертификат. Скачайте вручную с gosuslugi.ru/crt.")
        return 1

    DEST.write_bytes(data)
    print(f"Готово. Сертификат сохранён:\n  {DEST}")
    print()
    print("Приложение подхватит его автоматически при следующем запуске.")
    print("Если сервер уже запущен — перезапустите его (Ctrl + C, затем python run.py).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
