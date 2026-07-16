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
import logging
import os
import random
import tempfile
from datetime import datetime
from html import escape
from uuid import uuid4

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
BOT_VERSION = "1.2.0"

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
            d.setdefault("admins", [])
            d.setdefault("countries", {})
            d.setdefault("flag_library", [])

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
                for field in ("leader", "capital", "description"):
                    if isinstance(country.get(field), str):
                        country[field] = normalize_user_text(country[field])
                if country.get("flag_file_id"):
                    country["flag_media_type"] = (
                        country.get("flag_media_type") or "document"
                    )
                for field in ("continents", "lore_links"):
                    if isinstance(country.get(field), list):
                        country[field] = [
                            normalize_user_text(item) if isinstance(item, str) else item
                            for item in country[field]
                        ]
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
                    }
                )
            d["flag_library"] = flag_library
            return d
    return {"admins": [], "countries": {}, "flag_library": []}


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
    ADD_FLAG,
    ADD_HERB,
    ADD_DESC,
    ADD_LORE,
) = range(9)

SEARCH_COUNTRY_QUERY = 10

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

(ADD_LIBRARY_FLAG_NAME, ADD_LIBRARY_FLAG_FILE) = range(50, 52)

(
    EDIT_LIBRARY_FLAG_CHOOSE,
    EDIT_LIBRARY_FLAG_FIELD,
    EDIT_LIBRARY_FLAG_VALUE,
    EDIT_LIBRARY_FLAG_CONFIRM_DELETE,
) = range(60, 64)

(RESTORE_BACKUP_FILE, RESTORE_BACKUP_CONFIRM) = range(70, 72)

EDIT_FIELDS = {
    "name": "Название",
    "leader": "Лидер",
    "capital": "Столица",
    "continents": "Континент(ы)",
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


def get_country_by_id(country_id: str):
    """Возвращает текущее имя страны и данные по короткому внутреннему ID."""
    for name, country in DATA["countries"].items():
        if country.get("id") == country_id:
            return name, country
    return None, None


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


def library_flag_name_exists(name: str, exclude_id=None) -> bool:
    wanted = name.casefold()
    return any(
        entry.get("id") != exclude_id
        and entry.get("country_name", "").casefold() == wanted
        for entry in DATA.get("flag_library", [])
    )


def find_library_flag_by_name(name: str):
    wanted = name.casefold()
    for entry in DATA.get("flag_library", []):
        if entry.get("country_name", "").casefold() == wanted:
            return entry
    return None


def upsert_library_flag(
    name: str, file_id: str, media_type: str = "document"
) -> bool:
    """Добавляет флаг в библиотеку или обновляет одноимённый."""
    if not file_id:
        return False

    name = normalize_user_text(name.strip())
    media_type = media_type if media_type in ("document", "photo") else "document"
    entry = find_library_flag_by_name(name)
    if entry:
        changed = any(
            (
                entry.get("country_name") != name,
                entry.get("flag_file_id") != file_id,
                entry.get("flag_media_type", "document") != media_type,
            )
        )
        entry["country_name"] = name
        entry["flag_file_id"] = file_id
        entry["flag_media_type"] = media_type
        return changed

    DATA.setdefault("flag_library", []).append(
        {
            "id": uuid4().hex[:12],
            "country_name": name,
            "flag_file_id": file_id,
            "flag_media_type": media_type,
        }
    )
    return True


def region_library_flag_name(country_name: str, region_name: str) -> str:
    return f"{region_name} - {country_name}"


def sync_entity_flags_to_library() -> bool:
    """Подтягивает в библиотеку флаги всех уже сохранённых стран и регионов."""
    changed = False
    for country_name, country in DATA.get("countries", {}).items():
        if country.get("flag_file_id"):
            changed = upsert_library_flag(
                country_name,
                country["flag_file_id"],
                country.get("flag_media_type", "document"),
            ) or changed

        for region in country.get("regions", []):
            if not region.get("flag_file_id"):
                continue
            changed = upsert_library_flag(
                region_library_flag_name(country_name, region["name"]),
                region["flag_file_id"],
                region.get("flag_media_type", "document"),
            ) or changed
    return changed


def flag_choice_keyboard(prefix: str, allow_none: bool = False, page=0):
    buttons = [
        [InlineKeyboardButton("Загрузить новый флаг", callback_data=f"{prefix}:new")]
    ]
    if allow_none:
        buttons.append(
            [InlineKeyboardButton("Без флага", callback_data=f"{prefix}:none")]
        )

    entries = sorted(
        DATA.get("flag_library", []),
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
        "new_region_name",
        "new_region_capital",
        "new_region_description",
        "edit_region_id",
        "edit_region_field",
    ):
        context.user_data.pop(key, None)


def clear_library_flag_context(context: ContextTypes.DEFAULT_TYPE):
    for key in (
        "new_library_flag_name",
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


def regions_page_keyboard(country: dict, page=0):
    regions = sorted(
        country.get("regions", []), key=lambda item: item["name"].casefold()
    )
    page_regions, page, total_pages = paginate_items(regions, page)
    buttons = [
        [
            InlineKeyboardButton(
                region["name"],
                callback_data=f"regioninfo:{country['id']}:{region['id']}",
            )
        ]
        for region in page_regions
    ]
    navigation = pagination_row(
        page, total_pages, f"regionspage:{country['id']}"
    )
    if navigation:
        buttons.append(navigation)
    keyboard = InlineKeyboardMarkup(buttons) if page_regions else None
    return keyboard, page, total_pages


def flag_library_keyboard(prefix="libraryflag"):
    buttons = [
        [
            InlineKeyboardButton(
                f"Флаг {entry['country_name']}",
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
    entries = sorted(
        DATA.get("flag_library", []),
        key=lambda item: item["country_name"].casefold(),
    )
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


def build_info_text(c: dict) -> str:
    continents = ", ".join(c.get("continents", []))
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
    lore = c.get("lore_links", [])
    lore_text = "\n\n".join(lore) if lore else "-"
    text = (
        f"<b>{escape(c['name'])}</b>\n\n"
        f"<b>Кто у руля:</b> {escape(c.get('leader', '-'))}\n"
        f"Столица: {escape(c.get('capital', '-'))}\n"
        f"Где находится: {escape(continents or '-')}\n\n"
        f"<b>Регионы:</b>\n{escape(region_names or '-')}\n\n"
        f"<b>Немного о стране:</b>\n"
        f"<blockquote expandable>{escape(c.get('description') or '-')}</blockquote>\n\n"
        f"Почитать лор:\n{escape(lore_text)}"
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
    return InlineKeyboardMarkup(rows)


# ---------------------------------------------------------------------------
# /start и /countries
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📋 Список стран", callback_data="list_countries")],
            [InlineKeyboardButton("🔎 Поиск страны", callback_data="search_countries")],
            [InlineKeyboardButton("Библиотека флагов", callback_data="list_library_flags")],
        ]
    )
    await update.message.reply_text(
        "Привет! Это бот-путеводитель по Аурелии\n\n"
        "Тут можно полистать страны, посмотреть их флаги, гербы, регионы и лор. "
        "Жми кнопку ниже и выбирай, куда заглянем\n\n"
        f'v: "{BOT_VERSION}"',
        reply_markup=kb,
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
        await update.message.reply_text("Библиотека флагов пока пустая")
        return
    await update.message.reply_text(
        paginated_list_text("Выбирай флаг:", page, total_pages),
        reply_markup=kb,
    )


async def cb_list_library_flags(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    kb, page, total_pages = flag_library_page_keyboard()
    if not kb:
        await query.message.reply_text("Библиотека флагов пока пустая")
        return
    await query.message.reply_text(
        paginated_list_text("Выбирай флаг:", page, total_pages),
        reply_markup=kb,
    )


async def cb_flags_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    page = int(query.data.rsplit(":", 1)[1])
    kb, page, total_pages = flag_library_page_keyboard(page)
    if not kb:
        await query.edit_message_text("Библиотека флагов пока пустая")
        return
    await query.edit_message_text(
        paginated_list_text("Выбирай флаг:", page, total_pages),
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

    caption = f'Флаг {entry["country_name"]}'
    if entry.get("flag_media_type") == "photo":
        kb = InlineKeyboardMarkup(
            [[
                InlineKeyboardButton(
                    "Скинуть файлом",
                    callback_data=f"downloadflag:{entry['id']}",
                )
            ]]
        )
        await query.message.reply_photo(
            photo=entry["flag_file_id"], caption=caption, reply_markup=kb
        )
    else:
        kb = InlineKeyboardMarkup(
            [[
                InlineKeyboardButton(
                    "Скинуть оригинал без сжатия",
                    callback_data=f"downloadflag:{entry['id']}",
                )
            ]]
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
            await query.message.reply_document(
                document=entry["flag_file_id"],
                caption=f"{caption}\n\nПревью не получилось, поэтому сразу скинул оригинал",
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
    if entry.get("flag_media_type") == "photo":
        file_to_send = await build_flag_input_file(context, entry, ".jpg")
        await query.message.reply_document(
            document=file_to_send,
            caption=(
                f'Флаг {entry["country_name"]} файлом\n\n'
                "Изначально его загрузили как фото, поэтому несжатого оригинала у бота нет"
            ),
        )
    else:
        await query.message.reply_document(
            document=entry["flag_file_id"],
            caption=f'Оригинал флага {entry["country_name"]} без сжатия',
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

    await query.message.reply_text(
        build_region_info_text(region, country_name),
        parse_mode=ParseMode.HTML,
        reply_markup=region_info_buttons(country, region),
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
        "/addflag - добавить флаг в библиотеку\n"
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
    regions_count = sum(
        len(country.get("regions", []))
        for country in data.get("countries", {}).values()
    )
    flags_count = len(data.get("flag_library", []))
    return countries_count, regions_count, flags_count


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

    countries_count, regions_count, flags_count = database_counts(DATA)
    await send_database_backup(
        update.message,
        (
            "Готово, вот свежая резервная копия\n\n"
            f"Стран: {countries_count}\n"
            f"Регионов: {regions_count}\n"
            f"Флагов: {flags_count}"
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

    countries_count, regions_count, flags_count = database_counts(restored_data)
    context.user_data["restore_backup_data"] = restored_data
    buttons = [[
        InlineKeyboardButton("Восстановить", callback_data="restorebackup:yes"),
        InlineKeyboardButton("Отмена", callback_data="restorebackup:no"),
    ]]
    await update.message.reply_text(
        "Копия выглядит нормально\n\n"
        f"Стран: {countries_count}\n"
        f"Регионов: {regions_count}\n"
        f"Флагов: {flags_count}\n\n"
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
    countries_count, regions_count, flags_count = database_counts(DATA)
    clear_restore_context(context)
    await query.edit_message_text(
        "Готово, базу восстановил\n\n"
        f"Стран: {countries_count}\n"
        f"Регионов: {regions_count}\n"
        f"Флагов: {flags_count}"
    )
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
    DATA["countries"][c["name"]] = c
    upsert_library_flag(
        c["name"],
        c["flag_file_id"],
        c.get("flag_media_type", "document"),
    )
    save_data()

    await update.message.reply_text(f"Готово! Страна \"{c['name']}\" теперь в боте")
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


add_country_conv = ConversationHandler(
    entry_points=[CommandHandler("addcountry", add_country_start)],
    states={
        ADD_CARD: [MessageHandler(filters.PHOTO | filters.Document.IMAGE, add_card)],
        ADD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_name)],
        ADD_LEADER: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_leader)],
        ADD_CAPITAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_capital)],
        ADD_CONTINENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_continent)],
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
        raw = normalize_user_text(update.message.text.strip())
        c["continents"] = [x.strip() for x in raw.split(",") if x.strip()]

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
        DATA["countries"].pop(name)
        c["name"] = new_name
        DATA["countries"][new_name] = c
        name = new_name

    else:  # leader, capital
        c[field] = normalize_user_text(update.message.text.strip())

    upsert_library_flag(
        c["name"],
        c.get("flag_file_id"),
        c.get("flag_media_type", "document"),
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
    await update.message.reply_text(
        "Теперь выбери готовый флаг из библиотеки, загрузи новый или оставь регион без флага",
        reply_markup=flag_choice_keyboard("addregionflag", allow_none=True),
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
    if file_id:
        upsert_library_flag(
            region_library_flag_name(country_name, region_name),
            file_id,
            media_type or "document",
        )
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
        await query.edit_message_text(
            "Теперь выбери готовый флаг из библиотеки, загрузи новый или оставь регион без флага",
            reply_markup=flag_choice_keyboard(
                "addregionflag", allow_none=True, page=page
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

    entry = find_library_flag(choice)
    if not entry:
        await query.message.reply_text(
            "Похоже, этого флага уже нет. Выбери другой или загрузи новый",
            reply_markup=flag_choice_keyboard("addregionflag", allow_none=True),
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
        upsert_library_flag(
            region_library_flag_name(country_name, region["name"]),
            region["flag_file_id"],
            region.get("flag_media_type", "document"),
        )
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
            "new_library_flag_name",
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
    if library_flag_name_exists(name):
        await update.message.reply_text(
            "Флаг с такой подписью уже есть. Изменить его можно через /editflag"
        )
        return

    DATA.setdefault("flag_library", []).append(
        {
            "id": uuid4().hex[:12],
            "country_name": name,
            "flag_file_id": update.message.document.file_id,
            "flag_media_type": "document",
        }
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
    await update.message.reply_text(
        "Напиши название страны или территории для флага"
    )
    return ADD_LIBRARY_FLAG_NAME


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
    if library_flag_name_exists(name):
        await update.message.reply_text(
            "Флаг с такой подписью уже есть. Изменить его можно через /editflag"
        )
        return ADD_LIBRARY_FLAG_NAME

    context.user_data["new_library_flag_name"] = name
    await update.message.reply_text(
        "Теперь скинь флаг именно файлом, чтобы он сохранился без сжатия"
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

    DATA.setdefault("flag_library", []).append(
        {
            "id": uuid4().hex[:12],
            "country_name": name,
            "flag_file_id": update.message.document.file_id,
            "flag_media_type": "document",
        }
    )
    save_data()
    await update.message.reply_text(
        f'Готово! Флаг "{name}" появился в библиотеке'
    )
    clear_library_flag_context(context)
    return ConversationHandler.END


add_library_flag_conv = ConversationHandler(
    entry_points=[CommandHandler("addflag", add_library_flag_start)],
    states={
        ADD_LIBRARY_FLAG_NAME: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, add_library_flag_name)
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
    buttons = [
        [InlineKeyboardButton("✏️ Название", callback_data="editlibraryfield:name")],
        [InlineKeyboardButton("🖼 Файл", callback_data="editlibraryfield:file")],
        [InlineKeyboardButton("🗑 Удалить", callback_data="editlibraryfield:delete")],
    ]
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
        if library_flag_name_exists(name, exclude_id=entry["id"]):
            await update.message.reply_text("Флаг с такой подписью уже есть")
            return EDIT_LIBRARY_FLAG_VALUE
        entry["country_name"] = name

    elif field == "file":
        if not update.message.document:
            await update.message.reply_text("Флаг нужно отправить именно файлом")
            return EDIT_LIBRARY_FLAG_VALUE
        entry["flag_file_id"] = update.message.document.file_id
        entry["flag_media_type"] = "document"
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
        BotCommand("flags", "Библиотека флагов"),
    ]


def admin_commands():
    return public_commands() + [
        BotCommand("addcountry", "Добавить страну"),
        BotCommand("editcountry", "Редактировать страну"),
        BotCommand("addregion", "Добавить регион"),
        BotCommand("editregion", "Изменить или удалить регион"),
        BotCommand("addflag", "Добавить флаг в библиотеку"),
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
    application.add_handler(CommandHandler("flags", cmd_flags))
    application.add_handler(CommandHandler("admhelp", cmd_admhelp))
    application.add_handler(CommandHandler("addadmin", cmd_addadmin))
    application.add_handler(CommandHandler("backup", cmd_backup))

    application.add_handler(restore_backup_conv)
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
        CallbackQueryHandler(cb_show_library_flag, pattern=r"^libraryflag:")
    )
    application.add_handler(
        CallbackQueryHandler(cb_download_library_flag, pattern=r"^downloadflag:")
    )

    logger.info('АУРЕЛИЯ INFO BOT запущен, v: "%s"', BOT_VERSION)
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
