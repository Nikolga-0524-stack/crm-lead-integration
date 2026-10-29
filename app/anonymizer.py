"""Анонимизация персональных данных перед отправкой текста в GigaChat.

Экземпляр :class:`Anonymizer` создаётся на один запрос и хранит соответствие
«токен → исходное значение» только в памяти. После ответа модели объект
становится недоступен, поэтому mapping не сохраняется между запросами.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

# --- токены ------------------------------------------------------------------

# Формат: «[КАТЕГОРИЯ_НОМЕР]». Категория может состоять из нескольких слов,
# например «ДАТА_РОЖДЕНИЯ_1».
TOKEN_RE = re.compile(
    r"\[\s*(?P<kind>[A-ZА-ЯЁ]+(?:_[A-ZА-ЯЁ]+)*)\s*[_№\s-]*\s*(?P<num>\d+)\s*\]",
    re.IGNORECASE,
)

# На случай, если модель потеряла скобки: ТЕЛЕФОН_1, EMAIL_2.
LOOSE_TOKEN_RE = re.compile(
    r"(?<![\w\[])(?P<kind>[A-ZА-ЯЁ]+(?:_[A-ZА-ЯЁ]+)*)\s*[_№]\s*(?P<num>\d+)(?![\w\]@])",
    re.IGNORECASE,
)

KIND_CANON = {
    "ИМЯ": "ИМЯ",
    "ФИО": "ИМЯ",
    "ТЕЛЕФОН": "ТЕЛЕФОН",
    "EMAIL": "EMAIL",
    "ПОЧТА": "EMAIL",
    "АДРЕС": "АДРЕС",
    "ПАСПОРТ": "ПАСПОРТ",
    "СНИЛС": "СНИЛС",
    "ИНН": "ИНН",
    "КАРТА": "КАРТА",
    "ДАТА_РОЖДЕНИЯ": "ДАТА_РОЖДЕНИЯ",
    "КОМПАНИЯ": "КОМПАНИЯ",
}

NEUTRAL_VALUE = {
    "ИМЯ": "клиент",
    "ТЕЛЕФОН": "указанный телефон",
    "EMAIL": "указанная почта",
    "АДРЕС": "указанный адрес",
    "ПАСПОРТ": "указанные паспортные данные",
    "СНИЛС": "указанный СНИЛС",
    "ИНН": "указанный ИНН",
    "КАРТА": "указанная банковская карта",
    "ДАТА_РОЖДЕНИЯ": "указанная дата рождения",
    "КОМПАНИЯ": "указанная компания",
}

# --- поиск персональных данных ----------------------------------------------

SPACE = r"[ \t\u00a0]"

EMAIL_RE = re.compile(
    r"[\w.!#$%&'*+/=?^`{|}~\-]+@[\w\-]+(?:\.[\w\-]+)+",
    re.IGNORECASE,
)

# +7 900 123-45-67, 8 (900) 123 45 67, 79001234567 и похожие варианты.
# Разделители не пересекают границу строки и не «съедают» пробел после номера.
PHONE_SEP = r"[ \t\u00a0\-()]*"
PHONE_RE = re.compile(
    rf"(?<![\w+])(?:(?:\+7|8|7){PHONE_SEP})?\d{{3}}(?:{PHONE_SEP}\d){{7}}(?!\d)"
)

# Телефон без разделителей и префикса: 9001234567 / 79001234567.
LONG_DIGITS_RE = re.compile(r"(?<!\w)\d{10,11}(?!\d)")

# Банковские карты проверяем алгоритмом Луна, чтобы не маскировать
# произвольные длинные числа (номера заказов, накладные и т. п.).
CARD_RE = re.compile(r"(?<!\d)(?:\d[ -]?){15,18}\d(?!\d)")

SNILS_RE = re.compile(r"(?<!\d)\d{3}[-\s]\d{3}[-\s]\d{3}[-\s]?\d{2}(?!\d)")

INN_RE = re.compile(
    r"(?i)(\bИНН[ \t\u00a0]*:?[ \t\u00a0]*)(\d{10}|\d{12})(?!\d)"
)

# ИНН без подписи. 12 цифр не пересекаются с телефоном (10–11 цифр).
BARE_INN_RE = re.compile(r"(?<!\d)\d{12}(?!\d)")

PASSPORT_RE = re.compile(
    r"(?i)(\bпаспорт\w*[^\d\n]{0,40}?)"
    r"(\d{2}[ \t\u00a0]?\d{2}[ \t\u00a0]*(?:№|N)?[ \t\u00a0]*\d{6})(?!\d)"
)

# «Серия 4506 номер 123456» без слова «паспорт».
PASSPORT_SERIES_RE = re.compile(
    r"(?i)\bсерия[ \t\u00a0]*:?[ \t\u00a0]*\d{2}[ \t\u00a0]?\d{2}"
    r"[^\n]{0,30}?\bномер[ \t\u00a0]*:?[ \t\u00a0]*\d{6}(?!\d)"
)

BIRTH_DATE_RE = re.compile(
    r"(?i)(\b(?:дата|день)[ \t\u00a0]+рождения[ \t\u00a0]*:?[ \t\u00a0]*)"
    r"(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})(?!\d)"
)

# Поля с явными подписями: маскируем значение целиком, чтобы не потерять
# составные данные (адрес с квартирой, паспорт с серией и номером и т. п.).
ADDRESS_LABEL_RE = re.compile(
    r"(?im)^([ \t]*(?:Адрес(?:\s+клиента)?)[ \t]*:[ \t]*)([^\n]+?)[ \t]*$"
)
PASSPORT_LABEL_RE = re.compile(
    r"(?im)^([ \t]*(?:Паспорт(?:\s+РФ)?|Паспортные данные)[ \t]*:[ \t]*)"
    r"([^\n]+?)[ \t]*$"
)
SNILS_LABEL_RE = re.compile(
    r"(?im)^([ \t]*СНИЛС[ \t]*:?[ \t]*)([^\n]+?)[ \t]*$"
)
INN_LABEL_RE = re.compile(
    r"(?im)^([ \t]*ИНН[ \t]*:?[ \t]*)([^\n]+?)[ \t]*$"
)
BIRTH_LABEL_RE = re.compile(
    r"(?im)^([ \t]*(?:Дата рождения|День рождения)[ \t]*:[ \t]*)"
    r"([^\n]+?)[ \t]*$"
)
CARD_LABEL_RE = re.compile(
    r"(?im)^([ \t]*(?:Номер карты|Карта)[ \t]*:[ \t]*)([^\n]+?)[ \t]*$"
)

STREET_MARKER = (
    r"(?:ул\.?|улица|пр-т|проспект|пер\.?|переулок|наб\.?|набережная|"
    r"ш\.?|шоссе|пл\.?|площадь|мкр\.?|микрорайон|аллея|бульвар|проезд|тупик)"
    r"(?![А-ЯЁа-яё])"
)
HOUSE_MARKER = (
    r"(?:д\.?|дом|корп\.?|корпус|стр\.?|строение|кв\.?|квартира|"
    r"оф\.?|офис|помещение|этаж|подъезд)"
    r"(?![А-ЯЁа-яё])"
)
ADDRESS_RE = re.compile(
    rf"(?i)(?<!\w)(?:(?:г\.|город){SPACE}*)?"
    rf"(?:[А-ЯЁ][\w-]+{SPACE}*,{SPACE}*)?"
    rf"(?:[А-ЯЁ][\w-]+{SPACE}+)?{STREET_MARKER}\.?{SPACE}*"
    rf"(?:[^,.;\n]+|,{SPACE}*{HOUSE_MARKER}\.?{SPACE}*[^,.;\n]+)"
    rf"(?:{SPACE}*,{SPACE}*{HOUSE_MARKER}\.?{SPACE}*[^,.;\n]+)*"
)
CITY_POSTAL_RE = re.compile(
    rf"(?i)(?<!\w)(?:г\.|город){SPACE}*[А-ЯЁ][\w-]+"
    rf"(?:{SPACE}*,{SPACE}*)?\d{{6}}(?!\d)"
)

COMPANY_RE = re.compile(
    r"(?P<prefix>(?i:\b(?:ООО|ОАО|ЗАО|ПАО|АО|ИП|НКО|компания|фирма|"
    r"организация|предприятие)[ \t\u00a0]*(?:[—:-][ \t\u00a0]*)?"
    r"(?:[«\"'][ \t\u00a0]*)?))"
    r"(?P<name>[А-ЯЁA-Z][\w-]*(?:[ \t\u00a0]+[А-ЯЁA-Z][\w-]*){0,4})"
)

# Имя в свободном тексте ищем рядом с явным маркером, чтобы не замаскировать
# название компании, город или обычное слово с заглавной буквы.
NAME_CONTEXT_RE = re.compile(
    r"(?P<prefix>"
    r"(?i:(?:меня[ \t\u00a0]+зовут|мо[её][ \t\u00a0]+имя|"
    r"(?:(?:имя|фамилия|отчество|фио)(?:[ \t\u00a0]+клиента)?|"
    r"контактное[ \t\u00a0]+лицо)"
    r"[ \t\u00a0]*[:\-—]))"
    r"[ \t\u00a0]*)"
    r"(?P<name>[A-ZА-ЯЁ][A-Za-zА-Яа-яЁё-]+"
    r"(?:[ \t\u00a0]+[A-ZА-ЯЁ][A-Za-zА-Яа-яЁё-]+){0,2})"
)

NAME_WORD = r"[А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?"

# Эвристика для ФИО без явной подписи: два-три слова с заглавной буквы,
# где есть частое имя, отчество или типичный фамильный суффикс.
FIO_RE = re.compile(
    rf"(?<![\w-])({NAME_WORD})(?:{SPACE}+({NAME_WORD})){{1,2}}(?![\w-])"
)
SINGLE_NAME_RE = re.compile(
    rf"(?<![\w-])({NAME_WORD})(?={SPACE}*[.,!?])"
)

FIRST_NAMES = frozenset(
    {
        "александр", "алексей", "анатолий", "андрей", "антон", "аркадий",
        "арсений", "артём", "артем", "борис", "вадим", "валентин", "валерий",
        "василий", "виктор", "виталий", "владимир", "владислав", "всеволод",
        "вячеслав", "геннадий", "георгий", "герман", "глеб", "григорий",
        "давид", "даниил", "денис", "дмитрий", "евгений", "егор", "иван",
        "игнат", "игорь", "илья", "иннокентий", "кирилл", "константин",
        "лев", "леонид", "максим", "марк", "матвей", "михаил", "никита",
        "николай", "олег", "павел", "пётр", "петр", "платон", "роман",
        "ростислав", "руслан", "савелий", "семён", "семен", "сергей",
        "станислав", "степан", "тарас", "тимофей", "тимур", "фёдор", "федор",
        "филипп", "эдуард", "юрий", "яков", "ярослав",
        "анна", "алина", "алла", "анастасия", "ангелина", "валентина",
        "валерия", "варвара", "вера", "вероника", "виктория", "галина",
        "дарья", "диана", "ева", "евгения", "екатерина", "елена", "елизавета",
        "жанна", "зинаида", "зоя", "инна", "ирина", "карина", "кира",
        "клавдия", "кристина", "ксения", "лариса", "лидия", "любовь",
        "людмила", "маргарита", "марина", "мария", "милана", "надежда",
        "наталья", "наталия", "нина", "оксана", "ольга", "полина", "раиса",
        "регина", "римма", "светлана", "софия", "софья", "таисия", "тамара",
        "татьяна", "ульяна", "эвелина", "эльвира", "эмма", "юлия", "яна",
    }
)

# Слова, которые не должны запускать эвристику ФИО. Список намеренно
# короткий: лучше замаскировать лишнее слово, чем пропустить имя.
NAME_STOPWORDS = frozenset(
    {
        "офис", "офисный", "офисная", "офисное", "мебель", "стол", "столы",
        "кресло", "кресла", "шкаф", "шкафы", "диван", "диваны", "стеллаж",
        "стеллажи", "тумба", "тумбы", "полка", "полки", "компания", "фирма",
        "организация", "предприятие", "москва", "петербург", "россия",
        "январь", "февраль", "март", "апрель", "май", "июнь", "июль",
        "август", "сентябрь", "октябрь", "ноябрь", "декабрь", "понедельник",
        "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье",
    }
)

SURNAME_SUFFIXES = (
    "ов", "ев", "ёв", "ова", "ева", "ёва", "ин", "ина", "ын", "ына",
    "ский", "ская", "цкий", "цкая", "енко", "ук", "юк", "ич", "вич",
    "ян", "дзе", "швили",
)

PATRONYMIC_SUFFIXES = (
    "ович", "евич", "иевич", "овна", "евна", "иевна", "ична", "инична",
)


def _luhn_ok(value: str) -> bool:
    digits = [int(char) for char in value if char.isdigit()]
    if not 16 <= len(digits) <= 19:
        return False
    total = 0
    for index, digit in enumerate(reversed(digits)):
        if index % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def _has_surname_suffix(word: str) -> bool:
    lowered = word.lower()
    return any(lowered.endswith(suffix) for suffix in SURNAME_SUFFIXES)


def _is_patronymic(word: str) -> bool:
    lowered = word.lower()
    return any(lowered.endswith(suffix) for suffix in PATRONYMIC_SUFFIXES)


def _looks_like_person(words: list[str]) -> bool:
    if any(word.lower() in NAME_STOPWORDS for word in words):
        return False
    if any(_is_patronymic(word) for word in words):
        return True
    if any(word.lower() in FIRST_NAMES for word in words):
        return True
    return len(words) >= 2 and any(_has_surname_suffix(word) for word in words)


def _replace_group(match: re.Match[str], group: int, token: str) -> str:
    """Заменяет только одну группу внутри совпадения, сохраняя остальной текст."""
    start, end = match.span(group)
    whole = match.group(0)
    return whole[: start - match.start()] + token + whole[end - match.start() :]


class Anonymizer:
    """Заменяет ПДн на токены и умеет возвращать исходные значения обратно."""

    def __init__(self, known_values: dict[str, str] | None = None):
        known = known_values or {}
        self._known = {
            "name": (known.get("name") or "").strip(),
            "phone": (known.get("phone") or "").strip(),
            "email": (known.get("email") or "").strip(),
        }

        self._values: dict[str, str] = {}
        self._by_key: dict[tuple[str, str], str] = {}
        self._by_kind: dict[str, dict[int, str]] = defaultdict(dict)
        self._counters: dict[str, int] = defaultdict(int)

        self._name_surfaces: list[tuple[str, str]] = []
        self._name_surface_keys: set[tuple[str, str]] = set()

        self._foreign_tokens: set[str] = set()
        self._prepared = False
        self._unresolved = 0
        self._last_masked = ""

    # --- публичный интерфейс -------------------------------------------------

    def mask(self, text: str) -> str:
        """Возвращает текст, в котором ПДн заменены на токены."""
        if not text:
            return text

        self._prepare(text)

        masked = EMAIL_RE.sub(
            lambda match: self._token_for("EMAIL", match.group(0)),
            text,
        )
        masked = self._mask_cards(masked)

        masked = self._mask_group(masked, PASSPORT_RE, "ПАСПОРТ", group=2)
        masked = self._mask_group(masked, PASSPORT_SERIES_RE, "ПАСПОРТ")
        masked = self._mask_group(masked, SNILS_RE, "СНИЛС")
        masked = self._mask_group(masked, INN_RE, "ИНН", group=2)
        masked = self._mask_group(masked, BARE_INN_RE, "ИНН")
        masked = self._mask_group(masked, BIRTH_DATE_RE, "ДАТА_РОЖДЕНИЯ", group=2)

        # Явные подписи — страховка для форматов, которые не поймали
        # регулярные выражения выше. Если в строке уже есть токен, не
        # «съедаем» его повторно: иначе получится токен внутри токена.
        masked = self._mask_labeled(masked, PASSPORT_LABEL_RE, "ПАСПОРТ")
        masked = self._mask_labeled(masked, SNILS_LABEL_RE, "СНИЛС")
        masked = self._mask_labeled(masked, INN_LABEL_RE, "ИНН")
        masked = self._mask_labeled(masked, BIRTH_LABEL_RE, "ДАТА_РОЖДЕНИЯ")
        masked = self._mask_labeled(masked, CARD_LABEL_RE, "КАРТА")

        masked = PHONE_RE.sub(
            lambda match: self._token_for("ТЕЛЕФОН", match.group(0)),
            masked,
        )
        masked = LONG_DIGITS_RE.sub(
            lambda match: self._token_for("ТЕЛЕФОН", match.group(0)),
            masked,
        )

        masked = self._mask_labeled(masked, ADDRESS_LABEL_RE, "АДРЕС")
        masked = self._mask_group(masked, ADDRESS_RE, "АДРЕС")
        masked = self._mask_group(masked, CITY_POSTAL_RE, "АДРЕС")
        masked = self._mask_companies(masked)

        # Сначала имена по контексту («меня зовут…», «контактное лицо:…»),
        # затем известное имя из формы — чтобы ФИО заменилось целиком.
        masked = self._mask_context_names(masked)
        masked = self._mask_known_names(masked)
        masked = self._mask_heuristic_names(masked)
        self._last_masked = masked
        return masked

    def restore(self, text: str) -> str:
        """Подставляет исходные значения вместо токенов."""
        if not text:
            return text

        def replace(match: re.Match[str]) -> str:
            nonlocal changed
            kind = KIND_CANON.get(match.group("kind").upper())
            if kind is None:
                return match.group(0)

            token = f"[{kind}_{int(match.group('num'))}]"
            if token in self._values:
                changed = True
                return self._values[token]

            # Токен был в исходном тексте, а не создан нами — не трогаем.
            if token in self._foreign_tokens:
                return match.group(0)

            self._unresolved += 1
            changed = True
            values = self._by_kind.get(kind, {})
            if len(values) == 1:
                return next(iter(values.values()))
            return NEUTRAL_VALUE.get(kind, "скрытые данные")

        # Значение одного токена может само содержать токен (например,
        # подпись «ИНН: …» захватила уже замаскированную карту). Повторяем
        # подстановку, пока в тексте есть что заменять.
        for _ in range(10):
            changed = False
            text = TOKEN_RE.sub(replace, text)
            text = LOOSE_TOKEN_RE.sub(replace, text)
            if not changed:
                break
        return text

    def restore_result(self, value: Any) -> Any:
        """Рекурсивно восстанавливает значения во всех строковых полях."""
        if isinstance(value, str):
            return self.restore(value)
        if isinstance(value, list):
            return [self.restore_result(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self.restore_result(item) for item in value)
        if isinstance(value, dict):
            return {
                key: self.restore_result(item)
                for key, item in value.items()
            }
        return value

    def stats(self) -> dict[str, int]:
        """Сколько уникальных сущностей каждого типа реально попало в текст."""
        counts: dict[str, int] = defaultdict(int)
        seen: set[str] = set()

        for match in TOKEN_RE.finditer(self._last_masked):
            kind = KIND_CANON.get(match.group("kind").upper())
            if kind is None:
                continue
            token = f"[{kind}_{int(match.group('num'))}]"
            if token in self._values and token not in seen:
                seen.add(token)
                counts[kind] += 1

        names = {
            "ИМЯ": "name",
            "ТЕЛЕФОН": "phone",
            "EMAIL": "email",
            "АДРЕС": "address",
            "ПАСПОРТ": "passport",
            "СНИЛС": "snils",
            "ИНН": "inn",
            "КАРТА": "card",
            "ДАТА_РОЖДЕНИЯ": "birth_date",
            "КОМПАНИЯ": "company",
        }
        return {label: counts.get(kind, 0) for kind, label in names.items()}

    @property
    def unresolved_count(self) -> int:
        return self._unresolved

    # --- подготовка и регистрация -------------------------------------------

    def _prepare(self, text: str) -> None:
        if self._prepared:
            return
        self._prepared = True

        # Если в исходном тексте уже есть что-то похожее на наш токен,
        # начинаем нумерацию с большего числа, чтобы не перепутать значения.
        for pattern in (TOKEN_RE, LOOSE_TOKEN_RE):
            for match in pattern.finditer(text):
                kind = KIND_CANON.get(match.group("kind").upper())
                if kind is None:
                    continue
                number = int(match.group("num"))
                self._counters[kind] = max(self._counters[kind], number)
                self._foreign_tokens.add(f"[{kind}_{number}]")

        if self._known["name"]:
            self._add_name_surfaces(self._known["name"])
        if self._known["phone"]:
            self._token_for("ТЕЛЕФОН", self._known["phone"])
        if self._known["email"]:
            self._token_for("EMAIL", self._known["email"])

    def _add_name_surfaces(self, value: str) -> None:
        value = " ".join(value.split())
        if not value:
            return

        token = self._token_for("ИМЯ", value)
        self._remember_name_surface(value, token)

        for part in value.split():
            part = part.strip(",")
            if len(part) < 2:
                continue
            part_token = self._token_for("ИМЯ", part)
            self._remember_name_surface(part, part_token)

    def _remember_name_surface(self, surface: str, token: str) -> None:
        key = (self._norm_name(surface), token)
        if key in self._name_surface_keys:
            return
        self._name_surface_keys.add(key)
        self._name_surfaces.append((surface, token))

    def _token_for(self, kind: str, value: str) -> str:
        value = (value or "").strip()
        key = self._key(kind, value)
        if not key:
            return value

        existing = self._by_key.get((kind, key))
        if existing:
            return existing

        self._counters[kind] += 1
        token = f"[{kind}_{self._counters[kind]}]"
        while token in self._foreign_tokens:
            self._counters[kind] += 1
            token = f"[{kind}_{self._counters[kind]}]"

        self._by_key[(kind, key)] = token
        self._values[token] = value
        self._by_kind[kind][self._counters[kind]] = value
        return token

    # --- замена -----------------------------------------------------------------

    def _mask_labeled(
        self,
        text: str,
        pattern: re.Pattern[str],
        kind: str,
    ) -> str:
        def replace(match: re.Match[str]) -> str:
            value = match.group(2).strip()
            if not value or TOKEN_RE.search(value):
                return match.group(0)
            return match.group(1) + self._token_for(kind, value)

        return pattern.sub(replace, text)

    def _mask_group(
        self,
        text: str,
        pattern: re.Pattern[str],
        kind: str,
        *,
        group: int = 0,
    ) -> str:
        def replace(match: re.Match[str]) -> str:
            value = match.group(group)
            if not value:
                return match.group(0)
            token = self._token_for(kind, value)
            if group == 0:
                return token
            return _replace_group(match, group, token)

        return pattern.sub(replace, text)

    def _mask_cards(self, text: str) -> str:
        def replace(match: re.Match[str]) -> str:
            value = match.group(0)
            if not _luhn_ok(value):
                return value
            return self._token_for("КАРТА", value)

        return CARD_RE.sub(replace, text)

    def _mask_companies(self, text: str) -> str:
        def replace(match: re.Match[str]) -> str:
            return match.group("prefix") + self._token_for(
                "КОМПАНИЯ", match.group("name")
            )

        return COMPANY_RE.sub(replace, text)

    # --- замена имён ------------------------------------------------------------

    def _mask_context_names(self, text: str) -> str:
        def replace(match: re.Match[str]) -> str:
            raw_name = " ".join(match.group("name").split())
            token = self._token_for("ИМЯ", raw_name)
            self._add_name_surfaces(raw_name)
            return match.group("prefix") + token

        return NAME_CONTEXT_RE.sub(replace, text)

    def _mask_known_names(self, text: str) -> str:
        # Длинные варианты первыми: «Анна Смирнова» раньше, чем «Анна».
        for surface, token in sorted(
            self._name_surfaces,
            key=lambda item: len(item[0]),
            reverse=True,
        ):
            text = self._name_pattern(surface).sub(token, text)
        return text

    def _mask_heuristic_names(self, text: str) -> str:
        def fio_replace(match: re.Match[str]) -> str:
            value = match.group(0)
            if not _looks_like_person(value.split()):
                return value
            return self._token_for("ИМЯ", value)

        text = FIO_RE.sub(fio_replace, text)

        def single_replace(match: re.Match[str]) -> str:
            value = match.group(1)
            lowered = value.lower()
            if lowered not in FIRST_NAMES or lowered in NAME_STOPWORDS:
                return value
            return self._token_for("ИМЯ", value)

        return SINGLE_NAME_RE.sub(single_replace, text)

    @staticmethod
    def _name_pattern(surface: str) -> re.Pattern[str]:
        escaped = re.escape(surface).replace(r"\ ", r"\s+")
        return re.compile(
            rf"(?<![\w-]){escaped}(?![\w-])",
            re.IGNORECASE,
        )

    # --- нормализация ключей ----------------------------------------------------

    @staticmethod
    def _norm_email(value: str) -> str:
        return value.strip().casefold()

    @staticmethod
    def _norm_phone(value: str) -> str:
        digits = re.sub(r"\D", "", value or "")
        if len(digits) == 11 and digits[0] in "78":
            digits = "7" + digits[1:]
        elif len(digits) == 10:
            digits = "7" + digits
        return digits if len(digits) >= 10 else ""

    @staticmethod
    def _norm_name(value: str) -> str:
        return " ".join((value or "").split()).casefold().replace("ё", "е")

    @staticmethod
    def _norm_digits(value: str) -> str:
        return re.sub(r"\D", "", value or "")

    def _key(self, kind: str, value: str) -> str:
        if kind == "EMAIL":
            return self._norm_email(value)
        if kind == "ТЕЛЕФОН":
            return self._norm_phone(value)
        if kind == "ИМЯ":
            return self._norm_name(value)
        if kind in {"ПАСПОРТ", "СНИЛС", "ИНН", "КАРТА", "ДАТА_РОЖДЕНИЯ"}:
            return self._norm_digits(value) or value.casefold()
        if kind in {"АДРЕС", "КОМПАНИЯ"}:
            return " ".join(value.split()).casefold().replace("ё", "е")
        return value.casefold()
