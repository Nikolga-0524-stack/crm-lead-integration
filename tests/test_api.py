import csv
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app import bitrix24, main


RESULT = {
    "company": "Альфа-Логистик",
    "summary": "Клиент просит коммерческое предложение.",
    "lead_type": "целевой",
    "key_data": "Имя: Анна Смирнова",
    "missing_data": "Количество",
    "next_step": "Подготовить КП.",
    "reply_draft": "Здравствуйте, Анна! Подготовим КП.",
    "needs_human": False,
    "human_reason": "",
}

PAYLOAD = {
    "name": "Анна Смирнова",
    "phone": "+7 900 123-45-67",
    "email": "a.smirnova@alfa-logistics.ru",
    "text": "Нужны офисные кресла, пришлите коммерческое предложение.",
}


class ApiCrmTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log_file = Path(self.tmp.name) / "anonymization.csv"
        env = patch.dict(
            os.environ,
            {
                "ANONYMIZATION_LOG_ENABLED": "true",
                "ANONYMIZATION_LOG_FILE": str(self.log_file),
            },
        )
        env.start()
        self.addCleanup(env.stop)

    def read_log_rows(self) -> list[dict[str, str]]:
        with self.log_file.open(newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))

    def test_analyze_returns_crm_block(self) -> None:
        crm_status = bitrix24.CrmStatus(
            status="created",
            message="Заявка сохранена.",
            request_id="req-1",
            contact_id=11,
            deal_id=22,
            contact_updated=True,
            deal_created=True,
        )

        async def fake_analyze(
            lead_text: str,
            known_values: dict[str, str] | None = None,
            *,
            trace: main.gigachat.AnonymizationTrace | None = None,
        ) -> dict:
            if trace is not None:
                trace.text = (
                    "Имя клиента: [ИМЯ_1]\n"
                    "Телефон: [ТЕЛЕФОН_1]\n"
                    "Почта: [EMAIL_1]\n"
                    "Текст заявки:\n"
                    "Нужны офисные кресла, пришлите коммерческое предложение."
                )
                trace.stats = {"name": 1, "phone": 1, "email": 1}
            return RESULT

        with patch.object(
            main.gigachat, "analyze_lead", new=fake_analyze
        ), patch.object(
            main.bitrix24, "sync_lead", new=AsyncMock(return_value=crm_status)
        ):
            with TestClient(main.app) as client:
                response = client.post("/api/analyze", json=PAYLOAD)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["result"], RESULT)
        self.assertEqual(body["crm"]["status"], "created")
        self.assertEqual(body["crm"]["contact_id"], 11)
        self.assertEqual(body["crm"]["deal_id"], 22)
        self.assertEqual(body["client"]["name"], "Анна Смирнова")
        self.assertIn("[ИМЯ_1]", body["anonymized_text"])
        self.assertNotIn("Анна", body["anonymized_text"])
        self.assertEqual(body["anonymization_stats"]["name"], 1)
        self.assertTrue(body["request_id"])

        rows = self.read_log_rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["request_id"], body["request_id"])
        self.assertEqual(row["status"], "success")
        self.assertEqual(row["total_masked"], "3")
        self.assertEqual(row["name"], "1")
        self.assertNotIn("Анна", self.log_file.read_text(encoding="utf-8"))

    def test_crm_failure_does_not_break_analysis(self) -> None:
        with patch.object(
            main.gigachat, "analyze_lead", new=AsyncMock(return_value=RESULT)
        ), patch.object(
            main.bitrix24,
            "sync_lead",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ):
            with TestClient(main.app) as client:
                response = client.post("/api/analyze", json=PAYLOAD)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["result"], RESULT)
        self.assertEqual(body["crm"]["status"], "error")
        self.assertTrue(body["crm"]["warnings"])

    def test_model_error_returns_masked_text_when_available(self) -> None:
        async def fake_analyze(
            lead_text: str,
            known_values: dict[str, str] | None = None,
            *,
            trace: main.gigachat.AnonymizationTrace | None = None,
        ) -> dict:
            if trace is not None:
                trace.text = "Имя клиента: [ИМЯ_1]"
                trace.stats = {"name": 1}
            raise main.gigachat.GigaChatError("Модель недоступна.")

        with patch.object(main.gigachat, "analyze_lead", new=fake_analyze):
            with TestClient(main.app) as client:
                response = client.post("/api/analyze", json=PAYLOAD)

        self.assertEqual(response.status_code, 502)
        body = response.json()
        self.assertEqual(body["anonymized_text"], "Имя клиента: [ИМЯ_1]")
        self.assertEqual(body["anonymization_stats"], {"name": 1})
        self.assertIn("Модель недоступна", body["error"]["message"])
        self.assertTrue(body["request_id"])

        rows = self.read_log_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "error")
        self.assertEqual(rows[0]["error_type"], "GigaChatError")
        self.assertEqual(rows[0]["request_id"], body["request_id"])

    def test_empty_lead_text_is_rejected(self) -> None:
        with TestClient(main.app) as client:
            response = client.post("/api/analyze", json={**PAYLOAD, "text": "   "})

        self.assertEqual(response.status_code, 400)
        self.assertIn("текст", response.json()["error"]["message"].lower())

    def test_health_reports_bitrix24(self) -> None:
        with TestClient(main.app) as client:
            response = client.get("/api/health")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("bitrix24", body)
        self.assertIn("configured", body["bitrix24"])


if __name__ == "__main__":
    unittest.main()
