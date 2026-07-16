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
from html import escape
from uuid import uuid4

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
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

DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.json")
STICKER_SET_NAME = "AureliaPack"

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


def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
            d.setdefault("admins", [])
            d.setdefault("countries", {})

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
            return d
    return {"admins": [], "countries": {}}


def save_data():
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(DATA, f, ensure_ascii=False, indent=2)


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

(EDIT_CHOOSE_COUNTRY, EDIT_CHOOSE_FIELD, EDIT_VALUE) = range(20, 23)

(ADD_REGION_CHOOSE_COUNTRY, ADD_REGION_NAME, ADD_REGION_FLAG) = range(30, 33)

(
    EDIT_REGION_CHOOSE_COUNTRY,
    EDIT_REGION_CHOOSE_REGION,
    EDIT_REGION_CHOOSE_FIELD,
    EDIT_REGION_VALUE,
    EDIT_REGION_CONFIRM_DELETE,
) = range(40, 45)

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


def get_country_by_id(country_id: str):
    """Возвращает текущее имя страны и данные по короткому внутреннему ID."""
    for name, country in DATA["countries"].items():
        if country.get("id") == country_id:
            return name, country
    return None, None


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


def clear_region_context(context: ContextTypes.DEFAULT_TYPE):
    for key in (
        "region_country_id",
        "new_region_name",
        "edit_region_id",
        "edit_region_field",
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


def regions_keyboard(country: dict, prefix="regionflag"):
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


def build_info_text(c: dict) -> str:
    continents = ", ".join(c.get("continents", []))
    region_names = "\n\n".join(
        region["name"]
        for region in sorted(
            c.get("regions", []), key=lambda item: item["name"].casefold()
        )
    )
    lore = c.get("lore_links", [])
    lore_text = "\n\n".join(lore) if lore else "-"
    text = (
        f"<b>{escape(c['name'])}</b>\n\n"
        f"Кто у руля: {escape(c.get('leader', '-'))}\n"
        f"Столица: {escape(c.get('capital', '-'))}\n"
        f"Где находится: {escape(continents or '-')}\n\n"
        f"Регионы:\n{escape(region_names or '-')}\n\n"
        f"Немного о стране:\n{escape(c.get('description', '-'))}\n\n"
        f"Почитать лор:\n{escape(lore_text)}"
    )
    return text


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
        [[InlineKeyboardButton("📋 Список стран", callback_data="list_countries")]]
    )
    await update.message.reply_text(
        "Привет! Это бот-путеводитель по Аурелии\n\n"
        "Тут можно полистать страны, посмотреть их флаги, гербы, регионы и лор. "
        "Жми кнопку ниже и выбирай, куда заглянем",
        reply_markup=kb,
    )


async def cmd_countries(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = countries_keyboard()
    if not kb:
        await update.message.reply_text("Тут пока пусто - ни одной страны ещё не добавили")
        return
    await update.message.reply_text("Выбирай страну:", reply_markup=kb)


async def cb_list_countries(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    kb = countries_keyboard()
    if not kb:
        await query.edit_message_text("Тут пока пусто - ни одной страны ещё не добавили")
        return
    await query.message.reply_text("Выбирай страну:", reply_markup=kb)


async def cb_show_country(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    name = query.data.split(":", 1)[1]
    c = DATA["countries"].get(name)
    if not c:
        await query.message.reply_text("Похоже, этой страны уже нет")
        return

    # 1. Случайный стикер из официального набора Аурелии
    await send_random_country_sticker(query.message, context)

    # 2. Карточка
    await query.message.reply_photo(photo=c["card_file_id"], caption=f"<b>{escape(name)}</b>", parse_mode=ParseMode.HTML)

    # 3. Отдельное сообщение с инфо и кнопками
    await query.message.reply_text(
        build_info_text(c), parse_mode=ParseMode.HTML, reply_markup=info_buttons(c)
    )


async def cb_flag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    name = query.data.split(":", 1)[1]
    c = DATA["countries"].get(name)
    if not c or not c.get("flag_file_id"):
        await query.answer("Флаг куда-то запропастился", show_alert=True)
        return
    await query.answer()
    await query.message.reply_document(document=c["flag_file_id"], caption=f"Флаг: {name}")


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

    kb = regions_keyboard(country)
    if not kb:
        await query.answer("Регионы сюда пока не добавили", show_alert=True)
        return

    await query.answer()
    await query.message.reply_text(
        f'Вот регионы страны "{name}"\n\n'
        "Выбирай любой - если у него есть флаг, покажу",
        reply_markup=kb,
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
        "/addregion - добавить регион и его флаг\n"
        "/editregion - изменить или удалить регион\n"
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
        "Теперь скинь флаг. Лучше файлом, тогда Telegram его не пережмёт"
    )
    return ADD_FLAG


async def add_flag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.document:
        file_id = update.message.document.file_id
    elif update.message.photo:
        file_id = update.message.photo[-1].file_id
        await update.message.reply_text(
            "Флаг пришёл как обычное фото, так что Telegram мог его немного пережевать. "
            "В следующий раз лучше кидай файлом"
        )
    else:
        await update.message.reply_text("Мне нужна картинка с флагом")
        return ADD_FLAG

    context.user_data["new_country"]["flag_file_id"] = file_id
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
    save_data()

    await update.message.reply_text(f"Готово! Страна \"{c['name']}\" теперь в боте")
    context.user_data.pop("new_country", None)
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Окей, всё отменил")
    return ConversationHandler.END


add_country_conv = ConversationHandler(
    entry_points=[CommandHandler("addcountry", add_country_start)],
    states={
        ADD_CARD: [MessageHandler(filters.PHOTO | filters.Document.IMAGE, add_card)],
        ADD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_name)],
        ADD_LEADER: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_leader)],
        ADD_CAPITAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_capital)],
        ADD_CONTINENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_continent)],
        ADD_FLAG: [MessageHandler(filters.PHOTO | filters.Document.IMAGE, add_flag)],
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
        elif update.message.photo:
            file_id = update.message.photo[-1].file_id
            await update.message.reply_text(
                "Картинка пришла как фото, так что Telegram мог её немного сжать"
            )
        else:
            await update.message.reply_text("Тут нужна именно картинка")
            return EDIT_VALUE
        c["card_file_id" if field == "card" else "flag_file_id"] = file_id

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
    await update.message.reply_text(
        "Теперь можешь скинуть флаг региона файлом или фото. "
        "Если флага нет, просто напиши \"нет\""
    )
    return ADD_REGION_FLAG


async def add_region_flag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    country_id = context.user_data.get("region_country_id")
    country_name, country = get_country_by_id(country_id)
    region_name = context.user_data.get("new_region_name")
    if not country or not region_name:
        await update.message.reply_text("Что-то потерялось по дороге. Я всё отменил")
        clear_region_context(context)
        return ConversationHandler.END

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

    country.setdefault("regions", []).append(
        {
            "id": uuid4().hex[:12],
            "name": region_name,
            "flag_file_id": file_id,
            "flag_media_type": media_type,
        }
    )
    save_data()
    await update.message.reply_text(
        f'Готово! Регион "{region_name}" появился у страны "{country_name}"'
    )
    clear_region_context(context)
    return ConversationHandler.END


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
        ADD_REGION_FLAG: [
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
    if field not in ("name", "flag", "delete"):
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
    prompt = (
        "Как теперь будет называться регион?"
        if field == "name"
        else "Скинь новый флаг или напиши \"нет\", если флаг нужно убрать"
    )
    await query.message.reply_text(prompt)
    return EDIT_REGION_VALUE


async def edit_region_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    country_id = context.user_data.get("region_country_id")
    _, country = get_country_by_id(country_id)
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
# Меню команд
# ---------------------------------------------------------------------------

def public_commands():
    return [
        BotCommand("start", "Начать"),
        BotCommand("countries", "Список стран"),
    ]


def admin_commands():
    return public_commands() + [
        BotCommand("addcountry", "Добавить страну"),
        BotCommand("editcountry", "Редактировать страну"),
        BotCommand("addregion", "Добавить регион"),
        BotCommand("editregion", "Изменить или удалить регион"),
        BotCommand("addadmin", "Добавить админа"),
        BotCommand("admhelp", "Админ-команды"),
        BotCommand("cancel", "Отменить диалог"),
    ]


async def post_init(application: Application):
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
    application.add_handler(CommandHandler("admhelp", cmd_admhelp))
    application.add_handler(CommandHandler("addadmin", cmd_addadmin))

    application.add_handler(add_country_conv)
    application.add_handler(edit_country_conv)
    application.add_handler(add_region_conv)
    application.add_handler(edit_region_conv)

    application.add_handler(CallbackQueryHandler(cb_list_countries, pattern=r"^list_countries$"))
    application.add_handler(CallbackQueryHandler(cb_show_country, pattern=r"^country:"))
    application.add_handler(CallbackQueryHandler(cb_flag, pattern=r"^flag:"))
    application.add_handler(CallbackQueryHandler(cb_herb, pattern=r"^herb:"))
    application.add_handler(CallbackQueryHandler(cb_regions, pattern=r"^regions:"))
    application.add_handler(
        CallbackQueryHandler(cb_region_flag, pattern=r"^regionflag:")
    )

    logger.info("АУРЕЛИЯ INFO BOT запущен.")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
