import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app import gigachat


class SslContextTests(unittest.TestCase):
    def test_uses_system_store_and_adds_certifi_and_russian_root(self) -> None:
        context = MagicMock()
        with tempfile.TemporaryDirectory() as tmp:
            russian_root = Path(tmp) / "russian_root.pem"
            russian_root.write_text("dummy", encoding="ascii")

            with patch.object(
                gigachat.ssl, "create_default_context", return_value=context
            ) as create_context, patch.object(
                gigachat.certifi, "where", return_value="certifi.pem"
            ), patch.object(
                gigachat, "cert_path", return_value=russian_root
            ):
                result = gigachat._ssl_context()

        self.assertIs(result, context)
        create_context.assert_called_once_with()
        calls = context.load_verify_locations.call_args_list
        self.assertEqual(calls[0].kwargs, {"cafile": "certifi.pem"})
        self.assertEqual(calls[1].kwargs, {"cafile": str(russian_root)})

    def test_missing_custom_certificate_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.pem"
            with patch.object(
                gigachat.ssl, "create_default_context", return_value=MagicMock()
            ), patch.object(gigachat.certifi, "where", return_value="certifi.pem"), patch.object(
                gigachat, "cert_path", return_value=missing
            ):
                with self.assertRaises(gigachat.CertificateError):
                    gigachat._ssl_context()


if __name__ == "__main__":
    unittest.main()
