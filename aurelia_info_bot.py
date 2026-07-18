"""
АУРЕЛИЯ INFO BOT
Telegram-бот для каталога стран вселенной Аурелия.

Запуск:
    pip install -r requirements.txt
    export BOT_TOKEN="ваш_токен_от_BotFather"
    python aurelia_info_bot.py

Хранение данных: локальный файл data.json (создаётся автоматически рядом со скриптом).

Регионы:
    /addregion  - добавить регион выбранной стране;
    /editregion - переименовать регион, заменить его флаг или удалить регион.
"""

import json
import io
import logging
import os
import random
import re
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime
from decimal import Decimal, InvalidOperation
from html import escape
from uuid import uuid4
from zipfile import BadZipFile, ZipFile

from pypdf import PdfReader
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    InputFile,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# ---------------------------------------------------------------------------
# Настройки
# ---------------------------------------------------------------------------

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("aurelia_bot")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "PASTE_YOUR_TOKEN_HERE")
MAIN_ADMIN_ID = 7787565361

# Версии ведём в формате MAJOR.MINOR.PATCH
BOT_VERSION = "1.7.0"

DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.json")
STICKER_SET_NAME = "AureliaPack"
LIST_PAGE_SIZE = 8

# ---------------------------------------------------------------------------
# Хранилище данных (простой JSON-файл)
# ---------------------------------------------------------------------------

def normalize_user_text(text: str) -> str:
    if text is None:
        return text
    text = text.replace("\u2013", "-").replace("\u2014", "-")
    for quote in ("\u00ab", "\u00bb", "\u201c", "\u201d", "\u201e", "\u201f"):
        text = text.replace(quote, '"')
    return text


def normalize_area_value(text: str) -> str:
    value = normalize_user_text(text or "").strip().casefold()
    for suffix in ("км²", "км2", "кв. км", "кв км"):
        if value.endswith(suffix):
            value = value[:-len(suffix)].strip()
            break
    value = value.replace("\u00a0", "").replace(" ", "")
    if not re.fullmatch(r"\d+(?:[.,]\d+)?", value):
        raise ValueError("Площадь нужно написать числом")
    try:
        if Decimal(value.replace(",", ".")) <= 0:
            raise ValueError("Площадь должна быть больше нуля")
    except InvalidOperation as error:
        raise ValueError("Площадь нужно написать числом") from error
    return value.replace(".", ",")


def parse_borders_value(text: str):
    value = normalize_user_text(text or "").strip()
    if value.casefold() in ("нет", "-", "no"):
        return []
    borders = []
    used_names = set()
    for item in re.split(r"[,;\n]+", value):
        name = item.strip()
        if name and name.casefold() not in used_names:
            used_names.add(name.casefold())
            borders.append(name)
    return borders


def clean_journal_text(lines) -> str:
    text = "\n".join(lines).strip()
    return re.sub(r"\n{3,}", "\n\n", text)


FOUR_DIGIT_YEAR_TOKEN = r"(?:1\d{3}|20\d{2})"
EVENT_YEAR_TOKEN = FOUR_DIGIT_YEAR_TOKEN
RUSSIAN_MONTH_PATTERN = (
    r"(?:январ(?:ь|я)|феврал(?:ь|я)|март(?:а)?|апрел(?:ь|я)|май|мая|"
    r"июн(?:ь|я)|июл(?:ь|я)|август(?:а)?|сентябр(?:ь|я)|"
    r"октябр(?:ь|я)|ноябр(?:ь|я)|декабр(?:ь|я))"
)
EVENT_DATE_PATTERNS = (
    re.compile(
        rf"\b\d{{1,2}}\s+{RUSSIAN_MONTH_PATTERN}\s*(?:-|по)\s*"
        rf"\d{{1,2}}\s+{RUSSIAN_MONTH_PATTERN}\s+"
        rf"{EVENT_YEAR_TOKEN}(?:\s*г(?:ода|\.)?)?\b",
        flags=re.IGNORECASE,
    ),
    re.compile(
        rf"\b\d{{1,2}}\s*(?:-|по)\s*\d{{1,2}}\s+"
        rf"{RUSSIAN_MONTH_PATTERN}\s+{EVENT_YEAR_TOKEN}"
        r"(?:\s*г(?:ода|\.)?)?\b",
        flags=re.IGNORECASE,
    ),
    re.compile(
        rf"\b\d{{1,2}}\s+{RUSSIAN_MONTH_PATTERN}\s+"
        rf"{EVENT_YEAR_TOKEN}(?:\s*г(?:ода|\.)?)?\b",
        flags=re.IGNORECASE,
    ),
    re.compile(
        rf"\b\d{{1,2}}[./]\d{{1,2}}[./]{EVENT_YEAR_TOKEN}\b"
    ),
    re.compile(
        rf"\b{RUSSIAN_MONTH_PATTERN}\s+{EVENT_YEAR_TOKEN}"
        r"(?:\s*г(?:ода|\.)?)?\b",
        flags=re.IGNORECASE,
    ),
    re.compile(
        rf"\b{EVENT_YEAR_TOKEN}\s*-\s*{EVENT_YEAR_TOKEN}"
        r"(?:\s*(?:годы|гг?\.))?\b",
        flags=re.IGNORECASE,
    ),
    re.compile(
        rf"\b{FOUR_DIGIT_YEAR_TOKEN}"
        r"(?:\s*г(?:од(?:а|у|ом)?|\.))?\b",
        flags=re.IGNORECASE,
    ),
)


def extract_event_date(text: str) -> str:
    compact = " ".join(normalize_user_text(text or "").split())
    if not compact:
        return ""
    best_match = None
    for priority, pattern in enumerate(EVENT_DATE_PATTERNS):
        match = pattern.search(compact)
        if not match:
            continue
        candidate = (match.start(), priority, match.group(0))
        if best_match is None or candidate[:2] < best_match[:2]:
            best_match = candidate
    if not best_match:
        return ""
    found_date = best_match[2].strip(" ,;:.")
    if re.fullmatch(
        rf"{EVENT_YEAR_TOKEN}\s*г(?:од(?:а|у|ом)?|\.)?",
        found_date,
        flags=re.IGNORECASE,
    ):
        year_match = re.search(EVENT_YEAR_TOKEN, found_date)
        return year_match.group(0) if year_match else found_date
    return found_date


def event_date(event: dict) -> str:
    if not event:
        return ""
    stored_date = normalize_user_text(str(event.get("date") or "").strip())
    if stored_date:
        return stored_date
    return extract_event_date(
        f"{event.get('title') or ''}\n{event.get('text') or ''}"
    )


def event_title_with_date(event: dict) -> str:
    title = normalize_user_text(str(event.get("title") or "").strip())
    found_date = event_date(event)
    if not found_date or found_date.casefold() in title.casefold():
        return title
    return f"{found_date} - {title}"


def merge_event_histories(target, incoming):
    known_titles = {
        str(history.get("title") or "").strip().casefold()
        for history in target
        if isinstance(history, dict)
    }
    known_ids = {
        str(history.get("id") or "").strip()
        for history in target
        if isinstance(history, dict)
    }
    for history in incoming:
        title_key = str(history.get("title") or "").strip().casefold()
        if not title_key or title_key in known_titles:
            continue
        history_id = str(history.get("id") or "").strip()
        while not history_id or history_id in known_ids:
            history_id = uuid4().hex[:12]
        history["id"] = history_id
        known_ids.add(history_id)
        known_titles.add(title_key)
        target.append(history)


def normalize_event_histories(raw_histories):
    if not isinstance(raw_histories, list):
        return []
    histories = []
    used_history_ids = set()
    for raw_history in raw_histories:
        if not isinstance(raw_history, dict):
            continue
        title = normalize_user_text(str(raw_history.get("title") or "").strip())
        if not title:
            continue
        history_id = str(raw_history.get("id") or "").strip()
        while not history_id or history_id in used_history_ids:
            history_id = uuid4().hex[:12]
        used_history_ids.add(history_id)

        major_events = []
        used_major_ids = set()
        for raw_major in raw_history.get("major_events", []):
            if not isinstance(raw_major, dict):
                continue
            major_title = normalize_user_text(
                str(raw_major.get("title") or "").strip()
            )
            if not major_title:
                continue
            major_id = str(raw_major.get("id") or "").strip()
            while not major_id or major_id in used_major_ids:
                major_id = uuid4().hex[:12]
            used_major_ids.add(major_id)

            subevents = []
            used_subevent_ids = set()
            for raw_subevent in raw_major.get("subevents", []):
                if not isinstance(raw_subevent, dict):
                    continue
                subevent_title = normalize_user_text(
                    str(raw_subevent.get("title") or "").strip()
                )
                if not subevent_title:
                    continue
                subevent_id = str(raw_subevent.get("id") or "").strip()
                while not subevent_id or subevent_id in used_subevent_ids:
                    subevent_id = uuid4().hex[:12]
                used_subevent_ids.add(subevent_id)
                subevent_text = normalize_user_text(
                    str(raw_subevent.get("text") or "").strip()
                )
                subevent_date = normalize_user_text(
                    str(raw_subevent.get("date") or "").strip()
                ) or extract_event_date(
                    f"{subevent_title}\n{subevent_text}"
                )
                subevents.append(
                    {
                        "id": subevent_id,
                        "title": subevent_title,
                        "text": subevent_text,
                        "date": subevent_date,
                    }
                )
            major_text = normalize_user_text(
                str(raw_major.get("text") or "").strip()
            )
            major_date = normalize_user_text(
                str(raw_major.get("date") or "").strip()
            ) or extract_event_date(f"{major_title}\n{major_text}")
            major_events.append(
                {
                    "id": major_id,
                    "title": major_title,
                    "text": major_text,
                    "date": major_date,
                    "subevents": subevents,
                }
            )
        if not major_events:
            continue
        histories.append(
            {
                "id": history_id,
                "title": title,
                "intro": normalize_user_text(
                    str(raw_history.get("intro") or "").strip()
                ),
                "source_filename": normalize_user_text(
                    str(raw_history.get("source_filename") or "").strip()
                ),
                "source_file_id": raw_history.get("source_file_id"),
                "uploaded_at": raw_history.get("uploaded_at"),
                "parser_mode": (
                    raw_history.get("parser_mode")
                    if raw_history.get("parser_mode") in ("markers", "automatic")
                    else "markers"
                ),
                "major_events": major_events,
            }
        )
    return histories


def normalize_lore_countries(raw_lore_countries):
    if isinstance(raw_lore_countries, dict):
        converted = []
        for name, value in raw_lore_countries.items():
            if isinstance(value, dict):
                entry = dict(value)
                entry.setdefault("name", name)
            else:
                entry = {"name": name, "event_histories": value}
            converted.append(entry)
        raw_lore_countries = converted
    if not isinstance(raw_lore_countries, list):
        return []

    lore_countries = []
    by_name = {}
    used_ids = set()
    for raw_entry in raw_lore_countries:
        if not isinstance(raw_entry, dict):
            continue
        name = normalize_user_text(str(raw_entry.get("name") or "").strip())
        histories = normalize_event_histories(
            raw_entry.get("event_histories", [])
        )
        if not name or not histories:
            continue
        name_key = name.casefold()
        if name_key in by_name:
            existing_entry = by_name[name_key]
            merge_event_histories(
                existing_entry["event_histories"], histories
            )
            if not existing_entry.get("linked_country_id"):
                existing_entry["linked_country_id"] = (
                    str(raw_entry.get("linked_country_id") or "").strip()
                    or None
                )
            continue
        lore_id = str(raw_entry.get("id") or "").strip()
        while not lore_id or lore_id in used_ids:
            lore_id = uuid4().hex[:12]
        used_ids.add(lore_id)
        entry = {
            "id": lore_id,
            "name": name,
            "linked_country_id": (
                str(raw_entry.get("linked_country_id") or "").strip() or None
            ),
            "event_histories": histories,
        }
        by_name[name_key] = entry
        lore_countries.append(entry)
    return lore_countries


def load_data(data_file=None):
    data_file = data_file or DATA_FILE
    if os.path.exists(data_file):
        with open(data_file, "r", encoding="utf-8") as f:
            d = json.load(f)
            if not isinstance(d, dict):
                raise ValueError("Корень резервной копии должен быть JSON-объектом")
            if "admins" in d and not isinstance(d["admins"], list):
                raise ValueError("Поле admins должно быть списком")
            if "countries" in d and not isinstance(d["countries"], dict):
                raise ValueError("Поле countries должно быть объектом")
            if "flag_library" in d and not isinstance(
                d["flag_library"], (list, dict)
            ):
                raise ValueError("Поле flag_library должно быть списком или объектом")
            if "lore_countries" in d and not isinstance(
                d["lore_countries"], (list, dict)
            ):
                raise ValueError("Поле lore_countries должно быть списком или объектом")
            d.setdefault("admins", [])
            d.setdefault("countries", {})
            d.setdefault("flag_library", [])
            d.setdefault("lore_countries", [])

            normalized_admins = []
            for admin_id in d["admins"]:
                try:
                    admin_id = int(admin_id)
                except (TypeError, ValueError):
                    continue
                if admin_id not in normalized_admins and admin_id != MAIN_ADMIN_ID:
                    normalized_admins.append(admin_id)
            d["admins"] = normalized_admins

            # Сразу приводим старые тексты к обычному дефису и прямым
            # кавычкам, чтобы старое оформление не возвращалось из data.json.
            normalized_countries = {}
            for stored_name, country in d["countries"].items():
                if not isinstance(country, dict):
                    continue
                country_name = normalize_user_text(
                    str(country.get("name") or stored_name).strip()
                )
                country["name"] = country_name
                for field in ("leader", "capital", "description", "area_km2"):
                    if isinstance(country.get(field), str):
                        country[field] = normalize_user_text(country[field])
                if country.get("flag_file_id"):
                    country["flag_media_type"] = (
                        country.get("flag_media_type") or "document"
                    )
                for field in ("continents", "lore_links", "borders"):
                    if isinstance(country.get(field), list):
                        country[field] = [
                            normalize_user_text(item) if isinstance(item, str) else item
                            for item in country[field]
                        ]
                country["area_km2"] = str(country.get("area_km2") or "").strip()
                raw_borders = country.get("borders", [])
                if isinstance(raw_borders, str):
                    raw_borders = parse_borders_value(raw_borders)
                country["borders"] = raw_borders if isinstance(raw_borders, list) else []
                country["event_histories"] = normalize_event_histories(
                    country.get("event_histories", [])
                )
                normalized_countries[country_name] = country
            d["countries"] = normalized_countries

            # Совместимость со старыми data.json: раньше поля regions не было.
            # Также понимаем удобный сокращённый формат {"Название": "file_id"},
            # если кто-то добавлял регионы в JSON вручную.
            used_country_ids = set()
            for country in d["countries"].values():
                country_id = str(country.get("id", "")).strip()
                while not country_id or country_id in used_country_ids:
                    country_id = uuid4().hex[:12]
                used_country_ids.add(country_id)
                country["id"] = country_id

                raw_regions = country.get("regions", [])
                if isinstance(raw_regions, dict):
                    raw_regions = [
                        {"name": name, "flag_file_id": flag_file_id}
                        for name, flag_file_id in raw_regions.items()
                    ]
                if not isinstance(raw_regions, list):
                    raw_regions = []

                regions = []
                used_ids = set()
                for region in raw_regions:
                    if not isinstance(region, dict):
                        continue
                    name = normalize_user_text(str(region.get("name", "")).strip())
                    if not name:
                        continue
                    region_id = str(region.get("id", "")).strip()
                    while not region_id or region_id in used_ids:
                        region_id = uuid4().hex[:12]
                    used_ids.add(region_id)
                    regions.append(
                        {
                            "id": region_id,
                            "name": name,
                            "capital": normalize_user_text(
                                str(region.get("capital") or "").strip()
                            ),
                            "description": normalize_user_text(
                                str(region.get("description") or "").strip()
                            ),
                            "flag_file_id": region.get("flag_file_id"),
                            "flag_media_type": region.get("flag_media_type")
                            or (
                                "document"
                                if region.get("flag_file_id")
                                else None
                            ),
                        }
                    )
                country["regions"] = regions

            lore_countries = normalize_lore_countries(
                d.get("lore_countries", [])
            )
            country_by_id = {
                country["id"]: country for country in d["countries"].values()
            }
            lore_by_id = {entry["id"]: entry for entry in lore_countries}
            linked_country_ids = set()

            for lore_country in lore_countries:
                linked_country_id = lore_country.get("linked_country_id")
                if (
                    linked_country_id not in country_by_id
                    or linked_country_id in linked_country_ids
                ):
                    lore_country["linked_country_id"] = None
                    continue
                linked_country_ids.add(linked_country_id)
                country_by_id[linked_country_id]["lore_country_id"] = (
                    lore_country["id"]
                )

            for country in d["countries"].values():
                requested_lore_id = str(
                    country.get("lore_country_id") or ""
                ).strip()
                requested_lore = lore_by_id.get(requested_lore_id)
                if not requested_lore:
                    country["lore_country_id"] = None
                    continue
                linked_country_id = requested_lore.get("linked_country_id")
                if linked_country_id in (None, country["id"]):
                    requested_lore["linked_country_id"] = country["id"]
                    country["lore_country_id"] = requested_lore["id"]
                    linked_country_ids.add(country["id"])
                else:
                    country["lore_country_id"] = None

            for country in d["countries"].values():
                legacy_histories = country.get("event_histories", [])
                if not legacy_histories:
                    country["event_histories"] = []
                    continue
                lore_country = lore_by_id.get(country.get("lore_country_id"))
                if not lore_country:
                    lore_country = next(
                        (
                            entry
                            for entry in lore_countries
                            if entry["name"].casefold()
                            == country["name"].casefold()
                            and not entry.get("linked_country_id")
                        ),
                        None,
                    )
                if not lore_country:
                    lore_country = {
                        "id": uuid4().hex[:12],
                        "name": country["name"],
                        "linked_country_id": None,
                        "event_histories": [],
                    }
                    lore_countries.append(lore_country)
                    lore_by_id[lore_country["id"]] = lore_country
                merge_event_histories(
                    lore_country["event_histories"], legacy_histories
                )
                lore_country["linked_country_id"] = country["id"]
                country["lore_country_id"] = lore_country["id"]
                country["event_histories"] = []

            d["lore_countries"] = lore_countries

            raw_library = d.get("flag_library", [])
            if isinstance(raw_library, dict):
                raw_library = [
                    {"country_name": name, "flag_file_id": file_id}
                    for name, file_id in raw_library.items()
                ]
            if not isinstance(raw_library, list):
                raw_library = []

            flag_library = []
            used_flag_ids = set()
            for entry in raw_library:
                if not isinstance(entry, dict) or not entry.get("flag_file_id"):
                    continue
                country_name = normalize_user_text(
                    str(entry.get("country_name") or entry.get("name") or "").strip()
                )
                if not country_name:
                    continue
                entry_id = str(entry.get("id", "")).strip()
                while not entry_id or entry_id in used_flag_ids:
                    entry_id = uuid4().hex[:12]
                used_flag_ids.add(entry_id)
                flag_library.append(
                    {
                        "id": entry_id,
                        "country_name": country_name,
                        "flag_file_id": entry["flag_file_id"],
                        "flag_media_type": (
                            entry.get("flag_media_type") or "document"
                        ),
                        "flag_type": (
                            entry.get("flag_type")
                            if entry.get("flag_type") in ("country", "region")
                            else "country"
                        ),
                        "parent_flag_id": entry.get("parent_flag_id"),
                        "country_id": entry.get("country_id"),
                        "region_id": entry.get("region_id"),
                    }
                )
            d["flag_library"] = flag_library
            return d
    return {
        "admins": [],
        "countries": {},
        "flag_library": [],
        "lore_countries": [],
    }


def save_data():
    file_descriptor, temporary_path = tempfile.mkstemp(
        dir=os.path.dirname(DATA_FILE), prefix=".aurelia_data_", suffix=".tmp"
    )
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as temporary_file:
            json.dump(DATA, temporary_file, ensure_ascii=False, indent=2)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, DATA_FILE)
    finally:
        if os.path.exists(temporary_path):
            os.unlink(temporary_path)


def load_backup_payload(payload: bytes):
    try:
        raw_data = json.loads(payload.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("Файл не похож на корректную JSON-копию") from error

    file_descriptor, temporary_path = tempfile.mkstemp(
        prefix="aurelia_restore_", suffix=".json"
    )
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as temporary_file:
            json.dump(raw_data, temporary_file, ensure_ascii=False)
        restored_data = load_data(temporary_path)
        for country_name, country in restored_data["countries"].items():
            if not country_name:
                raise ValueError("В копии нашлась страна без названия")
            if not country.get("card_file_id"):
                raise ValueError(
                    f'У страны "{country_name}" нет файла карточки'
                )
            if not country.get("flag_file_id"):
                raise ValueError(f'У страны "{country_name}" нет флага')
        return restored_data
    finally:
        if os.path.exists(temporary_path):
            os.unlink(temporary_path)


DATA = load_data()


def is_admin(user_id: int) -> bool:
    return user_id == MAIN_ADMIN_ID or user_id in DATA["admins"]


# ---------------------------------------------------------------------------
# Состояния диалогов
# ---------------------------------------------------------------------------

(
    ADD_CARD,
    ADD_NAME,
    ADD_LEADER,
    ADD_CAPITAL,
    ADD_CONTINENT,
    ADD_AREA,
    ADD_BORDERS,
    ADD_FLAG,
    ADD_HERB,
    ADD_DESC,
    ADD_LORE,
    ADD_LINK_HISTORY,
) = range(12)

SEARCH_COUNTRY_QUERY = 12

(EDIT_CHOOSE_COUNTRY, EDIT_CHOOSE_FIELD, EDIT_VALUE) = range(20, 23)

(
    ADD_REGION_CHOOSE_COUNTRY,
    ADD_REGION_NAME,
    ADD_REGION_CAPITAL,
    ADD_REGION_DESC,
    ADD_REGION_FLAG,
) = range(30, 35)

(
    EDIT_REGION_CHOOSE_COUNTRY,
    EDIT_REGION_CHOOSE_REGION,
    EDIT_REGION_CHOOSE_FIELD,
    EDIT_REGION_VALUE,
    EDIT_REGION_CONFIRM_DELETE,
) = range(40, 45)

(
    ADD_LIBRARY_FLAG_TYPE,
    ADD_LIBRARY_FLAG_NAME,
    ADD_LIBRARY_FLAG_REGION_PARENT,
    ADD_LIBRARY_FLAG_REGION_NAME,
    ADD_LIBRARY_FLAG_FILE,
) = range(50, 55)

(
    EDIT_LIBRARY_FLAG_CHOOSE,
    EDIT_LIBRARY_FLAG_FIELD,
    EDIT_LIBRARY_FLAG_VALUE,
    EDIT_LIBRARY_FLAG_CONFIRM_DELETE,
) = range(60, 64)

(RESTORE_BACKUP_FILE, RESTORE_BACKUP_CONFIRM) = range(70, 72)

(
    ADD_HISTORY_CHOOSE_COUNTRY,
    ADD_HISTORY_NEW_COUNTRY_NAME,
    ADD_HISTORY_FILE,
) = range(80, 83)

EVENT_SEARCH_QUERY = 90
EVENT_SEARCH_LIMIT = 300

EDIT_FIELDS = {
    "name": "Название",
    "leader": "Лидер",
    "capital": "Столица",
    "continents": "Континент(ы)",
    "area_km2": "Площадь",
    "borders": "Границы",
    "card": "Карточка (картинка)",
    "flag": "Флаг",
    "herb": "Герб",
    "description": "Описание",
    "lore_links": "Ссылки на лор",
}

# ---------------------------------------------------------------------------
# Вспомогательные функции для клавиатур и вывода
# ---------------------------------------------------------------------------

def paginate_items(items, page: int):
    total_pages = max(1, (len(items) + LIST_PAGE_SIZE - 1) // LIST_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * LIST_PAGE_SIZE
    return items[start:start + LIST_PAGE_SIZE], page, total_pages


def pagination_row(page: int, total_pages: int, callback_prefix: str):
    row = []
    if page > 0:
        row.append(
            InlineKeyboardButton(
                "⬅️ Назад", callback_data=f"{callback_prefix}:{page - 1}"
            )
        )
    if page + 1 < total_pages:
        row.append(
            InlineKeyboardButton(
                "Дальше ➡️", callback_data=f"{callback_prefix}:{page + 1}"
            )
        )
    return row


def paginated_list_text(text: str, page: int, total_pages: int) -> str:
    if total_pages <= 1:
        return text
    return f"{text}\n\nСтраница {page + 1} из {total_pages}"


MAJOR_EVENT_PATTERN = re.compile(r"^\s*\[([^\[\]\r\n]+)\]\s*(.*)$")
YEAR_PATTERN = re.compile(rf"\b{EVENT_YEAR_TOKEN}\b")


def extract_docx_text(payload: bytes) -> str:
    try:
        with ZipFile(io.BytesIO(payload)) as archive:
            document_xml = archive.read("word/document.xml")
    except (BadZipFile, KeyError) as error:
        raise ValueError("Этот DOCX не получилось открыть") from error

    try:
        root = ET.fromstring(document_xml)
    except ET.ParseError as error:
        raise ValueError("Внутри DOCX сломана структура документа") from error

    namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    paragraphs = []
    for paragraph in root.iter(f"{namespace}p"):
        parts = []
        for node in paragraph.iter():
            if node.tag == f"{namespace}t":
                parts.append(node.text or "")
            elif node.tag == f"{namespace}tab":
                parts.append("\t")
            elif node.tag in (f"{namespace}br", f"{namespace}cr"):
                parts.append("\n")
        paragraphs.append("".join(parts))
    # Word хранит каждый абзац отдельным узлом. Двойной перенос сохраняет эту
    # структуру и помогает отличить короткий заголовок от следующего текста.
    return "\n\n".join(paragraphs)


def extract_txt_text(payload: bytes) -> str:
    encodings = ["utf-8-sig"]
    if payload.startswith((b"\xff\xfe", b"\xfe\xff")):
        encodings.insert(0, "utf-16")
    encodings.append("cp1251")
    for encoding in encodings:
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("Не получилось определить кодировку TXT")


def extract_pdf_text(payload: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(payload))
    except Exception as error:
        raise ValueError("Этот PDF не получилось открыть") from error

    paragraphs = []
    for page in reader.pages:
        try:
            page_text = page.extract_text(extraction_mode="layout") or ""
        except Exception:
            page_text = page.extract_text() or ""
        for block in re.split(r"\n\s*\n", page_text):
            lines = [" ".join(line.split()) for line in block.splitlines()]
            paragraph = " ".join(line for line in lines if line).strip()
            if paragraph:
                paragraphs.append(paragraph)
    if not paragraphs:
        raise ValueError("В PDF не нашлось читаемого текста")
    return "\n\n".join(paragraphs)


def extract_history_text(filename: str, payload: bytes) -> str:
    extension = os.path.splitext(filename or "")[1].casefold()
    if extension == ".docx":
        return extract_docx_text(payload)
    if extension == ".txt":
        return extract_txt_text(payload)
    if extension == ".md":
        return extract_txt_text(payload)
    if extension == ".pdf":
        return extract_pdf_text(payload)
    raise ValueError("Поддерживаются файлы DOCX, TXT, MD и PDF")


def strip_journal_markers(text: str) -> str:
    text = normalize_user_text(text or "")
    return (
        text.replace("[", "")
        .replace("]", "")
        .replace("{", "")
        .replace("}", "")
        .strip()
    )


def marker_title_looks_like_heading(title: str) -> bool:
    title = strip_journal_markers(title)
    letters = [char for char in title if char.isalpha()]
    uppercase_ratio = (
        sum(char.isupper() for char in letters) / len(letters)
        if letters
        else 0
    )
    return uppercase_ratio >= 0.7 or (
        bool(YEAR_PATTERN.search(title)) and (":" in title or "-" in title)
    )


def prepare_journal_lines(text: str):
    prepared = []
    for raw_line in normalize_user_text(text or "").replace("\r", "").split("\n"):
        segments = re.sub(r"\]\s+(?=\[)", "]\n", raw_line).split("\n")
        for segment in segments:
            trailing_marker = re.search(
                r"\s+(\[([^\[\]\n]{1,200})\])\s*$", segment
            )
            if trailing_marker and trailing_marker.start() > 0:
                prefix = segment[:trailing_marker.start()].rstrip()
                marker_title = trailing_marker.group(2)
                if prefix.endswith("}") or marker_title_looks_like_heading(
                    marker_title
                ):
                    prepared.append(prefix)
                    prepared.append(trailing_marker.group(1))
                    continue
            prepared.append(segment.rstrip())
    return prepared


def tokenize_structured_journal(text: str):
    lines = prepare_journal_lines(text)
    tokens = []
    index = 0
    while index < len(lines):
        line = lines[index]
        index += 1
        stripped = line.strip()
        major_match = MAJOR_EVENT_PATTERN.match(stripped)
        if major_match:
            title = major_match.group(1).strip()
            trailing_text = major_match.group(2).strip()
            if not trailing_text or marker_title_looks_like_heading(title):
                tokens.append(("major", title))
                if trailing_text:
                    lines.insert(index, trailing_text)
                continue

        if stripped.startswith("{"):
            content_parts = []
            current_part = stripped[1:]
            while True:
                if "}" in current_part:
                    before_close, after_close = current_part.split("}", 1)
                    content_parts.append(before_close)
                    if after_close.strip():
                        lines.insert(index, after_close.strip())
                    break
                content_parts.append(current_part)
                if index >= len(lines):
                    break
                possible_next = lines[index]
                next_major = MAJOR_EVENT_PATTERN.match(possible_next.strip())
                if next_major and not next_major.group(2).strip():
                    break
                current_part = possible_next.strip()
                index += 1
            tokens.append(("curly", clean_journal_text(content_parts)))
            continue

        tokens.append(("text", line))
    return tokens


def curly_block_is_title(content: str) -> bool:
    compact = " ".join((content or "").split())
    return (
        bool(compact)
        and len(compact) <= 160
        and len(compact.split()) <= 18
        and not compact.endswith((".", "!", "?"))
    )


def derive_event_title(text: str, fallback="Событие") -> str:
    compact = " ".join(strip_journal_markers(text).split())
    if not compact:
        return fallback
    first_sentence = re.split(r"(?<=[.!?])\s+", compact, maxsplit=1)[0]
    first_clause = re.split(r"[,;]", first_sentence, maxsplit=1)[0].strip()
    candidate = (
        first_clause
        if YEAR_PATTERN.search(first_clause) and len(first_clause) <= 100
        else first_sentence
    )
    if len(candidate) > 100:
        candidate = f"{candidate[:97].rstrip()}..."
    return candidate or fallback


def parse_structured_journal(tokens):
    intro_lines = []
    major_events = []
    current_major = None
    current_subevent = None

    for token_type, token_value in tokens:
        if token_type == "major":
            title = strip_journal_markers(token_value)
            if not title:
                continue
            current_major = {
                "id": uuid4().hex[:12],
                "title": title[:200],
                "_lines": [],
                "subevents": [],
            }
            major_events.append(current_major)
            current_subevent = None
            continue

        if token_type == "curly":
            if not current_major:
                intro_lines.append(strip_journal_markers(token_value))
                continue
            clean_content = strip_journal_markers(token_value)
            if not clean_content:
                continue
            if curly_block_is_title(clean_content):
                current_subevent = {
                    "id": uuid4().hex[:12],
                    "title": clean_content[:200],
                    "_lines": [],
                    "_title_only": True,
                }
            else:
                current_subevent = {
                    "id": uuid4().hex[:12],
                    "title": derive_event_title(clean_content),
                    "_lines": [clean_content],
                    "_title_only": False,
                }
            current_major["subevents"].append(current_subevent)
            if not current_subevent["_title_only"]:
                current_subevent = None
            continue

        line = strip_journal_markers(token_value)
        if current_subevent:
            current_subevent["_lines"].append(line)
        elif current_major:
            current_major["_lines"].append(line)
        else:
            intro_lines.append(line)

    for major in major_events:
        major["text"] = clean_journal_text(major.pop("_lines"))
        for subevent in major["subevents"]:
            subevent["text"] = clean_journal_text(subevent.pop("_lines"))
            if not subevent["text"]:
                subevent["text"] = subevent["title"]
            subevent.pop("_title_only", None)

    return {
        "intro": clean_journal_text(intro_lines),
        "major_events": major_events,
        "parser_mode": "markers",
    }


def plain_journal_paragraphs(text: str):
    normalized = normalize_user_text(text or "").replace("\r", "")
    if re.search(r"\n\s*\n", normalized):
        raw_paragraphs = re.split(r"\n\s*\n", normalized)
        return [
            " ".join(line.strip() for line in part.splitlines() if line.strip())
            for part in raw_paragraphs
            if part.strip()
        ]
    return [line.strip() for line in normalized.splitlines() if line.strip()]


def is_chronology_heading(text: str) -> bool:
    compact = " ".join(strip_journal_markers(text).split())
    if len(compact) > 160 or not YEAR_PATTERN.search(compact):
        return False
    if re.fullmatch(
        rf"{EVENT_YEAR_TOKEN}(?:\s*-\s*{EVENT_YEAR_TOKEN})?"
        r"(?:\s+(?:год|года))?[.:]?",
        compact,
        flags=re.IGNORECASE,
    ):
        return True
    if re.match(rf"^{EVENT_YEAR_TOKEN}\s*-", compact):
        return True
    if re.search(
        rf"\({EVENT_YEAR_TOKEN}\s*-\s*(?:{EVENT_YEAR_TOKEN}|н\.?\s*в\.?)\)\.?$",
        compact,
        flags=re.IGNORECASE,
    ):
        return True
    return bool(
        re.search(rf"{EVENT_YEAR_TOKEN}\s*-\s*{EVENT_YEAR_TOKEN}", compact)
        and len(compact) <= 120
    )


def is_plain_subheading(text: str, next_text: str) -> bool:
    compact = " ".join(strip_journal_markers(text).split())
    return (
        bool(compact)
        and len(compact) <= 120
        and len(compact.split()) <= 16
        and not is_chronology_heading(compact)
        and (compact.endswith(":") or not compact.endswith((".", "!", "?")))
        and len(next_text or "") >= 100
    )


def inline_marked_events(text: str):
    events = []
    used = set()
    for match in re.finditer(r"\[([^\[\]]{8,2000})\]|\{([^{}]{8,2000})\}", text):
        content = strip_journal_markers(match.group(1) or match.group(2) or "")
        key = content.casefold()
        if content and key not in used:
            used.add(key)
            events.append(
                {
                    "id": uuid4().hex[:12],
                    "title": derive_event_title(content),
                    "text": content,
                }
            )
    return events


def parse_plain_journal(text: str):
    paragraphs = plain_journal_paragraphs(text)
    if not paragraphs:
        raise ValueError("В файле не нашлось текста")

    major_indices = [
        index
        for index, paragraph in enumerate(paragraphs)
        if is_chronology_heading(paragraph)
    ]
    if len(major_indices) > 1:
        first_index = major_indices[0]
        first_candidate = strip_journal_markers(
            paragraphs[first_index]
        ).casefold()
        if first_index <= 1 and re.search(
            r"\b(?:история|хроника)\b", first_candidate
        ):
            # Название документа вроде "История Нолании (1808-2062)" - это
            # вступление, а не отдельное событие в меню.
            major_indices = major_indices[1:]
    intro = []
    major_events = []
    current_major = None
    current_subevent = None

    if not major_indices:
        first = strip_journal_markers(paragraphs[0])
        use_first_as_title = len(first) <= 160
        current_major = {
            "id": uuid4().hex[:12],
            "title": first[:200] if use_first_as_title else "Основная хроника",
            "_body": [],
            "subevents": [],
        }
        major_events.append(current_major)
        body_paragraphs = paragraphs[1:] if use_first_as_title else paragraphs
        current_major["_body"].extend(body_paragraphs)
    else:
        first_major_index = major_indices[0]
        intro.extend(paragraphs[:first_major_index])
        for index in range(first_major_index, len(paragraphs)):
            paragraph = paragraphs[index]
            clean_paragraph = strip_journal_markers(paragraph)
            if is_chronology_heading(paragraph):
                current_major = {
                    "id": uuid4().hex[:12],
                    "title": clean_paragraph[:200],
                    "_body": [],
                    "subevents": [],
                }
                major_events.append(current_major)
                current_subevent = None
                continue
            if not current_major:
                intro.append(clean_paragraph)
                continue
            next_text = (
                paragraphs[index + 1] if index + 1 < len(paragraphs) else ""
            )
            if is_plain_subheading(paragraph, next_text):
                current_subevent = {
                    "id": uuid4().hex[:12],
                    "title": clean_paragraph[:200],
                    "_lines": [],
                }
                current_major["subevents"].append(current_subevent)
                continue
            if current_subevent:
                current_subevent["_lines"].append(clean_paragraph)
            else:
                current_major["_body"].append(paragraph)

    for major in major_events:
        body_paragraphs = major.pop("_body")
        clean_body = [strip_journal_markers(item) for item in body_paragraphs]
        major["text"] = clean_journal_text(clean_body)
        for subevent in major["subevents"]:
            subevent["text"] = clean_journal_text(subevent.pop("_lines"))
            if not subevent["text"]:
                subevent["text"] = subevent["title"]

        if not major["subevents"]:
            for paragraph in body_paragraphs:
                major["subevents"].extend(inline_marked_events(paragraph))
        if not major["subevents"] and (
            len(body_paragraphs) > 1 or len(major_events) == 1
        ):
            for paragraph in clean_body:
                if len(paragraph) < 30:
                    continue
                major["subevents"].append(
                    {
                        "id": uuid4().hex[:12],
                        "title": derive_event_title(paragraph),
                        "text": paragraph,
                    }
                )

    return {
        "intro": clean_journal_text(
            [strip_journal_markers(item) for item in intro]
        ),
        "major_events": major_events,
        "parser_mode": "automatic",
    }


def parse_event_journal_text(text: str):
    tokens = tokenize_structured_journal(text)
    if any(token_type == "major" for token_type, _ in tokens):
        parsed = parse_structured_journal(tokens)
    else:
        parsed = parse_plain_journal(text)

    major_events = parsed["major_events"]
    if not major_events:
        raise ValueError("Не получилось выделить большие события")
    if len(major_events) > 200:
        raise ValueError("В одном файле можно сохранить максимум 200 больших событий")
    subevents_count = sum(
        len(major.get("subevents", [])) for major in major_events
    )
    if subevents_count > 2000:
        raise ValueError("В одном файле можно сохранить максимум 2000 подсобытий")
    for major in major_events:
        major["date"] = event_date(major)
        for subevent in major.get("subevents", []):
            subevent["date"] = event_date(subevent)
    return parsed


def short_button_text(text: str, limit=58) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else f"{text[:limit - 3]}..."


def find_event_history(country: dict, history_id: str):
    if not country:
        return None
    return next(
        (
            history
            for history in country.get("event_histories", [])
            if history.get("id") == history_id
        ),
        None,
    )


def find_major_event(history: dict, major_id: str):
    if not history:
        return None
    return next(
        (
            event
            for event in history.get("major_events", [])
            if event.get("id") == major_id
        ),
        None,
    )


def find_subevent(major_event: dict, subevent_id: str):
    if not major_event:
        return None
    return next(
        (
            event
            for event in major_event.get("subevents", [])
            if event.get("id") == subevent_id
        ),
        None,
    )


def normalize_event_search_text(text: str) -> str:
    normalized = normalize_user_text(text or "").casefold().replace("ё", "е")
    normalized = re.sub(r"[^\w]+", " ", normalized).replace("_", " ")
    return " ".join(normalized.split())


def search_journal_events(search_text: str):
    normalized_query = normalize_event_search_text(search_text)
    terms = normalized_query.split()
    if not terms:
        return []

    results = []
    sequence = 0
    for lore_country in DATA.get("lore_countries", []):
        country_name = lore_country_display_name(lore_country)
        for history in lore_country.get("event_histories", []):
            for major_event in history.get("major_events", []):
                events = [("major", major_event, None)]
                events.extend(
                    ("subevent", subevent, major_event)
                    for subevent in major_event.get("subevents", [])
                )
                for event_type, event, parent_major in events:
                    title = event_title_with_date(event)
                    normalized_title = normalize_event_search_text(title)
                    normalized_text = normalize_event_search_text(
                        str(event.get("text") or "")
                    )
                    haystack = f"{normalized_title}\n{normalized_text}"
                    if not all(term in haystack for term in terms):
                        continue
                    if normalized_title == normalized_query:
                        score = 0
                    elif normalized_title.startswith(normalized_query):
                        score = 1
                    elif normalized_query in normalized_title:
                        score = 2
                    else:
                        score = 3
                    results.append(
                        {
                            "type": event_type,
                            "country_id": lore_country["id"],
                            "country_name": country_name,
                            "history_id": history["id"],
                            "major_id": (
                                major_event["id"]
                                if event_type == "major"
                                else parent_major["id"]
                            ),
                            "event_id": event["id"],
                            "title": title,
                            "score": score,
                            "sequence": sequence,
                        }
                    )
                    sequence += 1

    results.sort(key=lambda item: (item["score"], item["sequence"]))
    return results[:EVENT_SEARCH_LIMIT]


def event_search_results_keyboard(results, page=0):
    page_results, page, total_pages = paginate_items(results, page)
    buttons = []
    for result in page_results:
        if result["type"] == "major":
            icon = "🔷"
            callback_data = (
                f"journalmajor:{result['country_id']}:"
                f"{result['history_id']}:{result['major_id']}"
            )
        else:
            icon = "▫️"
            callback_data = (
                f"journalsub:{result['country_id']}:"
                f"{result['history_id']}:{result['major_id']}:"
                f"{result['event_id']}"
            )
        label = short_button_text(
            f"{icon} {result['title']} - {result['country_name']}"
        )
        buttons.append(
            [InlineKeyboardButton(label, callback_data=callback_data)]
        )
    navigation = pagination_row(page, total_pages, "eventsearchpage")
    if navigation:
        buttons.append(navigation)
    buttons.append(
        [InlineKeyboardButton("⬅️ Назад в меню", callback_data="mainmenu")]
    )
    return InlineKeyboardMarkup(buttons), page, total_pages


def journal_countries_page_keyboard(page=0):
    countries = sorted(
        [
            (lore_country_display_name(country), country)
            for country in DATA.get("lore_countries", [])
            if country.get("event_histories")
        ],
        key=lambda item: item[0].casefold(),
    )
    page_countries, page, total_pages = paginate_items(countries, page)
    buttons = [
        [
            InlineKeyboardButton(
                name,
                callback_data=f"journalcountry:{country['id']}",
            )
        ]
        for name, country in page_countries
    ]
    navigation = pagination_row(page, total_pages, "journalpage")
    if navigation:
        buttons.append(navigation)
    buttons.append(
        [InlineKeyboardButton("⬅️ Назад в меню", callback_data="mainmenu")]
    )
    keyboard = InlineKeyboardMarkup(buttons) if page_countries else None
    return keyboard, page, total_pages


def journal_histories_page_keyboard(country: dict, page=0):
    histories = country.get("event_histories", []) if country else []
    page_histories, page, total_pages = paginate_items(histories, page)
    buttons = [
        [
            InlineKeyboardButton(
                short_button_text(history["title"]),
                callback_data=(
                    f"journalhistory:{country['id']}:{history['id']}"
                ),
            )
        ]
        for history in page_histories
    ]
    navigation = pagination_row(
        page, total_pages, f"journalcpage:{country['id']}"
    )
    if navigation:
        buttons.append(navigation)
    buttons.append(
        [
            InlineKeyboardButton(
                "⬅️ К странам", callback_data="journalpage:0"
            )
        ]
    )
    return InlineKeyboardMarkup(buttons), page, total_pages


def journal_major_events_page_keyboard(country: dict, history: dict, page=0):
    events = history.get("major_events", []) if history else []
    page_events, page, total_pages = paginate_items(events, page)
    buttons = [
        [
            InlineKeyboardButton(
                short_button_text(event_title_with_date(event)),
                callback_data=(
                    f"journalmajor:{country['id']}:{history['id']}:{event['id']}"
                ),
            )
        ]
        for event in page_events
    ]
    navigation = pagination_row(
        page,
        total_pages,
        f"journalhpage:{country['id']}:{history['id']}",
    )
    if navigation:
        buttons.append(navigation)
    buttons.append(
        [
            InlineKeyboardButton(
                "⬅️ К историям",
                callback_data=f"journalcountry:{country['id']}",
            )
        ]
    )
    return InlineKeyboardMarkup(buttons), page, total_pages


def journal_subevents_page_keyboard(
    country: dict, history: dict, major_event: dict, page=0
):
    subevents = major_event.get("subevents", []) if major_event else []
    page_events, page, total_pages = paginate_items(subevents, page)
    buttons = []
    if major_event.get("text"):
        buttons.append(
            [
                InlineKeyboardButton(
                    "📖 Текст большого события",
                    callback_data=(
                        f"journalmajortext:{country['id']}:{history['id']}:"
                        f"{major_event['id']}"
                    ),
                )
            ]
        )
    buttons.extend([
        [
            InlineKeyboardButton(
                short_button_text(event_title_with_date(event)),
                callback_data=(
                    f"journalsub:{country['id']}:{history['id']}:"
                    f"{major_event['id']}:{event['id']}"
                ),
            )
        ]
        for event in page_events
    ])
    navigation = pagination_row(
        page,
        total_pages,
        (
            f"journalmpage:{country['id']}:{history['id']}:"
            f"{major_event['id']}"
        ),
    )
    if navigation:
        buttons.append(navigation)
    buttons.append(
        [
            InlineKeyboardButton(
                "⬅️ К большим событиям",
                callback_data=(
                    f"journalhistory:{country['id']}:{history['id']}"
                ),
            )
        ]
    )
    return InlineKeyboardMarkup(buttons), page, total_pages


def countries_keyboard(prefix="country", use_ids=False):
    names = sorted(DATA["countries"].keys())
    buttons = []
    row = []
    for i, name in enumerate(names, start=1):
        callback_value = DATA["countries"][name]["id"] if use_ids else name
        row.append(
            InlineKeyboardButton(name, callback_data=f"{prefix}:{callback_value}")
        )
        if i % 2 == 0:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return InlineKeyboardMarkup(buttons) if buttons else None


def countries_page_keyboard(page=0):
    names = sorted(DATA["countries"].keys())
    page_names, page, total_pages = paginate_items(names, page)
    buttons = []
    row = []
    for index, name in enumerate(page_names, start=1):
        row.append(InlineKeyboardButton(name, callback_data=f"country:{name}"))
        if index % 2 == 0:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    navigation = pagination_row(page, total_pages, "countriespage")
    if navigation:
        buttons.append(navigation)
    keyboard = InlineKeyboardMarkup(buttons) if page_names else None
    return keyboard, page, total_pages


def country_selection_page_keyboard(
    item_prefix: str, navigation_prefix: str, page=0
):
    countries = sorted(DATA["countries"].items(), key=lambda item: item[0].casefold())
    page_countries, page, total_pages = paginate_items(countries, page)
    buttons = []
    row = []
    for index, (name, country) in enumerate(page_countries, start=1):
        row.append(
            InlineKeyboardButton(
                name,
                callback_data=f"{item_prefix}:{country['id']}",
            )
        )
        if index % 2 == 0:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    navigation = pagination_row(page, total_pages, navigation_prefix)
    if navigation:
        buttons.append(navigation)
    keyboard = InlineKeyboardMarkup(buttons) if page_countries else None
    return keyboard, page, total_pages


def add_history_lore_countries_keyboard(page=0):
    lore_countries = available_lore_countries(linked=True)
    page_countries, page, total_pages = paginate_items(lore_countries, page)
    buttons = [
        [
            InlineKeyboardButton(
                lore_country_display_name(lore_country),
                callback_data=f"addhistorycountry:{lore_country['id']}",
            )
        ]
        for lore_country in page_countries
    ]
    navigation = pagination_row(page, total_pages, "addhistorypage")
    if navigation:
        buttons.append(navigation)
    buttons.append(
        [
            InlineKeyboardButton(
                "➕ Новая страна только с лором",
                callback_data="addhistorynew",
            )
        ]
    )
    return InlineKeyboardMarkup(buttons), page, total_pages


def new_country_lore_link_keyboard(page=0):
    lore_countries = available_lore_countries(linked=False)
    page_countries, page, total_pages = paginate_items(lore_countries, page)
    buttons = [
        [
            InlineKeyboardButton(
                lore_country["name"],
                callback_data=f"addcountrylore:{lore_country['id']}",
            )
        ]
        for lore_country in page_countries
    ]
    navigation = pagination_row(page, total_pages, "addcountrylorepage")
    if navigation:
        buttons.append(navigation)
    buttons.append(
        [
            InlineKeyboardButton(
                "Не привязывать",
                callback_data="addcountrylore:skip",
            )
        ]
    )
    return InlineKeyboardMarkup(buttons), page, total_pages


def get_country_by_id(country_id: str):
    """Возвращает текущее имя страны и данные по короткому внутреннему ID."""
    for name, country in DATA["countries"].items():
        if country.get("id") == country_id:
            return name, country
    return None, None


def get_country_by_name(country_name: str):
    wanted = normalize_user_text(country_name.strip()).casefold()
    for name, country in DATA["countries"].items():
        if name.casefold() == wanted:
            return name, country
    return None, None


def get_lore_country_by_id(lore_country_id: str):
    for lore_country in DATA.get("lore_countries", []):
        if lore_country.get("id") == lore_country_id:
            return lore_country
    return None


def get_lore_country_by_name(country_name: str):
    wanted = normalize_user_text(country_name.strip()).casefold()
    return next(
        (
            lore_country
            for lore_country in DATA.get("lore_countries", [])
            if lore_country.get("name", "").casefold() == wanted
        ),
        None,
    )


def get_lore_for_country(country: dict):
    if not country:
        return None
    lore_country = get_lore_country_by_id(country.get("lore_country_id"))
    if lore_country:
        return lore_country
    return next(
        (
            entry
            for entry in DATA.get("lore_countries", [])
            if entry.get("linked_country_id") == country.get("id")
        ),
        None,
    )


def lore_country_display_name(lore_country: dict) -> str:
    if not lore_country:
        return "Страна"
    linked_name, linked_country = get_country_by_id(
        lore_country.get("linked_country_id")
    )
    return linked_name if linked_country else lore_country.get("name") or "Страна"


def get_journal_country(identifier: str):
    lore_country = get_lore_country_by_id(identifier)
    if lore_country:
        return lore_country_display_name(lore_country), lore_country
    country_name, country = get_country_by_id(identifier)
    lore_country = get_lore_for_country(country)
    if lore_country:
        return country_name, lore_country
    return None, None


def link_lore_country(country: dict, lore_country: dict):
    if not country or not lore_country:
        return
    old_lore = get_lore_for_country(country)
    if old_lore and old_lore.get("id") != lore_country.get("id"):
        old_lore["linked_country_id"] = None
    old_country_id = lore_country.get("linked_country_id")
    if old_country_id and old_country_id != country.get("id"):
        _, old_country = get_country_by_id(old_country_id)
        if old_country:
            old_country["lore_country_id"] = None
    lore_country["linked_country_id"] = country["id"]
    country["lore_country_id"] = lore_country["id"]


def available_lore_countries(linked=False):
    entries = [
        entry
        for entry in DATA.get("lore_countries", [])
        if entry.get("event_histories")
        and (linked or not entry.get("linked_country_id"))
    ]
    return sorted(entries, key=lambda item: item["name"].casefold())


def search_countries(search_text: str):
    wanted = normalize_user_text(search_text.strip()).casefold()
    if not wanted:
        return []
    matches = []
    for name, country in DATA["countries"].items():
        capital = country.get("capital") or ""
        if wanted in name.casefold() or wanted in capital.casefold():
            matches.append((name, country))
    return sorted(matches, key=lambda item: item[0].casefold())


def search_results_keyboard(matches):
    buttons = [
        [
            InlineKeyboardButton(
                name, callback_data=f"searchcountry:{country['id']}"
            )
        ]
        for name, country in matches
    ]
    return InlineKeyboardMarkup(buttons) if buttons else None


def find_region(country: dict, region_id: str):
    if not country:
        return None
    for region in country.get("regions", []):
        if region.get("id") == region_id:
            return region
    return None


def find_region_by_name(country: dict, region_name: str):
    if not country:
        return None
    wanted = normalize_user_text(region_name.strip()).casefold()
    for region in country.get("regions", []):
        if region.get("name", "").casefold() == wanted:
            return region
    return None


def region_name_exists(country: dict, name: str, exclude_id=None) -> bool:
    wanted = name.casefold()
    return any(
        region.get("id") != exclude_id
        and region.get("name", "").casefold() == wanted
        for region in country.get("regions", [])
    )


def find_library_flag(entry_id: str):
    for entry in DATA.get("flag_library", []):
        if entry.get("id") == entry_id:
            return entry
    return None


def library_flag_type(entry: dict) -> str:
    return "region" if entry.get("flag_type") == "region" else "country"


def library_flag_name_exists(
    name: str,
    exclude_id=None,
    flag_type=None,
    parent_flag_id=None,
) -> bool:
    wanted = name.casefold()
    for entry in DATA.get("flag_library", []):
        if entry.get("id") == exclude_id:
            continue
        if flag_type and library_flag_type(entry) != flag_type:
            continue
        if (
            flag_type == "region"
            and entry.get("parent_flag_id") != parent_flag_id
        ):
            continue
        if entry.get("country_name", "").casefold() == wanted:
            return True
    return False


def find_library_flag_by_name(
    name: str, flag_type="country", parent_flag_id=None
):
    wanted = name.casefold()
    for entry in DATA.get("flag_library", []):
        if library_flag_type(entry) != flag_type:
            continue
        if (
            flag_type == "region"
            and entry.get("parent_flag_id") != parent_flag_id
        ):
            continue
        if entry.get("country_name", "").casefold() == wanted:
            return entry
    return None


def upsert_library_flag(
    name: str,
    file_id: str,
    media_type: str = "document",
    flag_type="country",
    parent_flag_id=None,
    country_id=None,
    region_id=None,
    legacy_name=None,
) -> bool:
    """Добавляет флаг в нужный раздел библиотеки или обновляет его."""
    if not file_id:
        return False

    name = normalize_user_text(name.strip())
    flag_type = "region" if flag_type == "region" else "country"
    media_type = media_type if media_type in ("document", "photo") else "document"
    entry = None
    for candidate in DATA.get("flag_library", []):
        if flag_type == "region" and region_id:
            if candidate.get("region_id") == region_id:
                entry = candidate
                break
        elif flag_type == "region" and parent_flag_id:
            if (
                library_flag_type(candidate) == "region"
                and candidate.get("parent_flag_id") == parent_flag_id
                and candidate.get("country_name", "").casefold()
                == name.casefold()
            ):
                entry = candidate
                break
        elif flag_type == "country" and country_id:
            if (
                library_flag_type(candidate) == "country"
                and candidate.get("country_id") == country_id
            ):
                entry = candidate
                break

    if not entry:
        entry = find_library_flag_by_name(name, flag_type, parent_flag_id)

    if not entry and legacy_name:
        wanted_legacy_name = legacy_name.casefold()
        for candidate in DATA.get("flag_library", []):
            if (
                candidate.get("country_name", "").casefold()
                == wanted_legacy_name
            ):
                entry = candidate
                break

    if entry:
        changed = any(
            (
                entry.get("country_name") != name,
                entry.get("flag_file_id") != file_id,
                entry.get("flag_media_type", "document") != media_type,
                library_flag_type(entry) != flag_type,
                entry.get("parent_flag_id") != parent_flag_id,
                entry.get("country_id") != country_id,
                entry.get("region_id") != region_id,
            )
        )
        entry["country_name"] = name
        entry["flag_file_id"] = file_id
        entry["flag_media_type"] = media_type
        entry["flag_type"] = flag_type
        entry["parent_flag_id"] = parent_flag_id
        entry["country_id"] = country_id
        entry["region_id"] = region_id
        return changed

    DATA.setdefault("flag_library", []).append(
        {
            "id": uuid4().hex[:12],
            "country_name": name,
            "flag_file_id": file_id,
            "flag_media_type": media_type,
            "flag_type": flag_type,
            "parent_flag_id": parent_flag_id,
            "country_id": country_id,
            "region_id": region_id,
        }
    )
    return True


def region_library_flag_name(country_name: str, region_name: str) -> str:
    return f"{region_name} - {country_name}"


def country_library_flags():
    return sorted(
        [
            entry
            for entry in DATA.get("flag_library", [])
            if library_flag_type(entry) == "country"
        ],
        key=lambda item: item["country_name"].casefold(),
    )


def find_country_library_flag(country_id=None, country_name=None):
    if country_id:
        for entry in country_library_flags():
            if entry.get("country_id") == country_id:
                return entry
    if country_name:
        wanted = country_name.casefold()
        for entry in country_library_flags():
            if entry.get("country_name", "").casefold() == wanted:
                return entry
    return None


def sync_entity_flags_to_library() -> bool:
    """Подтягивает в библиотеку флаги всех уже сохранённых стран и регионов."""
    changed = False
    for country_name, country in DATA.get("countries", {}).items():
        if country.get("flag_file_id"):
            changed = upsert_library_flag(
                country_name,
                country["flag_file_id"],
                country.get("flag_media_type", "document"),
                flag_type="country",
                country_id=country.get("id"),
            ) or changed

        parent_flag = find_country_library_flag(
            country_id=country.get("id"), country_name=country_name
        )
        if not parent_flag:
            continue

        for entry in DATA.get("flag_library", []):
            if (
                library_flag_type(entry) == "region"
                and not entry.get("parent_flag_id")
                and entry.get("country_id") == country.get("id")
            ):
                entry["parent_flag_id"] = parent_flag["id"]
                changed = True

        for region in country.get("regions", []):
            legacy_name = region_library_flag_name(
                country_name, region["name"]
            )
            if not region.get("flag_file_id"):
                linked_entry = next(
                    (
                        entry
                        for entry in DATA.get("flag_library", [])
                        if library_flag_type(entry) == "region"
                        and (
                            entry.get("region_id") == region.get("id")
                            or (
                                entry.get("parent_flag_id") == parent_flag["id"]
                                and entry.get("country_name", "").casefold()
                                == region["name"].casefold()
                            )
                        )
                    ),
                    None,
                )
                if linked_entry:
                    region["flag_file_id"] = linked_entry["flag_file_id"]
                    region["flag_media_type"] = linked_entry.get(
                        "flag_media_type", "document"
                    )
                    changed = True
            if not region.get("flag_file_id"):
                legacy_entry = next(
                    (
                        entry
                        for entry in DATA.get("flag_library", [])
                        if entry.get("country_name", "").casefold()
                        == legacy_name.casefold()
                    ),
                    None,
                )
                if legacy_entry:
                    region["flag_file_id"] = legacy_entry["flag_file_id"]
                    region["flag_media_type"] = legacy_entry.get(
                        "flag_media_type", "document"
                    )
                    changed = True
            if not region.get("flag_file_id"):
                continue
            changed = upsert_library_flag(
                region["name"],
                region["flag_file_id"],
                region.get("flag_media_type", "document"),
                flag_type="region",
                parent_flag_id=parent_flag["id"],
                country_id=country.get("id"),
                region_id=region.get("id"),
                legacy_name=legacy_name,
            ) or changed
    return changed


def flag_choice_keyboard(
    prefix: str,
    allow_none: bool = False,
    page=0,
    flag_type="country",
    parent_flag_id=None,
    restrict_to_parent=False,
):
    buttons = [
        [InlineKeyboardButton("Загрузить новый флаг", callback_data=f"{prefix}:new")]
    ]
    if allow_none:
        buttons.append(
            [InlineKeyboardButton("Без флага", callback_data=f"{prefix}:none")]
        )

    entries = sorted(
        [
            entry
            for entry in DATA.get("flag_library", [])
            if library_flag_type(entry) == flag_type
            and (
                not restrict_to_parent
                or entry.get("parent_flag_id") == parent_flag_id
            )
        ],
        key=lambda item: item["country_name"].casefold(),
    )
    page_entries, page, total_pages = paginate_items(entries, page)
    for entry in page_entries:
        buttons.append(
            [
                InlineKeyboardButton(
                    f"Взять: {entry['country_name']}",
                    callback_data=f"{prefix}:{entry['id']}",
                )
            ]
        )
    navigation = pagination_row(page, total_pages, f"{prefix}:page")
    if navigation:
        buttons.append(navigation)
    return InlineKeyboardMarkup(buttons)


async def build_flag_input_file(
    context: ContextTypes.DEFAULT_TYPE,
    entry: dict,
    default_extension: str,
):
    telegram_file = await context.bot.get_file(entry["flag_file_id"])
    file_bytes = await telegram_file.download_as_bytearray()
    extension = os.path.splitext(telegram_file.file_path or "")[1].lower()
    if not extension or len(extension) > 5:
        extension = default_extension
    safe_name = "".join(
        char if char.isalnum() or char in (" ", "-", "_") else "_"
        for char in entry["country_name"]
    ).strip() or "flag"
    return InputFile(bytes(file_bytes), filename=f"{safe_name}{extension}")


def clear_region_context(context: ContextTypes.DEFAULT_TYPE):
    for key in (
        "region_country_id",
        "region_parent_flag_id",
        "new_region_name",
        "new_region_capital",
        "new_region_description",
        "edit_region_id",
        "edit_region_field",
    ):
        context.user_data.pop(key, None)


def clear_library_flag_context(context: ContextTypes.DEFAULT_TYPE):
    for key in (
        "new_library_flag_type",
        "new_library_flag_name",
        "new_library_flag_parent_id",
        "new_library_flag_country_id",
        "new_library_flag_region_id",
        "edit_library_flag_id",
        "edit_library_flag_field",
    ):
        context.user_data.pop(key, None)


async def send_random_country_sticker(
    message, context: ContextTypes.DEFAULT_TYPE
):
    """Отправляет случайный стикер и не ломает карточку при ошибке Telegram."""
    cache_key = "aurelia_pack_sticker_ids"
    sticker_ids = context.bot_data.get(cache_key)
    if not sticker_ids:
        try:
            sticker_set = await context.bot.get_sticker_set(STICKER_SET_NAME)
            sticker_ids = [sticker.file_id for sticker in sticker_set.stickers]
            if sticker_ids:
                context.bot_data[cache_key] = sticker_ids
        except Exception as e:
            logger.warning("Не удалось загрузить набор стикеров %s: %s", STICKER_SET_NAME, e)
            return

    if not sticker_ids:
        return

    try:
        await message.reply_sticker(sticker=random.choice(sticker_ids))
    except Exception as e:
        logger.warning("Не удалось отправить случайный стикер: %s", e)


def regions_keyboard(country: dict, prefix="regioninfo"):
    buttons = [
        [
            InlineKeyboardButton(
                region["name"],
                callback_data=f"{prefix}:{country['id']}:{region['id']}",
            )
        ]
        for region in sorted(
            country.get("regions", []), key=lambda item: item["name"].casefold()
        )
    ]
    return InlineKeyboardMarkup(buttons) if buttons else None


def regions_page_keyboard(
    country: dict,
    page=0,
    item_prefix="regioninfo",
    navigation_prefix="regionspage",
):
    regions = sorted(
        country.get("regions", []), key=lambda item: item["name"].casefold()
    )
    page_regions, page, total_pages = paginate_items(regions, page)
    buttons = [
        [
            InlineKeyboardButton(
                region["name"],
                callback_data=f"{item_prefix}:{country['id']}:{region['id']}",
            )
        ]
        for region in page_regions
    ]
    navigation = pagination_row(
        page, total_pages, f"{navigation_prefix}:{country['id']}"
    )
    if navigation:
        buttons.append(navigation)
    keyboard = InlineKeyboardMarkup(buttons) if page_regions else None
    return keyboard, page, total_pages


def flag_library_keyboard(prefix="libraryflag"):
    def button_text(entry):
        if library_flag_type(entry) == "region":
            parent_flag = find_library_flag(entry.get("parent_flag_id"))
            parent_name = parent_flag.get("country_name") if parent_flag else None
            suffix = f" - {parent_name}" if parent_name else ""
            return f"Регион: {entry['country_name']}{suffix}"
        return f"Флаг {entry['country_name']}"

    buttons = [
        [
            InlineKeyboardButton(
                button_text(entry),
                callback_data=f"{prefix}:{entry['id']}",
            )
        ]
        for entry in sorted(
            DATA.get("flag_library", []),
            key=lambda item: item["country_name"].casefold(),
        )
    ]
    return InlineKeyboardMarkup(buttons) if buttons else None


def flag_library_page_keyboard(page=0):
    entries = country_library_flags()
    page_entries, page, total_pages = paginate_items(entries, page)
    buttons = [
        [
            InlineKeyboardButton(
                f"Флаг {entry['country_name']}",
                callback_data=f"libraryflag:{entry['id']}",
            )
        ]
        for entry in page_entries
    ]
    navigation = pagination_row(page, total_pages, "flagspage")
    if navigation:
        buttons.append(navigation)
    keyboard = InlineKeyboardMarkup(buttons) if page_entries else None
    return keyboard, page, total_pages


def country_flag_selection_page_keyboard(
    item_prefix: str, navigation_prefix: str, page=0
):
    entries = country_library_flags()
    page_entries, page, total_pages = paginate_items(entries, page)
    buttons = [
        [
            InlineKeyboardButton(
                f"Флаг {entry['country_name']}",
                callback_data=f"{item_prefix}:{entry['id']}",
            )
        ]
        for entry in page_entries
    ]
    navigation = pagination_row(page, total_pages, navigation_prefix)
    if navigation:
        buttons.append(navigation)
    keyboard = InlineKeyboardMarkup(buttons) if page_entries else None
    return keyboard, page, total_pages


def regional_library_flags(parent_flag_id: str):
    return sorted(
        [
            entry
            for entry in DATA.get("flag_library", [])
            if library_flag_type(entry) == "region"
            and entry.get("parent_flag_id") == parent_flag_id
        ],
        key=lambda item: item["country_name"].casefold(),
    )


def regional_flag_library_page_keyboard(parent_flag_id: str, page=0):
    entries = regional_library_flags(parent_flag_id)
    page_entries, page, total_pages = paginate_items(entries, page)
    buttons = [
        [
            InlineKeyboardButton(
                f"Флаг {entry['country_name']}",
                callback_data=f"libraryflag:{entry['id']}",
            )
        ]
        for entry in page_entries
    ]
    navigation = pagination_row(
        page, total_pages, f"libraryregionspage:{parent_flag_id}"
    )
    if navigation:
        buttons.append(navigation)
    keyboard = InlineKeyboardMarkup(buttons) if page_entries else None
    return keyboard, page, total_pages


def library_region_flags_button(entry: dict):
    if (
        library_flag_type(entry) == "country"
        and regional_library_flags(entry["id"])
    ):
        return InlineKeyboardButton(
            "Флаги регионов",
            callback_data=f"libraryregions:{entry['id']}",
        )
    return None


def library_flag_preview_keyboard(entry: dict, download_text: str):
    rows = [[
        InlineKeyboardButton(
            download_text,
            callback_data=f"downloadflag:{entry['id']}",
        )
    ]]
    region_button = library_region_flags_button(entry)
    if region_button:
        rows.append([region_button])
    return InlineKeyboardMarkup(rows)


def build_lore_links(lore_links) -> str:
    links = []
    for index, raw_url in enumerate(lore_links or [], start=1):
        url = str(raw_url).strip()
        if not url:
            continue
        label = "открыть" if len(lore_links) == 1 else f"ссылка {index}"
        if url.casefold().startswith(("https://", "http://")):
            links.append(f'<a href="{escape(url)}">{label}</a>')
        else:
            links.append(escape(url))
    return ", ".join(links) if links else "-"


def build_info_text(c: dict) -> str:
    continents = ", ".join(c.get("continents", []))
    borders = ", ".join(c.get("borders", []))
    area = c.get("area_km2") or ""
    area_text = f"{area} км²" if area else "-"
    region_names = "\n\n".join(
        (
            f"{region['name']} ({region['capital']})"
            if region.get("capital")
            else region["name"]
        )
        for region in sorted(
            c.get("regions", []), key=lambda item: item["name"].casefold()
        )
    )
    lore_text = build_lore_links(c.get("lore_links", []))
    text = (
        f"<b>{escape(c['name'])}</b>\n\n"
        f"<b>Кто у руля:</b> {escape(c.get('leader', '-'))}\n"
        f"Столица: {escape(c.get('capital', '-'))}\n"
        f"Где находится: {escape(continents or '-')}\n"
        f"Площадь: {escape(area_text)}\n"
        f"Граничит с: {escape(borders or '-')}\n\n"
        f"<b>Регионы:</b>\n{escape(region_names or '-')}\n\n"
        f"<b>Немного о стране:</b>\n"
        f"<blockquote expandable>{escape(c.get('description') or '-')}</blockquote>\n\n"
        f"Почитать лор: {lore_text}"
    )
    return text


def build_region_info_text(region: dict, country_name: str) -> str:
    description = region.get("description") or "-"
    return (
        f"<b>{escape(region['name'])}</b>\n\n"
        f"<b>Страна:</b> {escape(country_name)}\n"
        f"<b>Столица региона:</b> {escape(region.get('capital') or '-')}\n\n"
        f"<b>Немного о регионе:</b>\n"
        f"<blockquote expandable>{escape(description)}</blockquote>"
    )


def region_info_buttons(country: dict, region: dict):
    if not region.get("flag_file_id"):
        return None
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton(
                "🚩 Флаг",
                callback_data=f"regionflag:{country['id']}:{region['id']}",
            )
        ]]
    )


def info_buttons(c: dict):
    row = [InlineKeyboardButton("🚩 Флаг", callback_data=f"flag:{c['name']}")]
    if c.get("herb_file_id"):
        row.append(InlineKeyboardButton("🛡 Герб", callback_data=f"herb:{c['name']}"))
    rows = [row]
    if c.get("regions"):
        rows.append(
            [
                InlineKeyboardButton(
                    f"🗺 Регионы ({len(c['regions'])})",
                    callback_data=f"regions:{c['id']}",
                )
            ]
        )
    lore_country = get_lore_for_country(c)
    if lore_country and lore_country.get("event_histories"):
        rows.append(
            [
                InlineKeyboardButton(
                    "📜 Журнал событий",
                    callback_data=f"journalcountry:{lore_country['id']}",
                )
            ]
        )
    return InlineKeyboardMarkup(rows)


def start_keyboard():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📋 Список стран", callback_data="list_countries")],
            [InlineKeyboardButton("🔎 Поиск страны", callback_data="search_countries")],
            [InlineKeyboardButton("🔎 Поиск событий", callback_data="search_events")],
            [InlineKeyboardButton("📜 Журнал событий", callback_data="journal")],
            [InlineKeyboardButton("Библиотека флагов", callback_data="list_library_flags")],
        ]
    )


def start_message_text():
    return (
        "Привет! Это бот-путеводитель по Аурелии\n\n"
        "Тут можно полистать страны, посмотреть их флаги, гербы, регионы, "
        "лор и журнал событий. Жми кнопку ниже и выбирай, куда заглянем\n\n"
        f'v: "{BOT_VERSION}"'
    )


# ---------------------------------------------------------------------------
# /start и /countries
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        start_message_text(),
        reply_markup=start_keyboard(),
    )


async def cb_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        start_message_text(),
        reply_markup=start_keyboard(),
    )


async def cmd_countries(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb, page, total_pages = countries_page_keyboard()
    if not kb:
        await update.message.reply_text("Тут пока пусто - ни одной страны ещё не добавили")
        return
    await update.message.reply_text(
        paginated_list_text("Выбирай страну:", page, total_pages),
        reply_markup=kb,
    )


async def cb_list_countries(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    kb, page, total_pages = countries_page_keyboard()
    if not kb:
        await query.edit_message_text("Тут пока пусто - ни одной страны ещё не добавили")
        return
    await query.message.reply_text(
        paginated_list_text("Выбирай страну:", page, total_pages),
        reply_markup=kb,
    )


async def cb_countries_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    page = int(query.data.rsplit(":", 1)[1])
    kb, page, total_pages = countries_page_keyboard(page)
    if not kb:
        await query.edit_message_text("Тут пока пусто - ни одной страны ещё не добавили")
        return
    await query.edit_message_text(
        paginated_list_text("Выбирай страну:", page, total_pages),
        reply_markup=kb,
    )


def journal_menu_excerpt(text: str, limit=1000) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    if len(text) > limit:
        text = f"{text[:limit - 3].rstrip()}..."
    return f"\n\n<blockquote expandable>{escape(text)}</blockquote>"


def split_text_for_html(text: str, max_html_length=3000):
    remaining = (text or "-").strip() or "-"
    chunks = []
    while remaining:
        low = 1
        high = min(len(remaining), max_html_length)
        best = 1
        while low <= high:
            middle = (low + high) // 2
            if len(escape(remaining[:middle])) <= max_html_length:
                best = middle
                low = middle + 1
            else:
                high = middle - 1
        split_at = best
        if split_at < len(remaining):
            newline_at = remaining.rfind("\n", 0, split_at)
            space_at = remaining.rfind(" ", 0, split_at)
            natural_split = max(newline_at, space_at)
            if natural_split >= split_at // 2:
                split_at = natural_split
        chunk = remaining[:split_at].strip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[split_at:].lstrip()
    return chunks or ["-"]


async def send_journal_detail(
    message,
    title: str,
    text: str,
    back_text: str,
    back_callback: str,
):
    chunks = split_text_for_html(text)
    for index, chunk in enumerate(chunks):
        heading = f"<b>{escape(title)}</b>\n\n" if index == 0 else ""
        reply_markup = None
        if index == len(chunks) - 1:
            reply_markup = InlineKeyboardMarkup(
                [[InlineKeyboardButton(back_text, callback_data=back_callback)]]
            )
        await message.reply_text(
            f"{heading}<blockquote expandable>{escape(chunk)}</blockquote>",
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
        )


async def cmd_journal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb, page, total_pages = journal_countries_page_keyboard()
    if not kb:
        await update.message.reply_text(
            "Журнал пока пустой - ни одной истории ещё не загрузили"
        )
        return
    await update.message.reply_text(
        paginated_list_text(
            "Выбирай страну, чью историю хочешь открыть:",
            page,
            total_pages,
        ),
        reply_markup=kb,
    )


async def cb_journal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    kb, page, total_pages = journal_countries_page_keyboard()
    if not kb:
        await query.edit_message_text(
            "Журнал пока пустой - ни одной истории ещё не загрузили",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Назад в меню", callback_data="mainmenu")]]
            ),
        )
        return
    await query.edit_message_text(
        paginated_list_text(
            "Выбирай страну, чью историю хочешь открыть:",
            page,
            total_pages,
        ),
        reply_markup=kb,
    )


async def cb_journal_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    page = int(query.data.rsplit(":", 1)[1])
    kb, page, total_pages = journal_countries_page_keyboard(page)
    if not kb:
        await query.edit_message_text(
            "Журнал пока пустой - ни одной истории ещё не загрузили"
        )
        return
    await query.edit_message_text(
        paginated_list_text(
            "Выбирай страну, чью историю хочешь открыть:",
            page,
            total_pages,
        ),
        reply_markup=kb,
    )


async def show_journal_country(query, country_id: str, page=0):
    country_name, country = get_journal_country(country_id)
    if not country or not country.get("event_histories"):
        await query.edit_message_text(
            "Похоже, у этой страны больше нет загруженных историй",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ К странам", callback_data="journalpage:0")]]
            ),
        )
        return
    kb, page, total_pages = journal_histories_page_keyboard(country, page)
    await query.edit_message_text(
        paginated_list_text(
            f'<b>Журнал событий - {escape(country_name)}</b>\n\nВыбирай историю:',
            page,
            total_pages,
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
    )


async def cb_journal_country(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    country_id = query.data.split(":", 1)[1]
    await show_journal_country(query, country_id)


async def cb_journal_country_page(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()
    try:
        _, country_id, raw_page = query.data.split(":", 2)
        page = int(raw_page)
    except (TypeError, ValueError):
        await query.edit_message_text("Эта кнопка почему-то сломалась")
        return
    await show_journal_country(query, country_id, page)


async def show_journal_history(
    query, country_id: str, history_id: str, page=0
):
    country_name, country = get_journal_country(country_id)
    history = find_event_history(country, history_id)
    if not country or not history:
        await query.edit_message_text(
            "Похоже, этой истории больше нет",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ К странам", callback_data="journalpage:0")]]
            ),
        )
        return
    kb, page, total_pages = journal_major_events_page_keyboard(
        country, history, page
    )
    text = (
        f"<b>{escape(history['title'])}</b>\n"
        f"Страна: {escape(country_name)}"
        f"{journal_menu_excerpt(history.get('intro'))}\n\n"
        "Выбирай большое событие:"
    )
    await query.edit_message_text(
        paginated_list_text(text, page, total_pages),
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
    )


async def cb_journal_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        _, country_id, history_id = query.data.split(":", 2)
    except ValueError:
        await query.edit_message_text("Эта кнопка почему-то сломалась")
        return
    await show_journal_history(query, country_id, history_id)


async def cb_journal_history_page(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()
    try:
        _, country_id, history_id, raw_page = query.data.split(":", 3)
        page = int(raw_page)
    except (TypeError, ValueError):
        await query.edit_message_text("Эта кнопка почему-то сломалась")
        return
    await show_journal_history(query, country_id, history_id, page)


async def show_journal_major_event(
    query, country_id: str, history_id: str, major_id: str, page=0
):
    _, country = get_journal_country(country_id)
    history = find_event_history(country, history_id)
    major_event = find_major_event(history, major_id)
    if not country or not history or not major_event:
        await query.edit_message_text(
            "Похоже, этого события больше нет",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ К странам", callback_data="journalpage:0")]]
            ),
        )
        return

    if not major_event.get("subevents"):
        await send_journal_detail(
            query.message,
            event_title_with_date(major_event),
            major_event.get("text") or "У этого события пока нет отдельного описания",
            "⬅️ К большим событиям",
            f"journalhistory:{country_id}:{history_id}",
        )
        return

    kb, page, total_pages = journal_subevents_page_keyboard(
        country, history, major_event, page
    )
    text = (
        f"<b>{escape(event_title_with_date(major_event))}</b>"
        f"{journal_menu_excerpt(major_event.get('text'))}\n\n"
        "Выбирай подсобытие:"
    )
    await query.edit_message_text(
        paginated_list_text(text, page, total_pages),
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
    )


async def cb_journal_major(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        _, country_id, history_id, major_id = query.data.split(":", 3)
    except ValueError:
        await query.edit_message_text("Эта кнопка почему-то сломалась")
        return
    await show_journal_major_event(
        query, country_id, history_id, major_id
    )


async def cb_journal_major_page(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()
    try:
        _, country_id, history_id, major_id, raw_page = query.data.split(
            ":", 4
        )
        page = int(raw_page)
    except (TypeError, ValueError):
        await query.edit_message_text("Эта кнопка почему-то сломалась")
        return
    await show_journal_major_event(
        query, country_id, history_id, major_id, page
    )


async def cb_journal_major_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()
    try:
        _, country_id, history_id, major_id = query.data.split(":", 3)
    except ValueError:
        await query.message.reply_text("Эта кнопка почему-то сломалась")
        return
    _, country = get_journal_country(country_id)
    history = find_event_history(country, history_id)
    major_event = find_major_event(history, major_id)
    if not country or not history or not major_event:
        await query.message.reply_text("Похоже, этого события больше нет")
        return
    await send_journal_detail(
        query.message,
        event_title_with_date(major_event),
        major_event.get("text") or "У этого события пока нет отдельного описания",
        "⬅️ К подсобытиям",
        f"journalmajor:{country_id}:{history_id}:{major_id}",
    )


async def cb_journal_subevent(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()
    try:
        _, country_id, history_id, major_id, subevent_id = query.data.split(
            ":", 4
        )
    except ValueError:
        await query.message.reply_text("Эта кнопка почему-то сломалась")
        return
    _, country = get_journal_country(country_id)
    history = find_event_history(country, history_id)
    major_event = find_major_event(history, major_id)
    subevent = find_subevent(major_event, subevent_id)
    if not country or not history or not major_event or not subevent:
        await query.message.reply_text("Похоже, этого подсобытия больше нет")
        return
    await send_journal_detail(
        query.message,
        event_title_with_date(subevent),
        subevent.get("text") or "У этого подсобытия пока нет отдельного описания",
        "⬅️ К подсобытиям",
        f"journalmajor:{country_id}:{history_id}:{major_id}",
    )


def clear_event_search_context(context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("event_search_results", None)
    context.user_data.pop("event_search_query", None)


def event_search_results_text(results, page, total_pages):
    if len(results) >= EVENT_SEARCH_LIMIT:
        heading = f"Показываю первые {EVENT_SEARCH_LIMIT} совпадений"
    else:
        heading = f"Нашёл событий: {len(results)}"
    return paginated_list_text(
        f"{heading}\n\n🔷 - большое событие\n▫️ - мелкое событие",
        page,
        total_pages,
    )


async def event_search_start(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    if update.callback_query:
        await update.callback_query.answer()
    clear_event_search_context(context)
    await update.effective_message.reply_text(
        "Напиши, что ищем в событиях\n\n"
        "Проверю названия и полный текст больших и мелких событий"
    )
    return EVENT_SEARCH_QUERY


async def event_search_query(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    search_text = normalize_user_text(update.message.text or "").strip()
    if len(search_text) < 2:
        await update.message.reply_text("Нужно хотя бы два символа")
        return EVENT_SEARCH_QUERY
    results = search_journal_events(search_text)
    if not results:
        clear_event_search_context(context)
        await update.message.reply_text(
            "Ничего не нашёл - попробуй другое слово или часть фразы"
        )
        return EVENT_SEARCH_QUERY
    context.user_data["event_search_results"] = results
    context.user_data["event_search_query"] = search_text
    keyboard, page, total_pages = event_search_results_keyboard(results)
    await update.message.reply_text(
        event_search_results_text(results, page, total_pages),
        reply_markup=keyboard,
    )
    return EVENT_SEARCH_QUERY


async def event_search_page(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()
    results = context.user_data.get("event_search_results") or []
    if not results:
        await query.edit_message_text(
            "Поиск уже устарел - запусти его ещё раз",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Назад в меню", callback_data="mainmenu")]]
            ),
        )
        return ConversationHandler.END
    page = int(query.data.rsplit(":", 1)[1])
    keyboard, page, total_pages = event_search_results_keyboard(results, page)
    await query.edit_message_text(
        event_search_results_text(results, page, total_pages),
        reply_markup=keyboard,
    )
    return EVENT_SEARCH_QUERY


async def event_search_open_major(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    clear_event_search_context(context)
    await cb_journal_major(update, context)
    return ConversationHandler.END


async def event_search_open_subevent(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    clear_event_search_context(context)
    await cb_journal_subevent(update, context)
    return ConversationHandler.END


async def event_search_back_to_menu(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    clear_event_search_context(context)
    await cb_main_menu(update, context)
    return ConversationHandler.END


async def send_country_details(message, context, name: str, country: dict):
    await send_random_country_sticker(message, context)
    await message.reply_photo(
        photo=country["card_file_id"],
        caption=f"<b>{escape(name)}</b>",
        parse_mode=ParseMode.HTML,
    )
    await message.reply_text(
        build_info_text(country),
        parse_mode=ParseMode.HTML,
        reply_markup=info_buttons(country),
    )


async def cb_show_country(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    name = query.data.split(":", 1)[1]
    c = DATA["countries"].get(name)
    if not c:
        await query.message.reply_text("Похоже, этой страны уже нет")
        return
    await send_country_details(query.message, context, name, c)


async def search_country_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
    await update.effective_message.reply_text(
        "Напиши название страны или её столицы"
    )
    return SEARCH_COUNTRY_QUERY


async def search_country_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    matches = search_countries(update.message.text)
    if not matches:
        await update.message.reply_text(
            "Ничего не нашёл. Попробуй другое название"
        )
        return SEARCH_COUNTRY_QUERY

    if len(matches) == 1:
        name, country = matches[0]
        await send_country_details(update.message, context, name, country)
        return ConversationHandler.END

    await update.message.reply_text(
        "Нашёл несколько вариантов. Выбирай:",
        reply_markup=search_results_keyboard(matches),
    )
    return ConversationHandler.END


async def cb_show_search_country(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    country_id = query.data.split(":", 1)[1]
    name, country = get_country_by_id(country_id)
    if not country:
        await query.message.reply_text("Похоже, этой страны уже нет")
        return
    await send_country_details(query.message, context, name, country)


async def cmd_flags(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb, page, total_pages = flag_library_page_keyboard()
    if not kb:
        await update.message.reply_text("Библиотека флагов стран пока пустая")
        return
    await update.message.reply_text(
        paginated_list_text("Выбирай флаг страны:", page, total_pages),
        reply_markup=kb,
    )


async def cb_list_library_flags(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    kb, page, total_pages = flag_library_page_keyboard()
    if not kb:
        await query.message.reply_text("Библиотека флагов стран пока пустая")
        return
    await query.message.reply_text(
        paginated_list_text("Выбирай флаг страны:", page, total_pages),
        reply_markup=kb,
    )


async def cb_flags_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    page = int(query.data.rsplit(":", 1)[1])
    kb, page, total_pages = flag_library_page_keyboard(page)
    if not kb:
        await query.edit_message_text("Библиотека флагов стран пока пустая")
        return
    await query.edit_message_text(
        paginated_list_text("Выбирай флаг страны:", page, total_pages),
        reply_markup=kb,
    )


async def cb_library_region_flags(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    parent_flag_id = query.data.split(":", 1)[1]
    parent_flag = find_library_flag(parent_flag_id)
    if not parent_flag or library_flag_type(parent_flag) != "country":
        await query.answer("Похоже, этого флага страны уже нет", show_alert=True)
        return

    kb, page, total_pages = regional_flag_library_page_keyboard(
        parent_flag_id
    )
    if not kb:
        await query.answer("У этой страны пока нет флагов регионов", show_alert=True)
        return

    await query.answer()
    await query.message.reply_text(
        paginated_list_text(
            f'Флаги регионов страны "{parent_flag["country_name"]}"',
            page,
            total_pages,
        ),
        reply_markup=kb,
    )


async def cb_library_region_flags_page(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()
    try:
        _, parent_flag_id, raw_page = query.data.split(":", 2)
        page = int(raw_page)
    except (TypeError, ValueError):
        await query.message.reply_text("Эта кнопка почему-то сломалась")
        return

    parent_flag = find_library_flag(parent_flag_id)
    if not parent_flag or library_flag_type(parent_flag) != "country":
        await query.edit_message_text("Похоже, этого флага страны уже нет")
        return
    kb, page, total_pages = regional_flag_library_page_keyboard(
        parent_flag_id, page
    )
    if not kb:
        await query.edit_message_text("У этой страны пока нет флагов регионов")
        return
    await query.edit_message_text(
        paginated_list_text(
            f'Флаги регионов страны "{parent_flag["country_name"]}"',
            page,
            total_pages,
        ),
        reply_markup=kb,
    )


async def cb_show_library_flag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    entry_id = query.data.split(":", 1)[1]
    entry = find_library_flag(entry_id)
    if not entry:
        await query.message.reply_text("Похоже, этого флага уже нет")
        return

    caption = (
        f'Флаг региона {entry["country_name"]}'
        if library_flag_type(entry) == "region"
        else f'Флаг {entry["country_name"]}'
    )
    if entry.get("flag_media_type") == "photo":
        kb = library_flag_preview_keyboard(
            entry, "Скинуть файлом"
        )
        await query.message.reply_photo(
            photo=entry["flag_file_id"], caption=caption, reply_markup=kb
        )
    else:
        kb = library_flag_preview_keyboard(
            entry, "Скинуть оригинал без сжатия"
        )
        try:
            preview = await build_flag_input_file(context, entry, ".png")
            await query.message.reply_photo(
                photo=preview, caption=caption, reply_markup=kb
            )
        except Exception as error:
            logger.warning(
                "Не удалось сделать фото-превью для флага %s: %s",
                entry["country_name"],
                error,
            )
            region_button = library_region_flags_button(entry)
            fallback_kb = (
                InlineKeyboardMarkup([[region_button]])
                if region_button
                else None
            )
            await query.message.reply_document(
                document=entry["flag_file_id"],
                caption=f"{caption}\n\nПревью не получилось, поэтому сразу скинул оригинал",
                reply_markup=fallback_kb,
            )


async def cb_download_library_flag(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()
    entry_id = query.data.split(":", 1)[1]
    entry = find_library_flag(entry_id)
    if not entry:
        await query.message.reply_text("Похоже, этого флага уже нет")
        return
    flag_label = (
        f'Флаг региона {entry["country_name"]}'
        if library_flag_type(entry) == "region"
        else f'Флаг {entry["country_name"]}'
    )
    if entry.get("flag_media_type") == "photo":
        file_to_send = await build_flag_input_file(context, entry, ".jpg")
        await query.message.reply_document(
            document=file_to_send,
            caption=(
                f"{flag_label} файлом\n\n"
                "Изначально его загрузили как фото, поэтому несжатого оригинала у бота нет"
            ),
        )
    else:
        await query.message.reply_document(
            document=entry["flag_file_id"],
            caption=f"Оригинал: {flag_label} без сжатия",
        )


async def cb_flag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    name = query.data.split(":", 1)[1]
    c = DATA["countries"].get(name)
    if not c or not c.get("flag_file_id"):
        await query.answer("Флаг куда-то запропастился", show_alert=True)
        return
    await query.answer()
    if c.get("flag_media_type") == "photo":
        await query.message.reply_photo(
            photo=c["flag_file_id"], caption=f"Флаг: {name}"
        )
    else:
        await query.message.reply_document(
            document=c["flag_file_id"], caption=f"Флаг: {name}"
        )


async def cb_herb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    name = query.data.split(":", 1)[1]
    c = DATA["countries"].get(name)
    if not c or not c.get("herb_file_id"):
        await query.answer("У этой страны герб пока не добавлен", show_alert=True)
        return
    await query.answer()
    await query.message.reply_document(document=c["herb_file_id"], caption=f"Герб: {name}")


async def cb_regions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    country_id = query.data.split(":", 1)[1]
    name, country = get_country_by_id(country_id)
    if not country:
        await query.answer("Похоже, этой страны уже нет", show_alert=True)
        return

    kb, page, total_pages = regions_page_keyboard(country)
    if not kb:
        await query.answer("Регионы сюда пока не добавили", show_alert=True)
        return

    await query.answer()
    text = (
        f'Вот регионы страны "{name}"\n\n'
        "Выбирай любой - покажу его столицу, описание и флаг"
    )
    await query.message.reply_text(
        paginated_list_text(text, page, total_pages),
        reply_markup=kb,
    )


async def cb_regions_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        _, country_id, raw_page = query.data.split(":", 2)
        page = int(raw_page)
    except (TypeError, ValueError):
        await query.message.reply_text("Эта кнопка почему-то сломалась")
        return

    name, country = get_country_by_id(country_id)
    if not country:
        await query.edit_message_text("Похоже, этой страны уже нет")
        return

    kb, page, total_pages = regions_page_keyboard(country, page)
    if not kb:
        await query.edit_message_text("Регионы сюда пока не добавили")
        return
    text = (
        f'Вот регионы страны "{name}"\n\n'
        "Выбирай любой - покажу его столицу, описание и флаг"
    )
    await query.edit_message_text(
        paginated_list_text(text, page, total_pages),
        reply_markup=kb,
    )


async def send_region_details(
    message,
    context: ContextTypes.DEFAULT_TYPE,
    country_name: str,
    country: dict,
    region: dict,
):
    title = f"<b>{escape(region['name'])}</b>"
    if region.get("flag_file_id"):
        if region.get("flag_media_type") == "photo":
            await message.reply_photo(
                photo=region["flag_file_id"],
                caption=title,
                parse_mode=ParseMode.HTML,
            )
        else:
            try:
                preview = await build_flag_input_file(
                    context,
                    {
                        "flag_file_id": region["flag_file_id"],
                        "country_name": region["name"],
                    },
                    ".png",
                )
                await message.reply_photo(
                    photo=preview,
                    caption=title,
                    parse_mode=ParseMode.HTML,
                )
            except Exception as error:
                logger.warning(
                    "Не удалось сделать превью флага региона %s: %s",
                    region["name"],
                    error,
                )
                await message.reply_document(
                    document=region["flag_file_id"],
                    caption=title,
                    parse_mode=ParseMode.HTML,
                )
    else:
        await message.reply_text(
            f"{title}\n\nФлаг этого региона пока не добавили",
            parse_mode=ParseMode.HTML,
        )

    await message.reply_text(
        build_region_info_text(region, country_name),
        parse_mode=ParseMode.HTML,
        reply_markup=region_info_buttons(country, region),
    )


async def cb_region_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        _, country_id, region_id = query.data.split(":", 2)
    except ValueError:
        await query.message.reply_text("Эта кнопка почему-то сломалась")
        return

    country_name, country = get_country_by_id(country_id)
    region = find_region(country, region_id)
    if not country or not region:
        await query.message.reply_text("Похоже, этого региона уже нет")
        return

    await send_region_details(
        query.message, context, country_name, country, region
    )


async def cb_region_flag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        _, country_id, region_id = query.data.split(":", 2)
    except ValueError:
        await query.answer("Эта кнопка почему-то сломалась", show_alert=True)
        return

    country_name, country = get_country_by_id(country_id)
    region = find_region(country, region_id) if country else None
    if not region:
        await query.answer("Похоже, этого региона уже нет", show_alert=True)
        return
    if not region.get("flag_file_id"):
        await query.answer("Флаг этого региона пока не добавили", show_alert=True)
        return

    await query.answer()
    caption = f'Флаг региона "{region["name"]}" - {country_name}'
    if region.get("flag_media_type") == "photo":
        await query.message.reply_photo(
            photo=region["flag_file_id"], caption=caption
        )
    else:
        await query.message.reply_document(
            document=region["flag_file_id"], caption=caption
        )


# ---------------------------------------------------------------------------
# /admhelp
# ---------------------------------------------------------------------------

async def cmd_admhelp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Не-а, эта команда только для админов")
        return
    text = (
        "<b>Вот что можно делать через админ-команды:</b>\n\n"
        "/addcountry - добавить новую страну (пошагово)\n"
        "/editcountry - отредактировать существующую страну\n"
        "/addregion - добавить регион\n"
        "/editregion - изменить или удалить регион\n"
        "/addhistory - загрузить историю DOCX, TXT, MD или PDF в журнал\n"
        "/addflag - добавить флаг страны или региона\n"
        "/editflag - изменить или удалить флаг из библиотеки\n"
        "Также можно просто отправить флаг файлом и написать страну в подписи\n"
        "/backup - скачать резервную копию базы\n"
        "/restore - восстановить базу из резервной копии\n"
        "/addadmin &lt;user_id&gt; - добавить нового админа\n"
        "/admhelp - это сообщение\n"
        "/cancel - отменить текущий диалог добавления/редактирования"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


# ---------------------------------------------------------------------------
# /addadmin
# ---------------------------------------------------------------------------

async def cmd_addadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("Не-а, эта команда только для админов")
        return

    if not context.args:
        await update.message.reply_text("Напиши вот так: /addadmin <user_id>")
        return

    try:
        new_admin_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Тут нужен числовой user_id, без букв и прочего")
        return

    if new_admin_id == MAIN_ADMIN_ID or new_admin_id in DATA["admins"]:
        await update.message.reply_text("Он и так уже админ")
        return

    DATA["admins"].append(new_admin_id)
    save_data()

    try:
        await context.bot.set_my_commands(
            admin_commands(), scope=BotCommandScopeChat(chat_id=new_admin_id)
        )
    except Exception as e:
        logger.warning("Не удалось выставить меню команд новому админу: %s", e)

    await update.message.reply_text(f"Готово! Пользователь {new_admin_id} теперь админ")


# ---------------------------------------------------------------------------
# Резервные копии
# ---------------------------------------------------------------------------

def database_counts(data: dict):
    countries_count = len(data.get("countries", {}))
    lore_countries_count = len(data.get("lore_countries", []))
    regions_count = sum(
        len(country.get("regions", []))
        for country in data.get("countries", {}).values()
    )
    flags_count = len(data.get("flag_library", []))
    histories_count = sum(
        len(country.get("event_histories", []))
        for country in data.get("lore_countries", [])
    )
    return (
        countries_count,
        lore_countries_count,
        regions_count,
        flags_count,
        histories_count,
    )


def clear_restore_context(context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("restore_backup_data", None)


async def send_database_backup(message, caption: str, filename_prefix="aurelia_backup"):
    save_data()
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    filename = f"{filename_prefix}_{timestamp}.json"
    with open(DATA_FILE, "rb") as backup_file:
        await message.reply_document(
            document=InputFile(backup_file, filename=filename),
            caption=caption,
        )


async def cmd_backup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Не-а, эта команда только для админов")
        return

    (
        countries_count,
        lore_countries_count,
        regions_count,
        flags_count,
        histories_count,
    ) = database_counts(DATA)
    await send_database_backup(
        update.message,
        (
            "Готово, вот свежая резервная копия\n\n"
            f"Стран: {countries_count}\n"
            f"Стран с лором: {lore_countries_count}\n"
            f"Регионов: {regions_count}\n"
            f"Флагов: {flags_count}\n"
            f"Историй: {histories_count}"
        ),
    )


async def restore_backup_start(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Не-а, эта команда только для админов")
        return ConversationHandler.END

    clear_restore_context(context)
    await update.message.reply_text(
        "Скинь JSON-файл резервной копии\n\n"
        "Сначала я всё проверю и только потом попрошу подтверждение"
    )
    return RESTORE_BACKUP_FILE


async def restore_backup_file(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    document = update.message.document
    if not document:
        await update.message.reply_text("Мне нужен именно JSON-файл резервной копии")
        return RESTORE_BACKUP_FILE
    if document.file_size and document.file_size > 20 * 1024 * 1024:
        await update.message.reply_text("Файл тяжелее 20 МБ, такую копию я не смогу проверить")
        return RESTORE_BACKUP_FILE

    try:
        telegram_file = await context.bot.get_file(document.file_id)
        payload = bytes(await telegram_file.download_as_bytearray())
        restored_data = load_backup_payload(payload)
    except Exception as error:
        logger.warning("Не удалось проверить резервную копию: %s", error)
        await update.message.reply_text(
            f"Не получилось прочитать эту копию\n\nПричина: {error}"
        )
        return RESTORE_BACKUP_FILE

    (
        countries_count,
        lore_countries_count,
        regions_count,
        flags_count,
        histories_count,
    ) = database_counts(restored_data)
    context.user_data["restore_backup_data"] = restored_data
    buttons = [[
        InlineKeyboardButton("Восстановить", callback_data="restorebackup:yes"),
        InlineKeyboardButton("Отмена", callback_data="restorebackup:no"),
    ]]
    await update.message.reply_text(
        "Копия выглядит нормально\n\n"
        f"Стран: {countries_count}\n"
        f"Стран с лором: {lore_countries_count}\n"
        f"Регионов: {regions_count}\n"
        f"Флагов: {flags_count}\n"
        f"Историй: {histories_count}\n\n"
        "Заменяем текущую базу?",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return RESTORE_BACKUP_CONFIRM


async def restore_backup_confirm(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()
    answer = query.data.split(":", 1)[1]
    if answer != "yes":
        clear_restore_context(context)
        await query.edit_message_text("Хорошо, текущую базу не трогаю")
        return ConversationHandler.END

    restored_data = context.user_data.get("restore_backup_data")
    if not restored_data:
        await query.edit_message_text("Данные копии потерялись. Запусти /restore ещё раз")
        clear_restore_context(context)
        return ConversationHandler.END

    try:
        await send_database_backup(
            query.message,
            "Страховочная копия базы перед восстановлением",
            filename_prefix="aurelia_before_restore",
        )
    except Exception as error:
        logger.warning("Не удалось отправить страховочную копию: %s", error)
        await query.message.reply_text(
            "Не смог отправить страховочную копию, поэтому восстановление отменил"
        )
        clear_restore_context(context)
        return ConversationHandler.END

    DATA.clear()
    DATA.update(restored_data)
    sync_entity_flags_to_library()
    save_data()
    (
        countries_count,
        lore_countries_count,
        regions_count,
        flags_count,
        histories_count,
    ) = database_counts(DATA)
    clear_restore_context(context)
    await query.edit_message_text(
        "Готово, базу восстановил\n\n"
        f"Стран: {countries_count}\n"
        f"Стран с лором: {lore_countries_count}\n"
        f"Регионов: {regions_count}\n"
        f"Флагов: {flags_count}\n"
        f"Историй: {histories_count}"
    )
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Загрузка истории для журнала событий
# ---------------------------------------------------------------------------

def clear_history_context(context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("history_lore_country_id", None)
    context.user_data.pop("history_new_country_name", None)


async def send_history_file_prompt(message, country_name: str):
    await message.reply_text(
        f'Скинь историю страны "{country_name}" файлом DOCX, TXT, MD или PDF\n\n'
        "Название файла станет названием истории\n\n"
        "Если [] и {} уже расставлены, бот использует их\n"
        "Если скобок нет, попробует сам найти даты и заголовки\n\n"
        "[] - большое событие\n"
        "{} - подсобытие, внутри может быть название или весь текст\n\n"
        "[Большое событие]\n"
        "Текст большого события\n\n"
        "{Дата - подсобытие}\n"
        "Текст подсобытия"
    )


async def add_history_start(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Не-а, эта команда только для админов")
        return ConversationHandler.END

    clear_history_context(context)
    kb, page, total_pages = add_history_lore_countries_keyboard()
    await update.message.reply_text(
        paginated_list_text(
            "Для какой страны загружаем лор?", page, total_pages
        ),
        reply_markup=kb,
    )
    return ADD_HISTORY_CHOOSE_COUNTRY


async def add_history_country_page(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()
    page = int(query.data.rsplit(":", 1)[1])
    kb, page, total_pages = add_history_lore_countries_keyboard(page)
    await query.edit_message_text(
        paginated_list_text(
            "Для какой страны загружаем лор?", page, total_pages
        ),
        reply_markup=kb,
    )
    return ADD_HISTORY_CHOOSE_COUNTRY


async def add_history_choose_country(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()
    lore_country_id = query.data.split(":", 1)[1]
    lore_country = get_lore_country_by_id(lore_country_id)
    if not lore_country:
        await query.message.reply_text("Похоже, этого лора уже нет")
        clear_history_context(context)
        return ConversationHandler.END

    context.user_data["history_lore_country_id"] = lore_country_id
    context.user_data.pop("history_new_country_name", None)
    await send_history_file_prompt(
        query.message, lore_country_display_name(lore_country)
    )
    return ADD_HISTORY_FILE


async def add_history_new_country_prompt(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()
    context.user_data.pop("history_lore_country_id", None)
    context.user_data.pop("history_new_country_name", None)
    await query.message.reply_text(
        "Напиши название страны для журнала\n\n"
        "Карточка и другая информация для этого не нужны"
    )
    return ADD_HISTORY_NEW_COUNTRY_NAME


async def add_history_new_country_name(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    country_name = normalize_user_text(update.message.text or "").strip()
    if not country_name:
        await update.message.reply_text("Название не должно быть пустым")
        return ADD_HISTORY_NEW_COUNTRY_NAME
    if len(country_name) > 100:
        await update.message.reply_text(
            "Название длиннее 100 символов - давай немного короче"
        )
        return ADD_HISTORY_NEW_COUNTRY_NAME
    existing_lore = get_lore_country_by_name(country_name)
    if existing_lore:
        context.user_data["history_lore_country_id"] = existing_lore["id"]
        context.user_data.pop("history_new_country_name", None)
        await update.message.reply_text(
            "Такая страна в журнале уже есть - добавлю историю туда"
        )
    else:
        context.user_data["history_new_country_name"] = country_name
        context.user_data.pop("history_lore_country_id", None)
    await send_history_file_prompt(update.message, country_name)
    return ADD_HISTORY_FILE


async def add_history_file(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    lore_country_id = context.user_data.get("history_lore_country_id")
    country = get_lore_country_by_id(lore_country_id)
    pending_country_name = context.user_data.get("history_new_country_name")
    if not country and not pending_country_name:
        await update.message.reply_text(
            "Страна для лора куда-то пропала - запусти /addhistory заново"
        )
        clear_history_context(context)
        return ConversationHandler.END
    country_name = (
        lore_country_display_name(country) if country else pending_country_name
    )

    document = update.message.document
    if not document:
        await update.message.reply_text("Нужен файл DOCX, TXT, MD или PDF")
        return ADD_HISTORY_FILE
    filename = normalize_user_text(document.file_name or "history.txt")
    extension = os.path.splitext(filename)[1].casefold()
    if extension not in (".docx", ".txt", ".md", ".pdf"):
        await update.message.reply_text(
            "Поддерживаются файлы DOCX, TXT, MD и PDF"
        )
        return ADD_HISTORY_FILE
    if document.file_size and document.file_size > 20 * 1024 * 1024:
        await update.message.reply_text("Файл тяжелее 20 МБ - возьми версию поменьше")
        return ADD_HISTORY_FILE

    title = normalize_user_text(os.path.splitext(os.path.basename(filename))[0])
    title = title.replace("_", " ").strip(" -_")
    title = re.sub(r"\s+\(\d+\)$", "", title).strip()
    if not title:
        await update.message.reply_text("У файла должно быть нормальное название")
        return ADD_HISTORY_FILE
    if len(title) > 100:
        await update.message.reply_text(
            "Название файла длиннее 100 символов - сначала переименуй его"
        )
        return ADD_HISTORY_FILE

    try:
        telegram_file = await context.bot.get_file(document.file_id)
        payload = bytes(await telegram_file.download_as_bytearray())
        source_text = extract_history_text(filename, payload)
        parsed = parse_event_journal_text(source_text)
    except Exception as error:
        logger.warning("Не удалось разобрать историю %s: %s", filename, error)
        await update.message.reply_text(
            f"Не получилось разобрать эту историю\n\nПричина: {error}"
        )
        return ADD_HISTORY_FILE

    linked_to_card = False
    if not country:
        country = {
            "id": uuid4().hex[:12],
            "name": pending_country_name,
            "linked_country_id": None,
            "event_histories": [],
        }
        _, info_country = get_country_by_name(pending_country_name)
        if info_country and not get_lore_for_country(info_country):
            country["linked_country_id"] = info_country["id"]
            info_country["lore_country_id"] = country["id"]
            linked_to_card = True
        DATA.setdefault("lore_countries", []).append(country)
    elif not country.get("linked_country_id"):
        _, info_country = get_country_by_name(country["name"])
        if info_country and not get_lore_for_country(info_country):
            link_lore_country(info_country, country)
            linked_to_card = True

    histories = country.setdefault("event_histories", [])
    existing_history = next(
        (
            history
            for history in histories
            if history.get("title", "").casefold() == title.casefold()
        ),
        None,
    )
    history = {
        "id": (
            existing_history["id"]
            if existing_history
            else uuid4().hex[:12]
        ),
        "title": title,
        "intro": parsed["intro"],
        "source_filename": filename,
        "source_file_id": document.file_id,
        "uploaded_at": datetime.now().isoformat(timespec="seconds"),
        "parser_mode": parsed["parser_mode"],
        "major_events": parsed["major_events"],
    }
    if existing_history:
        histories[histories.index(existing_history)] = history
        action = "обновил"
    else:
        histories.append(history)
        action = "добавил"

    save_data()
    subevents_count = sum(
        len(event.get("subevents", []))
        for event in history["major_events"]
    )
    all_events = list(history["major_events"])
    for major_event in history["major_events"]:
        all_events.extend(major_event.get("subevents", []))
    dated_events_count = sum(bool(event_date(event)) for event in all_events)
    parser_label = (
        "по [] и {}"
        if parsed["parser_mode"] == "markers"
        else "автоматически по датам и заголовкам"
    )
    await update.message.reply_text(
        f'Готово! Историю "{title}" {action} для страны "{country_name}"\n\n'
        f"Больших событий: {len(history['major_events'])}\n"
        f"Подсобытий: {subevents_count}\n"
        f"Дат найдено: {dated_events_count} из {len(all_events)}\n"
        f"Разбор: {parser_label}"
        + ("\nПривязал историю к карточке страны" if linked_to_card else "")
    )
    clear_history_context(context)
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Добавление страны - ConversationHandler
# ---------------------------------------------------------------------------

async def add_country_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Не-а, эта команда только для админов")
        return ConversationHandler.END

    context.user_data["new_country"] = {
        "id": uuid4().hex[:12],
        "regions": [],
        "event_histories": [],
    }
    await update.message.reply_text(
        "Окей, добавляем новую страну. Для начала скинь её карточку картинкой"
    )
    return ADD_CARD


async def add_card(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.photo:
        file_id = update.message.photo[-1].file_id
    elif update.message.document:
        file_id = update.message.document.file_id
    else:
        await update.message.reply_text("Мне нужна именно картинка с карточкой страны")
        return ADD_CARD

    context.user_data["new_country"]["card_file_id"] = file_id
    await update.message.reply_text("Карточку поймал. Теперь напиши название страны")
    return ADD_NAME


async def add_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = normalize_user_text(update.message.text.strip())
    if name in DATA["countries"]:
        await update.message.reply_text(
            "Страна с таким названием уже есть. Давай другое название"
        )
        return ADD_NAME
    context.user_data["new_country"]["name"] = name
    await update.message.reply_text("Кто у неё лидер?")
    return ADD_LEADER


async def add_leader(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_country"]["leader"] = normalize_user_text(update.message.text.strip())
    await update.message.reply_text("А столица как называется?")
    return ADD_CAPITAL


async def add_capital(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_country"]["capital"] = normalize_user_text(update.message.text.strip())
    await update.message.reply_text(
        "На каком она континенте? Если их несколько, перечисли через запятую"
    )
    return ADD_CONTINENT


async def add_continent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = normalize_user_text(update.message.text.strip())
    continents = [c.strip() for c in raw.split(",") if c.strip()]
    context.user_data["new_country"]["continents"] = continents
    await update.message.reply_text(
        "Какая у страны площадь в км²? Напиши только число, например 2059580,3"
    )
    return ADD_AREA


async def add_area(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        area = normalize_area_value(update.message.text)
    except ValueError as error:
        await update.message.reply_text(str(error))
        return ADD_AREA
    context.user_data["new_country"]["area_km2"] = area
    await update.message.reply_text(
        'С кем граничит страна? Перечисли через запятую или напиши "нет"'
    )
    return ADD_BORDERS


async def add_borders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_country"]["borders"] = parse_borders_value(
        update.message.text
    )
    await update.message.reply_text(
        "Теперь выбери готовый флаг из библиотеки или загрузи новый",
        reply_markup=flag_choice_keyboard("addcountryflag"),
    )
    return ADD_FLAG


async def add_country_flag_choice(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()
    choice = query.data.split(":", 1)[1]
    country = context.user_data.get("new_country")
    if not country:
        await query.message.reply_text("Данные страны потерялись. Давай начнём заново")
        return ConversationHandler.END

    if choice.startswith("page:"):
        page = int(choice.split(":", 1)[1])
        await query.edit_message_text(
            "Теперь выбери готовый флаг из библиотеки или загрузи новый",
            reply_markup=flag_choice_keyboard("addcountryflag", page=page),
        )
        return ADD_FLAG

    if choice == "new":
        await query.message.reply_text(
            "Скинь новый флаг. Лучше файлом, тогда Telegram его не пережмёт"
        )
        return ADD_FLAG

    entry = find_library_flag(choice)
    if not entry:
        await query.message.reply_text(
            "Похоже, этого флага уже нет. Выбери другой или загрузи новый",
            reply_markup=flag_choice_keyboard("addcountryflag"),
        )
        return ADD_FLAG

    country["flag_file_id"] = entry["flag_file_id"]
    country["flag_media_type"] = entry.get("flag_media_type", "document")
    await query.message.reply_text(
        f'Взял флаг "{entry["country_name"]}" из библиотеки\n\n'
        'Теперь герб. Лучше тоже файлом. Если герба нет, просто напиши "нет"'
    )
    return ADD_HERB


async def add_flag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.document:
        file_id = update.message.document.file_id
        media_type = "document"
    elif update.message.photo:
        file_id = update.message.photo[-1].file_id
        media_type = "photo"
        await update.message.reply_text(
            "Флаг пришёл как обычное фото, так что Telegram мог его немного пережевать. "
            "В следующий раз лучше кидай файлом"
        )
    else:
        await update.message.reply_text("Мне нужна картинка с флагом")
        return ADD_FLAG

    context.user_data["new_country"]["flag_file_id"] = file_id
    context.user_data["new_country"]["flag_media_type"] = media_type
    await update.message.reply_text(
        "Теперь герб. Лучше тоже файлом. Если герба нет, просто напиши \"нет\""
    )
    return ADD_HERB


async def add_herb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text and update.message.text.strip().lower() in ("нет", "-", "no"):
        context.user_data["new_country"]["herb_file_id"] = None
    elif update.message.document:
        context.user_data["new_country"]["herb_file_id"] = update.message.document.file_id
    elif update.message.photo:
        context.user_data["new_country"]["herb_file_id"] = update.message.photo[-1].file_id
        await update.message.reply_text(
            "Герб пришёл как фото, поэтому Telegram мог его немного сжать"
        )
    else:
        await update.message.reply_text(
            "Скинь герб картинкой или напиши \"нет\", если его нет"
        )
        return ADD_HERB

    await update.message.reply_text("Расскажи немного о стране. Тут максимум 500 символов")
    return ADD_DESC


async def add_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = normalize_user_text(update.message.text.strip())
    if len(text) > 500:
        await update.message.reply_text(
            f"Получилось длинновато: {len(text)} символов. Нужно уложиться в 500"
        )
        return ADD_DESC
    context.user_data["new_country"]["description"] = text
    await update.message.reply_text(
        "И последнее - скинь ссылки на лор. Если их несколько, раздели запятыми"
    )
    return ADD_LORE


async def add_lore(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = normalize_user_text(update.message.text.strip())
    links = [l.strip() for l in raw.split(",") if l.strip()]
    context.user_data["new_country"]["lore_links"] = links

    c = context.user_data["new_country"]
    c["lore_country_id"] = None
    candidates = available_lore_countries(linked=False)
    if candidates:
        kb, page, total_pages = new_country_lore_link_keyboard()
        await update.message.reply_text(
            paginated_list_text(
                f'Привязать к стране "{c["name"]}" уже загруженную историю?',
                page,
                total_pages,
            ),
            reply_markup=kb,
        )
        return ADD_LINK_HISTORY

    finalize_new_country(c)
    await update.message.reply_text(f'Готово! Страна "{c["name"]}" теперь в боте')
    context.user_data.pop("new_country", None)
    return ConversationHandler.END


def finalize_new_country(country: dict, lore_country=None):
    DATA["countries"][country["name"]] = country
    if lore_country:
        link_lore_country(country, lore_country)
    upsert_library_flag(
        country["name"],
        country["flag_file_id"],
        country.get("flag_media_type", "document"),
        flag_type="country",
        country_id=country.get("id"),
    )
    save_data()


async def add_country_lore_page(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()
    country = context.user_data.get("new_country")
    if not country:
        await query.edit_message_text("Данные страны потерялись - начни заново")
        return ConversationHandler.END
    page = int(query.data.rsplit(":", 1)[1])
    kb, page, total_pages = new_country_lore_link_keyboard(page)
    await query.edit_message_text(
        paginated_list_text(
            f'Привязать к стране "{country["name"]}" уже загруженную историю?',
            page,
            total_pages,
        ),
        reply_markup=kb,
    )
    return ADD_LINK_HISTORY


async def add_country_link_history(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()
    country = context.user_data.get("new_country")
    if not country:
        await query.edit_message_text("Данные страны потерялись - начни заново")
        return ConversationHandler.END
    choice = query.data.split(":", 1)[1]
    if choice == "skip":
        finalize_new_country(country)
        await query.edit_message_text(
            f'Готово! Страна "{country["name"]}" теперь в боте\n\n'
            "Историю пока не привязывал"
        )
        context.user_data.pop("new_country", None)
        return ConversationHandler.END

    lore_country = get_lore_country_by_id(choice)
    if not lore_country or lore_country.get("linked_country_id"):
        kb, page, total_pages = new_country_lore_link_keyboard()
        await query.edit_message_text(
            paginated_list_text(
                "Эта история уже недоступна - выбери другую",
                page,
                total_pages,
            ),
            reply_markup=kb,
        )
        return ADD_LINK_HISTORY

    finalize_new_country(country, lore_country)
    await query.edit_message_text(
        f'Готово! Страна "{country["name"]}" теперь в боте\n\n'
        f'Историю страны "{lore_country["name"]}" тоже привязал'
    )
    context.user_data.pop("new_country", None)
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Окей, всё отменил")
    return ConversationHandler.END


restore_backup_conv = ConversationHandler(
    entry_points=[CommandHandler("restore", restore_backup_start)],
    states={
        RESTORE_BACKUP_FILE: [
            MessageHandler(filters.Document.ALL, restore_backup_file)
        ],
        RESTORE_BACKUP_CONFIRM: [
            CallbackQueryHandler(
                restore_backup_confirm, pattern=r"^restorebackup:(yes|no)$"
            )
        ],
    },
    fallbacks=[CommandHandler("cancel", cancel)],
)


add_history_conv = ConversationHandler(
    entry_points=[
        CommandHandler(["addhistory", "addevents"], add_history_start)
    ],
    states={
        ADD_HISTORY_CHOOSE_COUNTRY: [
            CallbackQueryHandler(
                add_history_choose_country,
                pattern=r"^addhistorycountry:[^:]+$",
            ),
            CallbackQueryHandler(
                add_history_new_country_prompt,
                pattern=r"^addhistorynew$",
            ),
            CallbackQueryHandler(
                add_history_country_page,
                pattern=r"^addhistorypage:\d+$",
            ),
        ],
        ADD_HISTORY_NEW_COUNTRY_NAME: [
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                add_history_new_country_name,
            )
        ],
        ADD_HISTORY_FILE: [
            MessageHandler(filters.Document.ALL & ~filters.COMMAND, add_history_file)
        ],
    },
    fallbacks=[CommandHandler("cancel", cancel)],
)


search_country_conv = ConversationHandler(
    entry_points=[
        CommandHandler("search", search_country_start),
        CallbackQueryHandler(search_country_start, pattern=r"^search_countries$"),
    ],
    states={
        SEARCH_COUNTRY_QUERY: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, search_country_value)
        ]
    },
    fallbacks=[CommandHandler("cancel", cancel)],
)


event_search_conv = ConversationHandler(
    entry_points=[
        CommandHandler(["searchevents", "eventsearch"], event_search_start),
        CallbackQueryHandler(event_search_start, pattern=r"^search_events$"),
    ],
    states={
        EVENT_SEARCH_QUERY: [
            CallbackQueryHandler(
                event_search_back_to_menu, pattern=r"^mainmenu$"
            ),
            CallbackQueryHandler(
                event_search_page, pattern=r"^eventsearchpage:\d+$"
            ),
            CallbackQueryHandler(
                event_search_open_major,
                pattern=r"^journalmajor:[^:]+:[^:]+:[^:]+$",
            ),
            CallbackQueryHandler(
                event_search_open_subevent,
                pattern=r"^journalsub:[^:]+:[^:]+:[^:]+:[^:]+$",
            ),
            MessageHandler(
                filters.TEXT & ~filters.COMMAND, event_search_query
            ),
        ]
    },
    fallbacks=[CommandHandler("cancel", cancel)],
)


add_country_conv = ConversationHandler(
    entry_points=[CommandHandler("addcountry", add_country_start)],
    states={
        ADD_CARD: [MessageHandler(filters.PHOTO | filters.Document.IMAGE, add_card)],
        ADD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_name)],
        ADD_LEADER: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_leader)],
        ADD_CAPITAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_capital)],
        ADD_CONTINENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_continent)],
        ADD_AREA: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_area)],
        ADD_BORDERS: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_borders)],
        ADD_FLAG: [
            CallbackQueryHandler(
                add_country_flag_choice, pattern=r"^addcountryflag:"
            ),
            MessageHandler(filters.PHOTO | filters.Document.IMAGE, add_flag),
        ],
        ADD_HERB: [
            MessageHandler(
                (filters.PHOTO | filters.Document.IMAGE | filters.TEXT) & ~filters.COMMAND,
                add_herb,
            )
        ],
        ADD_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_desc)],
        ADD_LORE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_lore)],
        ADD_LINK_HISTORY: [
            CallbackQueryHandler(
                add_country_link_history,
                pattern=r"^addcountrylore:(?:skip|[^:]+)$",
            ),
            CallbackQueryHandler(
                add_country_lore_page,
                pattern=r"^addcountrylorepage:\d+$",
            ),
        ],
    },
    fallbacks=[CommandHandler("cancel", cancel)],
)


# ---------------------------------------------------------------------------
# Редактирование страны - ConversationHandler
# ---------------------------------------------------------------------------

async def edit_country_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Не-а, эта команда только для админов")
        return ConversationHandler.END

    kb = countries_keyboard(prefix="editsel")
    if not kb:
        await update.message.reply_text("Стран пока нет, так что редактировать нечего")
        return ConversationHandler.END

    await update.message.reply_text("Какую страну будем править?", reply_markup=kb)
    return EDIT_CHOOSE_COUNTRY


async def edit_choose_country(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    name = query.data.split(":", 1)[1]
    if name not in DATA["countries"]:
        await query.message.reply_text("Похоже, этой страны уже нет")
        return ConversationHandler.END

    context.user_data["edit_country_name"] = name

    buttons = [
        [InlineKeyboardButton(label, callback_data=f"editfield:{key}")]
        for key, label in EDIT_FIELDS.items()
    ]
    await query.message.reply_text(
        f'Окей, правим страну "{name}". Что именно меняем?',
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return EDIT_CHOOSE_FIELD


async def edit_choose_field(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    field = query.data.split(":", 1)[1]
    context.user_data["edit_field"] = field

    prompts = {
        "name": "Как теперь будет называться страна?",
        "leader": "Кто теперь у неё лидер?",
        "capital": "Как теперь называется столица?",
        "continents": "Перечисли континенты через запятую:",
        "area_km2": "Напиши новую площадь в км² только числом:",
        "borders": 'Перечисли соседние страны через запятую или напиши "нет":',
        "card": "Скинь новую карточку картинкой:",
        "flag": "Скинь новый флаг. Лучше файлом, без сжатия:",
        "herb": "Скинь новый герб файлом или напиши \"нет\", если его больше нет:",
        "description": "Напиши новое описание. Максимум 500 символов:",
        "lore_links": "Скинь новые ссылки на лор через запятую:",
    }
    await query.message.reply_text(prompts[field])
    return EDIT_VALUE


async def edit_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = context.user_data.get("edit_country_name")
    field = context.user_data.get("edit_field")
    c = DATA["countries"].get(name)
    if not c:
        await update.message.reply_text("Похоже, страна пропала из списка. Я всё отменил")
        return ConversationHandler.END

    if field in ("card", "flag"):
        if update.message.document:
            file_id = update.message.document.file_id
            media_type = "document"
        elif update.message.photo:
            file_id = update.message.photo[-1].file_id
            media_type = "photo"
            await update.message.reply_text(
                "Картинка пришла как фото, так что Telegram мог её немного сжать"
            )
        else:
            await update.message.reply_text("Тут нужна именно картинка")
            return EDIT_VALUE
        c["card_file_id" if field == "card" else "flag_file_id"] = file_id
        if field == "flag":
            c["flag_media_type"] = media_type

    elif field == "herb":
        if update.message.text and update.message.text.strip().lower() in ("нет", "-", "no"):
            c["herb_file_id"] = None
        elif update.message.document:
            c["herb_file_id"] = update.message.document.file_id
        elif update.message.photo:
            c["herb_file_id"] = update.message.photo[-1].file_id
        else:
            await update.message.reply_text("Скинь герб картинкой или напиши \"нет\"")
            return EDIT_VALUE

    elif field == "continents":
        if not update.message.text:
            await update.message.reply_text("Континенты нужно прислать текстом")
            return EDIT_VALUE
        raw = normalize_user_text(update.message.text.strip())
        c["continents"] = [x.strip() for x in raw.split(",") if x.strip()]

    elif field == "area_km2":
        if not update.message.text:
            await update.message.reply_text("Площадь нужно прислать текстом")
            return EDIT_VALUE
        try:
            c["area_km2"] = normalize_area_value(update.message.text)
        except ValueError as error:
            await update.message.reply_text(str(error))
            return EDIT_VALUE

    elif field == "borders":
        if not update.message.text:
            await update.message.reply_text("Границы нужно прислать текстом")
            return EDIT_VALUE
        c["borders"] = parse_borders_value(update.message.text)

    elif field == "lore_links":
        raw = normalize_user_text(update.message.text.strip())
        c["lore_links"] = [x.strip() for x in raw.split(",") if x.strip()]

    elif field == "description":
        text = normalize_user_text(update.message.text.strip())
        if len(text) > 500:
            await update.message.reply_text(
                f"Получилось {len(text)} символов, а можно максимум 500. Давай покороче"
            )
            return EDIT_VALUE
        c["description"] = text

    elif field == "name":
        new_name = normalize_user_text(update.message.text.strip())
        if new_name != name and new_name in DATA["countries"]:
            await update.message.reply_text("Такое название уже занято. Давай другое")
            return EDIT_VALUE
        old_name = name
        DATA["countries"].pop(name)
        c["name"] = new_name
        DATA["countries"][new_name] = c
        for other_country in DATA["countries"].values():
            other_country["borders"] = [
                new_name if border.casefold() == old_name.casefold() else border
                for border in other_country.get("borders", [])
            ]
        name = new_name

    else:  # leader, capital
        c[field] = normalize_user_text(update.message.text.strip())

    upsert_library_flag(
        c["name"],
        c.get("flag_file_id"),
        c.get("flag_media_type", "document"),
        flag_type="country",
        country_id=c.get("id"),
    )
    save_data()
    await update.message.reply_text("Готово, всё сохранил")
    context.user_data.pop("edit_country_name", None)
    context.user_data.pop("edit_field", None)
    return ConversationHandler.END


edit_country_conv = ConversationHandler(
    entry_points=[CommandHandler("editcountry", edit_country_start)],
    states={
        EDIT_CHOOSE_COUNTRY: [CallbackQueryHandler(edit_choose_country, pattern=r"^editsel:")],
        EDIT_CHOOSE_FIELD: [CallbackQueryHandler(edit_choose_field, pattern=r"^editfield:")],
        EDIT_VALUE: [
            MessageHandler(
                (filters.TEXT | filters.PHOTO | filters.Document.IMAGE) & ~filters.COMMAND,
                edit_value,
            )
        ],
    },
    fallbacks=[CommandHandler("cancel", cancel)],
)


# ---------------------------------------------------------------------------
# Добавление региона - ConversationHandler
# ---------------------------------------------------------------------------

async def add_region_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Не-а, эта команда только для админов")
        return ConversationHandler.END

    kb = countries_keyboard(prefix="addregionsel", use_ids=True)
    if not kb:
        await update.message.reply_text(
            "Сначала нужна хотя бы одна страна. Добавь её через /addcountry"
        )
        return ConversationHandler.END

    clear_region_context(context)
    await update.message.reply_text(
        "К какой стране относится новый регион?", reply_markup=kb
    )
    return ADD_REGION_CHOOSE_COUNTRY


async def add_region_choose_country(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()
    country_id = query.data.split(":", 1)[1]
    country_name, country = get_country_by_id(country_id)
    if not country:
        await query.message.reply_text("Похоже, этой страны уже нет")
        clear_region_context(context)
        return ConversationHandler.END

    context.user_data["region_country_id"] = country_id
    await query.message.reply_text(
        f'Добавляем регион для страны "{country_name}". Как он называется?'
    )
    return ADD_REGION_NAME


async def add_region_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    country_id = context.user_data.get("region_country_id")
    _, country = get_country_by_id(country_id)
    if not country:
        await update.message.reply_text("Страна куда-то пропала из списка. Я всё отменил")
        clear_region_context(context)
        return ConversationHandler.END

    name = normalize_user_text(update.message.text.strip())
    if not name:
        await update.message.reply_text("Без названия не получится. Напиши хоть что-нибудь")
        return ADD_REGION_NAME
    if len(name) > 100:
        await update.message.reply_text(
            f"Название длинновато: {len(name)} символов. Нужно уложиться в 100"
        )
        return ADD_REGION_NAME
    if region_name_exists(country, name):
        await update.message.reply_text(
            "Регион с таким названием уже есть. Давай другое"
        )
        return ADD_REGION_NAME

    context.user_data["new_region_name"] = name
    await update.message.reply_text("Как называется столица региона?")
    return ADD_REGION_CAPITAL


async def add_region_capital(update: Update, context: ContextTypes.DEFAULT_TYPE):
    capital = normalize_user_text(update.message.text.strip())
    if not capital:
        await update.message.reply_text("Столица не может быть пустой")
        return ADD_REGION_CAPITAL
    if len(capital) > 100:
        await update.message.reply_text(
            f"Название столицы длинновато: {len(capital)} символов. Максимум 100"
        )
        return ADD_REGION_CAPITAL

    context.user_data["new_region_capital"] = capital
    await update.message.reply_text(
        "Можешь добавить короткое описание региона. Максимум 500 символов. "
        "Если описание не нужно, напиши \"нет\""
    )
    return ADD_REGION_DESC


async def add_region_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = normalize_user_text(update.message.text.strip())
    if text.casefold() in ("нет", "-", "no"):
        text = ""
    if len(text) > 500:
        await update.message.reply_text(
            f"Получилось {len(text)} символов, а можно максимум 500"
        )
        return ADD_REGION_DESC

    context.user_data["new_region_description"] = text
    country_id = context.user_data.get("region_country_id")
    country_name, country = get_country_by_id(country_id)
    parent_flag = find_country_library_flag(
        country_id=country_id,
        country_name=country_name if country else None,
    )
    parent_flag_id = parent_flag.get("id") if parent_flag else None
    context.user_data["region_parent_flag_id"] = parent_flag_id
    await update.message.reply_text(
        "Теперь выбери готовый флаг из библиотеки, загрузи новый или оставь регион без флага",
        reply_markup=flag_choice_keyboard(
            "addregionflag",
            allow_none=True,
            flag_type="region",
            parent_flag_id=parent_flag_id,
            restrict_to_parent=True,
        ),
    )
    return ADD_REGION_FLAG


async def finish_add_region(
    message,
    context: ContextTypes.DEFAULT_TYPE,
    file_id,
    media_type,
):
    country_id = context.user_data.get("region_country_id")
    country_name, country = get_country_by_id(country_id)
    region_name = context.user_data.get("new_region_name")
    region_capital = context.user_data.get("new_region_capital")
    region_description = context.user_data.get("new_region_description", "")
    if not country or not region_name or not region_capital:
        await message.reply_text("Что-то потерялось по дороге. Я всё отменил")
        clear_region_context(context)
        return ConversationHandler.END

    region = {
        "id": uuid4().hex[:12],
        "name": region_name,
        "capital": region_capital,
        "description": region_description,
        "flag_file_id": file_id,
        "flag_media_type": media_type,
    }
    country.setdefault("regions", []).append(region)
    sync_entity_flags_to_library()
    save_data()
    await message.reply_text(
        f'Готово! Регион "{region_name}" появился у страны "{country_name}"'
    )
    clear_region_context(context)
    return ConversationHandler.END


async def add_region_flag_choice(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()
    choice = query.data.split(":", 1)[1]

    if choice.startswith("page:"):
        page = int(choice.split(":", 1)[1])
        parent_flag_id = context.user_data.get("region_parent_flag_id")
        await query.edit_message_text(
            "Теперь выбери готовый флаг из библиотеки, загрузи новый или оставь регион без флага",
            reply_markup=flag_choice_keyboard(
                "addregionflag",
                allow_none=True,
                page=page,
                flag_type="region",
                parent_flag_id=parent_flag_id,
                restrict_to_parent=True,
            ),
        )
        return ADD_REGION_FLAG

    if choice == "new":
        await query.message.reply_text(
            "Скинь новый флаг региона файлом или фото"
        )
        return ADD_REGION_FLAG
    if choice == "none":
        return await finish_add_region(query.message, context, None, None)

    parent_flag_id = context.user_data.get("region_parent_flag_id")
    entry = find_library_flag(choice)
    if (
        not entry
        or library_flag_type(entry) != "region"
        or entry.get("parent_flag_id") != parent_flag_id
    ):
        await query.message.reply_text(
            "Этот флаг не привязан к флагу выбранной страны. Выбери другой или загрузи новый",
            reply_markup=flag_choice_keyboard(
                "addregionflag",
                allow_none=True,
                flag_type="region",
                parent_flag_id=parent_flag_id,
                restrict_to_parent=True,
            ),
        )
        return ADD_REGION_FLAG

    return await finish_add_region(
        query.message,
        context,
        entry["flag_file_id"],
        entry.get("flag_media_type", "document"),
    )


async def add_region_flag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text and update.message.text.strip().lower() in ("нет", "-", "no"):
        file_id = None
        media_type = None
    elif update.message.document:
        file_id = update.message.document.file_id
        media_type = "document"
    elif update.message.photo:
        file_id = update.message.photo[-1].file_id
        media_type = "photo"
        await update.message.reply_text(
            "Флаг пришёл как фото, так что Telegram мог его немного сжать"
        )
    else:
        await update.message.reply_text(
            "Скинь картинку с флагом или напиши \"нет\""
        )
        return ADD_REGION_FLAG

    return await finish_add_region(
        update.message, context, file_id, media_type
    )


add_region_conv = ConversationHandler(
    entry_points=[CommandHandler("addregion", add_region_start)],
    states={
        ADD_REGION_CHOOSE_COUNTRY: [
            CallbackQueryHandler(
                add_region_choose_country, pattern=r"^addregionsel:"
            )
        ],
        ADD_REGION_NAME: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, add_region_name)
        ],
        ADD_REGION_CAPITAL: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, add_region_capital)
        ],
        ADD_REGION_DESC: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, add_region_desc)
        ],
        ADD_REGION_FLAG: [
            CallbackQueryHandler(
                add_region_flag_choice, pattern=r"^addregionflag:"
            ),
            MessageHandler(
                (filters.TEXT | filters.PHOTO | filters.Document.IMAGE)
                & ~filters.COMMAND,
                add_region_flag,
            )
        ],
    },
    fallbacks=[CommandHandler("cancel", cancel)],
)


# ---------------------------------------------------------------------------
# Редактирование и удаление региона - ConversationHandler
# ---------------------------------------------------------------------------

async def edit_region_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Не-а, эта команда только для админов")
        return ConversationHandler.END

    countries_with_regions = {
        name: country
        for name, country in DATA["countries"].items()
        if country.get("regions")
    }
    if not countries_with_regions:
        await update.message.reply_text(
            "Регионов пока нет. Первый можно добавить через /addregion"
        )
        return ConversationHandler.END

    buttons = []
    row = []
    for index, name in enumerate(sorted(countries_with_regions), start=1):
        country = countries_with_regions[name]
        row.append(
            InlineKeyboardButton(
                name, callback_data=f"editregioncountry:{country['id']}"
            )
        )
        if index % 2 == 0:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    clear_region_context(context)
    await update.message.reply_text(
        "В какой стране находится нужный регион?",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return EDIT_REGION_CHOOSE_COUNTRY


async def edit_region_choose_country(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()
    country_id = query.data.split(":", 1)[1]
    country_name, country = get_country_by_id(country_id)
    if not country:
        await query.message.reply_text("Похоже, этой страны уже нет")
        clear_region_context(context)
        return ConversationHandler.END

    kb = regions_keyboard(country, prefix="editregionsel")
    if not kb:
        await query.message.reply_text("Похоже, у этой страны регионов уже не осталось")
        clear_region_context(context)
        return ConversationHandler.END

    context.user_data["region_country_id"] = country_id
    await query.message.reply_text(
        f'Окей, страна "{country_name}". Какой регион будем править?', reply_markup=kb
    )
    return EDIT_REGION_CHOOSE_REGION


async def edit_region_choose_region(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()
    try:
        _, country_id, region_id = query.data.split(":", 2)
    except ValueError:
        await query.message.reply_text("Эта кнопка почему-то сломалась")
        clear_region_context(context)
        return ConversationHandler.END

    if country_id != context.user_data.get("region_country_id"):
        await query.message.reply_text("Страна успела измениться. Давай начнём заново")
        clear_region_context(context)
        return ConversationHandler.END

    _, country = get_country_by_id(country_id)
    region = find_region(country, region_id)
    if not region:
        await query.message.reply_text("Похоже, этого региона уже нет")
        clear_region_context(context)
        return ConversationHandler.END

    context.user_data["edit_region_id"] = region_id
    buttons = [
        [InlineKeyboardButton("✏️ Название", callback_data="editregionfield:name")],
        [InlineKeyboardButton("🏙 Столица", callback_data="editregionfield:capital")],
        [InlineKeyboardButton("📝 Описание", callback_data="editregionfield:description")],
        [InlineKeyboardButton("🚩 Флаг", callback_data="editregionfield:flag")],
        [InlineKeyboardButton("🗑 Удалить", callback_data="editregionfield:delete")],
    ]
    await query.message.reply_text(
        f'Правим регион "{region["name"]}". Что именно меняем?',
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return EDIT_REGION_CHOOSE_FIELD


async def edit_region_choose_field(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()
    field = query.data.split(":", 1)[1]
    if field not in ("name", "capital", "description", "flag", "delete"):
        await query.message.reply_text("Не понял, что нужно сделать. Давай начнём заново")
        clear_region_context(context)
        return ConversationHandler.END

    if field == "delete":
        country_id = context.user_data.get("region_country_id")
        _, country = get_country_by_id(country_id)
        region = find_region(country, context.user_data.get("edit_region_id"))
        if not region:
            await query.message.reply_text("Похоже, этого региона уже нет")
            clear_region_context(context)
            return ConversationHandler.END
        buttons = [[
            InlineKeyboardButton("Да, удалить", callback_data="regiondelete:yes"),
            InlineKeyboardButton("Нет", callback_data="regiondelete:no"),
        ]]
        await query.message.reply_text(
            f'Точно удалить регион "{region["name"]}"?',
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return EDIT_REGION_CONFIRM_DELETE

    context.user_data["edit_region_field"] = field
    prompts = {
        "name": "Как теперь будет называться регион?",
        "capital": "Как теперь называется столица региона?",
        "description": (
            "Напиши новое описание региона или \"нет\", если его нужно убрать"
        ),
        "flag": "Скинь новый флаг или напиши \"нет\", если флаг нужно убрать",
    }
    prompt = prompts[field]
    await query.message.reply_text(prompt)
    return EDIT_REGION_VALUE


async def edit_region_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    country_id = context.user_data.get("region_country_id")
    country_name, country = get_country_by_id(country_id)
    region = find_region(country, context.user_data.get("edit_region_id"))
    field = context.user_data.get("edit_region_field")
    if not country or not region:
        await update.message.reply_text("Похоже, регион уже пропал из списка. Я всё отменил")
        clear_region_context(context)
        return ConversationHandler.END

    if field == "name":
        if not update.message.text:
            await update.message.reply_text("Название нужно прислать обычным текстом")
            return EDIT_REGION_VALUE
        new_name = normalize_user_text(update.message.text.strip())
        if not new_name:
            await update.message.reply_text("Без названия не получится. Напиши хоть что-нибудь")
            return EDIT_REGION_VALUE
        if len(new_name) > 100:
            await update.message.reply_text(
                f"Название длинновато: {len(new_name)} символов. Нужно уложиться в 100"
            )
            return EDIT_REGION_VALUE
        if region_name_exists(country, new_name, exclude_id=region["id"]):
            await update.message.reply_text(
                "Регион с таким названием уже есть. Давай другое"
            )
            return EDIT_REGION_VALUE
        region["name"] = new_name

    elif field == "capital":
        if not update.message.text:
            await update.message.reply_text("Столицу нужно прислать обычным текстом")
            return EDIT_REGION_VALUE
        capital = normalize_user_text(update.message.text.strip())
        if not capital:
            await update.message.reply_text("Столица не может быть пустой")
            return EDIT_REGION_VALUE
        if len(capital) > 100:
            await update.message.reply_text(
                f"Название столицы длинновато: {len(capital)} символов. Максимум 100"
            )
            return EDIT_REGION_VALUE
        region["capital"] = capital

    elif field == "description":
        if not update.message.text:
            await update.message.reply_text("Описание нужно прислать обычным текстом")
            return EDIT_REGION_VALUE
        description = normalize_user_text(update.message.text.strip())
        if description.casefold() in ("нет", "-", "no"):
            description = ""
        if len(description) > 500:
            await update.message.reply_text(
                f"Получилось {len(description)} символов, а можно максимум 500"
            )
            return EDIT_REGION_VALUE
        region["description"] = description

    elif field == "flag":
        if update.message.text and update.message.text.strip().lower() in ("нет", "-", "no"):
            region["flag_file_id"] = None
            region["flag_media_type"] = None
        elif update.message.document:
            region["flag_file_id"] = update.message.document.file_id
            region["flag_media_type"] = "document"
        elif update.message.photo:
            region["flag_file_id"] = update.message.photo[-1].file_id
            region["flag_media_type"] = "photo"
            await update.message.reply_text(
                "Флаг пришёл как фото, так что Telegram мог его немного сжать"
            )
        else:
            await update.message.reply_text(
                "Скинь картинку с флагом или напиши \"нет\""
            )
            return EDIT_REGION_VALUE
    else:
        await update.message.reply_text("Не понял, что менять. Я всё отменил")
        clear_region_context(context)
        return ConversationHandler.END

    if region.get("flag_file_id"):
        sync_entity_flags_to_library()
    else:
        DATA["flag_library"] = [
            entry
            for entry in DATA.get("flag_library", [])
            if not (
                library_flag_type(entry) == "region"
                and entry.get("region_id") == region.get("id")
            )
        ]
    save_data()
    await update.message.reply_text("Готово, регион обновил")
    clear_region_context(context)
    return ConversationHandler.END


async def edit_region_confirm_delete(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()
    answer = query.data.split(":", 1)[1]
    if answer != "yes":
        await query.message.reply_text("Хорошо, ничего удалять не буду")
        clear_region_context(context)
        return ConversationHandler.END

    country_id = context.user_data.get("region_country_id")
    _, country = get_country_by_id(country_id)
    region = find_region(country, context.user_data.get("edit_region_id"))
    if not country or not region:
        await query.message.reply_text("Похоже, этого региона уже и так нет")
        clear_region_context(context)
        return ConversationHandler.END

    country["regions"].remove(region)
    DATA["flag_library"] = [
        entry
        for entry in DATA.get("flag_library", [])
        if not (
            library_flag_type(entry) == "region"
            and entry.get("region_id") == region.get("id")
        )
    ]
    save_data()
    await query.message.reply_text(f'Готово, регион "{region["name"]}" удалил')
    clear_region_context(context)
    return ConversationHandler.END


edit_region_conv = ConversationHandler(
    entry_points=[CommandHandler("editregion", edit_region_start)],
    states={
        EDIT_REGION_CHOOSE_COUNTRY: [
            CallbackQueryHandler(
                edit_region_choose_country, pattern=r"^editregioncountry:"
            )
        ],
        EDIT_REGION_CHOOSE_REGION: [
            CallbackQueryHandler(
                edit_region_choose_region, pattern=r"^editregionsel:"
            )
        ],
        EDIT_REGION_CHOOSE_FIELD: [
            CallbackQueryHandler(
                edit_region_choose_field, pattern=r"^editregionfield:"
            )
        ],
        EDIT_REGION_VALUE: [
            MessageHandler(
                (filters.TEXT | filters.PHOTO | filters.Document.IMAGE)
                & ~filters.COMMAND,
                edit_region_value,
            )
        ],
        EDIT_REGION_CONFIRM_DELETE: [
            CallbackQueryHandler(
                edit_region_confirm_delete, pattern=r"^regiondelete:(yes|no)$"
            )
        ],
    },
    fallbacks=[CommandHandler("cancel", cancel)],
)


# ---------------------------------------------------------------------------
# Библиотека флагов - добавление, редактирование и удаление
# ---------------------------------------------------------------------------

async def quick_add_library_flag(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    if not is_admin(update.effective_user.id):
        return
    if not update.message.document or not update.message.caption:
        return
    if any(
        key in context.user_data
        for key in (
            "new_country",
            "edit_country_name",
            "region_country_id",
            "new_library_flag_type",
            "new_library_flag_name",
            "new_library_flag_parent_id",
            "new_library_flag_country_id",
            "new_library_flag_region_id",
            "edit_library_flag_id",
        )
    ):
        return

    name = normalize_user_text(update.message.caption.splitlines()[0].strip())
    lowered = name.casefold()
    if lowered.startswith("флаг:"):
        name = name[5:].strip()
    elif lowered.startswith("флаг "):
        name = name[5:].strip()

    if not name:
        await update.message.reply_text("В подписи нужно написать название страны")
        return
    if len(name) > 100:
        await update.message.reply_text("Название в подписи длиннее 100 символов")
        return
    if library_flag_name_exists(name, flag_type="country"):
        await update.message.reply_text(
            "Флаг с такой подписью уже есть. Изменить его можно через /editflag"
        )
        return

    _, country = get_country_by_name(name)
    upsert_library_flag(
        name,
        update.message.document.file_id,
        "document",
        flag_type="country",
        country_id=country.get("id") if country else None,
    )
    save_data()
    await update.message.reply_text(
        f'Готово! Флаг "{name}" появился в библиотеке'
    )

async def add_library_flag_start(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Не-а, эта команда только для админов")
        return ConversationHandler.END

    clear_library_flag_context(context)
    buttons = [[
        InlineKeyboardButton(
            "Флаг страны", callback_data="addlibrarytype:country"
        ),
        InlineKeyboardButton(
            "Флаг региона", callback_data="addlibrarytype:region"
        ),
    ]]
    await update.message.reply_text(
        "Это флаг страны или региона?",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return ADD_LIBRARY_FLAG_TYPE


async def add_library_flag_type(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()
    flag_type = query.data.split(":", 1)[1]
    if flag_type not in ("country", "region"):
        await query.message.reply_text("Не понял, какой это флаг")
        return ADD_LIBRARY_FLAG_TYPE

    context.user_data["new_library_flag_type"] = flag_type
    if flag_type == "country":
        await query.message.reply_text(
            "Напиши название страны или территории для флага"
        )
        return ADD_LIBRARY_FLAG_NAME

    if not country_library_flags():
        await query.message.reply_text(
            "Сначала добавь хотя бы один флаг страны через /addflag"
        )
        clear_library_flag_context(context)
        return ConversationHandler.END

    kb, page, total_pages = country_flag_selection_page_keyboard(
        "addlibraryparent", "addlibraryparentpage"
    )
    await query.message.reply_text(
        paginated_list_text(
            "К флагу какой страны привязать регион?", page, total_pages
        ),
        reply_markup=kb,
    )
    return ADD_LIBRARY_FLAG_REGION_PARENT


async def add_library_flag_name(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    name = normalize_user_text(update.message.text.strip())
    if not name:
        await update.message.reply_text("Название не может быть пустым")
        return ADD_LIBRARY_FLAG_NAME
    if len(name) > 100:
        await update.message.reply_text(
            f"Название длинновато: {len(name)} символов. Максимум 100"
        )
        return ADD_LIBRARY_FLAG_NAME
    if library_flag_name_exists(name, flag_type="country"):
        await update.message.reply_text(
            "Флаг с такой подписью уже есть. Изменить его можно через /editflag"
        )
        return ADD_LIBRARY_FLAG_NAME

    context.user_data["new_library_flag_name"] = name
    await update.message.reply_text(
        "Теперь скинь флаг именно файлом, чтобы он сохранился без сжатия"
    )
    return ADD_LIBRARY_FLAG_FILE


async def add_library_flag_parent_page(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()
    page = int(query.data.rsplit(":", 1)[1])
    kb, page, total_pages = country_flag_selection_page_keyboard(
        "addlibraryparent", "addlibraryparentpage", page
    )
    await query.edit_message_text(
        paginated_list_text(
            "К флагу какой страны привязать регион?", page, total_pages
        ),
        reply_markup=kb,
    )
    return ADD_LIBRARY_FLAG_REGION_PARENT


async def add_library_flag_parent_choice(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()
    parent_flag_id = query.data.split(":", 1)[1]
    parent_flag = find_library_flag(parent_flag_id)
    if not parent_flag or library_flag_type(parent_flag) != "country":
        await query.message.reply_text("Похоже, этого флага страны уже нет")
        clear_library_flag_context(context)
        return ConversationHandler.END

    context.user_data["new_library_flag_parent_id"] = parent_flag_id
    await query.message.reply_text(
        f'Как называется регион страны "{parent_flag["country_name"]}"?'
    )
    return ADD_LIBRARY_FLAG_REGION_NAME


async def add_library_flag_region_name(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    parent_flag_id = context.user_data.get("new_library_flag_parent_id")
    parent_flag = find_library_flag(parent_flag_id)
    if not parent_flag or library_flag_type(parent_flag) != "country":
        await update.message.reply_text(
            "Флаг страны куда-то пропал. Запусти /addflag заново"
        )
        clear_library_flag_context(context)
        return ConversationHandler.END

    region_name = normalize_user_text(update.message.text.strip())
    if not region_name:
        await update.message.reply_text("Название региона не может быть пустым")
        return ADD_LIBRARY_FLAG_REGION_NAME
    if len(region_name) > 100:
        await update.message.reply_text(
            f"Название длинновато: {len(region_name)} символов. Максимум 100"
        )
        return ADD_LIBRARY_FLAG_REGION_NAME
    if library_flag_name_exists(
        region_name,
        flag_type="region",
        parent_flag_id=parent_flag_id,
    ):
        await update.message.reply_text(
            "Флаг региона с таким названием уже есть. Изменить его можно через /editflag"
        )
        return ADD_LIBRARY_FLAG_REGION_NAME

    context.user_data["new_library_flag_name"] = region_name
    await update.message.reply_text(
        f'Теперь скинь флаг региона "{region_name}" именно файлом, без сжатия'
    )
    return ADD_LIBRARY_FLAG_FILE


async def add_library_flag_file(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    name = context.user_data.get("new_library_flag_name")
    if not name:
        await update.message.reply_text("Название потерялось. Давай начнём заново")
        clear_library_flag_context(context)
        return ConversationHandler.END
    if not update.message.document:
        await update.message.reply_text("Флаг нужно отправить именно файлом")
        return ADD_LIBRARY_FLAG_FILE

    flag_type = context.user_data.get("new_library_flag_type", "country")
    if flag_type == "region":
        parent_flag_id = context.user_data.get("new_library_flag_parent_id")
        parent_flag = find_library_flag(parent_flag_id)
        if not parent_flag or library_flag_type(parent_flag) != "country":
            await update.message.reply_text(
                "Флаг страны куда-то пропал. Запусти /addflag заново"
            )
            clear_library_flag_context(context)
            return ConversationHandler.END

        country_id = parent_flag.get("country_id")
        _, country = get_country_by_id(country_id)
        region = find_region_by_name(country, name)
        if region:
            region["flag_file_id"] = update.message.document.file_id
            region["flag_media_type"] = "document"
        upsert_library_flag(
            name,
            update.message.document.file_id,
            "document",
            flag_type="region",
            parent_flag_id=parent_flag_id,
            country_id=country_id,
            region_id=region.get("id") if region else None,
        )
        result_text = (
            f'Готово! Флаг региона "{name}" привязан к флагу страны '
            f'"{parent_flag["country_name"]}"'
        )
    else:
        _, country = get_country_by_name(name)
        upsert_library_flag(
            name,
            update.message.document.file_id,
            "document",
            flag_type="country",
            country_id=country.get("id") if country else None,
        )
        result_text = f'Готово! Флаг "{name}" появился в библиотеке'

    save_data()
    await update.message.reply_text(result_text)
    clear_library_flag_context(context)
    return ConversationHandler.END


add_library_flag_conv = ConversationHandler(
    entry_points=[CommandHandler("addflag", add_library_flag_start)],
    states={
        ADD_LIBRARY_FLAG_TYPE: [
            CallbackQueryHandler(
                add_library_flag_type,
                pattern=r"^addlibrarytype:(country|region)$",
            )
        ],
        ADD_LIBRARY_FLAG_NAME: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, add_library_flag_name)
        ],
        ADD_LIBRARY_FLAG_REGION_PARENT: [
            CallbackQueryHandler(
                add_library_flag_parent_choice,
                pattern=r"^addlibraryparent:[^:]+$",
            ),
            CallbackQueryHandler(
                add_library_flag_parent_page,
                pattern=r"^addlibraryparentpage:\d+$",
            ),
        ],
        ADD_LIBRARY_FLAG_REGION_NAME: [
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                add_library_flag_region_name,
            )
        ],
        ADD_LIBRARY_FLAG_FILE: [
            MessageHandler(
                (filters.TEXT | filters.PHOTO | filters.Document.IMAGE)
                & ~filters.COMMAND,
                add_library_flag_file,
            )
        ],
    },
    fallbacks=[CommandHandler("cancel", cancel)],
)


async def edit_library_flag_start(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Не-а, эта команда только для админов")
        return ConversationHandler.END

    kb = flag_library_keyboard(prefix="editlibraryflag")
    if not kb:
        await update.message.reply_text(
            "Библиотека пока пустая. Первый флаг можно добавить через /addflag"
        )
        return ConversationHandler.END

    clear_library_flag_context(context)
    await update.message.reply_text("Какой флаг будем править?", reply_markup=kb)
    return EDIT_LIBRARY_FLAG_CHOOSE


async def edit_library_flag_choose(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()
    entry_id = query.data.split(":", 1)[1]
    entry = find_library_flag(entry_id)
    if not entry:
        await query.message.reply_text("Похоже, этого флага уже нет")
        return ConversationHandler.END

    context.user_data["edit_library_flag_id"] = entry_id
    buttons = []
    if not entry.get("country_id"):
        buttons.append(
            [InlineKeyboardButton("✏️ Название", callback_data="editlibraryfield:name")]
        )
    buttons.append(
        [InlineKeyboardButton("🖼 Файл", callback_data="editlibraryfield:file")]
    )
    if not (
        library_flag_type(entry) == "country"
        and (
            entry.get("country_id")
            or regional_library_flags(entry["id"])
        )
    ):
        buttons.append(
            [InlineKeyboardButton("🗑 Удалить", callback_data="editlibraryfield:delete")]
        )
    await query.message.reply_text(
        f'Правим флаг "{entry["country_name"]}". Что меняем?',
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return EDIT_LIBRARY_FLAG_FIELD


async def edit_library_flag_field(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()
    field = query.data.split(":", 1)[1]
    if field not in ("name", "file", "delete"):
        await query.message.reply_text("Не понял, что нужно сделать")
        clear_library_flag_context(context)
        return ConversationHandler.END

    if field == "delete":
        entry = find_library_flag(
            context.user_data.get("edit_library_flag_id")
        )
        if not entry:
            await query.message.reply_text("Похоже, этого флага уже нет")
            clear_library_flag_context(context)
            return ConversationHandler.END
        if library_flag_type(entry) == "country" and entry.get("country_id"):
            await query.message.reply_text(
                "Основной флаг страны удалить нельзя, но его можно заменить"
            )
            clear_library_flag_context(context)
            return ConversationHandler.END
        if (
            library_flag_type(entry) == "country"
            and regional_library_flags(entry["id"])
        ):
            await query.message.reply_text(
                "Сначала удали связанные флаги регионов, а потом уже флаг страны"
            )
            clear_library_flag_context(context)
            return ConversationHandler.END
        buttons = [[
            InlineKeyboardButton("Да, удалить", callback_data="libraryflagdelete:yes"),
            InlineKeyboardButton("Нет", callback_data="libraryflagdelete:no"),
        ]]
        await query.message.reply_text(
            f'Точно удалить флаг "{entry["country_name"]}"?',
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return EDIT_LIBRARY_FLAG_CONFIRM_DELETE

    context.user_data["edit_library_flag_field"] = field
    entry = find_library_flag(context.user_data.get("edit_library_flag_id"))
    if field == "name" and entry and entry.get("country_id"):
        await query.message.reply_text(
            "Название связанного флага меняется вместе со страной или регионом"
        )
        clear_library_flag_context(context)
        return ConversationHandler.END
    prompt = (
        "Напиши новую подпись для флага"
        if field == "name"
        else "Скинь новый флаг именно файлом, без сжатия"
    )
    await query.message.reply_text(prompt)
    return EDIT_LIBRARY_FLAG_VALUE


async def edit_library_flag_value(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    entry = find_library_flag(context.user_data.get("edit_library_flag_id"))
    field = context.user_data.get("edit_library_flag_field")
    if not entry:
        await update.message.reply_text("Похоже, этого флага уже нет")
        clear_library_flag_context(context)
        return ConversationHandler.END

    if field == "name":
        if not update.message.text:
            await update.message.reply_text("Название нужно прислать текстом")
            return EDIT_LIBRARY_FLAG_VALUE
        name = normalize_user_text(update.message.text.strip())
        if not name:
            await update.message.reply_text("Название не может быть пустым")
            return EDIT_LIBRARY_FLAG_VALUE
        if len(name) > 100:
            await update.message.reply_text(
                f"Название длинновато: {len(name)} символов. Максимум 100"
            )
            return EDIT_LIBRARY_FLAG_VALUE
        if library_flag_name_exists(
            name,
            exclude_id=entry["id"],
            flag_type=library_flag_type(entry),
            parent_flag_id=entry.get("parent_flag_id"),
        ):
            await update.message.reply_text("Флаг с такой подписью уже есть")
            return EDIT_LIBRARY_FLAG_VALUE
        entry["country_name"] = name

    elif field == "file":
        if not update.message.document:
            await update.message.reply_text("Флаг нужно отправить именно файлом")
            return EDIT_LIBRARY_FLAG_VALUE
        entry["flag_file_id"] = update.message.document.file_id
        entry["flag_media_type"] = "document"
        country_name, country = get_country_by_id(entry.get("country_id"))
        if library_flag_type(entry) == "region":
            region = find_region(country, entry.get("region_id"))
            if region:
                region["flag_file_id"] = entry["flag_file_id"]
                region["flag_media_type"] = "document"
        elif country:
            country["flag_file_id"] = entry["flag_file_id"]
            country["flag_media_type"] = "document"
    else:
        await update.message.reply_text("Не понял, что менять")
        clear_library_flag_context(context)
        return ConversationHandler.END

    save_data()
    await update.message.reply_text("Готово, флаг обновил")
    clear_library_flag_context(context)
    return ConversationHandler.END


async def edit_library_flag_confirm_delete(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()
    answer = query.data.split(":", 1)[1]
    if answer != "yes":
        await query.message.reply_text("Хорошо, ничего удалять не буду")
        clear_library_flag_context(context)
        return ConversationHandler.END

    entry = find_library_flag(context.user_data.get("edit_library_flag_id"))
    if not entry:
        await query.message.reply_text("Похоже, этого флага уже и так нет")
        clear_library_flag_context(context)
        return ConversationHandler.END

    if library_flag_type(entry) == "region":
        _, country = get_country_by_id(entry.get("country_id"))
        region = find_region(country, entry.get("region_id"))
        if region:
            region["flag_file_id"] = None
            region["flag_media_type"] = None
    DATA["flag_library"].remove(entry)
    save_data()
    await query.message.reply_text(
        f'Готово, флаг "{entry["country_name"]}" удалил'
    )
    clear_library_flag_context(context)
    return ConversationHandler.END


edit_library_flag_conv = ConversationHandler(
    entry_points=[CommandHandler("editflag", edit_library_flag_start)],
    states={
        EDIT_LIBRARY_FLAG_CHOOSE: [
            CallbackQueryHandler(
                edit_library_flag_choose, pattern=r"^editlibraryflag:"
            )
        ],
        EDIT_LIBRARY_FLAG_FIELD: [
            CallbackQueryHandler(
                edit_library_flag_field, pattern=r"^editlibraryfield:"
            )
        ],
        EDIT_LIBRARY_FLAG_VALUE: [
            MessageHandler(
                (filters.TEXT | filters.PHOTO | filters.Document.IMAGE)
                & ~filters.COMMAND,
                edit_library_flag_value,
            )
        ],
        EDIT_LIBRARY_FLAG_CONFIRM_DELETE: [
            CallbackQueryHandler(
                edit_library_flag_confirm_delete,
                pattern=r"^libraryflagdelete:(yes|no)$",
            )
        ],
    },
    fallbacks=[CommandHandler("cancel", cancel)],
)


# ---------------------------------------------------------------------------
# Меню команд
# ---------------------------------------------------------------------------

def public_commands():
    return [
        BotCommand("start", "Начать"),
        BotCommand("countries", "Список стран"),
        BotCommand("search", "Найти страну"),
        BotCommand("searchevents", "Найти событие"),
        BotCommand("journal", "Журнал событий"),
        BotCommand("flags", "Библиотека флагов"),
    ]


def admin_commands():
    return public_commands() + [
        BotCommand("addcountry", "Добавить страну"),
        BotCommand("editcountry", "Редактировать страну"),
        BotCommand("addregion", "Добавить регион"),
        BotCommand("editregion", "Изменить или удалить регион"),
        BotCommand("addhistory", "Загрузить историю в журнал"),
        BotCommand("addflag", "Добавить флаг страны или региона"),
        BotCommand("editflag", "Изменить или удалить флаг"),
        BotCommand("backup", "Скачать резервную копию"),
        BotCommand("restore", "Восстановить резервную копию"),
        BotCommand("addadmin", "Добавить админа"),
        BotCommand("admhelp", "Админ-команды"),
        BotCommand("cancel", "Отменить диалог"),
    ]


async def post_init(application: Application):
    if sync_entity_flags_to_library():
        save_data()

    await application.bot.set_my_commands(public_commands(), scope=BotCommandScopeDefault())
    try:
        await application.bot.set_my_commands(
            admin_commands(), scope=BotCommandScopeChat(chat_id=MAIN_ADMIN_ID)
        )
    except Exception as e:
        logger.warning("Не удалось выставить меню команд главному админу: %s", e)

    for admin_id in DATA["admins"]:
        try:
            await application.bot.set_my_commands(
                admin_commands(), scope=BotCommandScopeChat(chat_id=admin_id)
            )
        except Exception as e:
            logger.warning("Не удалось выставить меню команд админу %s: %s", admin_id, e)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    if BOT_TOKEN == "PASTE_YOUR_TOKEN_HERE":
        raise RuntimeError(
            "Укажите токен бота через переменную окружения BOT_TOKEN "
            "или впишите его прямо в код (переменная BOT_TOKEN)."
        )

    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("countries", cmd_countries))
    application.add_handler(CommandHandler("journal", cmd_journal))
    application.add_handler(CommandHandler("flags", cmd_flags))
    application.add_handler(CommandHandler("admhelp", cmd_admhelp))
    application.add_handler(CommandHandler("addadmin", cmd_addadmin))
    application.add_handler(CommandHandler("backup", cmd_backup))

    application.add_handler(restore_backup_conv)
    application.add_handler(add_history_conv)
    application.add_handler(event_search_conv)
    application.add_handler(add_country_conv)
    application.add_handler(edit_country_conv)
    application.add_handler(search_country_conv)
    application.add_handler(add_region_conv)
    application.add_handler(edit_region_conv)
    application.add_handler(add_library_flag_conv)
    application.add_handler(edit_library_flag_conv)
    application.add_handler(
        MessageHandler(filters.Document.IMAGE, quick_add_library_flag)
    )

    application.add_handler(CallbackQueryHandler(cb_list_countries, pattern=r"^list_countries$"))
    application.add_handler(
        CallbackQueryHandler(cb_main_menu, pattern=r"^mainmenu$")
    )
    application.add_handler(
        CallbackQueryHandler(cb_journal, pattern=r"^journal$")
    )
    application.add_handler(
        CallbackQueryHandler(cb_journal_page, pattern=r"^journalpage:\d+$")
    )
    application.add_handler(
        CallbackQueryHandler(
            cb_journal_country, pattern=r"^journalcountry:[^:]+$"
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            cb_journal_country_page,
            pattern=r"^journalcpage:[^:]+:\d+$",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            cb_journal_history,
            pattern=r"^journalhistory:[^:]+:[^:]+$",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            cb_journal_history_page,
            pattern=r"^journalhpage:[^:]+:[^:]+:\d+$",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            cb_journal_major,
            pattern=r"^journalmajor:[^:]+:[^:]+:[^:]+$",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            cb_journal_major_page,
            pattern=r"^journalmpage:[^:]+:[^:]+:[^:]+:\d+$",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            cb_journal_major_text,
            pattern=r"^journalmajortext:[^:]+:[^:]+:[^:]+$",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            cb_journal_subevent,
            pattern=r"^journalsub:[^:]+:[^:]+:[^:]+:[^:]+$",
        )
    )
    application.add_handler(
        CallbackQueryHandler(cb_countries_page, pattern=r"^countriespage:\d+$")
    )
    application.add_handler(CallbackQueryHandler(cb_show_country, pattern=r"^country:"))
    application.add_handler(
        CallbackQueryHandler(cb_show_search_country, pattern=r"^searchcountry:")
    )
    application.add_handler(CallbackQueryHandler(cb_flag, pattern=r"^flag:"))
    application.add_handler(CallbackQueryHandler(cb_herb, pattern=r"^herb:"))
    application.add_handler(CallbackQueryHandler(cb_regions, pattern=r"^regions:"))
    application.add_handler(
        CallbackQueryHandler(
            cb_regions_page, pattern=r"^regionspage:[^:]+:\d+$"
        )
    )
    application.add_handler(
        CallbackQueryHandler(cb_region_info, pattern=r"^regioninfo:")
    )
    application.add_handler(
        CallbackQueryHandler(cb_region_flag, pattern=r"^regionflag:")
    )
    application.add_handler(
        CallbackQueryHandler(cb_list_library_flags, pattern=r"^list_library_flags$")
    )
    application.add_handler(
        CallbackQueryHandler(cb_flags_page, pattern=r"^flagspage:\d+$")
    )
    application.add_handler(
        CallbackQueryHandler(
            cb_library_region_flags, pattern=r"^libraryregions:[^:]+$"
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            cb_library_region_flags_page,
            pattern=r"^libraryregionspage:[^:]+:\d+$",
        )
    )
    application.add_handler(
        CallbackQueryHandler(cb_show_library_flag, pattern=r"^libraryflag:")
    )
    application.add_handler(
        CallbackQueryHandler(cb_download_library_flag, pattern=r"^downloadflag:")
    )

    logger.info('АУРЕЛИЯ INFO BOT запущен, v: "%s"', BOT_VERSION)
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
