import asyncio
import logging
import sqlite3
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, CallbackQuery
from aiogram.enums import ParseMode
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiogram.enums import ParseMode
from aiohttp import web
import asyncpg

# --- НАСТРОЙКИ TELEGRAM БОТА ---
BOT_TOKEN = "8653801306:AAFfKR9d9D8bLYEArAHxov40_bi4b-N9BOM"
CHANNEL_ID = -1004330638807  # ID канала с -100
ADMIN_ID = 987506862         # Ваш личный Telegram ID старосты
PINNED_MESSAGE_ID = 3        # ID закрепленного сообщения

# --- НАСТРОЙКИ ОБЛАЧНОЙ БАЗЫ SUPABASE ---
DB_USER = "postgres"
DB_PASSWORD = "[/-s56B3sbWw+L&L]"  # Ваш пароль со скобками
DB_HOST = "db.tqpaoezbovvanysghfvl.supabase.co"  # Официальный пулер Supabase
DB_PORT = 5432  # Прямой порт PostgreSQL
DB_NAME = "postgres"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()
scheduler = AsyncIOScheduler() 
db_pool = None  # <-- ДОБАВЬТЕ ЭТУ СТРОКУ!


# --- ГЛАВНАЯ КЛАВИАТУРА СТАРОСТЫ ---
admin_main_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text='➕ Добавить дедлайн')],
        [KeyboardButton(text='🗂 Управление'), KeyboardButton(text='📢 Важное объявление')]
    ],
    resize_keyboard=True  # Делает кнопки аккуратными и маленькими
)

# --- КЛАВИАТУРА ВЫБОРА ПРЕДМЕТОВ ---
subjects_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text='Проектный семинар "Биоинформатика в агробиотехнологиях"')],
        [KeyboardButton(text='Биостатистика')],
        [KeyboardButton(text='Молекулярная эволюция')],
        [KeyboardButton(text='Генетические основы селекционного процесса в растениеводстве и животноводстве')]
    ],
    resize_keyboard=True,
    one_time_keyboard=True
)
# --- КЛАВИАТУРА ДЛЯ ВАЖНЫХ УВЕДОМЛЕНИЙ ---
pin_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text='📌 Да, закрепить в канале')],
        [KeyboardButton(text='❌ Нет, просто опубликовать')]
    ],
    resize_keyboard=True,
    one_time_keyboard=True
)




# --- СОСТОЯНИЯ ДЛЯ ПОШАГОВОГО ОПРОСА ---
class Form(StatesGroup):
    subject = State()
    description = State()
    deadline = State()
    select_email = State()  # Для выбора между m_selin и olesyuk
    file = State()

class EditForm(StatesGroup):    # <-- Вот этот класс обязательно должен быть здесь!
    task_id = State()
    choice = State()
    new_value = State()

class NoticeForm(StatesGroup):
    text = State()
    pin = State()

# --- ФУНКЦИЯ ОБНОВЛЕНИЯ ЗАКРЕПЛЕННОГО ПОСТА (С СОРТИРОВКОЙ ПО ПРЕДМЕТАМ ПОД SUPABASE) ---
async def update_pinned_post():
    # Открываем асинхронное подключение из пула Supabase
    async with db_pool.acquire() as conn:
        # Сначала берем список всех уникальных предметов, которые есть в базе
        unique_subjects_rows = await conn.fetch("SELECT DISTINCT subject FROM tasks")
        unique_subjects = [row['subject'] for row in unique_subjects_rows]
        
        text = "📌 <b>Актуальные дедлайны</b> 📌\n\n"
        
        if not unique_subjects:
            text += "Ура! Активных заданий нет 🎉"
        else:
            now = datetime.now()
            
            # Перебираем каждый предмет отдельно
            for subj_name in unique_subjects:
                # Определяем глобальный хештег для заголовка предмета
                if "Биостатистика" in subj_name: hashtag = "#биостатистика"
                elif "Молекулярная эволюция" in subj_name: hashtag = "#молекулярная_эволюция"
                elif "Генетические основы" in subj_name: hashtag = "#селекция"
                else: hashtag = "#биоинформатика"
                
                # Добавляем красивую шапку предмета
                text += f"📘 <b>{clean_html(subj_name)}</b> {hashtag}\n"
                text += f"‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾\n"
                
                # Вытаскиваем задания по этому предмету, сортируя их по дедлайну
                subj_tasks = await conn.fetch("SELECT description, deadline, submit_url FROM tasks WHERE subject = $1 ORDER BY deadline ASC", subj_name)
                
                for task in subj_tasks:
                    try:
                        desc_raw = task['description']
                        dead_raw = task['deadline']
                        url_raw = task['submit_url']
                        
                        task_deadline = datetime.strptime(dead_raw, "%Y-%m-%d %H:%M")
                        dt = task_deadline.strftime("%d.%m.%Y %H:%M")
                        is_expired = task_deadline < now
                        
                        desc = clean_html(desc_raw)
                        url = url_raw
                        
                        if is_expired:
                            # Если дедлайн прошел — зачеркиваем и дописываем статус
                            if str(url).startswith("http"):
                                text += f"❌ <s>• {desc} (до {dt})</s> <i>(дедлайн прошел)</i> — <s><a href='{url}'>Ссылка</a></s>\n"
                            else:
                                text += f"❌ <s>• {desc} (до {dt})</s> <i>(дедлайн прошел)</i> — <s>{clean_html(url)}</s>\n"
                        else:
                            # Если актуально — выводим красиво
                            if str(url).startswith("http"):
                                text += f"🔸 • {desc} (до <code>{dt}</code>) — <a href='{url}'>Ссылка</a>\n"
                            else:
                                text += f"🔸 • {desc} (до <code>{dt}</code>) — {clean_html(url)}\n"
                    except Exception as e:
                        logging.error(f"Ошибка парсинга строки таски в закрепе: {e}")
                        
                text += "\n" # Отступ между блоками разных предметов
                
        try:
            await bot.edit_message_text(text=text, chat_id=CHANNEL_ID, message_id=PINNED_MESSAGE_ID, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        except Exception as e:
            if "message is not modified" in str(e):
                logging.info("Закрепленный пост проверен: изменений нет.")
            else:
                logging.error(f"Ошибка обновления закрепа: {e}")


# Вспомогательная функция для безопасного текста в HTML
def clean_html(text):
    return str(text).replace("<", "&lt;").replace(">", "&gt;")
# --- АВТОМАТИЧЕСКОЕ УДАЛЕНИЕ ПРОСРОЧЕННЫХ ЗАДАНИЙ ЧЕРЕЗ 2 НЕДЕЛИ ---
async def clear_old_deadlines():
    # Находим задачи, у которых дедлайн наступил более 14 дней назад
    two_weeks_ago = (datetime.now() - timedelta(days=14)).strftime("%Y-%m-%d %H:%M")
    
    cursor.execute("SELECT id, message_id FROM tasks WHERE deadline < ?", (two_weeks_ago,))
    old_tasks = cursor.fetchall()
    
    for task in old_tasks:
        task_id, msg_id = task
        # Удаляем оригинальный пост из ленты канала
        if msg_id and msg_id != 0:
            try:
                await bot.delete_message(chat_id=CHANNEL_ID, message_id=msg_id)
            except Exception as e:
                logging.error(f"Не удалось автоматически удалить старый пост {msg_id}: {e}")
                
        # Стираем запись из локальной базы данных
        cursor.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        
    if old_tasks:
        conn.commit()
        logging.info(f"Автоочистка: удалено просроченных заданий: {len(old_tasks)}")
        await update_pinned_post()
# --- ДИАЛОГ ДЛЯ ВАЖНЫХ ОБЪЯВЛЕНИЙ ---
@router.message(Command("alert"), F.from_user.id == ADMIN_ID)
@router.message(F.text == '📢 Важное объявление', F.from_user.id == ADMIN_ID)
async def start_notice(message: Message, state: FSMContext):
    await message.answer("📢 Введи текст важного объявления (можно использовать абзацы):")
    await state.set_state(NoticeForm.text)

@router.message(NoticeForm.text)
async def process_notice_text(message: Message, state: FSMContext):
    await state.update_data(text=message.text)
    await message.answer("Хочешь закрепить это сообщение в ленте канала?", reply_markup=pin_keyboard)
    await state.set_state(NoticeForm.pin)

@router.message(NoticeForm.pin)
async def process_notice_pin(message: Message, state: FSMContext):
    user_data = await state.get_data()
    notice_text = clean_html(user_data['text'])
    
    # Красивый шаблон для важного объявления
    full_text = (
        f"🚨 <b>Важное объявление</b> #инфо\n"
        f"\n"
        f"{notice_text}\n"
        f"\n"
        f"Просьба ознакомиться!"
    )
    
    try:
        # Публикуем в канал
        msg = await bot.send_message(chat_id=CHANNEL_ID, text=full_text, parse_mode=ParseMode.HTML)
        
        # Если староста выбрал "Закрепить"
        if "Да, закрепить" in message.text:
            await bot.pin_chat_message(chat_id=CHANNEL_ID, message_id=msg.message_id)
            await message.answer("📢 Объявление опубликовано и закреплено!", reply_markup=ReplyKeyboardRemove())
        else:
            await message.answer("📢 Объявление успешно опубликовано!", reply_markup=ReplyKeyboardRemove())
            
    except Exception as e:
        logging.error(f"Ошибка отправки объявления: {e}")
        await message.answer("❌ Произошла ошибка при отправке сообщения в канал.", reply_markup=ReplyKeyboardRemove())
        
    await state.clear()
#пароль 3-430dsQ

# Специальное обновление закрепа, отображающее перечеркнутые правки
async def update_pinned_post_with_change(changed_id, field, old_desc, old_dead, old_url):
    cursor.execute("SELECT id, subject, description, deadline, submit_url FROM tasks ORDER BY deadline ASC")
    all_tasks = cursor.fetchall()
    
    text = "📌 <b>Актуальные дедлайны)</b> 📌\n\n"
    if not all_tasks:
        text += "Ура! Активных заданий нет 🎉"
    else:
        now = datetime.now()
        for task in all_tasks:
            try:
                t_id = task['id']
                subj_raw = task['subject']
                desc_raw = task['description']
                dead_raw = task['deadline']
                url_raw = task['submit_url']
                task_deadline = datetime.strptime(dead_raw, "%Y-%m-%d %H:%M")
                dt = task_deadline.strftime("%d.%m.%Y %H:%M")
                is_expired = task_deadline < now
                
                subj = clean_html(subj_raw)
                desc = clean_html(desc_raw)
                url = url_raw
                
                # Если это именно та задача, которую только что изменили
                if t_id == changed_id:
                    text += f"🔄 <b>Предмет:</b> {subj} (ИЗМЕНЕНО)\n"
                    if field == "description":
                        text += f"📝 <b>Что сделать:</b> <s>{clean_html(old_desc)}</s> ➡️ <b>{desc}</b>\n"
                    else:
                        text += f"📝 <b>Что сделать:</b> {desc}\n"
                    if field == "deadline":
                        text += f"⏰ <b>Сдать до:</b> <s>{old_dead}</s> ➡️ <code>{dt}</code>\n"
                    else:
                        text += f"⏰ <b>Сдать до:</b> <code>{dt}</code>\n"
                    if str(url).startswith("http"):
                        if field == "submit_url":
                            text += f"📥 <b>Сдача:</b> <s>{clean_html(old_url)}</s> ➡️ <a href='{url}'>Ссылка</a>\n"
                        else:
                            text += f"📥 <b>Сдача:</b> <a href='{url}'>Ссылка</a>\n"
                    else:
                        if field == "submit_url":
                            text += f"📥 <b>Сдача:</b> <s>{clean_html(old_url)}</s> ➡️ {clean_html(url)}\n"
                        else:
                            text += f"📥 <b>Сдача:</b> {clean_html(url)}\n"
                else:
                    # Для всех остальных задач выводим стандартный вид
                         # Стандартный вывод для остальных строк
                    if is_expired:
                        if str(url).startswith("http"):
                            text += f"❌ <s><b>Предмет:</b> {subj}\n<b>Что сделать:</b> {desc}\n<b>Сдать до:</b> {dt}</s> <i>(дедлайн прошел)</i> — <s><a href='{url}'>Ссылка</a></s>\n"
                        else:
                            text += f"❌ <s><b>Предмет:</b> {subj}\n<b>Что сделать:</b> {desc}\n<b>Сдать до:</b> {dt}</s> <i>(дедлайн прошел)</i> — <s>{clean_html(url)}</s>\n"


                    else:
                        if str(url).startswith("http"):
                            text += f"📘 <b>Предмет:</b> {subj}\n📝 <b>Что сделать:</b> {desc}\n⏰ <b>Сдать до:</b> <code>{dt}</code>\n📥 <b>Сдача:</b> <a href='{url}'>Ссылка</a>\n"
                        else:
                            text += f"📘 <b>Предмет:</b> {subj}\n📝 <b>Что сделать:</b> {desc}\n⏰ <b>Сдать до:</b> <code>{dt}</code>\n📥 <b>Сдача:</b> {clean_html(url)}\n"
                text += "‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾\n"
            except Exception as e:
                logging.error(f"Ошибка парсинга правок в закрепе: {e}")
            
    try:
        await bot.edit_message_text(text=text, chat_id=CHANNEL_ID, message_id=PINNED_MESSAGE_ID, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except Exception as e:
        logging.error(f"Ошибка правок закрепа: {e}")

# --- ПРОВЕРКА ДЕДЛАЙНОВ ЗА СУТКИ ---
async def check_24h_reminders():
    now = datetime.now()
    target_time_start = (now + timedelta(hours=23, minutes=30)).strftime("%Y-%m-%d %H:%M")
    target_time_end = (now + timedelta(hours=24)).strftime("%Y-%m-%d %H:%M")
    
    cursor.execute(
        "SELECT id, subject, description, deadline, submit_url, file_id FROM tasks WHERE deadline BETWEEN ? AND ? AND notified = 0",
        (target_time_start, target_time_end)
    )
    reminders = cursor.fetchall()
    
    for task in reminders:
        task_id, subj, desc, dead, url, file_id = task
        dt_format = datetime.strptime(dead, "%Y-%m-%d %H:%M").strftime("%d.%m.%Y %H:%M")
        
        alert_text = (
            f"🚨 <b>ВНИМАНИЕ! дедлайн через 24 часа</b> 🚨\n\n"
            f"📚 <b>Предмет:</b> {clean_html(subj)}\n"
            f"📝 <b>Что сдать:</b> {clean_html(desc)}\n"
            f"🔥 <b>Время:</b> <code>{dt_format}</code>\n"
        )
        
        kb = None
        if str(url).startswith("http"):
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📥 Куда сдавать", url=url)]])
        else:
            alert_text += f"📥 <b>Куда сдавать:</b> {clean_html(url)}\n"
        
        try:
            if file_id:
                msg = await bot.send_document(chat_id=CHANNEL_ID, document=file_id, caption=alert_text, reply_markup=kb, parse_mode=ParseMode.HTML)
            else:
                msg = await bot.send_message(chat_id=CHANNEL_ID, text=alert_text, reply_markup=kb, parse_mode=ParseMode.HTML)

            
            cursor.execute("UPDATE tasks SET notified = 1 WHERE id = ?", (task_id,))
            conn.commit()
        except Exception as e:
            logging.error(f"Не удалось отправить напоминание: {e}")

# --- ПАНЕЛЬ УПРАВЛЕНИЯ ЗАДАНИЯМИ (УДАЛЕНИЕ И ИЗМЕНЕНИЕ) ---
@router.message(Command("manage"), F.from_user.id == ADMIN_ID)
@router.message(F.text == '🗂 Управление', F.from_user.id == ADMIN_ID)
async def manage_tasks(message: Message):
    cursor.execute("SELECT id, subject, deadline FROM tasks ORDER BY deadline ASC")
    tasks = cursor.fetchall()
    
    if not tasks:
        await message.answer("В базе данных пока нет заданий.")
        return
        
    await message.answer("🗂 <b>Список заданий в базе:</b>\nВыберите действие:")
    
    for t in tasks:
        t_id, subj, dead = t
        try:
            dt = datetime.strptime(dead, "%Y-%m-%d %H:%M").strftime("%d.%m.%Y %H:%M")
        except Exception:
            dt = dead
        
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✏️ Изменить", callback_data=f"edit_{t_id}"),
                InlineKeyboardButton(text="❌ Удалить", callback_data=f"del_{t_id}")
            ]
        ])
        await message.answer(f"📘 <b>{clean_html(subj)}</b>\n⏰ Дедлайн: {dt}", reply_markup=kb, parse_mode=ParseMode.HTML)

# Удаление поста из ленты канала и строки из базы данных
@router.callback_query(F.data.startswith("del_"))
async def delete_task_callback(callback: CallbackQuery):
    task_id = int(callback.data.split("_")[1])  #  Исправлено

    cursor.execute("SELECT message_id FROM tasks WHERE id = ?", (task_id,))
    res = cursor.fetchone()
    
    if res and res[0] != 0:
        try:
            await bot.delete_message(chat_id=CHANNEL_ID, message_id=res[0])
        except Exception as e:
            logging.error(f"Не удалось стереть пост {res[0]} из ленты: {e}")
            
    cursor.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    conn.commit()
    
    await callback.answer("Задание удалено!")
    await callback.message.edit_text("🗑 Пост стёрт из ленты, базы и закрепа.")
    await update_pinned_post()


@router.callback_query(F.data.in_({"change_desc", "change_date", "change_url"})) #  Теперь поймает любой клик!
async def process_edit_choice(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    if callback.data == "change_desc":
        await state.update_data(edit_field="description")
        await callback.message.answer("Введите НОВОЕ описание домашнего задания:")
    elif callback.data == "change_date":
        await state.update_data(edit_field="deadline")
        await callback.message.answer("Введите НОВЫЙ дедлайн (ДД.ММ.ГГГГ ЧЧ:ММ):")
    elif callback.data == "change_url":
        await state.update_data(edit_field="submit_url")
        await callback.message.answer("Укажите НОВОЕ место сдачи (ссылку, почту или Telegram):")
    await state.set_state(EditForm.new_value)
# Логика автоматического редактирования старого поста в самом канале
# Логика автоматического редактирования, перечеркивания и отправки пуш-уведомления об изменениях
@router.message(EditForm.new_value)
async def process_edit_save(message: Message, state: FSMContext):
    user_data = await state.get_data()
    task_id = user_data['edit_task_id']
    field = user_data['edit_field']
    new_text = message.text
    
    cursor.execute("SELECT subject, description, deadline, submit_url, message_id FROM tasks WHERE id = ?", (task_id,))
    old_task = cursor.fetchone()
    
    if not old_task:
        await message.answer("Ошибка: Задание не найдено.")
        await state.clear()
        return
        
    subj, old_desc, old_dead, old_url, msg_id = old_task
    
    if field == "deadline":
        try:
            dt = datetime.strptime(new_text, "%d.%m.%Y %H:%M")
            new_text = dt.strftime("%Y-%m-%d %H:%M")
        except ValueError:
            await message.answer("Неверный формат! Введите ДД.ММ.ГГГГ ЧЧ:ММ:")
            return

    # Записываем изменения в базу данных
    if field == "deadline":
        cursor.execute("UPDATE tasks SET deadline = ?, notified = 0 WHERE id = ?", (new_text, task_id))
        final_dead, final_desc, final_url = new_text, old_desc, old_url
    elif field == "description":
        cursor.execute("UPDATE tasks SET description = ? WHERE id = ?", (new_text, task_id))
        final_dead, final_desc, final_url = old_dead, new_text, old_url
    elif field == "submit_url":
        cursor.execute("UPDATE tasks SET submit_url = ? WHERE id = ?", (new_text, task_id))
        final_dead, final_desc, final_url = old_dead, old_desc, new_text
    conn.commit()
    
    # Форматируем даты для текста изменений
    dt_old_format = datetime.strptime(old_dead, "%Y-%m-%d %H:%M").strftime("%d.%m.%Y %H:%M")
    dt_new_format = datetime.strptime(final_dead, "%Y-%m-%d %H:%M").strftime("%d.%m.%Y %H:%M")
    
    if "Биостатистика" in subj: hashtag = "#биостатистика"
    elif "Молекулярная эволюция" in subj: hashtag = "#молекулярная_эволюция"
    elif "Генетические основы" in subj: hashtag = "#селекция"
    else: hashtag = "#биоинформатика"

    # 1. ТЕКСТ ДЛЯ ОБНОВЛЕНИЯ СТАРОГО ПОСТА В ЛЕНТЕ (С ПЕРЕЧЕРКИВАНИЕМ)
    edited_post_text = f"🔄 <b>Задание изменено!</b> {hashtag}\n\n📘 <b>Предмет:</b> {clean_html(subj)}\n"
    if field == "description":
        edited_post_text += f"📝 <b>Что сделать:</b> <s>{clean_html(old_desc)}</s> ➡️ <b>{clean_html(new_text)}</b>\n"
    else:
        edited_post_text += f"📝 <b>Что сделать:</b> {clean_html(final_desc)}\n"
    if field == "deadline":
        edited_post_text += f"⏰ <b>Сдать до:</b> <s>{dt_old_format}</s> ➡️ <code>{dt_new_format}</code>\n"
    else:
        edited_post_text += f"⏰ <b>Сдать до:</b> <code>{dt_new_format}</code>\n"

    kb = None
    if str(final_url).startswith("http"):
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📥 Куда сдавать", url=final_url)]])
    else:
        if field == "submit_url":
            edited_post_text += f"📥 <b>Куда сдавать:</b> <s>{clean_html(old_url)}</s> ➡️ <b>{clean_html(new_text)}</b>\n"
        else:
            edited_post_text += f"📥 <b>Куда сдавать:</b> {clean_html(final_url)}\n"

    if msg_id and msg_id != 0:
        try:
            await bot.edit_message_text(chat_id=CHANNEL_ID, message_id=msg_id, text=edited_post_text, reply_markup=kb, parse_mode=ParseMode.HTML)
        except Exception as e:
            logging.error(f"Не удалось изменить пост {msg_id} в ленте канала: {e}")

        # 2. ОТПРАВЛЯЕМ НОВОЕ ОТДЕЛЬНОЕ СООБЩЕНИЕ УВЕДОМЛЕНИЯ В КАНАЛ
    alert_channel_text = f"🔔 <b>Внимание! Задание изменено</b> {hashtag}\n\n📚 <b>Предмет:</b> {clean_html(subj)}\n"
    if field == "description":
        alert_channel_text += f"❌ <s>📝 <b>Что сделать было:</b> {clean_html(old_desc)}</s>\n✅ 📝 <b>Что сделать стало:</b> {clean_html(new_text)}\n"
    elif field == "deadline":
        alert_channel_text += f"❌ <s>⏰ <b>Сдать до было:</b> {dt_old_format}</s>\n✅ ⏰ <b>Сдать до стало:</b> <code>{dt_new_format}</code>\n"
    elif field == "submit_url":
        alert_channel_text += f"❌ <s>📥 <b>Куда сдавать было:</b> {clean_html(old_url)}</s>\n✅ 📥 <b>Куда сдавать стало:</b> {clean_html(new_text)}\n"
    alert_channel_text += "\n📋 Изменения внесены в закрепленный пост группы!"

    try:
        # Отправляем пост и перехватываем его ID
        msg_alert = await bot.send_message(chat_id=CHANNEL_ID, text=alert_channel_text, parse_mode=ParseMode.HTML)
        
        # Получаем текущие сохраненные ID обновлений из базы
        cursor.execute("SELECT edit_message_ids FROM tasks WHERE id = ?", (task_id,))
        current_edits = cursor.fetchone()[0] or ""
        
        # Дописываем ID нового сообщения через запятую
        new_edits = f"{current_edits},{msg_alert.message_id}" if current_edits else str(msg_alert.message_id)
        
        cursor.execute("UPDATE tasks SET edit_message_ids = ? WHERE id = ?", (new_edits, task_id))
        conn.commit()
    except Exception as e:
        logging.error(f"Ошибка отправки уведомления об изменении: {e}")

    await state.clear()
    await message.answer("✨ Изменения успешно сохранены! Оригинальный пост переписан, закреп обновлен, уведомление отправлено в канал.")
    await update_pinned_post_with_change(task_id, field, old_desc, dt_old_format, old_url)


# Нажатие на кнопку «Изменить» — выбор, что менять
@router.callback_query(F.data.startswith("edit_"))
async def edit_task_callback(callback: CallbackQuery, state: FSMContext):
    task_id = int(callback.data.split("_")[1])
    await state.update_data(edit_task_id=task_id)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Изменить описание", callback_data="change_desc")],
        [InlineKeyboardButton(text="⏰ Изменить дату", callback_data="change_date")],
        [InlineKeyboardButton(text="📥 Изменить сдачу", callback_data="change_url")] #  Добавили третью кнопку
    ])
    
    await callback.answer()
    await callback.message.answer("Что именно вы хотите изменить в этом задании?", reply_markup=kb)
    await state.set_state(EditForm.choice)



# --- ПРИВЕТСТВИЕ И АКТИВАЦИЯ КНОПОК ---
@router.message(Command("start"), F.from_user.id == ADMIN_ID)
async def cmd_start(message: Message):
    await message.answer(
        "👋 Привет, староста! Рад приветствовать тебя в панели управления группой.\n\n"
        "✨ Лови меню быстрых кнопок внизу экрана!", 
        reply_markup=admin_main_keyboard
    )



# --- ДИАЛОГ СО СТАРОСТОЙ (ДОБАВЛЕНИЕ) ---
@router.message(Command("add"), F.from_user.id == ADMIN_ID)
@router.message(F.text == '➕ Добавить дедлайн', F.from_user.id == ADMIN_ID)
async def start_add(message: Message, state: FSMContext):
    await message.answer("Выбери название предмета из списка:", reply_markup=subjects_keyboard)
    await state.set_state(Form.subject)

@router.message(Form.subject)
async def process_subject(message: Message, state: FSMContext):
    await state.update_data(subject=message.text)
    await message.answer("Опиши, что нужно сделать (какая домашка):", reply_markup=ReplyKeyboardRemove())
    await state.set_state(Form.description)

@router.message(Form.description)
async def process_desc(message: Message, state: FSMContext):
    await state.update_data(description=message.text)
    await message.answer("Введи дедлайн в формате: ДД.ММ.ГГГГ ЧЧ:ММ\n(Например: 25.09.2026 18:00)")
    await state.set_state(Form.deadline)

@router.message(Form.deadline)
async def process_deadline(message: Message, state: FSMContext):
    try:
        # Проверяем и сохраняем дату
        dt = datetime.strptime(message.text, "%d.%m.%Y %H:%M")
        await state.update_data(deadline=dt.strftime("%Y-%m-%d %H:%M"))
        
        user_data = await state.get_data()
        subj = user_data['subject']
        
        # Автоматическая логика контактов в зависимости от предмета
        if 'Биоинформатика' in subj:
            await state.update_data(submit_url="Телеграмм - @KateChernyaeva")
            await message.answer("Прикрепи файл к этому дедлайну (документ, фото, архив) или напиши словом 'нет', если файла нет:")
            await state.set_state(Form.file)
            
        elif 'Биостатистика' in subj:
            await state.update_data(submit_url="отправят форму для дз позже")
            await message.answer("Прикрепи файл к этому дедлайну (документ, фото, архив) или напиши словом 'нет', если файла нет:")
            await state.set_state(Form.file)
            
        elif 'Молекулярная эволюция' in subj:
            await state.update_data(submit_url="dmitrii.lv.konovalov@gmail.com")
            await message.answer("Прикрепи файл к этому дедлайну (документ, фото, архив) или напиши словом 'нет', если файла нет:")
            await state.set_state(Form.file)
            
        elif 'Генетические основы' in subj:
            # Для селекции создаем кнопки выбора почты преподавателя
            kb_emails = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📧 m_selin@mail.ru", callback_data="mail_selin")],
                [InlineKeyboardButton(text="📧 olesyuk@rgau-msha.ru", callback_data="mail_olesyuk")]
            ])
            await message.answer("Выбери, на какую почту нужно отправить это задание:", reply_markup=kb_emails)
            await state.set_state(Form.select_email)
            
    except ValueError:
        await message.answer("Неверный формат даты! Попробуй еще раз (ДД.ММ.ГГГГ ЧЧ:ММ):")


# Обработка выбора почты для Генетических основ селекции
@router.callback_query(Form.select_email)
async def process_selection_email(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    
    if callback.data == "mail_selin":
        await state.update_data(submit_url="m_selin@mail.ru")
    elif callback.data == "mail_olesyuk":
        await state.update_data(submit_url="olesyuk@rgau-msha.ru")
        
    await callback.message.answer("Почта выбрана! Теперь прикрепи файл к этому дедлайну или напиши словом 'нет', если файла нет:")
    await state.set_state(Form.file)

@router.message(Form.file)
async def process_file(message: Message, state: FSMContext):
    data = await state.get_data()
    file_id = None
    
    if message.document:
        file_id = message.document.file_id
    elif message.photo:
        file_id = message.photo[-1].file_id

    # --- ПУБЛИКАЦИЯ В КАНАЛ ---
    dt_display = datetime.strptime(data['deadline'], "%Y-%m-%d %H:%M").strftime("%d.%m.%Y %H:%M")
    
    subj_html = clean_html(data['subject'])
    desc_html = clean_html(data['description'])
    url_val = data['submit_url']
    
    # Автоматически определяем нужный хештег по ключевым словам
    if "Биостатистика" in data['subject']:
        hashtag = "#биостатистика"
    elif "Молекулярная эволюция" in data['subject']:
        hashtag = "#молекулярная_эволюция"
    elif "Генетические основы" in data['subject']:
        hashtag = "#селекция"
    else:
        hashtag = "#биоинформатика"
    
    new_task_text = (
        f"📚 <b>Новое задание в расписании</b> {hashtag}\n\n"  #  Теперь заголовок будет реально жирным!
        f"📘 <b>Предмет:</b> {subj_html}\n"
        f"📝 <b>Что сделать:</b> {desc_html}\n"
        f"⏰ <b>Сдать до:</b> <code>{dt_display}</code>\n"
    )

    
    kb = None
    if str(url_val).startswith("http"):
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📥 Куда сдавать", url=url_val)]])
    else:
        new_task_text += f"📥 <b>Куда сдавать:</b> {str(url_val).replace('_', '\\_').replace('*', '\\*')}\n"
    
    posted_message_id = 0
    try:
        if file_id:
            msg = await bot.send_document(chat_id=CHANNEL_ID, document=file_id, caption=new_task_text, reply_markup=kb, parse_mode=ParseMode.HTML)
            posted_message_id = msg.message_id
        else:
            msg = await bot.send_message(chat_id=CHANNEL_ID, text=new_task_text, reply_markup=kb, parse_mode=ParseMode.HTML)
            posted_message_id = msg.message_id
    except Exception as e:
        logging.error(f"Ошибка отправки в канал: {e}")


    # --- СОХРАНЯЕМ В БАЗУ ДАННЫХ ВМЕСТЕ С ID ПОСТА ---
    cursor.execute(
        "INSERT INTO tasks (subject, description, deadline, submit_url, file_id, message_id) VALUES (?, ?, ?, ?, ?, ?)",
        (data['subject'], data['description'], data['deadline'], data['submit_url'], file_id, posted_message_id)
    )
    conn.commit()
    await state.clear()
    await message.answer("🎉 Задание успешно добавлено в базу данных!")

    await update_pinned_post()

# --- ЗАПУСК БОТА ---
# Хэндлер для веб-страницы (чтобы хостинг видел, что бот живой)
async def handle_web(request):
    return web.Response(text="Бот активен и работает 24/7!")

# --- ЗАПУСК БОТА ---
async def main():
    global db_pool  # <-- ДОБАВЬТЕ ЭТУ СТРОКУ!
    dp.include_router(router)

    # Создаем пул подключений к Supabase
    db_pool = await asyncpg.create_pool(
        user=DB_USER,
        password=DB_PASSWORD,
        host=DB_HOST,
        port=DB_PORT,
        database=DB_NAME
    )
    # Настраиваем задачи планировщика
    scheduler.add_job(check_24h_reminders, 'interval', minutes=15)
    scheduler.add_job(update_pinned_post, 'interval', minutes=15)
    scheduler.add_job(clear_old_deadlines, 'cron', hour=3, minute=0)
    scheduler.start()
    
    await update_pinned_post()
    
    # Создаем веб-сервер внутри aiogram для защиты от сна на Render
    app = web.Application()
    app.router.add_get("/", handle_web)
    runner = web.AppRunner(app)
    await runner.setup()
    
    # Render дает порт в переменных среды PORT, по умолчанию ставим 8080
    import os
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    # Запускаем чтение сообщений Telegram
    await dp.start_polling(bot)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())

