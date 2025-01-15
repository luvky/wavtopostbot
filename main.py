import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Updater, CommandHandler, CallbackContext, MessageHandler, Filters, CallbackQueryHandler
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, timedelta
import sqlite3
from config import BOT_TOKEN
from telegram.error import BadRequest, TelegramError
import pytz
import time
import os
import sys

# Настройка логирования
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

file_handler = logging.FileHandler('bot.log')
file_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
logger.addHandler(file_handler)

stream_handler = logging.StreamHandler()
stream_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
logger.addHandler(stream_handler)

# Установка временной зоны по умолчанию
DEFAULT_TIMEZONE = pytz.timezone('Asia/Bishkek')
current_timezone = DEFAULT_TIMEZONE

# Получение текущего времени в установленной временной зоне
def get_current_time():
    return datetime.now(current_timezone)

# Преобразование строки времени в объект datetime с временной зоной
def parse_time(time_str):
    return current_timezone.localize(datetime.strptime(time_str, '%Y-%m-%d %H:%M'))

# Подключение к базе данных с контекстным менеджером
def get_db_connection():
    try:
        conn = sqlite3.connect('reposts.db', check_same_thread=False)
        logger.debug("Успешное подключение к базе данных.")
        return conn
    except sqlite3.Error as e:
        logger.error(f"Ошибка при подключении к базе данных: {e}")
        raise

# Инициализация базы данных
def init_db():
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''CREATE TABLE IF NOT EXISTS reposts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER,
                from_chat_id INTEGER,
                message_id INTEGER,
                publish_time TEXT,
                publish_date TEXT,
                is_published INTEGER DEFAULT 0,
                UNIQUE(chat_id, from_chat_id, message_id, publish_date)
            )''')
            cursor.execute('''CREATE TABLE IF NOT EXISTS settings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER UNIQUE,
                time1 TEXT,
                days_offset INTEGER DEFAULT 10,
                timezone TEXT DEFAULT 'Asia/Bishkek',
                send_mode TEXT DEFAULT 'forward'
            )''')
            cursor.execute('''CREATE TABLE IF NOT EXISTS target_chats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER UNIQUE,
                target_chat_id INTEGER,
                target_chat_username TEXT
            )''')
            conn.commit()
            logger.info("Таблицы 'reposts', 'settings' и 'target_chats' созданы или уже существуют.")

            # Проверка наличия столбца send_mode в таблице settings
            cursor.execute("PRAGMA table_info(settings)")
            columns = cursor.fetchall()
            column_names = [column[1] for column in columns]
            if 'send_mode' not in column_names:
                cursor.execute('ALTER TABLE settings ADD COLUMN send_mode TEXT DEFAULT "forward"')
                conn.commit()
                logger.info("Столбец 'send_mode' добавлен в таблицу 'settings'.")
    except sqlite3.Error as e:
        logger.error(f"Ошибка при инициализации базы данных: {e}")
        raise

# Получение режима отправки
def get_send_mode(chat_id):
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT send_mode FROM settings WHERE chat_id = ?', (chat_id,))
            mode = cursor.fetchone()
            if mode:
                return mode[0]
            return "forward"  # Режим по умолчанию
    except sqlite3.Error as e:
        logger.error(f"Ошибка при получении режима отправки для чата {chat_id}: {e}")
        return "forward"

# Установка режима отправки
def set_send_mode(chat_id, mode):
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''UPDATE settings SET send_mode = ? WHERE chat_id = ?''', (mode, chat_id))
            if cursor.rowcount == 0:
                cursor.execute('''INSERT INTO settings (chat_id, send_mode) VALUES (?, ?)''', (chat_id, mode))
            conn.commit()
            logger.info(f"Режим отправки изменен для чата {chat_id}: {mode}")
    except sqlite3.Error as e:
        logger.error(f"Ошибка при установке режима отправки для чата {chat_id}: {e}")
        raise

# Получение времени публикации и количества дней
def get_publish_settings(chat_id):
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT time1, days_offset, timezone FROM settings WHERE chat_id = ?', (chat_id,))
            settings = cursor.fetchone()
            if settings:
                times_str = settings[0]
                times = times_str.split(", ") if times_str else []
                logger.debug(f"Настройки для чата {chat_id}: времена={times}, дней={settings[1]}, временная зона={settings[2]}")
                return times, settings[1], settings[2]
            logger.warning(f"Настройки для чата {chat_id} не установлены, используются значения по умолчанию.")
            return ["21:35", "21:37"], 10, DEFAULT_TIMEZONE  # Значения по умолчанию
    except sqlite3.Error as e:
        logger.error(f"Ошибка при получении настроек для чата {chat_id}: {e}")
        return None, None, None

# Установка времени публикации
def set_publish_times(chat_id, times_str):
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''INSERT OR REPLACE INTO settings (chat_id, time1) VALUES (?, ?)''', (chat_id, times_str))
            conn.commit()
            logger.info(f"Время публикации установлено для чата {chat_id}: {times_str}")
    except sqlite3.Error as e:
        logger.error(f"Ошибка при установке времени публикации для чата {chat_id}: {e}")
        raise

# Установка количества дней для отложения
def set_days_offset(chat_id, days_offset):
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''UPDATE settings SET days_offset = ? WHERE chat_id = ?''', (days_offset, chat_id))
            if cursor.rowcount == 0:
                cursor.execute('''INSERT INTO settings (chat_id, days_offset) VALUES (?, ?)''', (chat_id, days_offset))
            conn.commit()
            logger.info(f"Количество дней для отложения установлено для чата {chat_id}: {days_offset}")
    except sqlite3.Error as e:
        logger.error(f"Ошибка при установке количества дней для чата {chat_id}: {e}")
        raise

# Установка целевого канала
def set_target_chat(chat_id, target_chat_id, target_chat_username):
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''INSERT OR REPLACE INTO target_chats (chat_id, target_chat_id, target_chat_username) 
                              VALUES (?, ?, ?)''', (chat_id, target_chat_id, target_chat_username))
            conn.commit()
            logger.info(f"Целевой канал установлен для чата {chat_id}: {target_chat_id} ({target_chat_username})")
    except sqlite3.Error as e:
        logger.error(f"Ошибка при установке целевого канала для чата {chat_id}: {e}")
        raise

# Получение целевого канала
def get_target_chat(chat_id):
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT target_chat_id, target_chat_username FROM target_chats WHERE chat_id = ?', (chat_id,))
            target_chat = cursor.fetchone()
            if target_chat:
                logger.debug(f"Целевой канал для чата {chat_id}: {target_chat[0]} ({target_chat[1]})")
                return target_chat[0], target_chat[1]
            logger.warning(f"Целевой канал для чата {chat_id} не установлен.")
            return None, None
    except sqlite3.Error as e:
        logger.error(f"Ошибка при получении целевого канала для чата {chat_id}: {e}")
        return None, None

# Добавление репоста в базу данных
def add_repost_to_db(chat_id, from_chat_id, message_id, times, days_offset):
    try:
        now = get_current_time()
        with get_db_connection() as conn:
            cursor = conn.cursor()
            for day_offset in range(days_offset):
                for time_str in times:
                    publish_date = (now + timedelta(days=day_offset)).strftime('%Y-%m-%d') + f' {time_str}'
                    publish_date = parse_time(publish_date)
                    cursor.execute('''
                        INSERT OR IGNORE INTO reposts (chat_id, from_chat_id, message_id, publish_time, publish_date) 
                        VALUES (?, ?, ?, ?, ?)
                    ''', (chat_id, from_chat_id, message_id, time_str, publish_date.strftime('%Y-%m-%d %H:%M')))
            conn.commit()
            logger.info(f"Репост добавлен в чат {chat_id} из чата {from_chat_id}. "
                        f"ID сообщения: {message_id}. Публикации запланированы на {days_offset} дней.")
            logger.info(f"Время публикации: {times}.")
    except sqlite3.Error as e:
        logger.error(f"Ошибка при добавлении репоста в базу данных: {e}")
        raise

# Публикация репоста
def publish_repost(bot):
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            current_time = get_current_time().strftime('%Y-%m-%d %H:%M')
            logger.info(f"Планировщик запущен. Текущее время: {current_time}")

            cursor.execute('''SELECT reposts.chat_id, reposts.from_chat_id, reposts.message_id, 
                                     reposts.publish_time, reposts.publish_date, target_chats.target_chat_id
                              FROM reposts
                              LEFT JOIN target_chats ON reposts.chat_id = target_chats.chat_id
                              WHERE reposts.is_published = 0 AND reposts.publish_date = ?''', (current_time,))
            reposts = cursor.fetchall()

            if not reposts:
                logger.info("Нет репостов для публикации.")
                return

            for repost in reposts:
                chat_id, from_chat_id, message_id, publish_time, publish_date, target_chat_id = repost
                logger.info(f"Обработка репоста для публикации: {repost}")
                try:
                    if target_chat_id is None:
                        target_chat_id = chat_id

                    try:
                        bot.get_chat(target_chat_id)
                        logger.info(f"Бот имеет доступ к целевому чату: {target_chat_id}.")
                    except BadRequest as e:
                        logger.error(f"Бот не имеет доступа к целевому чату {target_chat_id}: {e}")
                        continue

                    mode = get_send_mode(chat_id)

                    max_attempts = 3
                    for attempt in range(max_attempts):
                        try:
                            if mode == "forward":
                                bot.copy_message(chat_id=target_chat_id, from_chat_id=from_chat_id, message_id=message_id)
                                logger.info(f"Опубликован репост (forward как новое сообщение): {message_id} из чата {from_chat_id} в канал {target_chat_id}.")
                            elif mode == "copy":
                                bot.copy_message(chat_id=target_chat_id, from_chat_id=from_chat_id, message_id=message_id)
                                logger.info(f"Опубликован репост (copy как новое сообщение): {message_id} из чата {from_chat_id} в канал {target_chat_id}.")
                            else:
                                logger.error(f"Неизвестный режим отправки: {mode}")
                                continue

                            cursor.execute('''UPDATE reposts SET is_published = 1 
                                              WHERE chat_id = ? AND from_chat_id = ? AND message_id = ? AND publish_date = ?''', 
                                          (chat_id, from_chat_id, message_id, publish_date))
                            conn.commit()
                            break
                        except BadRequest as e:
                            if "Message to forward not found" in str(e):
                                logger.error(f"Сообщение {message_id} не найдено. Попытка {attempt + 1} из {max_attempts}.")
                                if attempt == max_attempts - 1:
                                    logger.error(f"Сообщение {message_id} не найдено после {max_attempts} попыток. Пропускаем.")
                            elif "Chat not found" in str(e):
                                logger.error(f"Целевой чат {target_chat_id} не найден.")
                                break
                            else:
                                logger.error(f"Ошибка при публикации репоста: {e}")
                        except TelegramError as e:
                            logger.error(f"Ошибка Telegram API при публикации репоста: {e}")
                        except Exception as e:
                            logger.error(f"Ошибка при публикации репоста: {e}")

                        time.sleep(5)

                except Exception as e:
                    logger.error(f"Ошибка при обработке репоста: {e}")
            conn.commit()
    except sqlite3.Error as e:
        logger.error(f"Ошибка базы данных при публикации репостов: {e}")
    except Exception as e:
        logger.error(f"Ошибка при публикации репостов: {e}")

# Удаление репоста из базы данных по номерам
def delete_repost_by_numbers(update: Update, context: CallbackContext):
    try:
        args = context.args
        logger.info(f"Пользователь {update.message.from_user.id} вызвал команду /delete_repost с аргументами: {args}")
        
        if not args:
            update.message.reply_text("Используй команду в формате: /delete_repost <номера через пробел>")
            logger.warning("Не переданы номера для удаления.")
            return

        chat_id = update.message.chat_id
        numbers = [int(num) for num in args if num.isdigit()]  # Преобразуем аргументы в числа

        if not numbers:
            update.message.reply_text("Номера должны быть целыми числами.")
            logger.warning(f"Неверный формат номеров: {args}")
            return

        with get_db_connection() as conn:
            cursor = conn.cursor()
            # Получаем только неопубликованные репосты для данного чата
            cursor.execute('''
                SELECT id, from_chat_id, message_id, publish_date 
                FROM reposts 
                WHERE chat_id = ? AND is_published = 0
                ORDER BY publish_date
            ''', (chat_id,))
            reposts = cursor.fetchall()

            if not reposts:
                update.message.reply_text("Нет неопубликованных репостов для удаления.")
                logger.info(f"Для чата {chat_id} нет неопубликованных репостов.")
                return

            # Создаем список ID репостов
            repost_ids = [repost[0] for repost in reposts]

            # Удаляем репосты по номерам
            deleted_count = 0
            for number in numbers:
                if number < 1 or number > len(repost_ids):
                    update.message.reply_text(f"Номер {number} вне диапазона. Доступные номера: от 1 до {len(repost_ids)}.")
                    logger.warning(f"Номер {number} вне диапазона для чата {chat_id}.")
                    continue

                # Получаем ID репоста по номеру (номера начинаются с 1, поэтому number - 1)
                repost_id = repost_ids[number - 1]
                cursor.execute('DELETE FROM reposts WHERE id = ?', (repost_id,))
                deleted_count += 1
                logger.info(f"Удален репост {repost_id} для чата {chat_id}.")

            conn.commit()

            if deleted_count > 0:
                update.message.reply_text(f"Удалено {deleted_count} неопубликованных репостов.")
                logger.info(f"Удалено {deleted_count} неопубликованных репостов для чата {chat_id}.")
            else:
                update.message.reply_text("Не удалено ни одного неопубликованного репоста.")
                logger.info(f"Не удалено ни одного неопубликованного репоста для чата {chat_id}.")

    except sqlite3.Error as e:
        logger.error(f"Ошибка базы данных при удалении репостов: {e}")
        update.message.reply_text("Произошла ошибка при удалении репостов.")
    except Exception as e:
        logger.error(f"Ошибка при выполнении команды /delete_repost: {e}")
        update.message.reply_text("Произошла ошибка при удалении репостов.")

def start(update: Update, context: CallbackContext):
    try:
        user = update.message.from_user
        chat_id = update.message.chat_id
        logger.info(f"Пользователь {user.id} ({user.username}) вызвал команду /start в чате {chat_id}.")

        keyboard = [
            [InlineKeyboardButton("ℹ️ Инфо", callback_data='info'),
             InlineKeyboardButton("🕒 Время постов", callback_data='get_time')],  #Время постов
            [InlineKeyboardButton("🔄 Перезапуск бота", callback_data='restart')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        # Текст с командами, использующими нижнее подчеркивание
        update.message.reply_text(
            "🌟 Привет! Я бот для отложенной публикации репостов.\n\n"
            "📅 Как я могу помочь?\n"
            "Я могу автоматически публиковать пересланные сообщения в указанное время на несколько дней вперед.\n\n"
            "🔧 Доступные команды:\n"
            "🛠 /start - показать это сообщение\n"
            "⏰ /set_time <время1> <время2> ... - установить время публикации (например, /set_time 10:00 14:00 18:00)\n"
            "🕒 /get_time - узнать текущее время публикации\n"
            "📆 /day <количество_дней> - установить количество дней для отложения (например, /day 7)\n"
            "📌 /set_target <ID_канала или username> - указать целевой канал для репостов\n"
            "ℹ️ /info - узнать текущие настройки\n"
            "📋 /list - посмотреть все запланированные репосты (например, /list все репосты, а /list 10 для вывода 10 репостов и т.п.)\n"
            "🗑 /delete_repost <номера через пробел> - удалить репосты по номерам из списка\n"
            "🧹 /clear_sent - удалить все отправленные репосты\n"
            "🚮 /clear_all - удалить все репосты (отправленные и запланированные)\n"
            "🌍 /set_timezone <временная зона> - установить временную зону (например, /set_timezone Asia/Bishkek)\n"
            "📤 /set_mode <forward/copy> - установить режим отправки (репост или копирование)\n"
            "🔄 /restart - перезапустить бота\n\n"
            "📤 Как начать?\n"
            "Просто перешли мне сообщение, и я буду публиковать его каждый день в указанное время.\n\n"
            "🚀 Готов к работе!",
            reply_markup=reply_markup
        )
    except Exception as e:
        logger.error(f"Ошибка при выполнении команды /start: {e}")
        update.message.reply_text("Произошла ошибка при выполнении команды /start.")

# Обработчик inline-кнопок
def button_handler(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()

    if query.data == 'set_time':
        query.edit_message_text(text="🕒 Введите новое время в формате /set_time 10:00 14:00")
    elif query.data == 'set_target':
        query.edit_message_text(text="📌 Введите ID или username целевого канала в формате /set_target @my_channel")
    elif query.data == 'set_timezone':
        query.edit_message_text(text="🌍 Введите временную зону в формате /set_timezone Asia/Bishkek")
    elif query.data == 'set_mode':
        query.edit_message_text(text="📤 Выберите режим отправки: /set_mode forward или /set_mode copy")
    elif query.data == 'info':
        # Вызов функции info
        info(update, context)
    elif query.data == 'get_time':  # Обработка новой кнопки "Время постов"
        # Вызов функции get_time
        get_time(update, context)
    elif query.data == 'restart':
        # Вызов функции restart
        restart(update, context)
    else:
        query.edit_message_text(text="Неизвестная команда.")

# Команда /restart - перезапуск бота
def restart(update: Update, context: CallbackContext):
    try:
        if update.callback_query:
            query = update.callback_query
            query.answer()
            query.edit_message_text(text="Перезапустил бота.")  # Обновляем текст сообщения
        else:
            update.message.reply_text("Бот успешно перезапущен!")

        logger.info("Перезапуск бота...")

        # Перезапуск бота
        os.execl(sys.executable, sys.executable, *sys.argv)

        # Если перезапуск успешен, этот код не будет выполнен
        logger.info("Бот успешно перезапущен.")

        # Обновляем сообщение о успешном перезапуске
        if update.callback_query:
            query.edit_message_text(text="✅ Бот успешно перезапущен.")
        else:
            update.message.reply_text("✅ Бот успешно перезапущен.")

    except Exception as e:
        logger.error(f"Ошибка при перезапуске бота: {e}")
        if update.callback_query:
            query.edit_message_text(text="⛔ Ошибка перезапуска бота.")
        else:
            update.message.reply_text("⛔ Ошибка перезапуска бота.")

# Команда /set_time - устанавливает новое время публикации
def set_time(update: Update, context: CallbackContext):
    try:
        args = context.args
        logger.info(f"Пользователь {update.message.from_user.id} вызвал команду /set_time с аргументами: {args}")
        
        if not args:
            update.message.reply_text("Используй команду в формате: /set_time <время1> <время2> ...")
            logger.warning("Не переданы времена для установки.")
            return

        invalid_times = [time_str for time_str in args if not is_valid_time(time_str)]
        if invalid_times:
            update.message.reply_text(f"Неверный формат времени: {', '.join(invalid_times)}. Используйте формат HH:MM.")
            logger.warning(f"Неверный формат времени: {invalid_times}")
            return

        chat_id = update.message.chat_id
        times_str = ", ".join(args)
        set_publish_times(chat_id, times_str)
        update.message.reply_text(f"Время публикации изменено: {times_str}.")
        logger.info(f"Время публикации изменено для чата {chat_id}: {times_str}.")
    except sqlite3.Error as e:
        logger.error(f"Ошибка базы данных при установке времени публикации: {e}")
        update.message.reply_text("Произошла ошибка при изменении времени. Пожалуйста, попробуйте позже.")
    except Exception as e:
        logger.error(f"Ошибка при выполнении команды /set_time: {e}")
        update.message.reply_text("Произошла непредвиденная ошибка. Пожалуйста, попробуйте позже.")

# Команда /get_time - показывает текущее установленное время публикации
def get_time(update: Update, context: CallbackContext):
    try:
        if update.callback_query:
            query = update.callback_query
            chat_id = query.message.chat_id
            query.answer()
            message = query.message
        else:
            chat_id = update.message.chat_id
            message = update.message

        times, _, _ = get_publish_settings(chat_id)
        if not times:
            message.reply_text("Настройки времени публикации не установлены.")
            return
        message.reply_text(f"Текущее время публикации: {', '.join(times)}.")
        logger.info(f"Пользователь запросил текущее время публикации для чата {chat_id}.")
    except Exception as e:
        logger.error(f"Ошибка при получении времени публикации: {e}")
        if update.callback_query:
            update.callback_query.message.reply_text("Произошла ошибка при получении времени публикации.")
        else:
            update.message.reply_text("Произошла ошибка при получении времени публикации.")

# Команда /day - устанавливает количество дней для отложения
def set_days(update: Update, context: CallbackContext):
    try:
        args = context.args
        logger.info(f"Пользователь {update.message.from_user.id} вызвал команду /day с аргументами: {args}")
        if len(args) != 1:
            update.message.reply_text("Используй команду в формате: /day <количество_дней>")
            logger.warning(f"Неверное количество аргументов в команде /day: {args}")
            return

        days_offset = int(args[0])
        if days_offset <= 0:
            update.message.reply_text("Количество дней должно быть положительным числом.")
            logger.warning(f"Неверное значение количества дней в команде /day: {days_offset}")
            return

        chat_id = update.message.chat_id
        set_days_offset(chat_id, days_offset)
        update.message.reply_text(f"Количество дней для отложения изменено: {days_offset}.")
        logger.info(f"Количество дней для отложения изменено для чата {chat_id}: {days_offset}.")
    except ValueError:
        update.message.reply_text("Количество дней должно быть числом.")
        logger.warning(f"Неверный формат количества дней в команде /day: {args}")
    except Exception as e:
        logger.error(f"Ошибка при выполнении команды /day: {e}")
        update.message.reply_text("Произошла ошибка при изменении количества дней.")

# Команда /set_target - устанавливает целевой канал
def set_target(update: Update, context: CallbackContext):
    try:
        args = context.args
        logger.info(f"Пользователь {update.message.from_user.id} вызвал команду /set_target с аргументами: {args}")
        if len(args) != 1:
            update.message.reply_text("Используй команду в формате: /set_target <ID_канала или username>")
            logger.warning(f"Неверное количество аргументов в команде /set_target: {args}")
            return

        target_chat = args[0]
        chat_id = update.message.chat_id
        bot = context.bot

        if target_chat.startswith("@"):
            try:
                chat = bot.get_chat(target_chat)
                target_chat_id = chat.id
                target_chat_username = target_chat
            except BadRequest as e:
                update.message.reply_text(f"Не удалось найти канал {target_chat}. Убедитесь, что бот добавлен в канал.")
                logger.error(f"Ошибка при получении информации о канале {target_chat}: {e}")
                return
        else:
            try:
                target_chat_id = int(target_chat)
                target_chat_username = None
            except ValueError:
                update.message.reply_text("ID канала должен быть числом или начинаться с @.")
                logger.warning(f"Неверный формат ID канала: {target_chat}")
                return

        set_target_chat(chat_id, target_chat_id, target_chat_username)
        update.message.reply_text(f"Целевой канал установлен: {target_chat}.")
        logger.info(f"Целевой канал установлен для чата {chat_id}: {target_chat_id} ({target_chat_username}).")
    except Exception as e:
        logger.error(f"Ошибка при выполнении команды /set_target: {e}")
        update.message.reply_text("Произошла ошибка при установке целевого канала.")

# Команда /info - показывает текущие настройки
def info(update: Update, context: CallbackContext):
    try:
        if update.callback_query:
            query = update.callback_query
            chat_id = query.message.chat_id
            query.answer()
            message = query.message
        else:
            chat_id = update.message.chat_id
            message = update.message

        # Получаем текущее время и часовой пояс
        current_time = get_current_time().strftime('%H:%M')  # Текущее время в формате HH:MM
        current_timezone = get_publish_settings(chat_id)[2]  # Получаем текущий часовой пояс

        # Получаем настройки
        times, days_offset, timezone = get_publish_settings(chat_id)
        target_chat_id, target_chat_username = get_target_chat(chat_id)
        send_mode = get_send_mode(chat_id)

        # Получаем название канала, если возможно
        target_chat_name = None
        if target_chat_id:
            try:
                chat_info = context.bot.get_chat(target_chat_id)
                target_chat_name = chat_info.title  # Название канала
            except (BadRequest, TelegramError) as e:
                logger.warning(f"Не удалось получить информацию о канале {target_chat_id}: {e}")

        # Формируем строку с целевым каналом
        target_chat_info = f"{target_chat_id}"  # ID канала
        if target_chat_name:
            target_chat_info += f" ({target_chat_name})"  # Добавляем название канала, если доступно
        elif target_chat_username:
            target_chat_info += f" (@{target_chat_username})"  # Добавляем username, если доступно

        # Формируем сообщение с текущим временем и часовым поясом
        response = (
            f"📋 *Текущие настройки* (🕒 Текущее время: {current_time}, 🌍 Часовой пояс: {current_timezone}):\n\n"
            f"🕒 *Время публикации:* {', '.join(times) if times else 'не установлено'}\n"
            f"📅 *Количество дней:* {days_offset}\n"
            f"📌 *Целевой канал:* {target_chat_info if target_chat_id else 'не установлен'}\n"
            f"🌍 *Временная зона:* {timezone}\n"
            f"📤 *Режим отправки:* {send_mode}\n"
        )

        # Получаем ближайшие репосты
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT publish_date 
                FROM reposts 
                WHERE chat_id = ? AND is_published = 0
                ORDER BY publish_date
                LIMIT 3
            ''', (chat_id,))
            upcoming_reposts = cursor.fetchall()

        if upcoming_reposts:
            response += "\n📅 *Ближайшие репосты:*\n"
            now = get_current_time()  # Текущее время
            for repost in upcoming_reposts:
                publish_date_str = repost[0]
                publish_date = parse_time(publish_date_str)  # Преобразуем строку в datetime

                # Рассчитываем разницу во времени
                time_diff = (publish_date - now).total_seconds()  # Разница в секундах
                hours = int(time_diff // 3600)  # Часы
                minutes = int((time_diff % 3600) // 60)  # Минуты

                # Форматируем строку с временем до публикации
                time_left = f" (через {hours}ч{minutes}м)"
                response += f"- {publish_date_str}{time_left}\n"
        else:
            response += "\n📅 *Нет запланированных репостов.*\n"

        # Создаем клавиатуру
        keyboard = [
            [InlineKeyboardButton("🕒 Изменить время", callback_data='set_time')],
            [InlineKeyboardButton("📌 Изменить целевой канал", callback_data='set_target')],
            [InlineKeyboardButton("🌍 Изменить временную зону", callback_data='set_timezone')],
            [InlineKeyboardButton("📤 Изменить режим отправки", callback_data='set_mode')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        # Отправляем сообщение
        message.reply_text(
            response,
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )
        logger.info(f"Пользователь запросил информацию о настройках для чата {chat_id}.")
    except Exception as e:
        logger.error(f"Ошибка при выполнении команды /info: {e}")
        if update.callback_query:
            update.callback_query.message.reply_text("Произошла ошибка при получении информации о настройках.")
        else:
            update.message.reply_text("Произошла ошибка при получении информации о настройках.")

# Команда /clear_all - удаляет все репосты (отправленные и запланированные)
def clear_all_reposts(update: Update, context: CallbackContext):
    try:
        chat_id = update.message.chat_id
        logger.info(f"Пользователь {update.message.from_user.id} вызвал команду /clear_all для чата {chat_id}.")
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('DELETE FROM reposts WHERE chat_id = ?', (chat_id,))
            conn.commit()
            logger.info(f"Удалены все репосты для чата {chat_id}. Удалено {cursor.rowcount} записей.")
            update.message.reply_text("Все репосты (отправленные и запланированные) удалены.")
    except sqlite3.Error as e:
        logger.error(f"Ошибка при удалении всех репостов: {e}")
        update.message.reply_text("Произошла ошибка при удалении всех репостов.")
    except Exception as e:
        logger.error(f"Ошибка при выполнении команды /clear_all: {e}")
        update.message.reply_text("Произошла ошибка при удалении всех репостов.")

def list_scheduled_posts(update: Update, context: CallbackContext):
    try:
        chat_id = update.message.chat_id
        args = context.args

        # Определяем количество репостов для вывода
        limit = None  # По умолчанию выводятся все репосты
        if args and args[0].isdigit():
            limit = int(args[0])

        with get_db_connection() as conn:
            cursor = conn.cursor()
            # Получаем репосты для данного чата
            query = '''
                SELECT id, from_chat_id, message_id, publish_date, is_published 
                FROM reposts 
                WHERE chat_id = ?
                ORDER BY publish_date
            '''
            if limit is not None:
                query += f' LIMIT {limit}'  # Добавляем ограничение, если указано

            cursor.execute(query, (chat_id,))
            posts = cursor.fetchall()

            if not posts:
                update.message.reply_text("Нет репостов.")
                logger.info(f"Для чата {chat_id} нет репостов.")
                return

            # Получаем информацию о целевом канале
            target_chat_id, target_chat_username = get_target_chat(chat_id)
            target_chat_name = None
            if target_chat_id:
                try:
                    chat_info = context.bot.get_chat(target_chat_id)
                    target_chat_name = chat_info.title  # Название канала
                except (BadRequest, TelegramError) as e:
                    logger.warning(f"Не удалось получить информацию о канале {target_chat_id}: {e}")

            # Формируем строку с целевым каналом
            target_chat_info = f"{target_chat_id}"  # ID канала
            if target_chat_name:
                target_chat_info += f" ({target_chat_name})"  # Добавляем название канала, если доступно
            elif target_chat_username:
                target_chat_info += f" (@{target_chat_username})"  # Добавляем username, если доступно

            # Разделяем репосты на запланированные и опубликованные
            scheduled_posts = []
            published_posts = []
            now = get_current_time()

            for post in posts:
                repost_id, from_chat_id, message_id, publish_date, is_published = post
                publish_date_obj = parse_time(publish_date)  # Преобразуем строку в datetime

                if is_published:
                    published_posts.append((repost_id, from_chat_id, message_id, publish_date_obj))
                else:
                    scheduled_posts.append((repost_id, from_chat_id, message_id, publish_date_obj))

            # Формируем таблицу с репостами
            table = "📅 *Запланированные и опубликованные репосты:*\n\n"
            table += f"📌 *Целевой канал:* {target_chat_info if target_chat_id else 'не установлен'}\n\n"

            # Секция "Запланированные"
            if scheduled_posts:
                table += "📅 *Запланированные репосты:*\n"
                table += "№ | ID сообщения | Дата публикации | Статус\n"
                table += "-" * 50 + "\n"
                for index, post in enumerate(scheduled_posts, start=1):
                    repost_id, from_chat_id, message_id, publish_date = post
                    time_diff = (publish_date - now).total_seconds()  # Разница в секундах

                    # Определяем статус
                    if time_diff <= 86400:  # 24 часа в секундах
                        # Преобразуем разницу в часы и минуты
                        hours = int(time_diff // 3600)
                        minutes = int((time_diff % 3600) // 60)
                        status = f"🟢 Скоро (через {hours}ч{minutes}м)"
                    else:
                        status = "🟡 Ожидает"

                    table += f"{index} | {message_id} | *{publish_date.strftime('%Y-%m-%d %H:%M')}* | {status}\n"
                table += "\n"

            # Секция "Опубликованные"
            if published_posts:
                table += "✅ *Опубликованные репосты:*\n"
                table += "ID сообщения | Дата публикации | Статус\n"  # Добавляем колонку "Статус"
                table += "-" * 50 + "\n"
                for post in published_posts:
                    repost_id, from_chat_id, message_id, publish_date = post
                    table += f"{message_id} | *{publish_date.strftime('%Y-%m-%d %H:%M')}* | 🔵 Опубликован\n"  # Добавляем статус
                table += "\n"

            # Если нет репостов
            if not scheduled_posts and not published_posts:
                table += "📭 Нет запланированных или опубликованных репостов.\n"

            # Отправляем таблицу частями, если она слишком длинная
            max_length = 4096  # Максимальная длина сообщения в Telegram
            for i in range(0, len(table), max_length):
                update.message.reply_text(table[i:i + max_length], parse_mode="Markdown")

            logger.info(f"Пользователь запросил список репостов для чата {chat_id}.")
    except sqlite3.Error as e:
        logger.error(f"Ошибка базы данных при выполнении команды /list: {e}")
        update.message.reply_text("Произошла ошибка при подключении к базе данных.")
    except Exception as e:
        logger.error(f"Ошибка при выполнении команды /list: {e}")
        update.message.reply_text("Произошла ошибка при получении списка репостов.")
        
# Команда /set_timezone - устанавливает временную зону
def set_timezone(update: Update, context: CallbackContext):
    try:
        args = context.args
        logger.info(f"Пользователь {update.message.from_user.id} вызвал команду /set_timezone с аргументами: {args}")
        if len(args) != 1:
            update.message.reply_text("Используй команду в формате: /set_timezone <временная зона>")
            logger.warning(f"Неверное количество аргументов в команде /set_timezone: {args}")
            return

        timezone = args[0]
        try:
            pytz.timezone(timezone)
        except pytz.UnknownTimeZoneError:
            update.message.reply_text("Неверная временная зона. Пример: /set_timezone Asia/Bishkek")
            logger.warning(f"Неверная временная зона: {timezone}")
            return

        chat_id = update.message.chat_id
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''UPDATE settings SET timezone = ? WHERE chat_id = ?''', (timezone, chat_id))
            if cursor.rowcount == 0:
                cursor.execute('''INSERT INTO settings (chat_id, timezone) VALUES (?, ?)''', (chat_id, timezone))
            conn.commit()
        global current_timezone
        current_timezone = pytz.timezone(timezone)
        update.message.reply_text(f"Временная зона изменена: {timezone}.")
        logger.info(f"Временная зона изменена для чата {chat_id}: {timezone}.")
    except sqlite3.Error as e:
        logger.error(f"Ошибка базы данных при установке временной зоны: {e}")
        update.message.reply_text("Произошла ошибка при изменении временной зоны.")
    except Exception as e:
        logger.error(f"Ошибка при выполнении команды /set_timezone: {e}")
        update.message.reply_text("Произошла ошибка при изменении временной зоны.")

# Команда /set_mode - переключение режима отправки
def set_mode(update: Update, context: CallbackContext):
    try:
        args = context.args
        logger.info(f"Пользователь {update.message.from_user.id} вызвал команду /set_mode с аргументами: {args}")
        if len(args) != 1 or args[0].lower() not in ["forward", "copy"]:
            update.message.reply_text("Используй команду в формате: /set_mode <forward/copy>")
            logger.warning(f"Неверные аргументы в команде /set_mode: {args}")
            return

        mode = args[0].lower()
        chat_id = update.message.chat_id
        set_send_mode(chat_id, mode)
        update.message.reply_text(f"Режим отправки изменен: {mode}.")
        logger.info(f"Режим отправки изменен для чата {chat_id}: {mode}")
    except Exception as e:
        logger.error(f"Ошибка при выполнении команды /set_mode: {e}")
        update.message.reply_text("Произошла ошибка при изменении режима отправки.")

# Проверка корректности времени
def is_valid_time(time_str):
    try:
        datetime.strptime(time_str, '%H:%M')
        return True
    except ValueError:
        return False

def get_active_chats():
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT DISTINCT chat_id FROM reposts')
            chat_ids = [row[0] for row in cursor.fetchall()]
            return chat_ids
    except sqlite3.Error as e:
        logger.error(f"Ошибка при получении активных чатов: {e}")
        return []

# Обработчик пересланных сообщений
def handle_forwarded_message(update: Update, context: CallbackContext):
    try:
        if update.message.forward_from_chat:
            from_chat_id = update.message.forward_from_chat.id
            message_id = update.message.forward_from_message_id
            chat_id = update.message.chat_id
            times, days_offset, _ = get_publish_settings(chat_id)

            logger.info(f"Пользователь {update.message.from_user.id} переслал сообщение {message_id} из чата {from_chat_id}.")

            if not times or days_offset is None:
                update.message.reply_text(
                    "Настройки времени публикации или количества дней не установлены. "
                    "Используйте команды /set_time и /day для настройки."
                )
                logger.warning(f"Настройки времени публикации или количества дней не установлены для чата {chat_id}.")
                return

            add_repost_to_db(chat_id, from_chat_id, message_id, times, days_offset)
            update.message.reply_text(f"Сообщение добавлено в расписание для публикации в {', '.join(times)} на {days_offset} дней.")
            logger.info(f"Сообщение {message_id} из чата {from_chat_id} добавлено в расписание для чата {chat_id}.")
        else:
            update.message.reply_text("Перешлите сообщение из другого чата.")
            logger.warning(f"Пользователь {update.message.from_user.id} не переслал сообщение из другого чата.")
    except sqlite3.Error as e:
        logger.error(f"Ошибка базы данных при обработке пересланного сообщения: {e}")
        update.message.reply_text("Произошла ошибка при обработке сообщения.")
    except Exception as e:
        logger.error(f"Ошибка при обработке пересланного сообщения: {e}")
        update.message.reply_text("Произошла ошибка при обработке сообщения.")

# Запуск бота с планировщиком
# Определение функции clear_sent_reposts
def clear_sent_reposts(update: Update, context: CallbackContext):
    try:
        chat_id = update.message.chat_id
        logger.info(f"Пользователь {update.message.from_user.id} вызвал команду /clear_sent для чата {chat_id}.")
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('DELETE FROM reposts WHERE chat_id = ? AND is_published = 1', (chat_id,))
            conn.commit()
            logger.info(f"Удалены отправленные репосты для чата {chat_id}. Удалено {cursor.rowcount} записей.")
            update.message.reply_text("Все отправленные репосты удалены.")
    except sqlite3.Error as e:
        logger.error(f"Ошибка при удалении отправленных репостов: {e}")
        update.message.reply_text("Произошла ошибка при удалении отправленных репостов.")
    except Exception as e:
        logger.error(f"Ошибка при выполнении команды /clear_sent: {e}")
        update.message.reply_text("Произошла ошибка при удалении отправленных репостов.")

# Регистрация обработчиков команд
def run_bot():
    try:
        updater = Updater(BOT_TOKEN)
        dispatcher = updater.dispatcher

        # Инициализация базы данных
        init_db()

        # Регистрация обработчиков команд
        dispatcher.add_handler(CommandHandler("start", start))
        dispatcher.add_handler(CommandHandler("set_time", set_time))
        dispatcher.add_handler(CommandHandler("get_time", get_time))
        dispatcher.add_handler(CommandHandler("day", set_days))
        dispatcher.add_handler(CommandHandler("set_target", set_target))
        dispatcher.add_handler(CommandHandler("info", info))
        dispatcher.add_handler(CommandHandler("list", list_scheduled_posts))
        dispatcher.add_handler(CommandHandler("delete_repost", delete_repost_by_numbers))
        dispatcher.add_handler(CommandHandler("clear_sent", clear_sent_reposts))  # Регистрация команды /clear_sent
        dispatcher.add_handler(CommandHandler("clear_all", clear_all_reposts))
        dispatcher.add_handler(CommandHandler("set_timezone", set_timezone))
        dispatcher.add_handler(CommandHandler("set_mode", set_mode))
        dispatcher.add_handler(CommandHandler("restart", restart))
        dispatcher.add_handler(CallbackQueryHandler(button_handler))
        dispatcher.add_handler(MessageHandler(Filters.forwarded, handle_forwarded_message))

        # Запуск планировщика
        scheduler = BackgroundScheduler(timezone=current_timezone)
        scheduler.add_job(publish_repost, 'interval', minutes=1, args=[updater.bot])
        scheduler.start()
        logger.info("Планировщик запущен.")

        # Запуск бота
        updater.start_polling()
        logger.info("Бот запущен и готов к работе!")
        updater.idle()
        logger.info("Бот завершил работу.")
    except Exception as e:
        logger.error(f"Ошибка при запуске бота: {e}")

if __name__ == '__main__':
    try:
        run_bot()
    except Exception as e:
        logger.error(f"Ошибка при запуске бота: {e}")
    else:
        logger.info("Бот успешно перезапущен.")