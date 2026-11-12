import asyncio
import io
import logging
import os
import re
import ssl
import uuid
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    MenuButtonDefault,
    MenuButtonWebApp,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    WebAppInfo,
)
import aiohttp
from aiohttp import web
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import asyncpg

from config import (
    ADMIN_ID,
    BOT_TOKEN,
    CHANNEL_ID,
    DB_HOST,
    DB_NAME,
    DB_PASSWORD,
    DB_PORT,
    DB_USER,
    DEFAULT_SUBJECTS,
    MSK,
    clean_html,
    get_crm_base_url,
    get_hashtag_by_subject,
    now_msk,
    task_link,
)
from crm_server import MAX_FILE_BYTES, build_admin_magic_link, create_web_app
from task_sync import (
    build_submit_kb,
    delete_task_completely,
    get_all_subjects,
    migrate_schema,
    publish_task_to_channel,
    send_task_attachments,
    sync_task_attachments_in_channel,
    sync_task_edit_to_channel,
    update_pinned_post as sync_update_pinned_post,
)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()
scheduler = AsyncIOScheduler(timezone=MSK)
db_pool = None

# Буфер для безопасного накопления нескольких файлов / альбомов от пользователя
_PENDING_FILES: dict[tuple[int, str], list[dict]] = {}
_PENDING_LOCK = asyncio.Lock()

# --- ГЛАВНАЯ КЛАВИАТУРА СТАРОСТЫ ---
admin_main_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text='➕ Добавить дедлайн')],
        [KeyboardButton(text='🗂 Управление'), KeyboardButton(text='📢 Важное объявление')],
        [KeyboardButton(text='🌐 CRM Панель')],
    ],
    resize_keyboard=True
)


def build_subjects_keyboard(subjects: list[str] | None = None) -> ReplyKeyboardMarkup:
    items = subjects or DEFAULT_SUBJECTS
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=s)] for s in items],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


# --- КЛАВИАТУРА ВЫБОРА ПРЕДМЕТОВ (ПО УМОЛЧАНИЮ) ---
subjects_keyboard = build_subjects_keyboard()

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
    file = State()

class EditForm(StatesGroup):
    task_id = State()
    choice = State()
    new_value = State()
    files = State()

class NoticeForm(StatesGroup):
    text = State()
    pin = State()


async def _extract_and_download_file(message: Message) -> tuple[dict | None, str | None]:
    """Извлекает документ или фото из сообщения и скачивает байты для сохранения в Supabase."""
    file_id = None
    file_name = None
    mime_type = "application/octet-stream"
    file_size = 0
    file_kind = "document"

    if message.document:
        doc = message.document
        file_id = doc.file_id
        file_name = doc.file_name or f"document_{doc.file_unique_id}"
        mime_type = doc.mime_type or "application/octet-stream"
        file_size = doc.file_size or 0
        file_kind = "document"
    elif message.photo:
        ph = message.photo[-1]
        file_id = ph.file_id
        file_name = f"photo_{ph.file_unique_id}.jpg"
        mime_type = "image/jpeg"
        file_size = ph.file_size or 0
        file_kind = "photo"
    else:
        return None, None

    if file_size and file_size > MAX_FILE_BYTES:
        return None, f"Файл «{file_name}» больше 15 МБ и не может быть сохранён в бесплатную БД (лимит 15 МБ на файл)."

    file_bytes = None
    try:
        tg_file = await bot.get_file(file_id)
        if not tg_file.file_size or tg_file.file_size <= MAX_FILE_BYTES:
            buf = io.BytesIO()
            await bot.download_file(tg_file.file_path, destination=buf)
            raw = buf.getvalue()
            if len(raw) <= MAX_FILE_BYTES:
                file_bytes = raw
                file_size = len(raw)
    except Exception as exc:
        logging.warning(f"Не удалось скачать байты файла {file_name} для БД (будет сохранён telegram_file_id): {exc}")

    return {
        "file_name": file_name,
        "mime_type": mime_type,
        "file_size": file_size,
        "file_data": file_bytes,
        "telegram_file_id": file_id,
        "file_kind": file_kind,
    }, None

# --- ФУНКЦИЯ ОБНОВЛЕНИЯ ЗАКРЕПЛЕННОГО ПОСТА ---
async def update_pinned_post():
    if db_pool:
        await sync_update_pinned_post(bot, db_pool)

# --- АВТОМАТИЧЕСКАЯ ОЧИСТКА В КОНЦЕ МОДУЛЕЙ С РЕДАКТИРОВАНИЕМ ПОСТОВ В КАНАЛЕ ---
async def clear_old_deadlines():
    try:
        now = now_msk()
        current_date_str = now.strftime("%m-%d")
        
        module_end_dates = ["10-26", "12-21", "03-25", "06-21"]
        
        if current_date_str not in module_end_dates:
            logging.info(f"Проверка очистки: сегодня {current_date_str}, ждем конца модуля.")
            return

        logging.info(f"📆 Наступил конец модуля ({current_date_str}). Начинаем очистку постов и базы данных...")
        
        async with db_pool.acquire() as conn:
            all_module_tasks = await conn.fetch("SELECT id, message_id, edit_message_ids, subject FROM tasks")
            
            for task in all_module_tasks:
                msg_id = task['message_id']
                edit_ids_str = task['edit_message_ids']
                subj_name = task['subject']
                
                clear_text = f"📦 <b>Архив {current_date_str.split('-')[1]} модуля</b>\n📘 Предмет: {clean_html(subj_name)}\n✓ Обучение по модулю завершено."
                
                if msg_id and msg_id != 0:
                    try:
                        await bot.edit_message_text(
                            text=clear_text, 
                            chat_id=CHANNEL_ID, 
                            message_id=msg_id, 
                            parse_mode=ParseMode.HTML,
                            reply_markup=None
                        )
                        logging.info(f"✓ Схлопнут старый пост задания {msg_id}")
                    except Exception as e:
                        logging.error(f"Не удалось отредактировать старый пост {msg_id}: {e}")
                
                if edit_ids_str:
                    edit_ids = [int(x) for x in edit_ids_str.split(",") if x.strip()]
                    for e_id in edit_ids:
                        try:
                            await bot.edit_message_text(
                                text="🔄 <i>Это уведомление об изменении устарело (модуль завершен).</i>", 
                                chat_id=CHANNEL_ID, 
                                message_id=e_id, 
                                parse_mode=ParseMode.HTML
                            )
                        except Exception:
                            pass

            await conn.execute("DELETE FROM tasks")
            logging.info("🧹 Таблица tasks в базе данных успешно зачищена.")
            
        await update_pinned_post()
        
    except Exception as e:
        logging.error(f"Ошибка в clear_old_deadlines: {e}", exc_info=True)

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
    
    full_text = (
        f"🚨 <b>Важное объявление</b> #инфо\n\n"
        f"{notice_text}\n\n"
        f"Просьба ознакомиться!"
    )
    
    try:
        msg = await bot.send_message(chat_id=CHANNEL_ID, text=full_text, parse_mode=ParseMode.HTML)
        is_pinned = "Да, закрепить" in message.text
        if is_pinned:
            await bot.pin_chat_message(chat_id=CHANNEL_ID, message_id=msg.message_id)
        if db_pool:
            async with db_pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO announcements (title, body, is_pinned, channel_message_id)
                    VALUES ($1, $2, $3, $4)
                    """,
                    "Важное объявление",
                    user_data['text'].strip(),
                    is_pinned,
                    msg.message_id,
                )
        if is_pinned:
            await message.answer("📢 Объявление опубликовано, закреплено и добавлено в CRM!", reply_markup=admin_main_keyboard)
        else:
            await message.answer("📢 Объявление опубликовано и добавлено в CRM!", reply_markup=admin_main_keyboard)
    except Exception as e:
        logging.error(f"Ошибка отправки объявления: {e}")
        await message.answer("❌ Произошла ошибка при отправке сообщения в канал.", reply_markup=admin_main_keyboard)
        
    await state.clear()

# --- ПРОВЕРКА ДЕДЛАЙНОВ ЗА СУТКИ ---
async def check_24h_reminders():
    try:
        now = now_msk()
        deadline_threshold = (now + timedelta(hours=24)).strftime("%Y-%m-%d %H:%M")
        now_str = now.strftime("%Y-%m-%d %H:%M")
        
        async with db_pool.acquire() as conn:
            # 1. Помечаем уже прошедшие дедлайны как notified=1, чтобы не слать напоминания задним числом
            await conn.execute("UPDATE tasks SET notified = 1 WHERE deadline <= $1 AND notified = 0", now_str)
            
            # 2. Выбираем ВСЕ задачи, дедлайн которых наступает в ближайшие 24 часа и уведомление еще не уходило
            reminders = await conn.fetch(
                """
                SELECT id, subject, description, deadline, submit_url, file_id, message_id 
                FROM tasks 
                WHERE deadline <= $1 AND deadline > $2 AND notified = 0 AND COALESCE(is_published, TRUE) = TRUE
                ORDER BY deadline ASC
                """,
                deadline_threshold, now_str
            )
            
            for task in reminders:
                task_id = task['id']
                try:
                    dt_format = datetime.strptime(task['deadline'].strip(), "%Y-%m-%d %H:%M").strftime("%d.%m.%Y %H:%M")
                except Exception:
                    dt_format = task['deadline']
                
                alert_text = (
                    f"🚨 <b>ВНИМАНИЕ! Дедлайн через 24 часа</b> 🚨\n\n"
                    f"📚 <b>Предмет:</b> {clean_html(task['subject'])}\n"
                    f"📝 <b>Что сдать:</b> {clean_html(task['description'])}\n"
                    f"🔥 <b>Время:</b> <code>{dt_format}</code>\n"
                )
                
                kb = None
                url_str = str(task['submit_url'] or "").strip()
                match = re.search(r'(https?://\S+)', url_str)
                if match:
                    actual_url = match.group(1)
                    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📥 Куда сдавать", url=actual_url)]])
                    prefix = url_str[:match.start()].strip(" -:")
                    if prefix:
                        alert_text += f"📥 <b>Куда сдавать:</b> {clean_html(prefix)}\n"
                else:
                    alert_text += f"📥 <b>Куда сдавать:</b> {clean_html(url_str)}\n"

                msg_id = task['message_id']
                if msg_id and msg_id != 0:
                    alert_text += f"\n🔗 <a href='{task_link(msg_id)}'>📎 Открыть исходное задание</a>\n"
                
                try:
                    msg = await bot.send_message(
                        chat_id=CHANNEL_ID,
                        text=alert_text,
                        reply_markup=kb,
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True,
                    )
                    attach_ids = await send_task_attachments(
                        bot, conn, task_id, reply_to_msg_id=msg.message_id
                    )
                    if not attach_ids and task['file_id']:
                        fallback_doc = await bot.send_document(
                            chat_id=CHANNEL_ID,
                            document=task['file_id'],
                            reply_to_message_id=msg.message_id,
                        )
                        attach_ids.append(fallback_doc.message_id)

                    await conn.execute("UPDATE tasks SET notified = 1 WHERE id = $1", task_id)
                    logging.info(f"Успешно отправлено напоминание за 24ч для таски {task_id} (вложений: {len(attach_ids)})")
                    
                    # Напоминание и вложения удалятся в момент дедлайна
                    try:
                        try:
                            dead_dt = datetime.strptime(task['deadline'].strip(), "%Y-%m-%d %H:%M").replace(tzinfo=MSK)
                        except Exception:
                            dead_dt = now.replace(tzinfo=MSK) + timedelta(hours=24)
                        run_date = max(now.replace(tzinfo=MSK) + timedelta(minutes=1), dead_dt)
                        
                        for del_mid in [msg.message_id] + attach_ids:
                            scheduler.add_job(
                                bot.delete_message, 
                                'date', 
                                run_date=run_date, 
                                kwargs={"chat_id": CHANNEL_ID, "message_id": del_mid}
                            )
                        logging.info(f"⏰ Запланировано автоудаление напоминания {msg.message_id} на {run_date}")
                    except Exception as ex:
                        logging.error(f"Не удалось выставить таймер удаления напоминания: {ex}")

                except Exception as e:
                    logging.error(f"Не удалось отправить напоминание для таски {task_id}: {e}")
    except Exception as e:
        logging.error(f"Глобальная ошибка в check_24h_reminders: {e}", exc_info=True)

# --- УВЕДОМЛЕНИЯ О ПАРАХ ЗА 10 МИНУТ ---
async def send_lesson_reminders():
    """За 10 минут до пары отправляет уведомление в канал."""
    try:
        now = now_msk()
        today = now.date()

        async with db_pool.acquire() as conn:
            # 1. Помечаем пары, которые уже полностью завершились ранее, чтобы не слать их задним числом
            await conn.execute(
                """
                UPDATE schedule 
                SET notified = TRUE 
                WHERE (lesson_date < $1 OR (lesson_date = $1 AND end_time <= $2)) 
                  AND notified = FALSE
                """,
                today, now.strftime("%H:%M")
            )

            # 2. Выбираем сегодняшние не отправленные пары
            rows = await conn.fetch(
                """
                SELECT id, lesson_date, start_time, end_time, subject, kind, url
                FROM schedule
                WHERE lesson_date = $1 AND notified = FALSE
                ORDER BY start_time ASC
                """,
                today
            )

            for row in rows:
                try:
                    start_dt = datetime.strptime(
                        f"{row['lesson_date']} {row['start_time']}",
                        "%Y-%m-%d %H:%M"
                    )
                except ValueError:
                    continue

                diff_min = (start_dt - now).total_seconds() / 60

                # Отправляем, если до пары от -5 до 11 минут (за 10 мин до начала или если бот только проснулся)
                if -5.0 <= diff_min <= 11.0:
                    header = "⏰ <b>Через 10 минут начнётся пара!</b>" if diff_min > 0 else "⏰ <b>Пара началась!</b>"
                    subj = row['subject']
                    hashtag = get_hashtag_by_subject(subj)
                    text = (
                        f"{header}\n\n"
                        f"📘 <b>Предмет:</b> {clean_html(subj)}\n"
                        f"🎓 <b>Тип:</b> {clean_html(row['kind'])}\n"
                        f"🕒 <b>Начало:</b> <code>{row['start_time']}</code> – <code>{row['end_time']}</code>\n"
                        f"🔗 <b>Ссылка:</b> <a href='{row['url']}'>Телемост</a>\n\n"
                        f"{hashtag}"
                    )
                    kb = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="🎥 Подключиться", url=row['url'])]
                    ])
                    try:
                        msg = await bot.send_message(
                            chat_id=CHANNEL_ID, text=text,
                            reply_markup=kb, parse_mode=ParseMode.HTML,
                            disable_web_page_preview=True
                        )
                        await conn.execute(
                            "UPDATE schedule SET notified = TRUE, message_id = $1 WHERE id = $2",
                            msg.message_id, row['id']
                        )
                        logging.info(f"Отправлено уведомление о паре: {subj} в {row['start_time']}")
                    except Exception as e:
                        logging.error(f"Ошибка отправки уведомления о паре: {e}")
    except Exception as e:
        logging.error(f"Ошибка в send_lesson_reminders: {e}", exc_info=True)

async def cleanup_lesson_reminders():
    """Удаляет уведомление о паре строго ПОСЛЕ её завершения."""
    try:
        now = now_msk()
        today = now.date()
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, lesson_date, end_time, message_id 
                FROM schedule 
                WHERE notified = TRUE AND message_id > 0 AND lesson_date <= $1
                """,
                today
            )

            for row in rows:
                try:
                    end_dt = datetime.strptime(
                        f"{row['lesson_date']} {row['end_time']}",
                        "%Y-%m-%d %H:%M"
                    )
                except ValueError:
                    continue

                if now >= end_dt:
                    try:
                        await bot.delete_message(chat_id=CHANNEL_ID, message_id=row['message_id'])
                        logging.info(f"Пара завершена. Удалено уведомление (msg_id={row['message_id']})")
                    except Exception as e:
                        logging.error(f"Не удалось удалить уведомление {row['message_id']}: {e}")
                    
                    await conn.execute(
                        "UPDATE schedule SET message_id = 0 WHERE id = $1",
                        row['id']
                    )
    except Exception as e:
        logging.error(f"Ошибка в cleanup_lesson_reminders: {e}", exc_info=True)

# --- УТРЕННЯЯ СВОДКА ---
async def send_daily_morning_summary():
    """Каждое утро (в 10:00 или при старте после 10:00) отправляет сводку пар и дедлайнов на текущий день."""
    try:
        now = now_msk()
        today_date_str = now.strftime("%Y-%m-%d")
        today_display = now.strftime("%d.%m.%Y")
        
        async with db_pool.acquire() as conn:
            # Проверяем, не отправлялась ли уже сводка на сегодня
            already_sent_date = await conn.fetchval(
                "SELECT value FROM bot_metadata WHERE key = 'morning_summary_date'"
            )
            if already_sent_date == today_date_str:
                logging.info(f"Утренняя сводка на {today_date_str} уже была отправлена.")
                return

            # 1. ПОЛУЧАЕМ ПАРЫ НА СЕГОДНЯ
            rows_lessons = await conn.fetch(
                """
                SELECT start_time, end_time, subject, kind, url 
                FROM schedule 
                WHERE lesson_date = $1 
                ORDER BY start_time ASC
                """,
                now.date()
            )
            
            text = f"☀️ <b>Сводка на сегодня ({today_display})</b>\n\n"
            text += "📅 <b>Расписание пар на сегодня:</b>\n"
            if rows_lessons:
                for idx, row in enumerate(rows_lessons, start=1):
                    subj = clean_html(row['subject'])
                    kind = row['kind']
                    start = row['start_time']
                    end = row['end_time']
                    url = row['url']
                    
                    text += f"{idx}. 🕒 <code>{start} - {end}</code> | <b>{subj}</b> ({kind})\n"
                    text += f"   🔗 <a href='{url}'>Подключиться к Телемосту</a>\n"
            else:
                text += "🎉 Пар со ссылками на сегодня нет! Можно отдохнуть.\n"
                
            text += "\n"
            
            # 2. ПОЛУЧАЕМ ДЕДЛАЙНЫ НА СЕГОДНЯ
            today_start = f"{today_date_str} 00:00"
            today_end = f"{today_date_str} 23:59"
            
            rows_tasks = await conn.fetch(
                """
                SELECT description, deadline, message_id 
                FROM tasks 
                WHERE deadline BETWEEN $1 AND $2 
                ORDER BY deadline ASC
                """,
                today_start, today_end
            )
            
            text += "🔥 <b>Дедлайны, которые истекают СЕГОДНЯ:</b>\n"
            if rows_tasks:
                for idx, task in enumerate(rows_tasks, start=1):
                    desc = clean_html(task['description'])
                    dead_raw = task['deadline']
                    msg_id = task['message_id']
                    
                    try:
                        time_str = datetime.strptime(dead_raw.strip(), "%Y-%m-%d %H:%M").strftime("%H:%M")
                    except Exception:
                        time_str = dead_raw
                        
                    text += f"{idx}. 📝 {desc}\n"
                    text += f"   ⏰ <b>Заканчивается в:</b> <code>{time_str}</code>\n"
                    
                    if msg_id and msg_id != 0:
                        text += f"   🔗 <a href='{task_link(msg_id)}'>📎 Перейти к заданию</a>\n"
            else:
                text += "✅ Сегодня ничего сдавать не нужно. Чистый день!\n"
                
            msg_summary = await bot.send_message(chat_id=CHANNEL_ID, text=text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
            logging.info(f"Утренняя сводка успешно отправлена (msg_id={msg_summary.message_id}).")
            
            # Сохраняем в bot_metadata (не трогая schedule.message_id!)
            await conn.execute(
                """
                INSERT INTO bot_metadata (key, value) VALUES ('morning_summary_date', $1)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
                """,
                today_date_str
            )
            await conn.execute(
                """
                INSERT INTO bot_metadata (key, value) VALUES ('morning_summary_msg_id', $1)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
                """,
                str(msg_summary.message_id)
            )
    except Exception as e:
        logging.error(f"Ошибка при формировании утренней сводки: {e}", exc_info=True)

async def cleanup_daily_morning_summary():
    """В конце дня (в 23:59) удаляет утреннюю сводку, чтобы не захламлять ленту."""
    try:
        async with db_pool.acquire() as conn:
            msg_id_val = await conn.fetchval("SELECT value FROM bot_metadata WHERE key = 'morning_summary_msg_id'")
            if msg_id_val:
                try:
                    await bot.delete_message(chat_id=CHANNEL_ID, message_id=int(msg_id_val))
                    logging.info(f"Утренняя сводка (msg_id={msg_id_val}) успешно удалена в конце дня.")
                except Exception as e:
                    logging.error(f"Не удалось удалить утреннюю сводку: {e}")
                await conn.execute("DELETE FROM bot_metadata WHERE key = 'morning_summary_msg_id'")
    except Exception as e:
        logging.error(f"Ошибка в cleanup_daily_morning_summary: {e}", exc_info=True)

_SEEN_MEDIA_GROUPS: set[tuple[int, str, str]] = set()


async def _append_pending_file(
    user_id: int, flow_id: str, file_rec: dict, media_group_id: str | None = None
) -> tuple[int, bool]:
    """Потокобезопасно добавляет файл в буфер (включая альбомы из нескольких файлов)."""
    async with _PENDING_LOCK:
        key = (user_id, flow_id)
        lst = _PENDING_FILES.setdefault(key, [])
        lst.append(file_rec)
        total = len(lst)
        is_first_in_group = True
        if media_group_id:
            mg_key = (user_id, flow_id, str(media_group_id))
            if mg_key in _SEEN_MEDIA_GROUPS:
                is_first_in_group = False
            else:
                _SEEN_MEDIA_GROUPS.add(mg_key)
        return total, is_first_in_group


async def _pop_pending_files(user_id: int, flow_id: str | None) -> list[dict]:
    async with _PENDING_LOCK:
        if not flow_id:
            return []
        for k in [k for k in _SEEN_MEDIA_GROUPS if k[0] == user_id and k[1] == flow_id]:
            _SEEN_MEDIA_GROUPS.discard(k)
        return _PENDING_FILES.pop((user_id, flow_id), [])


async def _clear_user_pending(user_id: int):
    async with _PENDING_LOCK:
        for k in [k for k in _PENDING_FILES if k[0] == user_id]:
            _PENDING_FILES.pop(k, None)
        for k in [k for k in _SEEN_MEDIA_GROUPS if k[0] == user_id]:
            _SEEN_MEDIA_GROUPS.discard(k)


def _build_crm_message(is_admin: bool) -> tuple[str, InlineKeyboardMarkup | None]:
    """Формирует сообщение и кнопки для входа в CRM (Mini App + браузер)."""
    base_url = get_crm_base_url()
    has_https = base_url.startswith("https://")
    rows = []

    if is_admin:
        if has_https:
            rows.append([InlineKeyboardButton(text="📱 Открыть CRM (Mini App)", web_app=WebAppInfo(url=f"{base_url}/crm"))])
            magic = build_admin_magic_link()
            if magic:
                rows.append([InlineKeyboardButton(text="🌐 Открыть в браузере (Автовход)", url=magic)])
            text = (
                "🚀 <b>CRM-панель группы готова!</b>\n\n"
                "• <b>Mini App</b> открывается прямо внутри Telegram без пароля.\n"
                "• <b>В браузере</b> можно открыть с ПК по кнопке автовхода (ссылка действует 7 дней).\n\n"
                "В CRM можно загружать расписание из CSV, крепить много файлов к заданиям, "
                "вести базу текстов и конспектов по предметам и делиться доступом с одногруппниками."
            )
        else:
            port = int(os.environ.get("PORT", 10000))
            local_url = f"{base_url}/crm" if base_url else f"http://localhost:{port}/crm"
            text = (
                "🛠 <b>CRM-панель запущена локально</b>\n\n"
                f"Адрес для браузера: <code>{clean_html(local_url)}</code>\n"
                "После деплоя на Render появится кнопка Mini App и ссылка быстрого автовхода."
            )
    else:
        if has_https:
            rows.append([InlineKeyboardButton(text="📱 Открыть CRM (Mini App)", web_app=WebAppInfo(url=f"{base_url}/crm"))])
            rows.append([InlineKeyboardButton(text="🌐 Открыть в браузере", url=f"{base_url}/crm")])
            text = (
                "🎓 <b>Учебная CRM-панель группы</b>\n\n"
                "Здесь собраны актуальное расписание и ссылки на Телемост, "
                "дедлайны, прикреплённые файлы и конспекты по предметам.\n\n"
                "Через кнопку <b>Mini App</b> вход выполняется автоматически."
            )
        else:
            text = (
                "🎓 <b>Учебная CRM-панель</b> пока работает в локальном режиме. "
                "Актуальные дедлайны и пары публикуются в канале группы."
            )

    kb = InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
    return text, kb


# --- ПАНЕЛЬ УПРАВЛЕНИЯ ЗАДАНИЯМИ ---
@router.message(Command("manage"), F.from_user.id == ADMIN_ID)
@router.message(F.text == '🗂 Управление', F.from_user.id == ADMIN_ID)
async def manage_tasks(message: Message, state: FSMContext):
    await state.clear()
    await _clear_user_pending(message.from_user.id)
    if not db_pool:
        await message.answer("⚠️ База данных сейчас недоступна.")
        return
    async with db_pool.acquire() as conn:
        tasks = await conn.fetch(
            """
            SELECT t.id, t.subject, t.description, t.deadline, t.message_id,
                   (SELECT count(*) FROM task_files tf WHERE tf.task_id = t.id) AS files_count
            FROM tasks t
            ORDER BY t.deadline ASC
            """
        )

        if not tasks:
            await message.answer("В базе данных пока нет заданий.")
            return

        await message.answer("🗂 <b>Список заданий в базе:</b>\nВыберите действие:", parse_mode=ParseMode.HTML)

        for t in tasks:
            t_id = t['id']
            subj = t['subject']
            desc_raw = str(t['description'] or '').strip()
            short_desc = (desc_raw[:75] + "…") if len(desc_raw) > 75 else desc_raw
            dead = t['deadline']
            msg_id = t['message_id']
            f_cnt = t['files_count'] or 0

            try:
                dt = datetime.strptime(dead.strip(), "%Y-%m-%d %H:%M").strftime("%d.%m.%Y %H:%M")
            except Exception:
                dt = dead

            rows = []
            if msg_id and msg_id != 0:
                rows.append([InlineKeyboardButton(text="📎 Перейти к посту", url=task_link(msg_id))])
            rows.append([
                InlineKeyboardButton(text="✏️ Изменить", callback_data=f"edit_{t_id}"),
                InlineKeyboardButton(text="❌ Удалить", callback_data=f"del_{t_id}"),
            ])

            kb = InlineKeyboardMarkup(inline_keyboard=rows)
            await message.answer(
                f"📘 <b>{clean_html(subj)}</b>\n"
                f"📝 {clean_html(short_desc)}\n"
                f"⏰ Дедлайн: {dt}\n"
                f"📎 Файлов: {f_cnt} шт.",
                reply_markup=kb,
                parse_mode=ParseMode.HTML,
            )


# Удаление задания старостой
@router.callback_query(F.data.startswith("del_"), F.from_user.id == ADMIN_ID)
async def delete_task_callback(callback: CallbackQuery):
    task_id = int(callback.data.split("_")[1])
    if db_pool:
        await delete_task_completely(bot, db_pool, task_id)
    await callback.answer("Задание удалено!")
    await callback.message.edit_text("🗑 Задание стёрто из базы и закрепа. Посты и вложения в канале удалены.")


@router.callback_query(F.data.in_({"change_desc", "change_date", "change_url", "change_files"}), F.from_user.id == ADMIN_ID)
async def process_edit_choice(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    if callback.data == "change_files":
        data = await state.get_data()
        task_id = data.get("edit_task_id")
        if not task_id or not db_pool:
            await callback.message.answer("❌ Задание или база данных не найдены.")
            await state.clear()
            return
        flow_id = f"edit_{uuid.uuid4().hex[:8]}"
        await _clear_user_pending(callback.from_user.id)
        async with _PENDING_LOCK:
            _PENDING_FILES[(callback.from_user.id, flow_id)] = []

        async with db_pool.acquire() as conn:
            existing = await conn.fetch(
                "SELECT id, file_name, file_size FROM task_files WHERE task_id = $1 ORDER BY id ASC",
                task_id,
            )

        await state.update_data(edit_flow_id=flow_id, clear_old_files=False)
        files_text = "\n".join(
            f"• {clean_html(r['file_name'])} ({round((r['file_size'] or 0) / 1024)} КБ)"
            for r in existing
        ) or "<i>Пока нет прикреплённых файлов</i>"

        btns = []
        if existing:
            btns.append([InlineKeyboardButton(text="🗑 Удалить старые файлы при сохранении", callback_data="edit_files_clear")])
        btns.append([InlineKeyboardButton(text="✅ Сохранить и обновить пост", callback_data="edit_files_save")])
        await callback.message.answer(
            f"📎 <b>Управление файлами задания</b>\n\n"
            f"<b>Сейчас прикреплено:</b>\n{files_text}\n\n"
            f"Отправь один или несколько файлов (документы, архивы, фото или альбом), чтобы добавить их к заданию. "
            f"Когда закончишь — нажми «✅ Сохранить и обновить пост».",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=btns),
            parse_mode=ParseMode.HTML,
        )
        await state.set_state(EditForm.files)
        return

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


@router.callback_query(EditForm.files, F.data == "edit_files_clear", F.from_user.id == ADMIN_ID)
async def process_edit_files_clear(callback: CallbackQuery, state: FSMContext):
    await state.update_data(clear_old_files=True)
    await callback.answer("Старые файлы будут удалены после сохранения")
    await callback.message.answer(
        "🗑 Старые файлы помечены к удалению. Отправь новые файлы или нажми «✅ Сохранить и обновить пост».",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="✅ Сохранить и обновить пост", callback_data="edit_files_save")]]
        ),
    )


@router.callback_query(EditForm.files, F.data == "edit_files_save", F.from_user.id == ADMIN_ID)
async def process_edit_files_save_cb(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await _finalize_edit_files(callback.message, state, callback.from_user.id)


@router.message(EditForm.files, F.from_user.id == ADMIN_ID)
async def process_edit_files_msg(message: Message, state: FSMContext):
    data = await state.get_data()
    flow_id = data.get("edit_flow_id")
    if not flow_id:
        await state.clear()
        return

    if message.text and message.text.strip().lower() in {"готово", "сохранить", "все", "всё"}:
        await _finalize_edit_files(message, state, message.from_user.id)
        return

    file_rec, err = await _extract_and_download_file(message)
    if err:
        await message.answer(f"⚠️ {err}")
        return
    if not file_rec:
        await message.answer(
            "Пришлите файл/фото или нажмите «✅ Сохранить и обновить пост» (либо /cancel для отмены).",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="✅ Сохранить и обновить пост", callback_data="edit_files_save")]]
            ),
        )
        return

    total, is_first = await _append_pending_file(message.from_user.id, flow_id, file_rec, message.media_group_id)
    if message.media_group_id and not is_first:
        return
    if message.media_group_id:
        await asyncio.sleep(0.6)
        async with _PENDING_LOCK:
            total = len(_PENDING_FILES.get((message.from_user.id, flow_id), []))

    await message.answer(
        f"📎 Принято новых файлов: <b>{total}</b>.\nМожешь прислать ещё или нажать кнопку ниже:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=f"✅ Сохранить и обновить пост (+{total})", callback_data="edit_files_save")]]
        ),
        parse_mode=ParseMode.HTML,
    )


async def _finalize_edit_files(message: Message, state: FSMContext, user_id: int):
    data = await state.get_data()
    task_id = data.get("edit_task_id")
    flow_id = data.get("edit_flow_id")
    clear_old = bool(data.get("clear_old_files"))
    new_files = await _pop_pending_files(user_id, flow_id)
    await state.clear()

    if not task_id or not db_pool:
        await message.answer("⚠️ Ошибка: задание не найдено.", reply_markup=admin_main_keyboard)
        return

    async with db_pool.acquire() as conn:
        if clear_old:
            await conn.execute("DELETE FROM task_files WHERE task_id = $1", task_id)
            await conn.execute("UPDATE tasks SET file_id = NULL WHERE id = $1", task_id)
        for f in new_files:
            await conn.execute(
                """
                INSERT INTO task_files (task_id, file_name, mime_type, file_size, file_data, telegram_file_id, file_kind)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                """,
                task_id,
                f["file_name"],
                f["mime_type"],
                f["file_size"],
                f["file_data"],
                f["telegram_file_id"],
                f["file_kind"],
            )

    await sync_task_attachments_in_channel(bot, db_pool, task_id)
    await message.answer(
        f"✨ Файлы задания обновлены (добавлено: {len(new_files)} шт.), пост в канале и закреп синхронизированы!",
        reply_markup=admin_main_keyboard,
    )


@router.message(EditForm.new_value, F.from_user.id == ADMIN_ID)
async def process_edit_save(message: Message, state: FSMContext):
    user_data = await state.get_data()
    task_id = user_data['edit_task_id']
    field = user_data['edit_field']
    new_text = message.text

    if not new_text:
        await message.answer("Пожалуйста, отправьте текстовое значение:")
        return

    async with db_pool.acquire() as conn:
        old_task_row = await conn.fetchrow(
            "SELECT subject, description, deadline, submit_url, message_id FROM tasks WHERE id = $1",
            task_id,
        )

        if not old_task_row:
            await message.answer("Ошибка: Задание не найдено.", reply_markup=admin_main_keyboard)
            await state.clear()
            return

        old_task_dict = {
            "description": old_task_row['description'],
            "deadline": old_task_row['deadline'],
            "submit_url": old_task_row['submit_url'],
        }

        if field == "deadline":
            try:
                dt = datetime.strptime(new_text.strip(), "%d.%m.%Y %H:%M")
                dt = dt.replace(tzinfo=MSK).replace(tzinfo=None)
                new_text = dt.strftime("%Y-%m-%d %H:%M")
            except ValueError:
                await message.answer("Неверный формат! Введите ДД.ММ.ГГГГ ЧЧ:ММ:")
                return
            await conn.execute("UPDATE tasks SET deadline = $1, notified = 0 WHERE id = $2", new_text, task_id)
        elif field == "description":
            await conn.execute("UPDATE tasks SET description = $1 WHERE id = $2", new_text, task_id)
        elif field == "submit_url":
            await conn.execute("UPDATE tasks SET submit_url = $1 WHERE id = $2", new_text, task_id)

    await state.clear()
    await sync_task_edit_to_channel(bot, db_pool, task_id, old_task_dict, notify_channel=True)
    await message.answer(
        "✨ Изменения успешно сохранены! Пост переписан, закреп обновлён, уведомление отправлено в канал.",
        reply_markup=admin_main_keyboard,
    )
    await check_24h_reminders()


# Нажатие на кнопку «Изменить»
@router.callback_query(F.data.startswith("edit_"), F.from_user.id == ADMIN_ID)
async def edit_task_callback(callback: CallbackQuery, state: FSMContext):
    task_id = int(callback.data.split("_")[1])
    await state.update_data(edit_task_id=task_id)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Изменить описание", callback_data="change_desc")],
        [InlineKeyboardButton(text="⏰ Изменить дату", callback_data="change_date")],
        [InlineKeyboardButton(text="📥 Изменить сдачу", callback_data="change_url")],
        [InlineKeyboardButton(text="📎 Файлы (добавить / заменить)", callback_data="change_files")],
    ])

    await callback.answer()
    await callback.message.answer("Что именно вы хотите изменить в этом задании?", reply_markup=kb)
    await state.set_state(EditForm.choice)


# --- КОМАНДЫ /start, /crm И /cancel ---
@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await _clear_user_pending(message.from_user.id)
    await message.answer(
        "Действие отменено.",
        reply_markup=admin_main_keyboard if message.from_user.id == ADMIN_ID else ReplyKeyboardRemove(),
    )


@router.message(Command("id"))
async def cmd_id(message: Message):
    uname = f"@{clean_html(message.from_user.username)}" if message.from_user.username else "не задан"
    await message.answer(
        f"🆔 Твой Telegram ID: <code>{message.from_user.id}</code>\n"
        f"👤 Username: {uname}",
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("crm"), F.from_user.id == ADMIN_ID)
@router.message(F.text == '🌐 CRM Панель', F.from_user.id == ADMIN_ID)
async def cmd_crm(message: Message, state: FSMContext):
    await state.clear()
    await _clear_user_pending(message.from_user.id)
    text, kb = _build_crm_message(is_admin=True)
    await message.answer(text, reply_markup=kb, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


@router.message(Command("crm"))
async def cmd_crm_non_admin(message: Message, state: FSMContext):
    await state.clear()
    text, kb = _build_crm_message(is_admin=False)
    await message.answer(text, reply_markup=kb, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


@router.message(Command("start"), F.from_user.id == ADMIN_ID)
async def cmd_start_admin(message: Message, state: FSMContext):
    await state.clear()
    await _clear_user_pending(message.from_user.id)
    _, crm_kb = _build_crm_message(is_admin=True)
    await message.answer(
        "👋 Привет, староста! Рад приветствовать тебя в панели управления группой.\n\n"
        "✨ Меню быстрых кнопок внизу экрана. Для управления файлами, расписанием, объявлениями и таблицами открой /crm.",
        reply_markup=admin_main_keyboard,
    )
    if crm_kb:
        await message.answer("📱 Быстрый вход в CRM-панель (вход старосты выполняется автоматически):", reply_markup=crm_kb)


@router.message(Command("start"))
async def cmd_start_user(message: Message, state: FSMContext):
    await state.clear()
    _, crm_kb = _build_crm_message(is_admin=False)
    await message.answer(
        "👋 Привет! Чем могу помочь?\n\n"
        "Открой учебную CRM через кнопку ниже (или кнопку <b>CRM</b> слева от поля ввода) — "
        "там собраны актуальное расписание, ссылки на Телемост и таблицы, важные объявления и задания.",
        reply_markup=crm_kb if crm_kb else ReplyKeyboardRemove(),
        parse_mode=ParseMode.HTML,
    )

# --- ДОБАВЛЕНИЕ ДЕДЛАЙНА ---
@router.message(Command("add"), F.from_user.id == ADMIN_ID)
@router.message(F.text == '➕ Добавить дедлайн', F.from_user.id == ADMIN_ID)
async def start_add(message: Message, state: FSMContext):
    await state.clear()
    await _clear_user_pending(message.from_user.id)
    subjects = DEFAULT_SUBJECTS
    if db_pool:
        async with db_pool.acquire() as conn:
            subjects = await get_all_subjects(conn)
    await state.update_data(allowed_subjects=subjects)
    await message.answer("Выбери название предмета из списка (или напиши своё):", reply_markup=build_subjects_keyboard(subjects))
    await state.set_state(Form.subject)


@router.message(Form.subject)
async def process_subject(message: Message, state: FSMContext):
    if message.text and message.text.startswith("/"):
        return
    subj = (message.text or "").strip()
    if not subj:
        await message.answer("Пожалуйста, выберите или введите название предмета 👇")
        return
    await state.update_data(subject=subj)
    await message.answer("Опиши, что нужно сделать (какая домашка):", reply_markup=ReplyKeyboardRemove())
    await state.set_state(Form.description)


@router.message(Form.description)
async def process_desc(message: Message, state: FSMContext):
    if not message.text:
        await message.answer("Пожалуйста, отправьте текстовое описание задания:")
        return
    await state.update_data(description=message.text)
    await message.answer("Введи дедлайн в формате: ДД.ММ.ГГГГ ЧЧ:ММ\n(Например: 25.11.2026 18:00)")
    await state.set_state(Form.deadline)


@router.message(Form.deadline)
async def process_deadline(message: Message, state: FSMContext):
    try:
        dt = datetime.strptime((message.text or "").strip(), "%d.%m.%Y %H:%M")
        dt = dt.replace(tzinfo=MSK).replace(tzinfo=None)
        flow_id = f"add_{uuid.uuid4().hex[:8]}"
        await _clear_user_pending(message.from_user.id)
        async with _PENDING_LOCK:
            _PENDING_FILES[(message.from_user.id, flow_id)] = []

        await state.update_data(
            deadline=dt.strftime("%Y-%m-%d %H:%M"),
            submit_url="Уточняется (будет добавлено позже)",
            task_flow_id=flow_id,
        )
        await message.answer(
            "📎 Прикрепи <b>один или несколько файлов</b> к этому дедлайну "
            "(документы, фото, архивы или сразу альбом).\n\n"
            "Когда закончишь — нажми кнопку ниже (или напиши <code>готово</code> / <code>нет</code>):",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="⏭ Без файлов / Сохранить", callback_data="task_files_done")]]
            ),
            parse_mode=ParseMode.HTML,
        )
        await state.set_state(Form.file)
    except ValueError:
        await message.answer("Неверный формат даты! Попробуй еще раз (ДД.ММ.ГГГГ ЧЧ:ММ):")


async def _finalize_create_task(message: Message, state: FSMContext, user_id: int):
    data = await state.get_data()
    flow_id = data.get("task_flow_id")
    files = await _pop_pending_files(user_id, flow_id)
    await state.clear()

    if not db_pool:
        await message.answer("⚠️ Ошибка: база данных недоступна.", reply_markup=admin_main_keyboard)
        return

    first_tg_id = next((f["telegram_file_id"] for f in files if f.get("telegram_file_id")), None)
    async with db_pool.acquire() as conn:
        task_id = await conn.fetchval(
            """
            INSERT INTO tasks (subject, description, deadline, submit_url, file_id, message_id)
            VALUES ($1, $2, $3, $4, $5, 0)
            RETURNING id
            """,
            data['subject'],
            data['description'],
            data['deadline'],
            data['submit_url'],
            first_tg_id,
        )
        for f in files:
            await conn.execute(
                """
                INSERT INTO task_files (task_id, file_name, mime_type, file_size, file_data, telegram_file_id, file_kind)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                """,
                task_id,
                f["file_name"],
                f["mime_type"],
                f["file_size"],
                f["file_data"],
                f["telegram_file_id"],
                f["file_kind"],
            )

    await publish_task_to_channel(bot, db_pool, task_id)
    await message.answer(
        f"🎉 Задание успешно добавлено в базу и опубликовано в канал (прикреплено файлов: {len(files)})!",
        reply_markup=admin_main_keyboard,
    )
    await check_24h_reminders()


@router.callback_query(Form.file, F.data == "task_files_done", F.from_user.id == ADMIN_ID)
async def process_file_done_cb(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await _finalize_create_task(callback.message, state, callback.from_user.id)


@router.message(Form.file, F.from_user.id == ADMIN_ID)
async def process_file(message: Message, state: FSMContext):
    data = await state.get_data()
    flow_id = data.get("task_flow_id")
    if not flow_id:
        await state.clear()
        return

    if message.text and message.text.strip().lower() in {"нет", "готово", "сохранить", "пропустить", "все", "всё"}:
        await _finalize_create_task(message, state, message.from_user.id)
        return

    file_rec, err = await _extract_and_download_file(message)
    if err:
        await message.answer(f"⚠️ {err}")
        return
    if not file_rec:
        await message.answer(
            "Пришлите файл/фото или нажмите кнопку «✅ Сохранить задание» (либо /cancel для отмены).",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="✅ Сохранить задание", callback_data="task_files_done")]]
            ),
        )
        return

    total, is_first = await _append_pending_file(message.from_user.id, flow_id, file_rec, message.media_group_id)
    if message.media_group_id and not is_first:
        return
    if message.media_group_id:
        await asyncio.sleep(0.6)
        async with _PENDING_LOCK:
            total = len(_PENDING_FILES.get((message.from_user.id, flow_id), []))

    await message.answer(
        f"📎 Прикреплено файлов: <b>{total}</b>.\nОтправьте ещё файлы или сохраните задание:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=f"✅ Сохранить задание ({total} файл.)", callback_data="task_files_done")]]
        ),
        parse_mode=ParseMode.HTML,
    )

# Keep-alive фоновая задача для предотвращения засыпания бесплатного тарифа Render
async def keep_alive_ping():
    """Пингует /health на собственном Render сервисе, чтобы бесплатный инстанс не уходил в сон."""
    url = get_crm_base_url()
    if not url or "localhost" in url or "127.0.0.1" in url:
        return
    try:
        health_url = f"{url}/health"
        async with aiohttp.ClientSession() as session:
            async with session.get(health_url, timeout=10) as resp:
                logging.info(f"Keep-alive ping to {health_url}: статус {resp.status}")
    except Exception as e:
        logging.warning(f"Keep-alive ping ошибка: {e}")


# --- ЗАПУСК БОТА ---
async def main():
    global db_pool
    dp.include_router(router)

    # 1. Запуск веб-сервера для Render + CRM API и Mini App
    try:
        app = create_web_app(bot, lambda: db_pool)
        runner = web.AppRunner(app)
        await runner.setup()

        port = int(os.environ.get("PORT", 10000))
        site = web.TCPSite(runner, "0.0.0.0", port)
        await site.start()
        logging.info(f"Веб-сервер и CRM запущены на порту {port}")
    except Exception as e:
        logging.error(f"Ошибка запуска веб-сервера: {e}", exc_info=True)

    # 2. Подключение к базе данных Supabase с повторными попытками
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE

    db_pool = None
    for attempt in range(1, 6):
        try:
            logging.info(f"Подключение к Supabase (попытка {attempt}/5)...")
            db_pool = await asyncpg.create_pool(
                user=DB_USER, password=DB_PASSWORD, host=DB_HOST, port=DB_PORT,
                database=DB_NAME, ssl=ssl_context, min_size=1, max_size=5,
                max_inactive_connection_lifetime=60.0, timeout=30, command_timeout=60
            )

            async with db_pool.acquire() as conn:
                await migrate_schema(conn)
                version = await conn.fetchval("SELECT version()")
            logging.info(f"✅ Подключение к Supabase и миграция схемы OK: {version}")
            break
        except Exception as e:
            logging.error(f"❌ Ошибка подключения к БД (попытка {attempt}/5): {e!r}")
            if attempt < 5:
                await asyncio.sleep(5)
            else:
                logging.error("Не удалось подключиться к БД после 5 попыток.")

    # 3. Настройка задач планировщика
    scheduler.add_job(check_24h_reminders, 'interval', minutes=5)
    scheduler.add_job(update_pinned_post, 'interval', minutes=5)
    scheduler.add_job(clear_old_deadlines, 'cron', hour=3, minute=0)
    scheduler.add_job(send_lesson_reminders, 'interval', minutes=1)
    scheduler.add_job(cleanup_lesson_reminders, 'interval', minutes=1)
    scheduler.add_job(send_daily_morning_summary, 'cron', hour=10, minute=0)
    scheduler.add_job(cleanup_daily_morning_summary, 'cron', hour=23, minute=59)
    scheduler.add_job(keep_alive_ping, 'interval', minutes=10)
    scheduler.start()

    logging.info(f"⏰ Планировщик запущен. Внутреннее время: {now_msk()}")

    # 4. Установка кнопки Menu App (Mini App) для всех участников группы (внутри CRM права определяются по ID)
    crm_base = get_crm_base_url()
    try:
        if crm_base.startswith("https://"):
            menu_btn = MenuButtonWebApp(text="CRM", web_app=WebAppInfo(url=f"{crm_base}/crm"))
            await bot.set_chat_menu_button(menu_button=menu_btn)
            await bot.set_chat_menu_button(chat_id=ADMIN_ID, menu_button=menu_btn)
            logging.info(f"📱 MenuButtonWebApp 'CRM' настроен глобально на {crm_base}/crm")
        else:
            await bot.set_chat_menu_button(menu_button=MenuButtonDefault())
    except Exception as e:
        logging.warning(f"Не удалось настроить MenuButton: {e}")

    # Первичный запуск проверок при старте
    if db_pool:
        await update_pinned_post()
        await check_24h_reminders()
        await send_lesson_reminders()
        # Если бот запустился/перезапустился в 10:00 или позже — проверяем и отправляем утреннюю сводку
        if now_msk().hour >= 10:
            await send_daily_morning_summary()

    # 5. Запуск Long Polling с защитой от сбоев и переподключением
    await bot.delete_webhook(drop_pending_updates=False)
    logging.info("🤖 Запуск Polling...")
    while True:
        try:
            await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
        except Exception as e:
            logging.error(f"⚠️ Ошибка в процессе Polling: {e!r}. Переподключение через 5 сек...", exc_info=True)
            await asyncio.sleep(5)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
