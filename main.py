import os
import logging
from typing import Dict, Tuple, Set

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, types, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

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


def format_user_info(user: types.User) -> str:
    text = f"👤 <b>{user.full_name}</b>"
    if user.username:
        text += f" (@{user.username})"
    text += f"\n🆔 <code>{user.id}</code>"
    return text


# --- Хендлер /start в личке ---

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
        "или отправьте фото с подписью – админы всё прочитают и при необходимости ответят вам."
    )
    await message.answer(text)


# --- Сообщения пользователей боту в личку (текст/фото) ---

@dp.message(F.chat.type == "private")
async def handle_user_message(message: types.Message):
    # Игнорируем другие команды типа /help, /info и т.д.
    if message.text and message.text.startswith("/"):
        return

    user = message.from_user
    user_block = format_user_info(user)

    # 1) Подтверждение пользователю
    await message.answer("Спасибо, ваше сообщение отправлено админам ✅")

    # 2) Отправляем в админ-группу
    sent_msg = None

    # Текстовое сообщение
    if message.text:
        admin_text = (
            "📩 <b>Новое сообщение от пользователя</b>\n\n"
            f"{user_block}\n\n"
            f"💬 <b>Текст:</b>\n{message.text}"
        )
        sent_msg = await bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=admin_text,
        )

    # Фото с подписью
    elif message.photo:
        caption = message.caption or ""
        admin_caption = (
            "📩 <b>Новое сообщение с фото от пользователя</b>\n\n"
            f"{user_block}\n\n"
            f"💬 <b>Подпись:</b>\n{caption}"
        )
        sent_msg = await bot.send_photo(
            chat_id=ADMIN_CHAT_ID,
            photo=message.photo[-1].file_id,
            caption=admin_caption,
        )

    else:
        # Можно дописать обработку других типов, пока просто напишем юзеру
        await bot.send_message(
            chat_id=user.id,
            text="Пока я принимаю только текстовые сообщения и фотографии.",
        )
        return

    # 3) Запоминаем, что на это сообщение в группе можно ответить реплаем
    if sent_msg:
        message_targets[(ADMIN_CHAT_ID, sent_msg.message_id)] = user.id


# --- Ответы админов в группе (реплай на сообщение бота) ---

@dp.message(F.chat.id == ADMIN_CHAT_ID, F.reply_to_message)
async def handle_admin_reply(message: types.Message):
    key = (message.chat.id, message.reply_to_message.message_id)
    user_id = message_targets.get(key)

    if not user_id:
        # Нет привязки – значит это ответ не на "служебное" сообщение бота
        return

    admin_name = message.from_user.full_name

    # Формируем текст / медиасообщение для пользователя
    header = f"{admin_name} ответил(а) на ваше сообщение:"

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

    # Сообщаем в группе, что ответ ушёл
    await message.reply("✅ Ответ отправлен пользователю.")

    # Удаляем привязку, чтобы повторно не отвечать на ту же штуку
    del message_targets[key]


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
