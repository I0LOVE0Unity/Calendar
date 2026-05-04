import os
import pickle
import logging
import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from collections import defaultdict
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.client.session.aiohttp import AiohttpSession
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

import socks
import socket



def get_calendar_service(user_id: int):
    creds = get_credentials(user_id)
    return build('calendar', 'v3', credentials=creds)


socks.set_default_proxy(
    socks.SOCKS5,
    "127.0.0.1",
    12334,
    rdns=True
)
socket.socket = socks.socksocket

# ---------- ЛОГИРОВАНИЕ ----------
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


# ---------- КОНСТАНТЫ ----------
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
PROXY_URL = os.getenv("PROXY_URL", "socks5://127.0.0.1:12334")
CREDENTIALS_FILE = "credentials.json"
DB_FILE = "bot_data.db"
SCOPES = ['https://www.googleapis.com/auth/calendar.readonly']
TIMEZONE = timezone(timedelta(hours=3))      # МСК
UTC = timezone.utc


# ---------- БАЗА ДАННЫХ (обновлённая) ----------
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    reminder_minutes INTEGER DEFAULT 30)''')
    # Таблица для отслеживания всех типов уведомлений (reminder + countdown)
    c.execute('''CREATE TABLE IF NOT EXISTS notified_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    event_id TEXT,
                    notification_type TEXT,
                    notified_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    # Уникальность: для каждого события и типа уведомления только одна запись
    c.execute('''CREATE UNIQUE INDEX IF NOT EXISTS idx_notified_unique 
                 ON notified_events(user_id, event_id, notification_type)''')
    c.execute('''CREATE TABLE IF NOT EXISTS event_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    event_id TEXT UNIQUE,
                    summary TEXT,
                    start_time TIMESTAMP,
                    end_time TIMESTAMP,
                    link TEXT)''')
    conn.commit()
    conn.close()


# ---------- GOOGLE АВТОРИЗАЦИЯ (без изменений) ----------
def get_credentials(user_id: int):
    creds = None
    token_path = f"tokens/{user_id}_token.pickle"
    Path("tokens").mkdir(exist_ok=True)
    if os.path.exists(token_path):
        with open(token_path, 'rb') as token:
            creds = pickle.load(token)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server()      # ← можно заменить на run_local_server, если браузер удобнее
        with open(token_path, 'wb') as token:
            pickle.dump(creds, token)
    return creds



# ---------- API КАЛЕНДАРЯ (без изменений) ----------
def get_upcoming_events(user_id: int, minutes_ahead: int = 10):
    """Возвращает события на ближайшие minutes_ahead минут"""
    service = get_calendar_service(user_id)
    now = datetime.now(UTC)
    future = now + timedelta(minutes=minutes_ahead)
    events_result = service.events().list(
        calendarId='primary',
        timeMin=now.strftime('%Y-%m-%dT%H:%M:%S.%fZ'),
        timeMax=future.strftime('%Y-%m-%dT%H:%M:%S.%fZ'),
        singleEvents=True,
        orderBy='startTime'
    ).execute()
    return events_result.get('items', [])

def get_past_events(user_id: int, max_results: int = 10):
    service = get_calendar_service(user_id)
    now = datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%S.%fZ')
    events_result = service.events().list(
        calendarId='primary',
        timeMax=now,
        singleEvents=True,
        orderBy='startTime',
        maxResults=max_results
    ).execute()
    return events_result.get('items', [])

# ---------- КОМАНДЫ БОТА (aiogram) ----------
async def start_command(message: types.Message):
    user_id = message.from_user.id
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
    conn.commit()
    conn.close()
    token_path = f"tokens/{user_id}_token.pickle"
    if not os.path.exists(token_path):
        await message.reply("Для доступа к календарю нужно авторизоваться.\nЗапустите /auth и следуйте инструкциям.")
    else:
        await message.reply("Вы уже авторизованы! Используйте /help для списка команд.")

async def auth_command(message: types.Message):
    user_id = message.from_user.id
    try:
        get_credentials(user_id)
        await message.reply("✅ Авторизация успешна!")
    except Exception as e:
        logger.error(f"Auth error for {user_id}: {e}")
        await message.reply("❌ Ошибка авторизации. Проверьте логи.")

async def set_reminder(message: types.Message):
    user_id = message.from_user.id
    args = message.text.split()
    if len(args) < 2:
        await message.reply("Использование: /set_reminder <минуты>")
        return
    try:
        minutes = int(args[1])
        if minutes <= 0:
            raise ValueError
    except ValueError:
        await message.reply("Использование: /set_reminder <минуты>")
        return
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET reminder_minutes = ? WHERE user_id = ?", (minutes, user_id))
    conn.commit()
    conn.close()
    await message.reply(f"⏰ Напоминание установлено за {minutes} мин. до встречи.")

async def history_command(message: types.Message):
    user_id = message.from_user.id
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT summary, start_time, link FROM event_history WHERE user_id = ? ORDER BY start_time DESC LIMIT 10",
        (user_id,))
    rows = cursor.fetchall()
    conn.close()
    if not rows:
        await message.reply("История пуста.")
        return
    text = "📋 *Последние 10 встреч:*\n"
    for i, (summary, start, link) in enumerate(rows, 1):
        start_dt = datetime.fromisoformat(start).astimezone(TIMEZONE)
        start_str = start_dt.strftime("%d.%m.%Y %H:%M")
        text += f"{i}. [{start_str}] {summary}"
        if link:
            text += f" [ссылка]({link})"
        text += "\n"
    await message.reply(text, disable_web_page_preview=True, parse_mode="Markdown")

async def help_command(message: types.Message):
    await message.reply(
        "/start - регистрация\n"
        "/auth - авторизация Google\n"
        "/set_reminder <минуты> - напоминание за N минут\n"
        "/history - последние встречи"
    )


# ---------- НОВАЯ СИСТЕМА ОТСЧЁТА И УВЕДОМЛЕНИЙ ----------
events_cache = defaultdict(lambda: (datetime.min.replace(tzinfo=UTC), []))

async def countdown_and_reminders(bot: Bot):

    while True:
        try:
            now = datetime.now(TIMEZONE)

            # 1. Получаем список всех пользователей
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            cursor.execute("SELECT user_id, reminder_minutes FROM users")
            users = cursor.fetchall()
            conn.close()

            for user_id, reminder_min in users:
                # Проверяем, нужно ли обновить кэш событий (раз в 30 секунд)
                last_update, cached_events = events_cache[user_id]
                if (now - last_update).total_seconds() > 30:
                    try:
                        fresh_events = get_upcoming_events(user_id, minutes_ahead=10)
                        events_cache[user_id] = (now, fresh_events)
                        logger.debug(f"Обновлён кэш для {user_id}: {len(fresh_events)} событий")
                    except Exception as e:
                        logger.error(f"Ошибка получения событий для {user_id}: {e}")
                        continue
                else:
                    fresh_events = cached_events

                # 2. Обрабатываем каждое событие из кэша
                for event in fresh_events:
                    start_info = event['start'].get('dateTime', event['start'].get('date'))
                    if 'date' in event['start'] and 'dateTime' not in event['start']:
                        continue  # события на целый день пропускаем
                    start_dt = datetime.fromisoformat(start_info.replace('Z', '+00:00'))
                    # Переводим в московское время для вычисления разницы
                    start_moscow = start_dt.astimezone(TIMEZONE)
                    delta_seconds = (start_moscow - now).total_seconds()

                    event_id = event['id']
                    summary = event.get('summary', 'Без названия')

                    # --- напоминание в чат за reminder_minutes ---
                    if 0 < delta_seconds <= reminder_min * 60:
                        if not is_notified(user_id, event_id, "reminder"):
                            link = event.get('hangoutLink') or event.get('htmlLink')
                            msg = f"⏰ Напоминание!\nВстреча: *{summary}*\nВремя: {start_moscow.strftime('%d.%m.%Y %H:%M')} (МСК)"
                            if link:
                                msg += f"\nСсылка: {link}"
                            try:
                                await bot.send_message(chat_id=user_id, text=msg,
                                                       parse_mode='Markdown', disable_web_page_preview=True)
                                logger.info(f"Отправлено напоминание пользователю {user_id}: {summary}")
                                mark_notified(user_id, event_id, "reminder")
                            except Exception as e:
                                logger.error(f"Ошибка отправки напоминания {user_id}: {e}")

                    # --- терминальный отсчёт ---
                    # Определяем список порогов и соответствующие типы уведомлений
                    thresholds = [
                        (300, 360, "countdown_5min", "до события '{0}' осталось 5 минут"),
                        (240, 300, "countdown_4min", "до события '{0}' осталось 4 минуты"),
                        (180, 240, "countdown_3min", "до события '{0}' осталось 3 минуты"),
                        (120, 180, "countdown_2min", "до события '{0}' осталось 2 минуты"),
                        (60,  120, "countdown_1min", "до события '{0}' осталось 1 минута"),
                        (30,  60,  "countdown_30sec", "до события '{0}' осталось 30 секунд"),
                        (10,  30,  "countdown_10sec", "до события '{0}' осталось 10 секунд"),
                    ]
                    # Добавляем посекундные пороги для 5..1 секунд
                    for sec in [5,4,3,2,1]:
                        thresholds.append(
                            (sec, sec+1, f"countdown_{sec}sec", f"до события '{{0}}' осталось {sec} секунд")
                        )
                    # Порог "0 секунд" – событие началось
                    thresholds.append(
                        (0, 1, "countdown_start", "❗ Событие '{0}' начинается сейчас!")
                    )

                    for low, high, notif_type, template in thresholds:
                        if low <= delta_seconds < high:
                            if not is_notified(user_id, event_id, notif_type):
                                msg_text = template.format(summary)

                                logger.info(msg_text)

                                if notif_type == "countdown_start":
                                    try:
                                        await bot.send_message(chat_id=user_id, text=f"🔔 {msg_text}")
                                    except Exception as e:
                                        logger.error(f"Ошибка отправки стартового уведомления: {e}")
                                mark_notified(user_id, event_id, notif_type)
                            break

            await asyncio.sleep(1)
        except Exception as e:
            logger.error(f"Ошибка в цикле countdown: {e}")
            await asyncio.sleep(5)

def is_notified(user_id, event_id, notif_type):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT id FROM notified_events WHERE user_id=? AND event_id=? AND notification_type=?",
              (user_id, event_id, notif_type))
    res = c.fetchone()
    conn.close()
    return res is not None

def mark_notified(user_id, event_id, notif_type):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO notified_events (user_id, event_id, notification_type) VALUES (?,?,?)",
              (user_id, event_id, notif_type))
    conn.commit()
    conn.close()

# ---------- ФОНОВАЯ ИСТОРИЯ (как раньше) ----------
async def update_history():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM users")
    users = cursor.fetchall()
    conn.close()
    for user_id, in users:
        token_path = f"tokens/{user_id}_token.pickle"
        if not os.path.exists(token_path):
            continue
        try:
            past_events = get_past_events(user_id, max_results=10)
        except Exception as e:
            logger.error(f"Ошибка получения истории для {user_id}: {e}")
            continue
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        for event in past_events:
            event_id = event['id']
            summary = event.get('summary', 'Без названия')
            start = event['start'].get('dateTime', event['start'].get('date'))
            end = event['end'].get('dateTime', event['end'].get('date'))
            link = event.get('hangoutLink') or event.get('htmlLink')
            cursor.execute(
                "INSERT OR IGNORE INTO event_history (user_id, event_id, summary, start_time, end_time, link) VALUES (?,?,?,?,?,?)",
                (user_id, event_id, summary, start, end, link))
        conn.commit()
        conn.close()

async def periodic_history(interval_minutes: int):
    while True:
        logger.info("📋 Запуск обновления истории встреч.")
        await update_history()
        await asyncio.sleep(interval_minutes * 60)

# ---------- ЗАПУСК ----------
async def main():
    if not TELEGRAM_TOKEN:
        logger.error("TELEGRAM_TOKEN не найден. Укажите его в .env файле.")
        return

    init_db()

    session = AiohttpSession(proxy=PROXY_URL)
    bot = Bot(token=TELEGRAM_TOKEN, session=session)
    dp = Dispatcher()

    dp.message.register(start_command, Command("start"))
    dp.message.register(auth_command, Command("auth"))
    dp.message.register(set_reminder, Command("set_reminder"))
    dp.message.register(history_command, Command("history"))
    dp.message.register(help_command, Command("help"))

    # Запускаем фоновые задачи
    asyncio.create_task(countdown_and_reminders(bot))   
    asyncio.create_task(periodic_history(60))

    logger.info("Бот запущен с системой обратного отсчёта и напоминаниями")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())