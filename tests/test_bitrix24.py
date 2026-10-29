import os
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from app import bitrix24


LEAD_TEXT = (
    "Имя клиента: Анна Смирнова\n"
    "Телефон: +7 900 123-45-67\n"
    "Почта: a.smirnova@alfa-logistics.ru\n"
    "Текст заявки:\n"
    "Нужны офисные кресла, пришлите коммерческое предложение."
)

CLIENT = {
    "name": "Анна Смирнова",
    "phone": "+7 900 123-45-67",
    "email": "a.smirnova@alfa-logistics.ru",
}

RESULT = {
    "company": "Альфа-Логистик",
    "summary": "Клиент просит коммерческое предложение на офисные кресла.",
    "lead_type": "целевой",
    "key_data": "Имя: Анна Смирнова; Телефон: +7 900 123-45-67",
    "missing_data": "Количество кресел",
    "next_step": "Подготовить коммерческое предложение.",
    "reply_draft": "Здравствуйте, Анна! Подготовим коммерческое предложение.",
    "needs_human": True,
    "human_reason": "Нужно уточнить количество.",
}

ENV = {
    "B24_URL": "https://example.bitrix24.ru/rest/1/test-token/",
    "B24_RESPONSIBLE_ID": "",
    "B24_SOURCE_ID": "WEB",
    "B24_CATEGORY_ID": "0",
    "B24_STAGE_ID": "",
    "B24_TASK_MODE": "needs_human",
    "B24_NOTIFY_MODE": "all",
    "B24_TIMELINE_COMMENT": "true",
}


def ok_call(calls, *, duplicate=None, deal_error=False, contact_error=False):
    async def fake_call(method, params=None):
        calls.append((method, params or {}))
        if method == "crm.duplicate.findbycomm":
            return {"CONTACT": duplicate or []}
        if method == "crm.item.add":
            if params["entityTypeId"] == bitrix24.CONTACT_ENTITY_TYPE_ID:
                if contact_error:
                    raise bitrix24.Bitrix24APIError(method, code="CONTACT_ERROR")
                return {"item": {"id": 101}}
            if deal_error:
                raise bitrix24.Bitrix24APIError(method, code="DEAL_ERROR")
            return {"item": {"id": 202}}
        if method == "crm.item.update":
            return {"item": {"id": params.get("id")}}
        if method == "crm.timeline.comment.add":
            return 303
        if method == "tasks.task.add":
            return {"task": {"id": "404"}}
        if method == "im.notify":
            return 505
        raise AssertionError(f"Неожиданный метод: {method}")

    return fake_call


class Bitrix24SyncTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        bitrix24._fields_cache.clear()
        self.env = patch.dict(os.environ, ENV, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)

    async def run_sync(self, *, calls, result=None, duplicate=None, deal_error=False):
        with patch.object(bitrix24, "_crm_mode", new=AsyncMock(return_value=2)), patch.object(
            bitrix24, "fields_ready", new=AsyncMock(return_value=True)
        ), patch.object(
            bitrix24,
            "call",
            new=AsyncMock(side_effect=ok_call(calls, duplicate=duplicate, deal_error=deal_error)),
        ):
            return await bitrix24.sync_lead(
                result=result or RESULT,
                client=CLIENT,
                lead_text=LEAD_TEXT,
                model="GigaChat-2-Pro",
                request_id="req-123",
            )

    async def test_disabled_without_webhook(self) -> None:
        with patch.dict(os.environ, {"B24_URL": ""}, clear=False):
            status = await bitrix24.sync_lead(
                result=RESULT,
                client=CLIENT,
                lead_text=LEAD_TEXT,
            )
        self.assertEqual(status.status, "disabled")
        self.assertIn("B24_URL", status.message)

    async def test_creates_contact_deal_timeline_task_and_notification(self) -> None:
        calls: list[tuple[str, dict]] = []
        status = await self.run_sync(calls=calls)

        self.assertEqual(status.status, "created")
        self.assertEqual(status.contact_id, 101)
        self.assertEqual(status.deal_id, 202)
        self.assertEqual(status.timeline_comment_id, 303)
        self.assertEqual(status.task_id, 404)
        self.assertTrue(status.notified)
        self.assertTrue(status.contact_created)
        self.assertTrue(status.contact_updated)
        self.assertEqual(status.warnings, [])
        self.assertEqual(
            status.as_dict()["contact_url"],
            "https://example.bitrix24.ru/crm/contact/details/101/",
        )
        self.assertEqual(
            status.as_dict()["deal_url"],
            "https://example.bitrix24.ru/crm/deal/details/202/",
        )

        methods = [method for method, _ in calls]
        self.assertEqual(
            methods,
            [
                "crm.duplicate.findbycomm",
                "crm.duplicate.findbycomm",
                "crm.item.add",
                "crm.item.add",
                "crm.item.update",
                "crm.timeline.comment.add",
                "tasks.task.add",
                "im.notify",
            ],
        )

        contact_add = calls[2][1]
        self.assertEqual(contact_add["entityTypeId"], 3)
        self.assertEqual(contact_add["fields"]["name"], "Анна Смирнова")
        self.assertEqual(contact_add["fields"]["fm"][0]["typeId"], "PHONE")
        self.assertEqual(contact_add["fields"]["fm"][0]["value"], "+7 900 123-45-67")
        self.assertEqual(contact_add["fields"]["fm"][1]["typeId"], "EMAIL")

        deal_add = calls[3][1]
        self.assertEqual(deal_add["entityTypeId"], 2)
        self.assertEqual(deal_add["fields"]["contactId"], 101)
        self.assertEqual(deal_add["fields"]["UF_CRM_AI_LEAD_TYPE"], "целевой")
        self.assertEqual(deal_add["fields"]["UF_CRM_AI_NEEDS_HUMAN"], "Y")
        self.assertEqual(deal_add["fields"]["UF_CRM_AI_REQUEST_ID"], "req-123")
        self.assertIn("Альфа-Логистик", deal_add["fields"]["title"])

        contact_update = calls[4][1]
        self.assertEqual(contact_update["entityTypeId"], 3)
        self.assertEqual(contact_update["fields"]["UF_CRM_AI_LAST_DEAL_ID"], "202")
        self.assertEqual(contact_update["fields"]["UF_CRM_AI_LAST_LEAD_TYPE"], "целевой")
        self.assertEqual(contact_update["fields"]["UF_CRM_AI_LAST_NEEDS_HUMAN"], "Y")
        self.assertEqual(contact_update["fields"]["UF_CRM_AI_LAST_REQUEST_ID"], "req-123")

        timeline = calls[5][1]["fields"]
        self.assertEqual(timeline["ENTITY_TYPE"], "contact")
        self.assertEqual(timeline["ENTITY_ID"], 101)
        self.assertIn("https://example.bitrix24.ru/crm/deal/details/202/", timeline["COMMENT"])

        task = calls[6][1]["fields"]
        self.assertIn("D_202", task["UF_CRM_TASK"])
        self.assertIn("C_101", task["UF_CRM_TASK"])
        self.assertEqual(task["RESPONSIBLE_ID"], 1)

        notify = calls[7][1]
        self.assertEqual(notify["TYPE"], "SYSTEM")
        self.assertEqual(notify["USER_ID"], 1)
        self.assertNotIn("TAG", notify)
        self.assertNotIn("SUB_TAG", notify)

    async def test_existing_contact_is_reused_and_no_task_without_human(self) -> None:
        calls: list[tuple[str, dict]] = []
        result = dict(RESULT, needs_human=False, human_reason="")
        status = await self.run_sync(calls=calls, result=result, duplicate=[77])

        self.assertEqual(status.status, "created")
        self.assertEqual(status.contact_id, 77)
        self.assertFalse(status.contact_created)
        self.assertIsNone(status.task_id)
        self.assertEqual(
            [method for method, _ in calls],
            [
                "crm.duplicate.findbycomm",
                "crm.item.add",
                "crm.item.update",
                "crm.timeline.comment.add",
                "im.notify",
            ],
        )
        self.assertEqual(calls[1][1]["fields"]["contactId"], 77)
        self.assertNotIn("tasks.task.add", [method for method, _ in calls])

    async def test_contact_is_updated_even_when_deal_creation_fails(self) -> None:
        calls: list[tuple[str, dict]] = []
        status = await self.run_sync(calls=calls, deal_error=True)

        self.assertEqual(status.status, "partial")
        self.assertEqual(status.contact_id, 101)
        self.assertIsNone(status.deal_id)
        self.assertTrue(status.contact_updated)
        self.assertTrue(any("сделку" in warning for warning in status.warnings))

        contact_update = next(
            params for method, params in calls if method == "crm.item.update"
        )
        self.assertEqual(contact_update["fields"]["UF_CRM_AI_LAST_DEAL_ID"], "")
        self.assertEqual(contact_update["fields"]["UF_CRM_AI_LAST_SUMMARY"], RESULT["summary"])

    async def test_contact_creation_failure_does_not_block_deal(self) -> None:
        calls: list[tuple[str, dict]] = []
        with patch.object(bitrix24, "_crm_mode", new=AsyncMock(return_value=2)), patch.object(
            bitrix24, "fields_ready", new=AsyncMock(return_value=True)
        ), patch.object(
            bitrix24,
            "call",
            new=AsyncMock(
                side_effect=ok_call(calls, contact_error=True)
            ),
        ):
            status = await bitrix24.sync_lead(
                result=RESULT,
                client=CLIENT,
                lead_text=LEAD_TEXT,
                request_id="req-123",
            )

        self.assertEqual(status.status, "partial")
        self.assertIsNone(status.contact_id)
        self.assertEqual(status.deal_id, 202)
        self.assertTrue(any("контакт" in warning for warning in status.warnings))

    async def test_unsupported_crm_mode(self) -> None:
        with patch.object(bitrix24, "_crm_mode", new=AsyncMock(return_value=1)):
            status = await bitrix24.sync_lead(
                result=RESULT,
                client=CLIENT,
                lead_text=LEAD_TEXT,
            )
        self.assertEqual(status.status, "unsupported_mode")
        self.assertIn("простой режим", status.message)

    async def test_missing_deal_fields_are_reported(self) -> None:
        calls: list[tuple[str, dict]] = []
        with patch.object(bitrix24, "_crm_mode", new=AsyncMock(return_value=2)), patch.object(
            bitrix24, "fields_ready", new=AsyncMock(side_effect=[False, True])
        ), patch.object(
            bitrix24, "call", new=AsyncMock(side_effect=ok_call(calls))
        ):
            status = await bitrix24.sync_lead(
                result=RESULT,
                client=CLIENT,
                lead_text=LEAD_TEXT,
                request_id="req-123",
            )

        self.assertEqual(status.status, "partial")
        self.assertTrue(any("Поля сделки" in warning for warning in status.warnings))
        deal_add = next(
            params
            for method, params in calls
            if method == "crm.item.add" and params["entityTypeId"] == 2
        )
        self.assertNotIn("UF_CRM_AI_SUMMARY", deal_add["fields"])
        self.assertIn("AI-карточка заявки", deal_add["fields"]["comments"])


class Bitrix24CallTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.env = patch.dict(os.environ, ENV, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)

    async def test_retries_503_then_returns_result(self) -> None:
        attempts: list[int] = []

        def handler(_: httpx.Request) -> httpx.Response:
            attempts.append(1)
            if len(attempts) == 1:
                return httpx.Response(503, json={"error": "SERVICE_UNAVAILABLE"})
            return httpx.Response(200, json={"result": {"ok": True}})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            with patch.object(bitrix24, "_get_client", return_value=client), patch.object(
                bitrix24, "_wait_for_slot", new=AsyncMock()
            ), patch("app.bitrix24.asyncio.sleep", new=AsyncMock()):
                result = await bitrix24.call("test.method", {"x": 1})
        finally:
            await client.aclose()

        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(attempts), 2)

    async def test_timeout_is_not_retried(self) -> None:
        attempts: list[int] = []

        def handler(_: httpx.Request) -> httpx.Response:
            attempts.append(1)
            raise httpx.ReadTimeout("timeout")

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            with patch.object(bitrix24, "_get_client", return_value=client), patch.object(
                bitrix24, "_wait_for_slot", new=AsyncMock()
            ):
                with self.assertRaises(bitrix24.Bitrix24Timeout):
                    await bitrix24.call("test.method")
        finally:
            await client.aclose()

        self.assertEqual(len(attempts), 1)

    async def test_api_error_is_raised_without_token_in_message(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"error": "ACCESS_DENIED", "error_description": "Нет доступа"},
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            with patch.object(bitrix24, "_get_client", return_value=client), patch.object(
                bitrix24, "_wait_for_slot", new=AsyncMock()
            ):
                with self.assertRaises(bitrix24.Bitrix24APIError) as ctx:
                    await bitrix24.call("crm.item.add")
        finally:
            await client.aclose()

        self.assertEqual(ctx.exception.code, "ACCESS_DENIED")
        self.assertNotIn("test-token", str(ctx.exception))
        self.assertNotIn("test-token", ctx.exception.message)


class Bitrix24FieldTests(unittest.IsolatedAsyncioTestCase):
    def test_field_codes_are_unique_and_fit_bitrix_limit(self) -> None:
        specs = bitrix24._contact_field_specs() + bitrix24._deal_field_specs()
        codes = [spec.code for spec in specs]
        self.assertEqual(len(codes), len(set(codes)))
        self.assertTrue(all(len(code) <= 50 for code in codes))
        self.assertTrue(all(code.startswith("UF_CRM_AI_") for code in codes))

    async def test_setup_creates_only_missing_fields(self) -> None:
        existing_deal = bitrix24._deal_field_specs()[0].code
        calls: list[tuple[str, dict]] = []

        async def fake_call(method, params=None):
            calls.append((method, params or {}))
            if method == "crm.contact.userfield.list":
                return []
            if method == "crm.deal.userfield.list":
                return [{"FIELD_NAME": existing_deal}]
            if method.endswith(".userfield.add"):
                return 1
            raise AssertionError(method)

        with patch.object(bitrix24, "call", new=AsyncMock(side_effect=fake_call)):
            report = await bitrix24.setup_user_fields()

        self.assertEqual(len(report["contact"]["created"]), len(bitrix24._contact_field_specs()))
        self.assertEqual(len(report["deal"]["existing"]), 1)
        self.assertEqual(
            len(report["deal"]["created"]),
            len(bitrix24._deal_field_specs()) - 1,
        )
        add_methods = [method for method, _ in calls if method.endswith(".userfield.add")]
        added_codes = [
            params["fields"]["FIELD_NAME"]
            for method, params in calls
            if method.endswith(".userfield.add")
        ]
        self.assertNotIn(existing_deal, added_codes)
        self.assertEqual(
            len(add_methods),
            len(bitrix24._contact_field_specs())
            + len(bitrix24._deal_field_specs())
            - 1,
        )


if __name__ == "__main__":
    unittest.main()
