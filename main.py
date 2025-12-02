import os
import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, types
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

# Загружаем переменные из .env (локально)
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/webhook")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

logging.basicConfig(level=logging.INFO)

# Создаем FastAPI-приложение
app = FastAPI()

# Создаем сессию и бота
session = AiohttpSession()
bot = Bot(
    token=BOT_TOKEN,
    session=session,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)

dp = Dispatcher()


# Хендлер на любое текстовое сообщение
@dp.message()
async def handle_all_messages(message: types.Message):
    # Пока просто эхо-ответ
    if message.text:
        await message.answer(f"Ты написал: <b>{message.text}</b>")
    else:
        await message.answer("Я пока отвечаю только на текст 🙂")


# Webhook endpoint – сюда Телеграм будет слать обновления
@app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    try:
        data = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"ok": False})

    update = types.Update.model_validate(data)
    await dp.feed_update(bot, update)
    return {"ok": True}


# Простой GET-эндпоинт для проверки, что сервер жив
@app.get("/")
async def root():
    return {"status": "ok", "message": "Telegram bot webhook is running"}
