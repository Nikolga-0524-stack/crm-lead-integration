"""Проверка и создание пользовательских полей Битрикс24.

Запуск:
    python setup_bitrix24.py

Скрипт идемпотентный: существующие поля не пересоздаются. Он создаёт
наборы UF_CRM_AI_* для сделок и UF_CRM_AI_LAST_* для контактов.
"""

from __future__ import annotations

import asyncio
import sys

from app import bitrix24


def _print_report(report: dict[str, dict[str, list[str]]]) -> None:
    titles = {"contact": "Контакт (карточка клиента)", "deal": "Сделка (заявка)"}
    for entity_type in ("contact", "deal"):
        data = report.get(entity_type, {})
        created = data.get("created", [])
        existing = data.get("existing", [])
        print(f"\n{titles[entity_type]}:")
        if created:
            print(f"  создано: {len(created)}")
            for code in created:
                print(f"    + {code}")
        else:
            print("  создано: 0")
        print(f"  уже были: {len(existing)}")


async def _run() -> int:
    if not bitrix24.is_configured():
        print("B24_URL не заполнен в .env.")
        print("Создайте входящий вебхук с правами crm, task, im и вставьте его URL.")
        return 2

    try:
        return await _run_setup()
    finally:
        await bitrix24.close_client()


async def _run_setup() -> int:
    print("Проверяю подключение к Битрикс24...")
    try:
        mode = await bitrix24.call("crm.settings.mode.get")
    except bitrix24.Bitrix24Error as exc:
        print(f"Ошибка подключения: {exc.message}")
        if isinstance(exc, bitrix24.Bitrix24APIError) and exc.code:
            print(f"Код Битрикс24: {exc.code}")
        return 1

    try:
        mode_value = int(mode)
    except (TypeError, ValueError):
        print(f"Не удалось определить режим CRM. Ответ Битрикс24: {mode!r}")
        return 1

    if mode_value == 2:
        print("Режим CRM: простой (без лидов). Контакт + сделка — подходит.")
    else:
        print("Режим CRM: классический (с лидами).")
        print("Интеграция рассчитана на простой режим. Поля всё равно будут созданы,")
        print("но для работы приложения переключите CRM в простой режим.")

    print("Создаю недостающие пользовательские поля...")
    try:
        report = await bitrix24.setup_user_fields()
    except bitrix24.Bitrix24Error as exc:
        print(f"Ошибка создания полей: {exc.message}")
        if isinstance(exc, bitrix24.Bitrix24APIError) and exc.code:
            print(f"Код Битрикс24: {exc.code}")
        return 1

    _print_report(report)
    print("\nГотово. Перезапустите сервер приложения, чтобы он увидел новые поля.")
    print("Поля появятся в карточках в блоке «Дополнительно» (при необходимости")
    print("их можно вынести в основной блок через настройку карточки Битрикс24).")
    return 0


def main() -> None:
    code = 1
    try:
        code = asyncio.run(_run())
    except KeyboardInterrupt:
        code = 130
    finally:
        try:
            asyncio.run(bitrix24.close_client())
        except RuntimeError:
            pass
    sys.exit(code)


if __name__ == "__main__":
    main()
