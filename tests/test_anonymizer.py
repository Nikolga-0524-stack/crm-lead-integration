import unittest

from app.anonymizer import Anonymizer


class AnonymizerTests(unittest.TestCase):
    def known(self) -> dict[str, str]:
        return {
            "name": "Анна Смирнова",
            "phone": "+7 900 123-45-67",
            "email": "a.smirnova@alfa-logistics.ru",
        }

    def test_masks_known_fields_and_restores_them(self) -> None:
        text = (
            "Имя клиента: Анна Смирнова\n"
            "Телефон: +7 900 123-45-67\n"
            "Почта: a.smirnova@alfa-logistics.ru"
        )
        anonymizer = Anonymizer(self.known())

        masked = anonymizer.mask(text)

        self.assertNotIn("Анна", masked)
        self.assertNotIn("900", masked)
        self.assertNotIn("smirnova", masked.lower())
        self.assertIn("[ИМЯ_1]", masked)
        self.assertIn("[ТЕЛЕФОН_1]", masked)
        self.assertIn("[EMAIL_1]", masked)
        self.assertEqual(anonymizer.restore(masked), text)

    def test_same_phone_in_different_formats_gets_one_token(self) -> None:
        anonymizer = Anonymizer(self.known())
        text = "Звоните 8 (900) 123-45-67 или 79001234567"

        masked = anonymizer.mask(text)

        self.assertEqual(masked.count("[ТЕЛЕФОН_1]"), 2)
        self.assertNotIn("900", masked)
        self.assertEqual(
            anonymizer.restore(masked),
            "Звоните +7 900 123-45-67 или +7 900 123-45-67",
        )

    def test_different_phones_get_different_tokens(self) -> None:
        anonymizer = Anonymizer()
        text = "Первый +7 900 123-45-67, второй +7 911 222-33-44"

        masked = anonymizer.mask(text)

        self.assertIn("[ТЕЛЕФОН_1]", masked)
        self.assertIn("[ТЕЛЕФОН_2]", masked)
        self.assertNotIn("900", masked)
        self.assertNotIn("911", masked)

    def test_masks_cyrillic_email(self) -> None:
        anonymizer = Anonymizer()
        text = "Почта: анна@пример.рф"

        masked = anonymizer.mask(text)

        self.assertIn("[EMAIL_1]", masked)
        self.assertNotIn("анна@", masked)
        self.assertEqual(anonymizer.restore(masked), text)

    def test_phone_without_country_prefix(self) -> None:
        anonymizer = Anonymizer()
        text = "Телефон: 900 123-45-67"

        masked = anonymizer.mask(text)

        self.assertIn("[ТЕЛЕФОН_1]", masked)
        self.assertNotIn("900", masked)
        self.assertEqual(anonymizer.restore(masked), text)

    def test_detects_name_phone_and_email_in_free_text(self) -> None:
        anonymizer = Anonymizer()
        text = (
            "Меня зовут Иван Петров, телефон 8 (912) 345-67-89, "
            "почта ivan@example.ru"
        )

        masked = anonymizer.mask(text)

        self.assertNotIn("Иван", masked)
        self.assertNotIn("Петров", masked)
        self.assertNotIn("912", masked)
        self.assertNotIn("ivan@", masked)
        self.assertIn("[ИМЯ_1]", masked)
        self.assertIn("[ТЕЛЕФОН_1]", masked)
        self.assertIn("[EMAIL_1]", masked)
        self.assertEqual(anonymizer.restore(masked), text)

    def test_known_name_parts_are_masked_in_later_text(self) -> None:
        anonymizer = Anonymizer({"name": "Анна Смирнова"})
        text = "Контактное лицо: Анна Смирнова. Анна просит КП."

        masked = anonymizer.mask(text)

        self.assertNotIn("Анна", masked)
        self.assertNotIn("Смирнова", masked)
        self.assertEqual(anonymizer.restore(masked), text)

    def test_masks_company_by_explicit_marker_but_not_city(self) -> None:
        text = "Компания: Ромашка, город Москва, нужны кресла."
        anonymizer = Anonymizer()

        masked = anonymizer.mask(text)

        self.assertIn("[КОМПАНИЯ_1]", masked)
        self.assertNotIn("Ромашка", masked)
        self.assertIn("Москва", masked)
        self.assertEqual(anonymizer.restore(masked), text)

    def test_masks_and_restores_address(self) -> None:
        text = "Приезжайте: г. Москва, ул. Ленина, д. 10, кв. 5."
        anonymizer = Anonymizer()

        masked = anonymizer.mask(text)

        self.assertIn("[АДРЕС_1]", masked)
        self.assertNotIn("Ленина", masked)
        self.assertEqual(anonymizer.restore(masked), text)

    def test_masks_and_restores_address_with_label(self) -> None:
        text = "Адрес: г. Москва, ул. Ленина, д. 10, кв. 5"
        anonymizer = Anonymizer()

        masked = anonymizer.mask(text)

        self.assertEqual(masked, "Адрес: [АДРЕС_1]")
        self.assertEqual(anonymizer.restore(masked), text)

    def test_masks_address_with_street_type_after_name(self) -> None:
        text = "Ленинский проспект, д. 10, кв. 5"
        anonymizer = Anonymizer()

        masked = anonymizer.mask(text)

        self.assertIn("[АДРЕС_1]", masked)
        self.assertNotIn("Ленинский", masked)
        self.assertEqual(anonymizer.restore(masked), text)

    def test_masks_and_restores_identifiers(self) -> None:
        text = (
            "Паспорт: 45 06 123456\n"
            "СНИЛС: 123-456-789 00\n"
            "ИНН: 7707083893\n"
            "Номер карты: 4111 1111 1111 1111\n"
            "Дата рождения: 01.02.1990"
        )
        anonymizer = Anonymizer()

        masked = anonymizer.mask(text)

        for token in (
            "[ПАСПОРТ_1]",
            "[СНИЛС_1]",
            "[ИНН_1]",
            "[КАРТА_1]",
            "[ДАТА_РОЖДЕНИЯ_1]",
        ):
            self.assertIn(token, masked)

        self.assertEqual(anonymizer.restore(masked), text)

    def test_masks_passport_series_and_number_without_label(self) -> None:
        text = "Серия 4506 номер 123456"
        anonymizer = Anonymizer()

        masked = anonymizer.mask(text)

        self.assertIn("[ПАСПОРТ_1]", masked)
        self.assertNotIn("4506", masked)
        self.assertNotIn("123456", masked)
        self.assertEqual(anonymizer.restore(masked), text)

    def test_masks_free_text_fio_by_heuristic(self) -> None:
        text = "Клиент Иван Петров просит коммерческое предложение."
        anonymizer = Anonymizer()

        masked = anonymizer.mask(text)

        self.assertIn("[ИМЯ_1]", masked)
        self.assertNotIn("Иван", masked)
        self.assertNotIn("Петров", masked)
        self.assertEqual(anonymizer.restore(masked), text)

    def test_does_not_mask_domain_words(self) -> None:
        text = "Офис на 30 человек, нужны столы и кресла."
        masked = Anonymizer().mask(text)

        self.assertEqual(masked, text)

    def test_restores_token_aliases(self) -> None:
        anonymizer = Anonymizer(self.known())
        anonymizer.mask("+7 900 123-45-67")

        self.assertEqual(anonymizer.restore("[ФИО_1]"), "Анна Смирнова")
        self.assertEqual(
            anonymizer.restore("[ПОЧТА_1]"),
            "a.smirnova@alfa-logistics.ru",
        )

    def test_restores_nested_result(self) -> None:
        anonymizer = Anonymizer(self.known())
        anonymizer.mask("+7 900 123-45-67")

        result = {
            "key_data": "Телефон: [ТЕЛЕФОН_1]",
            "items": ["[ТЕЛЕФОН_1]"],
            "count": 1,
        }
        restored = anonymizer.restore_result(result)

        self.assertEqual(restored["key_data"], "Телефон: +7 900 123-45-67")
        self.assertEqual(restored["items"], ["+7 900 123-45-67"])
        self.assertEqual(restored["count"], 1)

    def test_unknown_token_with_two_values_becomes_neutral(self) -> None:
        anonymizer = Anonymizer()
        anonymizer.mask("+7 900 123-45-67 и +7 911 222-33-44")

        restored = anonymizer.restore("Позвонить [ТЕЛЕФОН_9]")

        self.assertEqual(restored, "Позвонить указанный телефон")
        self.assertEqual(anonymizer.unresolved_count, 1)

    def test_literal_token_in_source_is_preserved(self) -> None:
        anonymizer = Anonymizer()
        text = "В тексте уже есть [ТЕЛЕФОН_1], а телефон +7 900 123-45-67"

        masked = anonymizer.mask(text)

        self.assertIn("[ТЕЛЕФОН_1]", masked)
        self.assertIn("[ТЕЛЕФОН_2]", masked)
        self.assertEqual(anonymizer.restore(masked), text)


if __name__ == "__main__":
    unittest.main()
