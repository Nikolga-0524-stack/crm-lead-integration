import unittest
from unittest.mock import patch

from app import gigachat

LEAD_TEXT = (
    "Имя клиента: Анна Смирнова\n"
    "Телефон: +7 900 123-45-67\n"
    "Почта: a.smirnova@alfa-logistics.ru\n"
    "Текст заявки:\n"
    "Нужны офисные кресла, пришлите коммерческое предложение."
)

KNOWN_VALUES = {
    "name": "Анна Смирнова",
    "phone": "+7 900 123-45-67",
    "email": "a.smirnova@alfa-logistics.ru",
}

MODEL_ANSWER = {
    "company": "не указано",
    "summary": "Клиент [ИМЯ_1] просит коммерческое предложение.",
    "lead_type": "целевой",
    "key_data": "Имя: [ИМЯ_1]; Телефон: [ТЕЛЕФОН_1]; Email: [EMAIL_1]",
    "missing_data": "Недостающих данных нет",
    "next_step": "Подготовить коммерческое предложение.",
    "reply_draft": "Здравствуйте, [ИМЯ_1]! Подготовим коммерческое предложение.",
    "needs_human": False,
    "human_reason": "",
}


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_gets_only_masked_data_and_result_is_restored(self) -> None:
        captured: dict[str, str] = {}

        async def fake_chat(text: str, retry_note: bool = False) -> dict:
            captured["text"] = text
            return dict(MODEL_ANSWER)

        with patch.object(gigachat, "is_configured", return_value=True), patch.object(
            gigachat, "_chat", side_effect=fake_chat
        ):
            result = await gigachat.analyze_lead(
                LEAD_TEXT,
                known_values=KNOWN_VALUES,
            )

        sent = captured["text"]

        # В GigaChat не должно уйти ни одного реального значения.
        self.assertNotIn("Анна", sent)
        self.assertNotIn("Смирнова", sent)
        self.assertNotIn("900", sent)
        self.assertNotIn("smirnova", sent.lower())

        # Вместо них — токены.
        self.assertIn("[ИМЯ_1]", sent)
        self.assertIn("[ТЕЛЕФОН_1]", sent)
        self.assertIn("[EMAIL_1]", sent)

        # Пользователю возвращаются настоящие данные.
        for value in result.values():
            if isinstance(value, str):
                self.assertNotIn("[ИМЯ_", value)
                self.assertNotIn("[ТЕЛЕФОН_", value)
                self.assertNotIn("[EMAIL_", value)

        self.assertIn("Анна Смирнова", result["key_data"])
        self.assertIn("+7 900 123-45-67", result["key_data"])
        self.assertIn("a.smirnova@alfa-logistics.ru", result["key_data"])
        self.assertIn("Анна Смирнова", result["reply_draft"])

    async def test_trace_contains_exact_text_sent_to_model(self) -> None:
        captured: dict[str, str] = {}

        async def fake_chat(text: str, retry_note: bool = False) -> dict:
            captured["text"] = text
            return dict(MODEL_ANSWER)

        trace = gigachat.AnonymizationTrace()

        with patch.object(gigachat, "is_configured", return_value=True), patch.object(
            gigachat, "_chat", side_effect=fake_chat
        ):
            await gigachat.analyze_lead(
                LEAD_TEXT,
                known_values=KNOWN_VALUES,
                trace=trace,
            )

        self.assertEqual(trace.text, captured["text"])
        self.assertNotIn("Анна", trace.text)
        self.assertNotIn("900", trace.text)
        self.assertNotIn("smirnova", trace.text.lower())
        self.assertIn("[ИМЯ_1]", trace.text)
        self.assertGreaterEqual(trace.stats.get("name", 0), 1)
        self.assertGreaterEqual(trace.stats.get("phone", 0), 1)
        self.assertGreaterEqual(trace.stats.get("email", 0), 1)

    async def test_address_company_and_identifiers_are_masked_and_restored(self) -> None:
        lead_text = (
            "Имя клиента: Анна Смирнова\n"
            "Телефон: +7 900 123-45-67\n"
            "Почта: a.smirnova@alfa-logistics.ru\n"
            "Текст заявки:\n"
            "Компания «Альфа-Логистик», адрес: г. Москва, ул. Ленина, д. 10. "
            "ИНН 7707083893, карта 4111 1111 1111 1111."
        )
        answer = dict(MODEL_ANSWER)
        answer["company"] = "[КОМПАНИЯ_1]"
        answer["summary"] = (
            "Клиент [ИМЯ_1] из компании [КОМПАНИЯ_1] просит коммерческое предложение."
        )
        answer["key_data"] = (
            "Имя: [ИМЯ_1]; Телефон: [ТЕЛЕФОН_1]; Email: [EMAIL_1]; "
            "Адрес: [АДРЕС_1]; ИНН: [ИНН_1]; Карта: [КАРТА_1]"
        )
        captured: dict[str, str] = {}

        async def fake_chat(text: str, retry_note: bool = False) -> dict:
            captured["text"] = text
            return dict(answer)

        with patch.object(gigachat, "is_configured", return_value=True), patch.object(
            gigachat, "_chat", side_effect=fake_chat
        ):
            result = await gigachat.analyze_lead(
                lead_text,
                known_values=KNOWN_VALUES,
            )

        sent = captured["text"]

        self.assertNotIn("Альфа-Логистик", sent)
        self.assertNotIn("Ленина", sent)
        self.assertNotIn("7707083893", sent)
        self.assertNotIn("4111", sent)
        self.assertIn("[КОМПАНИЯ_1]", sent)
        self.assertIn("[АДРЕС_1]", sent)
        self.assertIn("[ИНН_1]", sent)
        self.assertIn("[КАРТА_1]", sent)

        self.assertEqual(result["company"], "Альфа-Логистик")
        self.assertIn("Анна Смирнова", result["summary"])
        self.assertIn("Альфа-Логистик", result["summary"])
        self.assertIn("г. Москва, ул. Ленина, д. 10", result["key_data"])
        self.assertIn("7707083893", result["key_data"])
        self.assertIn("4111 1111 1111 1111", result["key_data"])

    async def test_retry_also_gets_masked_text(self) -> None:
        calls: list[str] = []
        answers = [
            "not a json object",
            dict(MODEL_ANSWER),
        ]

        async def fake_chat(text: str, retry_note: bool = False) -> dict:
            calls.append(text)
            return answers[len(calls) - 1]

        with patch.object(gigachat, "is_configured", return_value=True), patch.object(
            gigachat, "_chat", side_effect=fake_chat
        ):
            result = await gigachat.analyze_lead(
                LEAD_TEXT,
                known_values=KNOWN_VALUES,
            )

        self.assertEqual(len(calls), 2)
        for sent in calls:
            self.assertNotIn("Анна", sent)
            self.assertNotIn("900", sent)
            self.assertNotIn("smirnova", sent.lower())
            self.assertIn("[ТЕЛЕФОН_1]", sent)

        self.assertIn("Анна Смирнова", result["key_data"])


if __name__ == "__main__":
    unittest.main()
