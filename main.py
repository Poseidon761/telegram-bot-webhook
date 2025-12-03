import os
import time
import logging
from typing import Dict, Tuple, Set, Any

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

# (chat_id, bot_message_id) -> user_id (для ответов из группы)
message_targets: Dict[Tuple[int, int], int] = {}

# защита от повторной обработки одного и того же апдейта
processed_updates: Set[int] = set()

# пользователи с включенным анонимным режимом
anon_users: Set[int] = set()

# заблокированные пользователи
banned_users: Set[int] = set()

# статистика
stats_total_messages: int = 0
stats_text_messages: int = 0
stats_photo_messages: int = 0
stats_unique_users: Set[int] = set()

# анти-дубляж: последний отправленный в группу месседж для каждого пользователя
# user_id -> {"chat_id": int, "message_id": int, "text": str, "time": float, "is_anon": bool}
last_admin_message: Dict[int, Dict[str, Any]] = {}


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


# --- Команда /start в личке ---


@dp.message(F.chat.type == "private", F.text == "/start")
async def cmd_start(message: types.Message):
    text = (
        "Привет! 👋\n\n"
        "Это бот для отправки сообщений Аббасу Галлямову.\n\n"
        "Здесь вы можете:\n"
        "• задать вопрос\n"
        "• поделиться мнением\n"
        "• отправить идею или предложение\n\n"
        "Просто напишите сообщение одним или несколькими абзацами, "
        "или отправьте фото с подписью – админы всё прочитают и при необходимости ответят вам.\n\n"
        "Если хотите скрыть свои данные от админов, отправьте команду /anon – тогда ваши сообщения "
        "будут приходить как анонимные. При этом бот все равно сможет вам отвечать."
    )
    await message.answer(text)


# --- Команда /anon (анонимный режим) ---


@dp.message(F.chat.type == "private", F.text.regexp(r"^/anon"))
async def cmd_anon(message: types.Message):
    user_id = message.from_user.id
    parts = message.text.split(maxsplit=1)

    # переключатель: если /anon без аргументов - просто переключаем режим
    if len(parts) == 1:
        if user_id in anon_users:
            anon_users.remove(user_id)
            await message.answer(
                "Анонимный режим отключен. Ваши будущие сообщения будут приходить админам с вашим именем."
            )
        else:
            anon_users.add(user_id)
            await message.answer(
                "Анонимный режим включен. Ваши будущие сообщения будут приходить админам как анонимные."
            )
        return

    arg = parts[1].strip().lower()
    if arg in ("on", "вкл", "on.", "включить"):
        anon_users.add(user_id)
        await message.answer(
            "Анонимный режим включен. Ваши будущие сообщения будут приходить админам как анонимные."
        )
    elif arg in ("off", "выкл", "выключить"):
        anon_users.discard(user_id)
        await message.answer(
            "Анонимный режим отключен. Ваши будущие сообщения будут приходить админам с вашим именем."
        )
    else:
        await message.answer(
            "Использование команды:\n"
            "/anon - переключить режим\n"
            "/anon on - включить анонимный режим\n"
            "/anon off - выключить анонимный режим"
        )


# --- Сообщения пользователей боту в личке (текст/фото) ---


@dp.message(F.chat.type == "private")
async def handle_user_message(message: types.Message):
    global stats_total_messages, stats_text_messages, stats_photo_messages

    user = message.from_user
    user_id = user.id

    # игнорируем все команды (кроме /start и /anon - они уже обработаны выше)
    if message.text and message.text.startswith("/"):
        return

    # проверка на бан
    if user_id in banned_users:
        await message.answer(
            "Вы были заблокированы и больше не можете пользоваться этим ботом."
        )
        return

    # статистика
    stats_total_messages += 1
    stats_unique_users.add(user_id)
    if message.text:
        stats_text_messages += 1
    elif message.photo:
        stats_photo_messages += 1

    is_anon = user_id in anon_users

    # подтверждение пользователю
    await message.answer("Спасибо, ваше сообщение отправлено админам ✅")

    # формируем текст для админов
    sent_msg: types.Message | None = None

    # вариант: только текст
    if message.text:
        base_text: str
        if is_anon:
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

        now = time.time()
        info = last_admin_message.get(user_id)

        # если последнее сообщение этого пользователя было меньше 60 секунд назад и тоже текстовое - редактируем его
        if info and now - info["time"] <= 60 and not info.get("has_photo", False):
            # дополняем существующий текст
            new_text = info["text"] + "\n\n➕ <b>Дополнение:</b>\n" + message.text
            await bot.edit_message_text(
                chat_id=info["chat_id"],
                message_id=info["message_id"],
                text=new_text,
            )
            # обновляем время и текст
            info["time"] = now
            info["text"] = new_text
            last_admin_message[user_id] = info
            # бан-кнопка уже есть в этом сообщении, заново не добавляем
            sent_msg = None
        else:
            # отправляем новое сообщение в админ-группу
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
                "has_photo": False,
                "is_anon": is_anon,
            }

    # вариант: фото
    elif message.photo:
        caption = message.caption or ""
        if is_anon:
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

        sent_msg = await bot.send_photo(
            chat_id=ADMIN_CHAT_ID,
            photo=message.photo[-1].file_id,
            caption=admin_caption,
            reply_markup=make_ban_keyboard(user_id),
        )

        # фото не мержим в одно сообщение с текстом
        last_admin_message[user_id] = {
            "chat_id": ADMIN_CHAT_ID,
            "message_id": sent_msg.message_id,
            "text": admin_caption,
            "time": time.time(),
            "has_photo": True,
            "is_anon": is_anon,
        }

    else:
        await bot.send_message(
            chat_id=user_id,
            text="Пока я принимаю только текстовые сообщения и фотографии.",
        )
        return

    # привязка для ответов (если есть свежее сообщение от бота в группе)
    if sent_msg:
        message_targets[(ADMIN_CHAT_ID, sent_msg.message_id)] = user_id


# --- Ответы админов в группе (реплай на сообщение бота) ---


@dp.message(F.chat.id == ADMIN_CHAT_ID, F.reply_to_message)
async def handle_admin_reply(message: types.Message):
    key = (message.chat.id, message.reply_to_message.message_id)
    user_id = message_targets.get(key)

    if not user_id:
        # нет привязки – значит это ответ не на "служебное" сообщение бота
        return

    # фиксированное имя отправителя для пользователя
    admin_name = "Аббас Галлямов"
    header = f"{admin_name} ответил на ваше сообщение:"

    # если пользователь уже в бане – не отвечаем
    if user_id in banned_users:
        await message.reply("Пользователь уже заблокирован, ответ не отправлен.")
        return

    # Ответ текстом
    if message.text:
        await bot.send_message(
            chat_id=user_id,
            text=f"{header}\n\n{message.text}",
        )

    # Ответ фотографией
    elif message.photo:
        caption = message.caption or ""
        await bot.send_photo(
            chat_id=user_id,
            photo=message.photo[-1].file_id,
            caption=f"{header}\n\n{caption}",
        )

    else:
        await bot.send_message(
            chat_id=user_id,
            text=f"{header}\n\n(отправлен ответ, который я пока не умею переслать в исходном виде)",
        )

    await message.reply("✅ Ответ отправлен пользователю.")


# --- Кнопки бана (ban, confirm, cancel) ---


@dp.callback_query(F.message.chat.id == ADMIN_CHAT_ID, F.data.startswith("ban:"))
async def handle_ban_button(callback: types.CallbackQuery):
    data = callback.data or ""
    try:
        _, user_id_str = data.split(":", 1)
        target_user_id = int(user_id_str)
    except Exception:
        await callback.answer("Ошибка: не могу прочитать ID пользователя.", show_alert=True)
        return

    # показываем клавиатуру подтверждения
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

    # убираем клавиатуру или меняем её обратно на одну кнопку
    await callback.message.edit_reply_markup(
        reply_markup=None
    )
    await callback.message.reply("🚫 Пользователь заблокирован.")
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

    # возвращаем обычную кнопку "Заблокировать пользователя"
    await callback.message.edit_reply_markup(
        reply_markup=make_ban_keyboard(target_user_id)
    )
    await callback.answer("Блокировка отменена.", show_alert=False)


# --- Статистика для админов ---


@dp.message(F.chat.id == ADMIN_CHAT_ID, F.text == "/stats")
async def cmd_stats(message: types.Message):
    text = (
        "📊 <b>Статистика бота</b>\n\n"
        f"Всего сообщений от пользователей: <b>{stats_total_messages}</b>\n"
        f"Уникальных пользователей: <b>{len(stats_unique_users)}</b>\n"
        f"Текстовых сообщений: <b>{stats_text_messages}</b>\n"
        f"Сообщений с фото: <b>{stats_photo_messages}</b>\n"
        f"Анонимных пользователей (сейчас): <b>{len(anon_users)}</b>\n"
        f"Заблокированных пользователей: <b>{len(banned_users)}</b>"
    )
    await message.reply(text)


# --- Webhook FastAPI часть ---


@app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    try:
        data = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"ok": False})

    update = types.Update.model_validate(data)

    # простая защита от повторной обработки одного и того же апдейта
    if update.update_id in processed_updates:
        return {"ok": True}
    processed_updates.add(update.update_id)

    await dp.feed_update(bot, update)
    return {"ok": True}


@app.get("/")
async def root():
    return {"status": "ok", "message": "Telegram bot webhook is running"}
