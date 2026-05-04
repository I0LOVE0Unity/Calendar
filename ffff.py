import asyncio
import logging
logging.basicConfig(level=logging.DEBUG)  # DEBUG вместо INFO


from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.client.session.aiohttp import AiohttpSession

logging.basicConfig(level=logging.INFO)

# 🔴 Ваш токен
TOKEN = "8663900694:AAGg2GNmZUxT-nYRonuzBcUnOBpw-Z476ro"

# 🟢 Порт SOCKS5-прокси (возьмите из настроек Hiddify/Happ/V2RayTun)
PROXY_URL = "socks5://127.0.0.1:12334"  # Замените порт если нужно

# Создаём сессию с поддержкой прокси
session = AiohttpSession(proxy=PROXY_URL)

# Передаём сессию в Bot
bot = Bot(token=TOKEN, session=session)

dp = Dispatcher()


@dp.message(Command("start", "help"))
async def send_welcome(message: types.Message):
    await message.reply("Привет! Я бот через VPN. Напиши мне что-нибудь.")

@dp.message()
async def echo(message: types.Message):
    await message.answer(f"Ты сказал: {message.text}")

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())