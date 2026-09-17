import asyncio
import os
import threading
from flask import Flask
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command

# --- ФЛАСК ДЛЯ ПОДДЕРЖКИ ПОРТА НА RENDER ---
app = Flask(__name__)

@app.route('/')
@app.route('/health')
def health():
    return "Bot is running!"

def run_flask():
    # Render сам передает порт через переменную окружения PORT
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

# --- БОТ ---
bot = Bot(token=os.getenv("BOT_TOKEN"))
dp = Dispatcher()

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer("Привет! Я бот-планировщик. Напиши мне свои задачи, и я их структурирую.")

@dp.message()
async def handle_text(message: types.Message):
    # Здесь пока просто эхо, потом заменим на парсинг через LLM
    await message.answer(f"Я записал: {message.text}")

async def main():
    # Запускаем Flask в отдельном потоке
    threading.Thread(target=run_flask, daemon=True).start()
    # Запускаем бота
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
