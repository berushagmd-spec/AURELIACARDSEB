import json
import logging
import os
from pathlib import Path

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------- Настройки ----------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "ВАШ_ТОКЕН_СЮДА")
MAIN_ADMIN_ID = 7787565361  # главный админ, только он может добавлять новых админов

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)
COUNTRIES_FILE = DATA_DIR / "countries.json"
ADMINS_FILE = DATA_DIR / "admins.json"


def fix_dashes(text: str) -> str:
    if text is None:
        return text
    return text.replace("–", "-").replace("—", "-")


def load_json(path: Path, default):
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path: Path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_countries():
    return load_json(COUNTRIES_FILE, {})


def save_countries(data):
    save_json(COUNTRIES_FILE, data)


def load_admins():
    admins = load_json(ADMINS_FILE, [])
    if MAIN_ADMIN_ID not in admins:
        admins.append(MAIN_ADMIN_ID)
        save_json(ADMINS_FILE, admins)
    return admins


def save_admins(admins):
    save_json(ADMINS_FILE, admins)


def is_admin(user_id: int) -> bool:
    return user_id in load_admins()


# ---------- Состояния диалога добавления страны ----------
(
    CARD,
    NAME,
    LEADER,
    CAPITAL,
    CONTINENT,
    FLAG,
    COAT,
    DESCRIPTION,
    LORE,
) = range(9)


def get_file_id(message):
    """Возвращает (file_id, тип) из сообщения - документ приоритетнее фото."""
    if message.document:
        return message.document.file_id, "document"
    if message.photo:
        return message.photo[-1].file_id, "photo"
    return None, None


# ---------- /start ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Список стран", callback_data="list_countries")]]
    )
    text = (
        "Привет! Это АУРЕЛИЯ INFO BOT.\n\n"
        "Здесь можно посмотреть информацию о странах мира Аурелии - "
        "лидеров, столицы, флаги, гербы и лор.\n\n"
        "Нажми на кнопку ниже, чтобы посмотреть список стран."
    )
    await update.message.reply_text(text, reply_markup=keyboard)


# ---------- Список стран ----------
def build_countries_keyboard(countries: dict) -> InlineKeyboardMarkup:
    buttons = []
    row = []
    for key, c in sorted(countries.items(), key=lambda x: x[1]["name"]):
        row.append(InlineKeyboardButton(c["name"], callback_data=f"country_{key}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return InlineKeyboardMarkup(buttons)


async def countries_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    countries = load_countries()
    if not countries:
        await update.message.reply_text("Список стран пока пуст.")
        return
    await update.message.reply_text(
        "Выберите страну:", reply_markup=build_countries_keyboard(countries)
    )


async def list_countries_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    countries = load_countries()
    if not countries:
        await query.message.reply_text("Список стран пока пуст.")
        return
    await query.message.reply_text(
        "Выберите страну:", reply_markup=build_countries_keyboard(countries)
    )


def build_country_text(c: dict) -> str:
    continents = ", ".join(c.get("continents", []))
    lore_text = "\n\n".join(c.get("lore_links", []) or [])
    text = (
        f"🏳 {c['name']}\n\n"
        f"👤 Лидер: {c['leader']}\n"
        f"🏛 Столица: {c['capital']}\n"
        f"🌍 Континент: {continents}\n\n"
        f"📝 Описание:\n{c['description']}\n"
    )
    if lore_text:
        text += f"\n📚 Лор:\n{lore_text}"
    return fix_dashes(text)


async def country_detail_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    key = query.data.split("country_", 1)[1]
    countries = load_countries()
    c = countries.get(key)
    if not c:
        await query.message.reply_text("Страна не найдена, возможно она была удалена.")
        return

    text = build_country_text(c)
    buttons = []
    if c.get("flag_file_id"):
        buttons.append(InlineKeyboardButton("Флаг", callback_data=f"flag_{key}"))
    if c.get("coat_file_id"):
        buttons.append(InlineKeyboardButton("Герб", callback_data=f"coat_{key}"))
    markup = InlineKeyboardMarkup([buttons]) if buttons else None

    if c.get("card_file_id"):
        await query.message.reply_photo(
            photo=c["card_file_id"], caption=text, reply_markup=markup
        )
    else:
        await query.message.reply_text(text, reply_markup=markup)


async def flag_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    key = query.data.split("flag_", 1)[1]
    countries = load_countries()
    c = countries.get(key)
    if not c or not c.get("flag_file_id"):
        await query.message.reply_text("Флаг не найден.")
        return
    if c.get("flag_type") == "document":
        await query.message.reply_document(document=c["flag_file_id"], caption=f"Флаг {c['name']}")
    else:
        await query.message.reply_photo(photo=c["flag_file_id"], caption=f"Флаг {c['name']}")


async def coat_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    key = query.data.split("coat_", 1)[1]
    countries = load_countries()
    c = countries.get(key)
    if not c or not c.get("coat_file_id"):
        await query.message.reply_text("Герб не найден.")
        return
    if c.get("coat_type") == "document":
        await query.message.reply_document(document=c["coat_file_id"], caption=f"Герб {c['name']}")
    else:
        await query.message.reply_photo(photo=c["coat_file_id"], caption=f"Герб {c['name']}")


# ---------- /addadmin (только главный админ) ----------
async def addadmin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != MAIN_ADMIN_ID:
        await update.message.reply_text("Эта команда доступна только главному админу.")
        return

    if not context.args:
        await update.message.reply_text(
            "Использование: /addadmin <telegram_id>\nНапример: /addadmin 123456789"
        )
        return

    try:
        new_admin_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("ID должен быть числом.")
        return

    admins = load_admins()
    if new_admin_id in admins:
        await update.message.reply_text("Этот пользователь уже админ.")
        return

    admins.append(new_admin_id)
    save_admins(admins)
    await set_admin_commands(context.application, new_admin_id)
    await update.message.reply_text(f"Пользователь {new_admin_id} добавлен в админы.")

    try:
        await context.bot.send_message(
            chat_id=new_admin_id,
            text="Вас назначили админом АУРЕЛИЯ INFO BOT. Теперь вам доступна команда /addcountry.",
        )
    except Exception:
        pass


# ---------- /addcountry (диалог) ----------
async def addcountry_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Эта команда доступна только админам.")
        return ConversationHandler.END

    context.user_data["new_country"] = {}
    await update.message.reply_text(
        "Добавление новой страны.\n\n"
        "Пришлите карточку страны (картинкой).\n"
        "Для отмены в любой момент используйте /cancel"
    )
    return CARD


async def addcountry_card(update: Update, context: ContextTypes.DEFAULT_TYPE):
    file_id, ftype = get_file_id(update.message)
    if not file_id:
        await update.message.reply_text("Пришлите, пожалуйста, картинку карточки страны.")
        return CARD
    context.user_data["new_country"]["card_file_id"] = file_id
    await update.message.reply_text("Отлично. Теперь пришлите название страны.")
    return NAME


async def addcountry_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_country"]["name"] = fix_dashes(update.message.text.strip())
    await update.message.reply_text("Имя лидера страны?")
    return LEADER


async def addcountry_leader(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_country"]["leader"] = fix_dashes(update.message.text.strip())
    await update.message.reply_text("Столица страны?")
    return CAPITAL


async def addcountry_capital(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_country"]["capital"] = fix_dashes(update.message.text.strip())
    await update.message.reply_text(
        "Континент? Если их 2 или 3 - перечислите через запятую."
    )
    return CONTINENT


async def addcountry_continent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip()
    continents = [fix_dashes(x.strip()) for x in raw.split(",") if x.strip()]
    context.user_data["new_country"]["continents"] = continents
    await update.message.reply_text(
        "Пришлите флаг страны БЕЗ СЖАТИЯ - отправьте его как файл (документ), а не как фото."
    )
    return FLAG


async def addcountry_flag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    file_id, ftype = get_file_id(update.message)
    if not file_id:
        await update.message.reply_text("Пришлите флаг файлом (документом) или фото.")
        return FLAG
    if ftype == "photo":
        await update.message.reply_text(
            "Внимание: изображение отправлено как фото и будет сжато Telegram. "
            "Если важно сохранить качество, пришлите его заново как файл (документ)."
        )
    context.user_data["new_country"]["flag_file_id"] = file_id
    context.user_data["new_country"]["flag_type"] = ftype
    await update.message.reply_text(
        "Пришлите герб страны БЕЗ СЖАТИЯ (как файл/документ).\n"
        "Если герба нет - напишите /skip"
    )
    return COAT


async def addcountry_coat_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_country"]["coat_file_id"] = None
    context.user_data["new_country"]["coat_type"] = None
    await update.message.reply_text(
        "Хорошо, без герба. Теперь пришлите описание страны (до 500 символов)."
    )
    return DESCRIPTION


async def addcountry_coat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    file_id, ftype = get_file_id(update.message)
    if not file_id:
        await update.message.reply_text(
            "Пришлите герб файлом (документом) или фото, либо /skip если герба нет."
        )
        return COAT
    if ftype == "photo":
        await update.message.reply_text(
            "Внимание: изображение отправлено как фото и будет сжато Telegram. "
            "Если важно сохранить качество, пришлите его заново как файл (документ)."
        )
    context.user_data["new_country"]["coat_file_id"] = file_id
    context.user_data["new_country"]["coat_type"] = ftype
    await update.message.reply_text("Теперь пришлите описание страны (до 500 символов).")
    return DESCRIPTION


async def addcountry_description(update: Update, context: ContextTypes.DEFAULT_TYPE):
    desc = update.message.text.strip()
    if len(desc) > 500:
        await update.message.reply_text(
            f"Описание слишком длинное ({len(desc)} символов). Максимум 500. Пришлите заново."
        )
        return DESCRIPTION
    context.user_data["new_country"]["description"] = fix_dashes(desc)
    await update.message.reply_text("Последнее - пришлите ссылки на лор через запятую.")
    return LORE


async def addcountry_lore(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip()
    links = [fix_dashes(x.strip()) for x in raw.split(",") if x.strip()]
    country = context.user_data["new_country"]
    country["lore_links"] = links

    countries = load_countries()
    key = country["name"].lower().replace(" ", "_")
    base_key = key
    i = 1
    while key in countries:
        i += 1
        key = f"{base_key}_{i}"

    countries[key] = country
    save_countries(countries)

    text = build_country_text(country)
    await update.message.reply_text("Страна успешно добавлена!")
    if country.get("card_file_id"):
        await update.message.reply_photo(photo=country["card_file_id"], caption=text)
    else:
        await update.message.reply_text(text)

    context.user_data.pop("new_country", None)
    return ConversationHandler.END


async def addcountry_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("new_country", None)
    await update.message.reply_text("Добавление страны отменено.")
    return ConversationHandler.END


# ---------- Редактирование страны ----------
EDIT_SELECT_COUNTRY, EDIT_SELECT_FIELD, EDIT_WAITING_VALUE = range(100, 103)

FIELD_LABELS = {
    "card": "Карточка",
    "name": "Название",
    "leader": "Лидер",
    "capital": "Столица",
    "continent": "Континент",
    "flag": "Флаг",
    "coat": "Герб",
    "description": "Описание",
    "lore": "Лор",
}

FIELD_PROMPTS = {
    "card": "Пришлите новую карточку страны (картинкой).",
    "name": "Пришлите новое название страны.",
    "leader": "Пришлите новое имя лидера.",
    "capital": "Пришлите новую столицу.",
    "continent": "Пришлите новый(е) континент(ы) через запятую.",
    "flag": "Пришлите новый флаг БЕЗ СЖАТИЯ (файлом/документом).",
    "coat": "Пришлите новый герб БЕЗ СЖАТИЯ (файлом/документом). Чтобы удалить герб - напишите /skip",
    "description": "Пришлите новое описание (до 500 символов).",
    "lore": "Пришлите новые ссылки на лор через запятую.",
}


def build_field_keyboard() -> InlineKeyboardMarkup:
    buttons = []
    row = []
    for field, label in FIELD_LABELS.items():
        row.append(InlineKeyboardButton(label, callback_data=f"editfield_{field}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("✅ Готово", callback_data="editfield_done")])
    return InlineKeyboardMarkup(buttons)


async def editcountry_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Эта команда доступна только админам.")
        return ConversationHandler.END

    countries = load_countries()
    if not countries:
        await update.message.reply_text("Список стран пока пуст, нечего редактировать.")
        return ConversationHandler.END

    buttons = []
    row = []
    for key, c in sorted(countries.items(), key=lambda x: x[1]["name"]):
        row.append(InlineKeyboardButton(c["name"], callback_data=f"editsel_{key}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    await update.message.reply_text(
        "Какую страну редактируем?", reply_markup=InlineKeyboardMarkup(buttons)
    )
    return EDIT_SELECT_COUNTRY


async def editcountry_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    key = query.data.split("editsel_", 1)[1]
    countries = load_countries()
    if key not in countries:
        await query.message.reply_text("Страна не найдена.")
        return ConversationHandler.END

    context.user_data["edit_key"] = key
    await query.message.reply_text(
        f"Редактируем «{countries[key]['name']}». Что меняем?",
        reply_markup=build_field_keyboard(),
    )
    return EDIT_SELECT_FIELD


async def editcountry_field(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    field = query.data.split("editfield_", 1)[1]

    if field == "done":
        key = context.user_data.get("edit_key")
        countries = load_countries()
        c = countries.get(key)
        context.user_data.pop("edit_key", None)
        context.user_data.pop("edit_field", None)
        if c:
            await query.message.reply_text("Редактирование завершено. Вот итог:")
            text = build_country_text(c)
            if c.get("card_file_id"):
                await query.message.reply_photo(photo=c["card_file_id"], caption=text)
            else:
                await query.message.reply_text(text)
        else:
            await query.message.reply_text("Редактирование завершено.")
        return ConversationHandler.END

    context.user_data["edit_field"] = field
    await query.message.reply_text(fix_dashes(FIELD_PROMPTS[field]))
    return EDIT_WAITING_VALUE


async def editcountry_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    key = context.user_data.get("edit_key")
    field = context.user_data.get("edit_field")
    countries = load_countries()
    c = countries.get(key)

    if not c or not field:
        await update.message.reply_text("Что-то пошло не так, начните заново через /editcountry")
        return ConversationHandler.END

    if field in ("card", "flag", "coat"):
        file_id, ftype = get_file_id(update.message)
        if not file_id:
            await update.message.reply_text(
                "Нужна картинка. " + fix_dashes(FIELD_PROMPTS[field])
            )
            return EDIT_WAITING_VALUE
        if field == "card":
            c["card_file_id"] = file_id
        else:
            if ftype == "photo":
                await update.message.reply_text(
                    "Внимание: изображение отправлено как фото и будет сжато Telegram. "
                    "Если важно сохранить качество, пришлите его заново как файл (документ)."
                )
            c[f"{field}_file_id"] = file_id
            c[f"{field}_type"] = ftype

    elif field == "description":
        desc = update.message.text.strip() if update.message.text else ""
        if len(desc) > 500:
            await update.message.reply_text(
                f"Описание слишком длинное ({len(desc)} символов). Максимум 500. Пришлите заново."
            )
            return EDIT_WAITING_VALUE
        c["description"] = fix_dashes(desc)

    elif field == "continent":
        raw = update.message.text.strip() if update.message.text else ""
        c["continents"] = [fix_dashes(x.strip()) for x in raw.split(",") if x.strip()]

    elif field == "lore":
        raw = update.message.text.strip() if update.message.text else ""
        c["lore_links"] = [fix_dashes(x.strip()) for x in raw.split(",") if x.strip()]

    else:  # name, leader, capital - простые текстовые поля
        value = update.message.text.strip() if update.message.text else ""
        if not value:
            await update.message.reply_text(fix_dashes(FIELD_PROMPTS[field]))
            return EDIT_WAITING_VALUE
        c[field] = fix_dashes(value)

    countries[key] = c
    save_countries(countries)
    context.user_data.pop("edit_field", None)

    await update.message.reply_text(
        "Обновлено! Что ещё меняем?", reply_markup=build_field_keyboard()
    )
    return EDIT_SELECT_FIELD


async def editcountry_coat_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    key = context.user_data.get("edit_key")
    field = context.user_data.get("edit_field")
    if field != "coat":
        await update.message.reply_text("Команда /skip доступна только при редактировании герба.")
        return EDIT_WAITING_VALUE

    countries = load_countries()
    c = countries.get(key)
    if c:
        c["coat_file_id"] = None
        c["coat_type"] = None
        countries[key] = c
        save_countries(countries)

    context.user_data.pop("edit_field", None)
    await update.message.reply_text(
        "Герб удалён. Что ещё меняем?", reply_markup=build_field_keyboard()
    )
    return EDIT_SELECT_FIELD


async def editcountry_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("edit_key", None)
    context.user_data.pop("edit_field", None)
    await update.message.reply_text("Редактирование отменено.")
    return ConversationHandler.END


# ---------- Команды меню ----------
async def set_admin_commands(application: Application, admin_id: int):
    commands = [
        BotCommand("start", "Запустить бота"),
        BotCommand("countries", "Список стран"),
        BotCommand("addcountry", "Добавить страну"),
        BotCommand("addadmin", "Добавить админа"),
    ]
    try:
        await application.bot.set_my_commands(
            commands, scope=BotCommandScopeChat(chat_id=admin_id)
        )
    except Exception as e:
        logger.warning("Не удалось установить команды для админа %s: %s", admin_id, e)


async def post_init(application: Application):
    default_commands = [
        BotCommand("start", "Запустить бота"),
        BotCommand("countries", "Список стран"),
    ]
    await application.bot.set_my_commands(default_commands, scope=BotCommandScopeDefault())

    for admin_id in load_admins():
        await set_admin_commands(application, admin_id)


def main():
    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("countries", countries_command))
    application.add_handler(CommandHandler("addadmin", addadmin_command))

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("addcountry", addcountry_start)],
        states={
            CARD: [MessageHandler(filters.PHOTO | filters.Document.IMAGE, addcountry_card)],
            NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, addcountry_name)],
            LEADER: [MessageHandler(filters.TEXT & ~filters.COMMAND, addcountry_leader)],
            CAPITAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, addcountry_capital)],
            CONTINENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, addcountry_continent)],
            FLAG: [MessageHandler(filters.PHOTO | filters.Document.IMAGE, addcountry_flag)],
            COAT: [
                CommandHandler("skip", addcountry_coat_skip),
                MessageHandler(filters.PHOTO | filters.Document.IMAGE, addcountry_coat),
            ],
            DESCRIPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, addcountry_description)],
            LORE: [MessageHandler(filters.TEXT & ~filters.COMMAND, addcountry_lore)],
        },
        fallbacks=[CommandHandler("cancel", addcountry_cancel)],
    )
    application.add_handler(conv_handler)

    application.add_handler(CallbackQueryHandler(list_countries_callback, pattern="^list_countries$"))
    application.add_handler(CallbackQueryHandler(country_detail_callback, pattern="^country_"))
    application.add_handler(CallbackQueryHandler(flag_callback, pattern="^flag_"))
    application.add_handler(CallbackQueryHandler(coat_callback, pattern="^coat_"))

    application.run_polling()


if __name__ == "__main__":
    main()
