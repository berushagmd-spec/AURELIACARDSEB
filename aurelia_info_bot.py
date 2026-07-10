"""
АУРЕЛИЯ INFO BOT
Telegram-бот для каталога стран вселенной Аурелия.

Запуск:
    pip install -r requirements.txt
    export BOT_TOKEN="ваш_токен_от_BotFather"
    python aurelia_bot.py

Хранение данных: локальный файл data.json (создаётся автоматически рядом со скриптом).
"""

import json
import logging
import os
from html import escape

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

# ---------------------------------------------------------------------------
# Хранилище данных (простой JSON-файл)
# ---------------------------------------------------------------------------

def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
            d.setdefault("admins", [])
            d.setdefault("countries", {})
            return d
    return {"admins": [], "countries": {}}


def save_data():
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(DATA, f, ensure_ascii=False, indent=2)


DATA = load_data()


def is_admin(user_id: int) -> bool:
    return user_id == MAIN_ADMIN_ID or user_id in DATA["admins"]


def fix_dashes(text: str) -> str:
    if text is None:
        return text
    return text.replace("\u2013", "-").replace("\u2014", "-")


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

def countries_keyboard(prefix="country"):
    names = sorted(DATA["countries"].keys())
    buttons = []
    row = []
    for i, name in enumerate(names, start=1):
        row.append(InlineKeyboardButton(name, callback_data=f"{prefix}:{name}"))
        if i % 2 == 0:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return InlineKeyboardMarkup(buttons) if buttons else None


def build_info_text(c: dict) -> str:
    continents = ", ".join(c.get("continents", []))
    lore = c.get("lore_links", [])
    lore_text = "\n\n".join(lore) if lore else "-"
    text = (
        f"<b>{escape(c['name'])}</b>\n\n"
        f"Лидер: {escape(c.get('leader', '-'))}\n"
        f"Столица: {escape(c.get('capital', '-'))}\n"
        f"Континент(ы): {escape(continents)}\n\n"
        f"Описание:\n{escape(c.get('description', '-'))}\n\n"
        f"Ссылки на лор:\n{escape(lore_text)}"
    )
    return text


def info_buttons(c: dict):
    row = [InlineKeyboardButton("🚩 Флаг", callback_data=f"flag:{c['name']}")]
    if c.get("herb_file_id"):
        row.append(InlineKeyboardButton("🛡 Герб", callback_data=f"herb:{c['name']}"))
    return InlineKeyboardMarkup([row])


# ---------------------------------------------------------------------------
# /start и /countries
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("📋 Список стран", callback_data="list_countries")]]
    )
    await update.message.reply_text(
        "Добро пожаловать в АУРЕЛИЯ INFO BOT!\n\n"
        "Здесь собрана информация о странах вселенной Аурелия.\n"
        "Нажмите кнопку ниже, чтобы посмотреть список стран.",
        reply_markup=kb,
    )


async def cmd_countries(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = countries_keyboard()
    if not kb:
        await update.message.reply_text("Список стран пока пуст.")
        return
    await update.message.reply_text("Выберите страну:", reply_markup=kb)


async def cb_list_countries(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    kb = countries_keyboard()
    if not kb:
        await query.edit_message_text("Список стран пока пуст.")
        return
    await query.message.reply_text("Выберите страну:", reply_markup=kb)


async def cb_show_country(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    name = query.data.split(":", 1)[1]
    c = DATA["countries"].get(name)
    if not c:
        await query.message.reply_text("Страна не найдена.")
        return

    # 1. Карточка
    await query.message.reply_photo(photo=c["card_file_id"], caption=f"<b>{escape(name)}</b>", parse_mode=ParseMode.HTML)

    # 2. Отдельное сообщение с инфо и кнопками
    await query.message.reply_text(
        build_info_text(c), parse_mode=ParseMode.HTML, reply_markup=info_buttons(c)
    )


async def cb_flag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    name = query.data.split(":", 1)[1]
    c = DATA["countries"].get(name)
    if not c or not c.get("flag_file_id"):
        await query.answer("Флаг не найден.", show_alert=True)
        return
    await query.answer()
    await query.message.reply_document(document=c["flag_file_id"], caption=f"Флаг: {name}")


async def cb_herb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    name = query.data.split(":", 1)[1]
    c = DATA["countries"].get(name)
    if not c or not c.get("herb_file_id"):
        await query.answer("Герба нет.", show_alert=True)
        return
    await query.answer()
    await query.message.reply_document(document=c["herb_file_id"], caption=f"Герб: {name}")


# ---------------------------------------------------------------------------
# /admhelp
# ---------------------------------------------------------------------------

async def cmd_admhelp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Эта команда только для админов.")
        return
    text = (
        "<b>Админ-команды:</b>\n\n"
        "/addcountry - добавить новую страну (пошагово)\n"
        "/editcountry - отредактировать существующую страну\n"
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
        await update.message.reply_text("Эта команда только для админов.")
        return

    if not context.args:
        await update.message.reply_text("Использование: /addadmin <user_id>")
        return

    try:
        new_admin_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("user_id должен быть числом.")
        return

    if new_admin_id == MAIN_ADMIN_ID or new_admin_id in DATA["admins"]:
        await update.message.reply_text("Этот пользователь уже админ.")
        return

    DATA["admins"].append(new_admin_id)
    save_data()

    try:
        await context.bot.set_my_commands(
            admin_commands(), scope=BotCommandScopeChat(chat_id=new_admin_id)
        )
    except Exception as e:
        logger.warning("Не удалось выставить меню команд новому админу: %s", e)

    await update.message.reply_text(f"Пользователь {new_admin_id} добавлен как админ.")


# ---------------------------------------------------------------------------
# Добавление страны - ConversationHandler
# ---------------------------------------------------------------------------

async def add_country_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Эта команда только для админов.")
        return ConversationHandler.END

    context.user_data["new_country"] = {}
    await update.message.reply_text(
        "Добавление новой страны.\n\nСкиньте карточку страны (картинкой)."
    )
    return ADD_CARD


async def add_card(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.photo:
        file_id = update.message.photo[-1].file_id
    elif update.message.document:
        file_id = update.message.document.file_id
    else:
        await update.message.reply_text("Пожалуйста, пришлите изображение карточки.")
        return ADD_CARD

    context.user_data["new_country"]["card_file_id"] = file_id
    await update.message.reply_text("Теперь напишите название страны.")
    return ADD_NAME


async def add_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = fix_dashes(update.message.text.strip())
    if name in DATA["countries"]:
        await update.message.reply_text(
            "Страна с таким названием уже существует. Введите другое название."
        )
        return ADD_NAME
    context.user_data["new_country"]["name"] = name
    await update.message.reply_text("Имя лидера страны?")
    return ADD_LEADER


async def add_leader(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_country"]["leader"] = fix_dashes(update.message.text.strip())
    await update.message.reply_text("Столица?")
    return ADD_CAPITAL


async def add_capital(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_country"]["capital"] = fix_dashes(update.message.text.strip())
    await update.message.reply_text(
        "Континент(ы)? Если несколько - перечислите через запятую."
    )
    return ADD_CONTINENT


async def add_continent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = fix_dashes(update.message.text.strip())
    continents = [c.strip() for c in raw.split(",") if c.strip()]
    context.user_data["new_country"]["continents"] = continents
    await update.message.reply_text(
        "Пришлите флаг. Отправляйте файлом (как документ), чтобы избежать сжатия."
    )
    return ADD_FLAG


async def add_flag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.document:
        file_id = update.message.document.file_id
    elif update.message.photo:
        file_id = update.message.photo[-1].file_id
        await update.message.reply_text(
            "Внимание: изображение отправлено как фото и могло быть сжато Telegram. "
            "В следующий раз лучше отправлять файлом."
        )
    else:
        await update.message.reply_text("Пожалуйста, пришлите изображение флага.")
        return ADD_FLAG

    context.user_data["new_country"]["flag_file_id"] = file_id
    await update.message.reply_text(
        "Пришлите герб (файлом, без сжатия). Если герба нет - напишите \"нет\"."
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
            "Внимание: изображение отправлено как фото и могло быть сжато Telegram."
        )
    else:
        await update.message.reply_text(
            "Пришлите герб файлом, либо напишите \"нет\", если герба нет."
        )
        return ADD_HERB

    await update.message.reply_text("Описание страны (максимум 500 символов)?")
    return ADD_DESC


async def add_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = fix_dashes(update.message.text.strip())
    if len(text) > 500:
        await update.message.reply_text(
            f"Слишком длинно ({len(text)} символов). Максимум 500. Попробуйте ещё раз."
        )
        return ADD_DESC
    context.user_data["new_country"]["description"] = text
    await update.message.reply_text(
        "Ссылки на лор (через запятую, если несколько)."
    )
    return ADD_LORE


async def add_lore(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = fix_dashes(update.message.text.strip())
    links = [l.strip() for l in raw.split(",") if l.strip()]
    context.user_data["new_country"]["lore_links"] = links

    c = context.user_data["new_country"]
    DATA["countries"][c["name"]] = c
    save_data()

    await update.message.reply_text(
        f"Страна \"{c['name']}\" успешно добавлена!"
    )
    context.user_data.pop("new_country", None)
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Действие отменено.")
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
        await update.message.reply_text("Эта команда только для админов.")
        return ConversationHandler.END

    kb = countries_keyboard(prefix="editsel")
    if not kb:
        await update.message.reply_text("Список стран пуст, нечего редактировать.")
        return ConversationHandler.END

    await update.message.reply_text("Какую страну редактируем?", reply_markup=kb)
    return EDIT_CHOOSE_COUNTRY


async def edit_choose_country(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    name = query.data.split(":", 1)[1]
    if name not in DATA["countries"]:
        await query.message.reply_text("Страна не найдена.")
        return ConversationHandler.END

    context.user_data["edit_country_name"] = name

    buttons = [
        [InlineKeyboardButton(label, callback_data=f"editfield:{key}")]
        for key, label in EDIT_FIELDS.items()
    ]
    await query.message.reply_text(
        f"Редактируем: {name}\nЧто меняем?", reply_markup=InlineKeyboardMarkup(buttons)
    )
    return EDIT_CHOOSE_FIELD


async def edit_choose_field(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    field = query.data.split(":", 1)[1]
    context.user_data["edit_field"] = field

    prompts = {
        "name": "Новое название страны:",
        "leader": "Новое имя лидера:",
        "capital": "Новая столица:",
        "continents": "Новый список континентов (через запятую):",
        "card": "Пришлите новую карточку (картинкой):",
        "flag": "Пришлите новый флаг (файлом, без сжатия):",
        "herb": "Пришлите новый герб (файлом, без сжатия), либо напишите \"нет\":",
        "description": "Новое описание (максимум 500 символов):",
        "lore_links": "Новые ссылки на лор (через запятую):",
    }
    await query.message.reply_text(prompts[field])
    return EDIT_VALUE


async def edit_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = context.user_data.get("edit_country_name")
    field = context.user_data.get("edit_field")
    c = DATA["countries"].get(name)
    if not c:
        await update.message.reply_text("Страна не найдена, отмена.")
        return ConversationHandler.END

    if field in ("card", "flag"):
        if update.message.document:
            file_id = update.message.document.file_id
        elif update.message.photo:
            file_id = update.message.photo[-1].file_id
            await update.message.reply_text(
                "Внимание: изображение могло быть сжато Telegram (отправлено как фото)."
            )
        else:
            await update.message.reply_text("Пришлите изображение.")
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
            await update.message.reply_text("Пришлите изображение герба или напишите \"нет\".")
            return EDIT_VALUE

    elif field == "continents":
        raw = fix_dashes(update.message.text.strip())
        c["continents"] = [x.strip() for x in raw.split(",") if x.strip()]

    elif field == "lore_links":
        raw = fix_dashes(update.message.text.strip())
        c["lore_links"] = [x.strip() for x in raw.split(",") if x.strip()]

    elif field == "description":
        text = fix_dashes(update.message.text.strip())
        if len(text) > 500:
            await update.message.reply_text(
                f"Слишком длинно ({len(text)} символов). Максимум 500. Попробуйте ещё раз."
            )
            return EDIT_VALUE
        c["description"] = text

    elif field == "name":
        new_name = fix_dashes(update.message.text.strip())
        if new_name != name and new_name in DATA["countries"]:
            await update.message.reply_text("Страна с таким названием уже есть. Введите другое.")
            return EDIT_VALUE
        DATA["countries"].pop(name)
        c["name"] = new_name
        DATA["countries"][new_name] = c
        name = new_name

    else:  # leader, capital
        c[field] = fix_dashes(update.message.text.strip())

    save_data()
    await update.message.reply_text("Изменения сохранены.")
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

    application.add_handler(CallbackQueryHandler(cb_list_countries, pattern=r"^list_countries$"))
    application.add_handler(CallbackQueryHandler(cb_show_country, pattern=r"^country:"))
    application.add_handler(CallbackQueryHandler(cb_flag, pattern=r"^flag:"))
    application.add_handler(CallbackQueryHandler(cb_herb, pattern=r"^herb:"))

    logger.info("АУРЕЛИЯ INFO BOT запущен.")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
