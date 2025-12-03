import os
import time
import logging
from typing import Dict, Tuple, Set, Any, List

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, types, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

# Загружаем .env локально (на Render переменные берутся из Environment)
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/webhook")
ADMIN_CHAT_ID_STR = os.getenv("ADMIN_CHAT_ID")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

if not ADMIN_CHAT_ID_STR:
    raise RuntimeError("ADMIN_CHAT_ID is not set")

try:
    ADMIN_CHAT_ID = int(ADMIN_CHAT_ID_STR)
except ValueError:
    raise RuntimeError("ADMIN_CHAT_ID must be integer (например -1001234567890)")

logging.basicConfig(level=logging.INFO)

app = FastAPI()

session = AiohttpSession()
bot = Bot(
    token=BOT_TOKEN,
    session=session,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()

# --- Глобальные структуры ---

# (chat_id, bot_message_id) -> user_id (для ответов из группы)
message_targets: Dict[Tuple[int, int], int] = {}

# защита от повторной обработки одного и того же апдейта
processed_updates: Set[int] = set()

# настройки пользователей: user_id -> {"lang": "ru"/"en", "anon": bool, "status_msg_id": int|None}
user_settings: Dict[int, Dict[str, Any]] = {}

# заблокированные пользователи
banned_users: Set[int] = set()

# журнал банов: user_id -> {"timestamp": float, "name": str|None, "username": str|None}
ban_log: Dict[int, Dict[str, Any]] = {}

# лог всех пользовательских сообщений для статистики
# каждый элемент: {"user_id": int, "timestamp": float, "type": "text"|"photo"|"video", "is_anon": bool}
user_message_log: List[Dict[str, Any]] = []

# анти-дубляж: последний отправленный в группу месседж для каждого пользователя
# user_id -> {"chat_id": int, "message_id": int, "text": str, "time": float, "has_media": bool, "is_anon": bool}
last_admin_message: Dict[int, Dict[str, Any]] = {}

# обработанные media_group_id, чтобы не слать "спасибо" по 10 раз на альбом
handled_media_groups: Set[str] = set()


# --- Вспомогательные функции ---

def get_user_settings(user_id: int) -> Dict[str, Any]:
    if user_id not in user_settings:
        user_settings[user_id] = {
            "lang": "ru",
            "anon": False,
            "status_msg_id": None,
        }
    return user_settings[user_id]


def get_lang(user_id: int) -> str:
    return get_user_settings(user_id).get("lang", "ru")


def is_anon(user_id: int) -> bool:
    return bool(get_user_settings(user_id).get("anon", False))


def format_user_info(user: types.User) -> str:
    text = f"👤 <b>{user.full_name}</b>"
    if user.username:
        text += f" (@{user.username})"
    text += f"\n🆔 <code>{user.id}</code>"
    return text


def make_ban_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🚫 Заблокировать пользователя",
                    callback_data=f"ban:{user_id}",
                )
            ]
        ]
    )


def make_unban_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔓 Разблокировать пользователя",
                    callback_data=f"unban:{user_id}",
                )
            ]
        ]
    )


def build_status_text(lang: str, anon: bool) -> str:
    if lang == "en":
        anon_part = "Anon: ON" if anon else "Anon: OFF"
        lang_part = "Lang: English"
        return f"{anon_part} | {lang_part}"
    else:
        anon_part = "Анон: Вкл" if anon else "Анон: Выкл"
        lang_part = "Язык: Русский"
        return f"{anon_part} | {lang_part}"


async def ensure_status_message(user_id: int) -> None:
    settings = get_user_settings(user_id)
    lang = settings["lang"]
    anon = settings["anon"]
    status_msg_id = settings.get("status_msg_id")

    text = build_status_text(lang, anon)

    if status_msg_id:
        try:
            await bot.edit_message_text(
                chat_id=user_id,
                message_id=status_msg_id,
                text=text,
            )
            return
        except Exception:
            pass

    msg = await bot.send_message(chat_id=user_id, text=text)
    try:
        await bot.pin_chat_message(chat_id=user_id, message_id=msg.message_id)
    except Exception:
        pass
    settings["status_msg_id"] = msg.message_id


def build_start_text(lang: str) -> str:
    if lang == "en":
        return (
            "Hi! 👋\n\n"
            "This is a bot for sending messages to Abbas Gallyamov.\n\n"
            "Here you can:\n"
            "• ask a question\n"
            "• share your opinion\n"
            "• send an idea or suggestion\n\n"
            "You can send a message as text, photo or video. "
            "Admins will read it and, if necessary, reply to you.\n\n"
            "You can enable anonymous mode so that admins do not see your data. "
            "Use the button below or the /anon command.\n"
            "After changing anonymity or language, just send your message."
        )
    else:
        return (
            "Привет! 👋\n\n"
            "Это бот для отправки сообщений Аббасу Галлямову.\n\n"
            "Здесь вы можете:\n"
            "• задать вопрос\n"
            "• поделиться мнением\n"
            "• отправить идею или предложение\n\n"
            "Вы можете отправлять текст, фотографии и видео. "
            "Админы всё прочитают и при необходимости ответят вам.\n\n"
            "Вы можете включить анонимный режим, чтобы админы не видели ваши данные. "
            "Используйте кнопку ниже или команду /anon.\n"
            "После изменения анонимности или языка просто отправьте сообщение."
        )


def build_thanks_text(lang: str) -> str:
    if lang == "en":
        return "Thank you, your message has been sent ✅"
    else:
        return "Спасибо, ваше сообщение отправлено ✅"


def build_blocked_text(lang: str) -> str:
    if lang == "en":
        return "You have been blocked and can no longer use this bot."
    else:
        return "Вы были заблокированы и больше не можете пользоваться этим ботом."


def build_unsupported_text(lang: str) -> str:
    if lang == "en":
        return "Right now I only support text messages, photos and videos."
    else:
        return "Пока я принимаю только текстовые сообщения, фотографии и видео."


def build_anon_on_text(lang: str) -> str:
    if lang == "en":
        return "Anonymous mode is now ON. Your next messages will be sent anonymously."
    else:
        return "Анонимный режим включен. Ваши следующие сообщения будут приходить как анонимные."


def build_anon_off_text(lang: str) -> str:
    if lang == "en":
        return "Anonymous mode is now OFF. Your future messages will be sent with your data."
    else:
        return "Анонимный режим отключен. Ваши будущие сообщения будут приходить с вашими данными."


def build_answer_header(lang: str) -> str:
    if lang == "en":
        return "Abbas Gallyamov replied to your message:"
    else:
        return "Аббас Галлямов ответил на ваше сообщение:"


def build_stats_period_label(period: str) -> str:
    if period == "day":
        return "за последние 24 часа"
    elif period == "week":
        return "за последнюю неделю"
    elif period == "month":
        return "за последний месяц"
    else:
        return "за все время"


# --- Клавиатура под /start ---

def make_start_keyboard(lang: str, anon: bool) -> InlineKeyboardMarkup:
    if lang == "en":
        anon_text = "Disable anonymous mode" if anon else "Enable anonymous mode"
        ru_btn = "Русский"
        en_btn = "English"
    else:
        anon_text = "Выключить анонимный режим" if anon else "Включить анонимный режим"
        ru_btn = "Русский"
        en_btn = "English"

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=anon_text,
                    callback_data="toggle_anon",
                )
            ],
            [
                InlineKeyboardButton(
                    text=ru_btn,
                    callback_data="lang:ru",
                ),
                InlineKeyboardButton(
                    text=en_btn,
                    callback_data="lang:en",
                ),
            ],
        ]
    )


# --- /start в личке ---


@dp.message(F.chat.type == "private", F.text == "/start")
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    settings = get_user_settings(user_id)
    lang = settings["lang"]
    anon = settings["anon"]

    await message.answer(
        build_start_text(lang),
        reply_markup=make_start_keyboard(lang, anon),
    )

    await ensure_status_message(user_id)


# --- Callback: смена языка и анонимности (кнопки под стартовым) ---


@dp.callback_query(F.message.chat.type == "private", F.data == "toggle_anon")
async def cb_toggle_anon(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    settings = get_user_settings(user_id)
    settings["anon"] = not settings.get("anon", False)
    lang = settings["lang"]

    await ensure_status_message(user_id)

    try:
        await callback.message.edit_reply_markup(
            reply_markup=make_start_keyboard(lang, settings["anon"])
        )
    except Exception:
        pass

    await callback.answer(
        build_anon_on_text(lang) if settings["anon"] else build_anon_off_text(lang),
        show_alert=False,
    )


@dp.callback_query(F.message.chat.type == "private", F.data.startswith("lang:"))
async def cb_set_lang(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    settings = get_user_settings(user_id)
    _, lang_code = callback.data.split(":", 1)

    if lang_code not in ("ru", "en"):
        await callback.answer("Unknown language", show_alert=True)
        return

    settings["lang"] = lang_code
    lang = settings["lang"]

    await ensure_status_message(user_id)

    # меняем и текст приветствия, и клавиатуру
    try:
        await callback.message.edit_text(
            build_start_text(lang),
            reply_markup=make_start_keyboard(lang, settings["anon"]),
        )
    except Exception:
        try:
            await callback.message.edit_reply_markup(
                reply_markup=make_start_keyboard(lang, settings["anon"])
            )
        except Exception:
            pass

    if lang == "en":
        await callback.answer("Language switched to English", show_alert=False)
    else:
        await callback.answer("Язык переключен на русский", show_alert=False)


# --- /anon в личке ---


@dp.message(F.chat.type == "private", F.text.regexp(r"^/anon"))
async def cmd_anon(message: types.Message):
    user_id = message.from_user.id
    settings = get_user_settings(user_id)
    lang = settings["lang"]

    parts = message.text.split(maxsplit=1)

    if len(parts) == 1:
        settings["anon"] = not settings["anon"]
        await ensure_status_message(user_id)
        if settings["anon"]:
            await message.answer(build_anon_on_text(lang))
        else:
            await message.answer(build_anon_off_text(lang))
        return

    arg = parts[1].strip().lower()
    if arg in ("on", "вкл", "on.", "включить"):
        settings["anon"] = True
        await ensure_status_message(user_id)
        await message.answer(build_anon_on_text(lang))
    elif arg in ("off", "выкл", "выключить"):
        settings["anon"] = False
        await ensure_status_message(user_id)
        await message.answer(build_anon_off_text(lang))
    else:
        if lang == "en":
            await message.answer(
                "Usage:\n"
                "/anon - toggle anonymous mode\n"
                "/anon on - enable anonymous mode\n"
                "/anon off - disable anonymous mode"
            )
        else:
            await message.answer(
                "Использование команды:\n"
                "/anon - переключить режим\n"
                "/anon on - включить анонимный режим\n"
                "/anon off - выключить анонимный режим"
            )


# --- Сообщения пользователей боту в личке ---


@dp.message(F.chat.type == "private")
async def handle_user_message(message: types.Message):
    user = message.from_user
    user_id = user.id
    settings = get_user_settings(user_id)
    lang = settings["lang"]
    anon = settings["anon"]

    # игнорируем команды (кроме /start и /anon - уже обработаны)
    if message.text and message.text.startswith("/"):
        return

    # игнорируем закрепленные сообщения и прочий сервис, который не является реальным вводом
    if message.content_type == "pinned_message":
        return

    # проверка на бан
    if user_id in banned_users:
        await message.answer(build_blocked_text(lang))
        return

    # Определяем тип сообщения
    kind = None
    if message.text:
        kind = "text"
    elif message.photo:
        kind = "photo"
    elif message.video:
        kind = "video"
    else:
        kind = "unsupported"

    media_group_id = message.media_group_id
    is_album_first = False
    if media_group_id:
        if media_group_id in handled_media_groups:
            is_album_first = False
        else:
            is_album_first = True
            handled_media_groups.add(media_group_id)

    # Если тип не поддерживается – просто скажем об этом, без "спасибо"
    if kind == "unsupported":
        await message.answer(build_unsupported_text(lang))
        return

    # Логируем сообщение для статистики (один раз на альбом)
    if (not media_group_id) or is_album_first:
        user_message_log.append(
            {
                "user_id": user_id,
                "timestamp": time.time(),
                "type": kind,
                "is_anon": anon,
            }
        )

    # "Спасибо" только если сообщение будет реально отправлено (и раз на альбом)
    if (not media_group_id) or is_album_first:
        await message.answer(build_thanks_text(lang))

    sent_msg: types.Message | None = None

    # --- Текст (с анти-дубляжом, включая медиа, если последнее было медиа) ---
    if kind == "text":
        base_text: str
        now = time.time()
        info = last_admin_message.get(user_id)

        if anon:
            text_block = message.text
            # если есть последнее сообщение и оно свежее - дополняем его
        else:
            text_block = message.text






        if info and now - info["time"] <= 60:
            # есть последнее сообщение (может быть текст или медиа), добавим "Дополнение"
            old_text = info["text"]
            new_block = old_text + "\n\n➕ <b>Дополнение:</b>\n" + message.text

            # выбираем, какая клавиатура должна быть сейчас
            if user_id in banned_users:
                kb = make_unban_keyboard(user_id)
            else:
                kb = make_ban_keyboard(user_id)

            if info.get("has_media", False):
                # редактируем подпись медиа и сохраняем клавиатуру
                await bot.edit_message_caption(
                    chat_id=info["chat_id"],
                    message_id=info["message_id"],
                    caption=new_block,
                    reply_markup=kb,
                )
            else:
                # редактируем текст и сохраняем клавиатуру
                await bot.edit_message_text(
                    chat_id=info["chat_id"],
                    message_id=info["message_id"],
                    text=new_block,
                    reply_markup=kb,
                )

            info["time"] = now
            info["text"] = new_block
            last_admin_message[user_id] = info
            sent_msg = None






        else:
            # создаем новое текстовое сообщение
            if anon:
                base_text = (
                    "📩 <b>Новое анонимное сообщение</b>\n\n"
                    f"💬 <b>Текст:</b>\n{message.text}"
                )
            else:
                user_block = format_user_info(user)
                base_text = (
                    "📩 <b>Новое сообщение от пользователя</b>\n\n"
                    f"{user_block}\n\n"
                    f"💬 <b>Текст:</b>\n{message.text}"
                )

            sent_msg = await bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=base_text,
                reply_markup=make_ban_keyboard(user_id),
            )
            last_admin_message[user_id] = {
                "chat_id": ADMIN_CHAT_ID,
                "message_id": sent_msg.message_id,
                "text": base_text,
                "time": now,
                "has_media": False,
                "is_anon": anon,
            }

    # --- Фото ---
    elif kind == "photo":
        caption = message.caption or ""
        if anon:
            admin_caption = (
                "📩 <b>Новое анонимное сообщение с фото</b>\n\n"
                f"💬 <b>Подпись:</b>\n{caption}"
            )
        else:
            user_block = format_user_info(user)
            admin_caption = (
                "📩 <b>Новое сообщение с фото от пользователя</b>\n\n"
                f"{user_block}\n\n"
                f"💬 <b>Подпись:</b>\n{caption}"
            )

        # если это часть альбома, но не первая – просто досылаем фото
        if media_group_id and not is_album_first:
            await bot.send_photo(
                chat_id=ADMIN_CHAT_ID,
                photo=message.photo[-1].file_id,
                caption=caption or None,
            )
            return

        sent_msg = await bot.send_photo(
            chat_id=ADMIN_CHAT_ID,
            photo=message.photo[-1].file_id,
            caption=admin_caption,
            reply_markup=make_ban_keyboard(user_id),
        )

        last_admin_message[user_id] = {
            "chat_id": ADMIN_CHAT_ID,
            "message_id": sent_msg.message_id,
            "text": admin_caption,
            "time": time.time(),
            "has_media": True,
            "is_anon": anon,
        }

    # --- Видео ---
    elif kind == "video":
        caption = message.caption or ""
        if anon:
            admin_caption = (
                "📩 <b>Новое анонимное сообщение с видео</b>\n\n"
                f"💬 <b>Подпись:</b>\n{caption}"
            )
        else:
            user_block = format_user_info(user)
            admin_caption = (
                "📩 <b>Новое сообщение с видео от пользователя</b>\n\n"
                f"{user_block}\n\n"
                f"💬 <b>Подпись:</b>\n{caption}"
            )

        if media_group_id and not is_album_first:
            await bot.send_video(
                chat_id=ADMIN_CHAT_ID,
                video=message.video.file_id,
                caption=caption or None,
            )
            return

        sent_msg = await bot.send_video(
            chat_id=ADMIN_CHAT_ID,
            video=message.video.file_id,
            caption=admin_caption,
            reply_markup=make_ban_keyboard(user_id),
        )

        last_admin_message[user_id] = {
            "chat_id": ADMIN_CHAT_ID,
            "message_id": sent_msg.message_id,
            "text": admin_caption,
            "time": time.time(),
            "has_media": True,
            "is_anon": anon,
        }

    else:
        await message.answer(build_unsupported_text(lang))
        return

    if sent_msg:
        message_targets[(ADMIN_CHAT_ID, sent_msg.message_id)] = user_id


# --- Ответы админов в группе (реплай на сообщение бота) ---


@dp.message(F.chat.id == ADMIN_CHAT_ID, F.reply_to_message)
async def handle_admin_reply(message: types.Message):
    key = (message.chat.id, message.reply_to_message.message_id)
    user_id = message_targets.get(key)

    if not user_id:
        return

    settings = get_user_settings(user_id)
    lang = settings["lang"]

    header = build_answer_header(lang)

    if user_id in banned_users:
        await message.reply("Пользователь уже заблокирован, ответ не отправлен.")
        return

    if message.text:
        await bot.send_message(
            chat_id=user_id,
            text=f"{header}\n\n{message.text}",
        )
    elif message.photo:
        caption = message.caption or ""
        await bot.send_photo(
            chat_id=user_id,
            photo=message.photo[-1].file_id,
            caption=f"{header}\n\n{caption}",
        )
    elif message.video:
        caption = message.caption or ""
        await bot.send_video(
            chat_id=user_id,
            video=message.video.file_id,
            caption=f"{header}\n\n{caption}",
        )
    else:
        await bot.send_message(
            chat_id=user_id,
            text=f"{header}\n\n(отправлен ответ, который я пока не умею переслать в исходном виде)",
        )

    await message.reply("✅ Ответ отправлен пользователю.")


# --- Кнопки бана и разбана ---


@dp.callback_query(F.message.chat.id == ADMIN_CHAT_ID, F.data.startswith("ban:"))
async def handle_ban_button(callback: types.CallbackQuery):
    data = callback.data or ""
    try:
        _, user_id_str = data.split(":", 1)
        target_user_id = int(user_id_str)
    except Exception:
        await callback.answer("Ошибка: не могу прочитать ID пользователя.", show_alert=True)
        return

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Подтвердить блокировку",
                    callback_data=f"banconfirm:{target_user_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="❌ Отмена",
                    callback_data=f"bancancel:{target_user_id}",
                )
            ],
        ]
    )
    await callback.message.edit_reply_markup(reply_markup=kb)
    await callback.answer("Вы уверены, что хотите заблокировать пользователя?", show_alert=False)


@dp.callback_query(F.message.chat.id == ADMIN_CHAT_ID, F.data.startswith("banconfirm:"))
async def handle_ban_confirm(callback: types.CallbackQuery):
    data = callback.data or ""
    try:
        _, user_id_str = data.split(":", 1)
        target_user_id = int(user_id_str)
    except Exception:
        await callback.answer("Ошибка: не могу прочитать ID пользователя.", show_alert=True)
        return

    banned_users.add(target_user_id)

    ts = time.time()
    name = None
    username = None
    try:
        chat = await bot.get_chat(target_user_id)
        name = chat.full_name
        username = chat.username
    except Exception:
        pass

    ban_log[target_user_id] = {
        "timestamp": ts,
        "name": name,
        "username": username,
    }

    await callback.message.edit_reply_markup(
        reply_markup=make_unban_keyboard(target_user_id)
    )
    await callback.answer("Пользователь добавлен в черный список.", show_alert=False)


@dp.callback_query(F.message.chat.id == ADMIN_CHAT_ID, F.data.startswith("bancancel:"))
async def handle_ban_cancel(callback: types.CallbackQuery):
    data = callback.data or ""
    try:
        _, user_id_str = data.split(":", 1)
        target_user_id = int(user_id_str)
    except Exception:
        await callback.answer("Отмена.", show_alert=False)
        return

    await callback.message.edit_reply_markup(
        reply_markup=make_ban_keyboard(target_user_id)
    )
    await callback.answer("Блокировка отменена.", show_alert=False)


@dp.callback_query(F.message.chat.id == ADMIN_CHAT_ID, F.data.startswith("unban:"))
async def handle_unban_button(callback: types.CallbackQuery):
    data = callback.data or ""
    try:
        _, user_id_str = data.split(":", 1)
        target_user_id = int(user_id_str)
    except Exception:
        await callback.answer("Ошибка: не могу прочитать ID пользователя.", show_alert=True)
        return

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Подтвердить разблокировку",
                    callback_data=f"unbanconfirm:{target_user_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="❌ Отмена",
                    callback_data=f"unbancancel:{target_user_id}",
                )
            ],
        ]
    )
    await callback.message.edit_reply_markup(reply_markup=kb)
    await callback.answer("Подтвердить разблокировку пользователя?", show_alert=False)


@dp.callback_query(F.message.chat.id == ADMIN_CHAT_ID, F.data.startswith("unbanconfirm:"))
async def handle_unban_confirm(callback: types.CallbackQuery):
    data = callback.data or ""
    try:
        _, user_id_str = data.split(":", 1)
        target_user_id = int(user_id_str)
    except Exception:
        await callback.answer("Ошибка: не могу прочитать ID пользователя.", show_alert=True)
        return

    banned_users.discard(target_user_id)
    ban_log.pop(target_user_id, None)

    await callback.message.edit_reply_markup(
        reply_markup=make_ban_keyboard(target_user_id)
    )
    await callback.answer("Пользователь удален из черного списка.", show_alert=False)


@dp.callback_query(F.message.chat.id == ADMIN_CHAT_ID, F.data.startswith("unbancancel:"))
async def handle_unban_cancel(callback: types.CallbackQuery):
    data = callback.data or ""
    try:
        _, user_id_str = data.split(":", 1)
        target_user_id = int(user_id_str)
    except Exception:
        await callback.answer("Отмена.", show_alert=False)
        return

    await callback.message.edit_reply_markup(
        reply_markup=make_unban_keyboard(target_user_id)
    )
    await callback.answer("Разблокировка отменена.", show_alert=False)


# --- /bans: список банов ---


@dp.message(F.chat.id == ADMIN_CHAT_ID, F.text == "/bans")
async def cmd_bans(message: types.Message):
    if not banned_users:
        await message.reply("🚫 В черном списке пока никого нет.")
        return

    for i, uid in enumerate(sorted(banned_users), start=1):
        info = ban_log.get(uid)
        if info:
            name = info.get("name") or "Имя неизвестно"
            username = info.get("username")
            ts = info.get("timestamp")
            if ts:
                dt_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
            else:
                dt_str = "дата неизвестна"

            text = f"{i}) {name}"
            if username:
                text += f" (@{username})"
            text += (
                f"\nID: <code>{uid}</code>\n"
                f"Заблокирован: {dt_str}"
            )
        else:
            text = (
                f"{i}) ID: <code>{uid}</code>\n"
                "Дополнительных данных нет."
            )

        await message.reply(text, reply_markup=make_unban_keyboard(uid))


# --- Статистика: выбор периода и расчет ---


def make_stats_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📅 За сутки",
                    callback_data="stats:day",
                ),
                InlineKeyboardButton(
                    text="📅 За неделю",
                    callback_data="stats:week",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📅 За месяц",
                    callback_data="stats:month",
                ),
                InlineKeyboardButton(
                    text="📅 За все время",
                    callback_data="stats:all",
                ),
            ],
        ]
    )


def make_stats_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔙 Назад",
                    callback_data="stats:back",
                )
            ]
        ]
    )


@dp.message(F.chat.id == ADMIN_CHAT_ID, F.text == "/stats")
async def cmd_stats(message: types.Message):
    kb = make_stats_menu_keyboard()
    await message.reply("Выберите период для статистики:", reply_markup=kb)


def build_stats_text(period: str) -> str:
    now = time.time()

    if period == "day":
        cutoff = now - 24 * 60 * 60
    elif period == "week":
        cutoff = now - 7 * 24 * 60 * 60
    elif period == "month":
        cutoff = now - 30 * 24 * 60 * 60
    else:
        cutoff = 0

    label = build_stats_period_label(period)

    filtered = [e for e in user_message_log if e["timestamp"] >= cutoff]

    if not filtered:
        return f"📊 За период {label} сообщений от пользователей не было."

    total = len(filtered)
    users = {e["user_id"] for e in filtered}
    text_count = sum(1 for e in filtered if e["type"] == "text")
    photo_count = sum(1 for e in filtered if e["type"] == "photo")
    video_count = sum(1 for e in filtered if e["type"] == "video")
    anon_users_in_period = {e["user_id"] for e in filtered if e["is_anon"]}

    text = (
        f"📊 <b>Статистика {label}</b>\n\n"
        f"Всего сообщений: <b>{total}</b>\n"
        f"Уникальных пользователей: <b>{len(users)}</b>\n"
        f"Текстовых сообщений: <b>{text_count}</b>\n"
        f"Сообщений с фото: <b>{photo_count}</b>\n"
        f"Сообщений с видео: <b>{video_count}</b>\n"
        f"Пользователей, писавших анонимно в этот период: <b>{len(anon_users_in_period)}</b>\n"
        f"Заблокированных пользователей сейчас: <b>{len(banned_users)}</b>"
    )
    return text


@dp.callback_query(F.message.chat.id == ADMIN_CHAT_ID, F.data.startswith("stats:"))
async def handle_stats_callback(callback: types.CallbackQuery):
    data = callback.data or ""
    try:
        _, period = data.split(":", 1)
    except Exception:
        await callback.answer("Ошибка при выборе периода.", show_alert=True)
        return

    if period == "back":
        # возвращаем меню выбора периода
        await callback.message.edit_text(
            "Выберите период для статистики:",
            reply_markup=make_stats_menu_keyboard(),
        )
        await callback.answer()
        return

    stats_text = build_stats_text(period)
    await callback.message.edit_text(
        stats_text,
        reply_markup=make_stats_back_keyboard(),
    )
    await callback.answer()


# --- Webhook FastAPI часть ---


@app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    try:
        data = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"ok": False})

    update = types.Update.model_validate(data)

    if update.update_id in processed_updates:
        return {"ok": True}
    processed_updates.add(update.update_id)

    await dp.feed_update(bot, update)
    return {"ok": True}


@app.get("/")
async def root():
    return {"status": "ok", "message": "Telegram bot webhook is running"}
