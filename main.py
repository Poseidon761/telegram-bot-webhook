import os
import logging
from typing import Dict, Tuple

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, types, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ForceReply,
)

# Загружаем .env (локально), на Render переменные берутся из Environment
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

# (admin_id, prompt_message_id) -> target_user_id
reply_targets: Dict[Tuple[int, int], int] = {}


def format_user_info(user: types.User) -> str:
    text = f"👤 <b>{user.full_name}</b>\n🆔 <code>{user.id}</code>"
    if user.username:
        text += f"\n📛 @{user.username}"
    return text


# 1) Хендлер для ответов админов (они отвечают на ForceReply-сообщение)
@dp.message()
async def handle_message(message: types.Message):
    # --- режим ответа админа ---
    if message.reply_to_message:
        key = (message.from_user.id, message.reply_to_message.message_id)
        target_user_id = reply_targets.get(key)

        if target_user_id:
            # Это ответ на "Введите текст ответа..."
            if message.text:
                await bot.send_message(
                    chat_id=target_user_id,
                    text=message.text,
                )
                await message.answer("✅ Ответ отправлен пользователю.")
            else:
                await message.answer(
                    "Пока я умею отправлять только текстовые ответы."
                )
            # больше эта связка не нужна
            del reply_targets[key]
            return

    # --- все Остальные сообщения ---

    # Игнорируем группы/каналы (бот в админ-группе тоже сюда пишет)
    if message.chat.type != "private":
        return

    # Сообщение от обычного пользователя боту в личку
    user = message.from_user
    text = message.text or "<нет текста>"

    # Подтверждение пользователю
    await message.answer("Спасибо, ваше сообщение отправлено админам ✅")

    # Текст для админ-группы
    user_block = format_user_info(user)
    admin_text = (
        "📩 <b>Новое сообщение от пользователя</b>\n\n"
        f"{user_block}\n\n"
        f"💬 <b>Текст:</b>\n{text}"
    )

    # Кнопка "Ответить"
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Ответить пользователю",
                    callback_data=f"reply:{user.id}",
                )
            ]
        ]
    )

    # Отправляем в админ-группу
    await bot.send_message(
        chat_id=ADMIN_CHAT_ID,
        text=admin_text,
        reply_markup=kb,
    )


# 2) Обработчик нажатия на кнопку "Ответить"
@dp.callback_query(F.data.startswith("reply:"))
async def handle_reply_button(callback: types.CallbackQuery):
    data = callback.data or ""
    try:
        _, user_id_str = data.split(":", 1)
        target_user_id = int(user_id_str)
    except Exception:
        await callback.answer("Ошибка: не могу прочитать ID пользователя.", show_alert=True)
        return

    # Чтобы бот мог написать админу в личку, админ должен хотя бы раз написать боту /start
    prompt = await bot.send_message(
        chat_id=callback.from_user.id,
        text=(
            "✉️ Отправьте ответ для пользователя с ID "
            f"<code>{target_user_id}</code>.\n"
            "Просто напишите сообщение, ответом на это."
        ),
        reply_markup=ForceReply(selective=True),
    )

    # Запоминаем, что если этот админ ответит на prompt, то это ответ этому user_id
    reply_targets[(callback.from_user.id, prompt.message_id)] = target_user_id

    await callback.answer("Режим ответа открыт в личке ✅", show_alert=False)


# --- Webhook FastAPI часть ---


@app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    try:
        data = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"ok": False})

    update = types.Update.model_validate(data)
    await dp.feed_update(bot, update)
    return {"ok": True}


@app.get("/")
async def root():
    return {"status": "ok", "message": "Telegram bot webhook is running"}
