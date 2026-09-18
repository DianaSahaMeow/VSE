import asyncio
import logging
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
import urllib.parse 

# --- НАСТРОЙКИ TELEGRAM БОТА ---
BOT_TOKEN = "8653801306:AAFfKR9d9D8bLYEArAHxov40_bi4b-N9BOM"
CHANNEL_ID = -1004330638807  # ID канала с -100
ADMIN_ID = 987506862         # Ваш личный Telegram ID старосты
PINNED_MESSAGE_ID = 3        # ID закрепленного сообщения

# --- НАСТРОЙКИ ОБЛАЧНОЙ БАЗЫ SUPABASE ---
# --- НАСТРОЙКИ ОБЛАЧНОЙ БАЗЫ SUPABASE (IPv4 через Session Pooler) ---
DB_USER = "postgres.tqpaoezbovvanysghfvl"
DB_PASSWORD = "/-s56B3sbWw+L&L"                       # без %2F, %2B, %26
DB_HOST = "aws-1-eu-west-1.pooler.supabase.com"
DB_PORT = 5432
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
                                text += f"❌ <s> {desc} (до {dt})</s> <i>(дедлайн прошел)</i> — <s><a href='{url}'>Ссылка</a></s>\n"
                            else:
                                text += f"❌ <s> {desc} (до {dt})</s> <i>(дедлайн прошел)</i> — <s>{clean_html(url)}</s>\n"
                        else:
                            # Если актуально — выводим красиво
                            if str(url).startswith("http"):
                                text += f"🔸  {desc} (до <code>{dt}</code>) — <a href='{url}'>Ссылка</a>\n"
                            else:
                                text += f"🔸  {desc} (до <code>{dt}</code>) — {clean_html(url)}\n"
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
# --- АВТОМАТИЧЕСКОЕ УДАЛЕНИЕ ПРОСРОЧЕННЫХ ЗАДАНИЙ ЧЕРЕЗ 2 НЕДЕЛИ ПОСЛЕ ДЕДЛАЙНА ---
async def clear_old_deadlines():
    # Рассчитываем временную метку: текущее время минус 14 дней
    two_weeks_ago = (datetime.now() - timedelta(days=14)).strftime("%Y-%m-%d %H:%M")
    
    # Открываем коннект к Supabase
    async with db_pool.acquire() as conn:
        old_tasks = await conn.fetch("SELECT id, message_id FROM tasks WHERE deadline < $1", two_weeks_ago)
        
        for task in old_tasks:
            task_id = task['id']
            msg_id = task['message_id']
            
            # Удаляем оригинальный пост из ленты канала
            if msg_id and msg_id != 0:
                try:
                    await bot.delete_message(chat_id=CHANNEL_ID, message_id=msg_id)
                except Exception as e:
                    logging.error(f"Не удалось автоматически удалить старый пост {msg_id}: {e}")
                    
            # Стираем запись из облачной базы данных
            await conn.execute("DELETE FROM tasks WHERE id = $1", task_id)
            
        if old_tasks:
            logging.info(f"Автоочистка архива: успешно удалено заданий: {len(old_tasks)}")
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
# --- ОБНОВЛЕНИЕ ЗАКРЕПЛЕННОГО ПОСТА С ОТОБРАЖЕНИЕМ ПРАВОК И СОРТИРОВКОЙ ПО ПРЕДМЕТАМ (ПОД SUPABASE) ---
async def update_pinned_post_with_change(changed_id, field, old_desc, old_dead, old_url):
    async with db_pool.acquire() as conn:
        # Вытаскиваем список уникальных предметов
        unique_subjects_rows = await conn.fetch("SELECT DISTINCT subject FROM tasks")
        unique_subjects = [row['subject'] for row in unique_subjects_rows]
        
        text = "📌 <b>Актулаьные дедлайны</b> 📌\n\n"
        
        if not unique_subjects:
            text += "Ура! Активных заданий нет 🎉"
        else:
            now = datetime.now()
            for subj_name in unique_subjects:
                if "Биостатистика" in subj_name: hashtag = "#биостатистика"
                elif "Молекулярная эволюция" in subj_name: hashtag = "#молекулярная_эволюция"
                elif "Генетические основы" in subj_name: hashtag = "#селекция"
                else: hashtag = "#биоинформатика"
                
                text += f"📘 <b>{clean_html(subj_name)}</b> {hashtag}\n"
                text += f"‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾\n"
                
                # Вытаскиваем таски предмета
                subj_tasks = await conn.fetch("SELECT id, description, deadline, submit_url FROM tasks WHERE subject = $1 ORDER BY deadline ASC", subj_name)
                
                for task in subj_tasks:
                    try:
                        t_id = task['id']
                        desc_raw = task['description']
                        dead_raw = task['deadline']
                        url_raw = task['submit_url']
                        
                        task_deadline = datetime.strptime(dead_raw, "%Y-%m-%d %H:%M")
                        dt = task_deadline.strftime("%d.%m.%Y %H:%M")
                        is_expired = task_deadline < now
                        
                        desc = clean_html(desc_raw)
                        url = url_raw
                        
                        # Если это таска, которую только что изменили — показываем "Было -> Стало"
                        if t_id == changed_id:
                            text += "🔄 "
                            if field == "description":
                                text += f"• <s>{clean_html(old_desc)}</s> ➡️ <b>{desc}</b>"
                            else:
                                text += f"• {desc}"
                                
                            if field == "deadline":
                                text += f" (сроки: <s>{old_dead}</s> ➡️ <code>{dt}</code>)"
                            else:
                                text += f" (до <code>{dt}</code>)"
                                
                            if field == "submit_url":
                                if str(url).startswith("http"):
                                    text += f" — сдача: <s>{clean_html(old_url)}</s> ➡️ <a href='{url}'>Ссылка</a>\n"
                                else:
                                    text += f" — сдача: <s>{clean_html(old_url)}</s> ➡️ {clean_html(url)}\n"
                        else:
                            # Стандартный вывод для остальных строк
                            if is_expired:
                                if str(url).startswith("http"):
                                    text += f"❌ <s>• {desc} (до {dt})</s> <i>(дедлайн прошел)</i> — <s><a href='{url}'>Ссылка</a></s>\n"
                                else:
                                    text += f"❌ <s>• {desc} (до {dt})</s> <i>(дедлайн прошел)</i> — <s>{clean_html(url)}</s>\n"
                            else:
                                if str(url).startswith("http"):
                                    text += f"🔸 • {desc} (до <code>{dt}</code>) — <a href='{url}'>Ссылка</a>\n"
                                else:
                                    text += f"🔸 • {desc} (до <code>{dt}</code>) — {clean_html(url)}\n"
                    except Exception as e:
                        logging.error(f"Ошибка правок закрепа: {e}")
                        
                text += "\n"
                
        try:
            await bot.edit_message_text(text=text, chat_id=CHANNEL_ID, message_id=PINNED_MESSAGE_ID, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        except Exception as e:
            logging.error(f"Ошибка правок закрепа: {e}")


# --- ПРОВЕРКА ДЕДЛАЙНОВ ЗА СУТКИ ---
# --- ПРОВЕРКА ДЕДЛАЙНОВ ЗА СУТКИ ПОД SUPABASE ---
async def check_24h_reminders():
    now = datetime.now()
    target_time_start = (now + timedelta(hours=23, minutes=30)).strftime("%Y-%m-%d %H:%M")
    target_time_end = (now + timedelta(hours=24)).strftime("%Y-%m-%d %H:%M")
    
    async with db_pool.acquire() as conn:
        reminders = await conn.fetch(
            "SELECT id, subject, description, deadline, submit_url, file_id FROM tasks WHERE deadline BETWEEN $1 AND $2 AND notified = 0", 
            target_time_start, target_time_end
        )
        
        for task in reminders:
            task_id = task['id']
            dt_format = datetime.strptime(task['deadline'], "%Y-%m-%d %H:%M").strftime("%d.%m.%Y %H:%M")
            
            alert_text = (
                f"🚨 <b>ВНИМАНИЕ! дедлайн через 24 часа</b> 🚨\n\n"
                f"📚 <b>Предмет:</b> {clean_html(task['subject'])}\n"
                f"📝 <b>Что сдать:</b> {clean_html(task['description'])}\n"
                f"🔥 <b>Время:</b> <code>{dt_format}</code>\n"
            )
            
            kb = None
            if str(task['submit_url']).startswith("http"):
                kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📥 Куда сдавать", url=task['submit_url'])]])
            else:
                alert_text += f"📥 <b>Куда сдавать:</b> {clean_html(task['submit_url'])}\n"
            
            try:
                if task['file_id']:
                    await bot.send_document(chat_id=CHANNEL_ID, document=task['file_id'], caption=alert_text, reply_markup=kb, parse_mode=ParseMode.HTML)
                else:
                    await bot.send_message(chat_id=CHANNEL_ID, text=alert_text, reply_markup=kb, parse_mode=ParseMode.HTML)
                
                await conn.execute("UPDATE tasks SET notified = 1 WHERE id = $1", task_id)
            except Exception as e:
                logging.error(f"Не удалось отправить напоминание: {e}")

# --- ПАНЕЛЬ УПРАВЛЕНИЯ ЗАДАНИЯМИ (УДАЛЕНИЕ И ИЗМЕНЕНИЕ) ---
# --- ПАНЕЛЬ УПРАВЛЕНИЯ ЗАДАНИЯМИ ДЛЯ SUPABASE ---
@router.message(Command("manage"), F.from_user.id == ADMIN_ID)
@router.message(F.text == '🗂 Управление', F.from_user.id == ADMIN_ID)
async def manage_tasks(message: Message):
    async with db_pool.acquire() as conn:
        tasks = await conn.fetch("SELECT id, subject, deadline FROM tasks ORDER BY deadline ASC")
        
        if not tasks:
            await message.answer("В базе данных пока нет заданий.")
            return
            
        await message.answer("🗂 <b>Список заданий в базе:</b>\nВыберите действие:")
        
        for t in tasks:
            t_id, subj, dead = t['id'], t['subject'], t['deadline']
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

# Удаление поста, закрепа и всей цепочки промежуточных уведомлений об изменениях (Supabase)
@router.callback_query(F.data.startswith("del_"))
async def delete_task_callback(callback: CallbackQuery):
    task_id = int(callback.data.split("_")[1])  # Извлекаем ID из кнопки
    
    async with db_pool.acquire() as conn:
        # Вытаскиваем ID оригинального сообщения и строку со всеми ID правок
        res = await conn.fetchrow("SELECT message_id, edit_message_ids FROM tasks WHERE id = $1", task_id)
        
        if res:
            orig_msg_id = res['message_id']
            edit_msg_ids_str = res['edit_message_ids']
            
            # 1. Стираем оригинальный пост из ленты канала
            if orig_msg_id and orig_msg_id != 0:
                try:
                    await bot.delete_message(chat_id=CHANNEL_ID, message_id=orig_msg_id)
                except Exception as e:
                    logging.error(f"Не удалось стереть оригинальный пост {orig_msg_id}: {e}")
                    
            # 2. Поочередно стираем все посты с уведомлениями об изменениях
            if edit_msg_ids_str:
                # Превращаем текст "124,125" в список чисел и удаляем пробелы
                edit_ids = [int(x) for x in edit_msg_ids_str.split(",") if x.strip()]
                for e_id in edit_ids:
                    try:
                        await bot.delete_message(chat_id=CHANNEL_ID, message_id=e_id)
                    except Exception as e:
                        logging.error(f"Не удалось стереть промежуточное обновление {e_id}: {e}")
                
        # 3. Полностью удаляем саму строчку задания из облака Supabase
        await conn.execute("DELETE FROM tasks WHERE id = $1", task_id)
        
    await callback.answer("Задание удалено!")
    await callback.message.edit_text("🗑 Пост и все его уведомления полностью стёрты из ленты, базы и закрепа.")
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
# Логика автоматического редактирования, перечеркивания и отправки пуш-уведомлений (Supabase)
@router.message(EditForm.new_value)
async def process_edit_save(message: Message, state: FSMContext):
    user_data = await state.get_data()
    task_id = user_data['edit_task_id']
    field = user_data['edit_field']
    new_text = message.text
    
    async with db_pool.acquire() as conn:
        # Получаем старые данные из Supabase
        old_task = await conn.fetchrow("SELECT subject, description, deadline, submit_url, message_id FROM tasks WHERE id = $1", task_id)
        
        if not old_task:
            await message.answer("Ошибка: Задание не найдено.")
            await state.clear()
            return
            
        subj = old_task['subject']
        old_desc = old_task['description']
        old_dead = old_task['deadline']
        old_url = old_task['submit_url']
        msg_id = old_task['message_id']
        
        if field == "deadline":
            try:
                dt = datetime.strptime(new_text, "%d.%m.%Y %H:%M")
                new_text = dt.strftime("%Y-%m-%d %H:%M")
            except ValueError:
                await message.answer("Неверный формат! Введите ДД.ММ.ГГГГ ЧЧ:ММ:")
                return

        # Записываем изменения в облачную базу данных в зависимости от поля
        if field == "deadline":
            await conn.execute("UPDATE tasks SET deadline = $1, notified = 0 WHERE id = $2", new_text, task_id)
            final_dead, final_desc, final_url = new_text, old_desc, old_url
        elif field == "description":
            await conn.execute("UPDATE tasks SET description = $1 WHERE id = $2", new_text, task_id)
            final_dead, final_desc, final_url = old_dead, new_text, old_url
        elif field == "submit_url":
            await conn.execute("UPDATE tasks SET submit_url = $1 WHERE id = $2", new_text, task_id)
            final_dead, final_desc, final_url = old_dead, old_desc, new_text
        
        # Форматируем даты для текста изменений
        dt_old_format = datetime.strptime(old_dead, "%Y-%m-%d %H:%M").strftime("%d.%m.%Y %H:%M")
        dt_new_format = datetime.strptime(final_dead, "%Y-%m-%d %H:%M").strftime("%d.%m.%Y %H:%M")
        
        if "Биостатистика" in subj: hashtag = "#биостатистика"
        elif "Молекулярная эволюция" in subj: hashtag = "#молекулярная_эволюция"
        elif "Генетические основы" in subj: hashtag = "#селекция"
        else: hashtag = "#биоинформатика"

        # 1. ТЕКСТ ДЛЯ ОБНОВЛЕНИЯ СТАРОГО ПОСТА В ЛЕНТЕ (С П ПЕРЕЧЕРКИВАНИЕМ)
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
            msg_alert = await bot.send_message(chat_id=CHANNEL_ID, text=alert_channel_text, parse_mode=ParseMode.HTML)
            
            # Получаем текущие сохраненные ID обновлений из базы
            current_edits_row = await conn.fetchrow("SELECT edit_message_ids FROM tasks WHERE id = $1", task_id)
            current_edits = current_edits_row['edit_message_ids'] or ""
            
            # Дописываем ID нового сообщения через запятую
            new_edits = f"{current_edits},{msg_alert.message_id}" if current_edits else str(msg_alert.message_id)
            
            await conn.execute("UPDATE tasks SET edit_message_ids = $1 WHERE id = $2", new_edits, task_id)
        except Exception as e:
            logging.error(f"Ошибка отправки уведомления об изменении: {e}")

    await state.clear()
    await message.answer("✨ Изменения успешно сохранены! Оригинальный пост переписан, закреп обновлен, уведомление отправлено в канал.", reply_markup=admin_main_keyboard)
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
    # --- СОХРАНЯЕМ В ОБЛАЧНУЮ БАЗУ SUPABASE ---
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO tasks (subject, description, deadline, submit_url, file_id, message_id) VALUES ($1, $2, $3, $4, $5, $6)",
            data['subject'], data['description'], data['deadline'], data['submit_url'], file_id, posted_message_id
        )

    await state.clear()
    await message.answer("🎉 Задание успешно добавлено в базу данных!", reply_markup=admin_main_keyboard)
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
    # Создаем пул подключений к Supabase по экранированной защищенной строке
    # Создаем пул подключений к Supabase по экранированной защищенной строке с SNI
    import ssl

    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE

    try:
        db_pool = await asyncpg.create_pool(
            user=DB_USER,
            password=DB_PASSWORD,
            host=DB_HOST,
            port=DB_PORT,
            database=DB_NAME,
            ssl=ssl_context,
            min_size=1,
            max_size=5,
            timeout=15,
            command_timeout=60
        )

        async with db_pool.acquire() as conn:
            await conn.execute('''
            CREATE TABLE IF NOT EXISTS tasks (
                id SERIAL PRIMARY KEY,
                subject TEXT,
                description TEXT,
                deadline TEXT,
                submit_url TEXT,
                file_id TEXT,
                notified INTEGER DEFAULT 0,
                message_id INTEGER DEFAULT 0,
                edit_message_ids TEXT DEFAULT ''
            )
            ''')
        async with db_pool.acquire() as conn:
            version = await conn.fetchval("SELECT version()")
        logging.info(f"✅ Подключение к Supabase OK: {version}")
    except Exception as e:
        logging.error(f"❌ Ошибка подключения к БД: {e!r}")
        raise
    

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

