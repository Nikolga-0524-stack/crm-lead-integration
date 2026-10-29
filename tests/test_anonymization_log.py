import csv
import hashlib
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from app import anonymization_log
from app.gigachat import AnonymizationTrace


def make_trace() -> AnonymizationTrace:
    return AnonymizationTrace(
        text="Имя клиента: [ИМЯ_1]\nТелефон: [ТЕЛЕФОН_1]\nПочта: [EMAIL_1]",
        stats={
            "name": 1,
            "phone": 1,
            "email": 1,
            "address": 0,
            "passport": 0,
            "snils": 0,
            "inn": 0,
            "card": 0,
            "birth_date": 0,
            "company": 0,
        },
        unresolved=0,
    )


class AnonymizationLogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log_file = Path(self.tmp.name) / "logs" / "anonymization.csv"
        env = patch.dict(
            os.environ,
            {
                "ANONYMIZATION_LOG_ENABLED": "true",
                "ANONYMIZATION_LOG_FILE": str(self.log_file),
            },
        )
        env.start()
        self.addCleanup(env.stop)

    def read_rows(self) -> list[dict[str, str]]:
        with self.log_file.open(newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))

    def test_writes_header_and_metadata(self) -> None:
        trace = make_trace()

        written = anonymization_log.write_entry(
            trace,
            request_id="req-1",
            status="success",
            model="GigaChat-2-Pro",
            original_chars=150,
        )

        self.assertTrue(written)
        rows = self.read_rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["request_id"], "req-1")
        self.assertEqual(row["status"], "success")
        self.assertEqual(row["error_type"], "")
        self.assertEqual(row["model"], "GigaChat-2-Pro")
        self.assertEqual(row["original_chars"], "150")
        self.assertEqual(row["masked_chars"], str(len(trace.text)))
        self.assertEqual(row["total_masked"], "3")
        self.assertEqual(row["name"], "1")
        self.assertEqual(row["phone"], "1")
        self.assertEqual(row["email"], "1")
        self.assertEqual(row["unresolved"], "0")
        self.assertEqual(
            row["masked_sha256"],
            hashlib.sha256(trace.text.encode("utf-8")).hexdigest(),
        )
        self.assertTrue(row["timestamp_utc"].endswith("Z"))

    def test_appends_rows_and_does_not_duplicate_header(self) -> None:
        for index in range(2):
            anonymization_log.write_entry(
                make_trace(),
                request_id=f"req-{index}",
                status="success",
                model="GigaChat-2-Pro",
                original_chars=100,
            )

        rows = self.read_rows()
        self.assertEqual([row["request_id"] for row in rows], ["req-0", "req-1"])
        raw = self.log_file.read_text(encoding="utf-8")
        self.assertEqual(raw.count("timestamp_utc"), 1)

    def test_does_not_store_text_or_personal_data(self) -> None:
        trace = make_trace()
        trace.text += "\nАнна Смирнова, +7 900 123-45-67, a.smirnova@example.ru"

        anonymization_log.write_entry(
            trace,
            request_id="req-private",
            status="success",
            model="GigaChat-2-Pro",
            original_chars=200,
        )

        raw = self.log_file.read_text(encoding="utf-8")
        self.assertNotIn("Анна", raw)
        self.assertNotIn("Смирнова", raw)
        self.assertNotIn("900 123-45-67", raw)
        self.assertNotIn("smirnova", raw)
        self.assertNotIn("[ИМЯ_1]", raw)
        self.assertIn("req-private", raw)

    def test_writes_error_status(self) -> None:
        anonymization_log.write_entry(
            make_trace(),
            request_id="req-error",
            status="error",
            error_type="GigaChatTimeout",
            model="GigaChat-2",
            original_chars=90,
        )

        row = self.read_rows()[0]
        self.assertEqual(row["status"], "error")
        self.assertEqual(row["error_type"], "GigaChatTimeout")

    def test_can_be_disabled(self) -> None:
        with patch.dict(os.environ, {"ANONYMIZATION_LOG_ENABLED": "false"}):
            self.assertFalse(anonymization_log.ensure_file())
            self.assertFalse(
                anonymization_log.write_entry(
                    make_trace(),
                    request_id="req-off",
                    status="success",
                    model="GigaChat-2-Pro",
                    original_chars=10,
                )
            )

        self.assertFalse(self.log_file.exists())

    def test_write_failure_does_not_raise(self) -> None:
        with patch.dict(
            os.environ,
            {"ANONYMIZATION_LOG_FILE": self.tmp.name},
        ):
            self.assertFalse(
                anonymization_log.write_entry(
                    make_trace(),
                    request_id="req-bad-path",
                    status="success",
                    model="GigaChat-2-Pro",
                    original_chars=10,
                )
            )

    def test_concurrent_writes_keep_one_header(self) -> None:
        def write(index: int) -> bool:
            return anonymization_log.write_entry(
                make_trace(),
                request_id=f"req-{index}",
                status="success",
                model="GigaChat-2-Pro",
                original_chars=100,
            )

        with ThreadPoolExecutor(max_workers=6) as executor:
            results = list(executor.map(write, range(12)))

        self.assertTrue(all(results))
        rows = self.read_rows()
        self.assertEqual(len(rows), 12)
        self.assertEqual(
            self.log_file.read_text(encoding="utf-8").count("timestamp_utc"),
            1,
        )


if __name__ == "__main__":
    unittest.main()
