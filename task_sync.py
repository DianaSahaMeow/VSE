import logging
import re
from datetime import datetime, timedelta

from aiogram.enums import ParseMode
from aiogram.types import (
    BufferedInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaDocument,
    InputMediaPhoto,
)

from config import (
    CHANNEL_ID,
    DEFAULT_SUBJECTS,
    PINNED_MESSAGE_ID,
    clean_html,
    format_submit_url,
    get_hashtag_by_subject,
    now_msk,
    task_link,
)


async def migrate_schema(conn):
    """Создаёт и обновляет структуру таблиц в Supabase PostgreSQL."""
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            id SERIAL PRIMARY KEY,
            subject TEXT,
            description TEXT,
            deadline TEXT,
            submit_url TEXT,
            file_id TEXT,
            notified INTEGER DEFAULT 0,
            message_id INTEGER DEFAULT 0,
            edit_message_ids TEXT DEFAULT '',
            attachment_msg_ids TEXT DEFAULT ''
        )
        """
    )
    await conn.execute(
        "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS attachment_msg_ids TEXT DEFAULT ''"
    )
    await conn.execute(
        "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS is_published BOOLEAN DEFAULT TRUE"
    )

    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schedule (
            id SERIAL PRIMARY KEY,
            lesson_date DATE NOT NULL,
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL,
            subject TEXT NOT NULL,
            kind TEXT NOT NULL,
            url TEXT NOT NULL,
            notified BOOLEAN DEFAULT FALSE,
            message_id BIGINT DEFAULT 0,
            module INTEGER DEFAULT 1,
            teacher TEXT DEFAULT ''
        )
        """
    )
    await conn.execute(
        "ALTER TABLE schedule ADD COLUMN IF NOT EXISTS teacher TEXT DEFAULT ''"
    )

    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bot_metadata (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """
    )

    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS task_files (
            id SERIAL PRIMARY KEY,
            task_id INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
            file_name TEXT NOT NULL,
            mime_type TEXT DEFAULT 'application/octet-stream',
            file_size INTEGER DEFAULT 0,
            file_data BYTEA,
            telegram_file_id TEXT,
            file_kind TEXT DEFAULT 'document',
            created_at TIMESTAMP DEFAULT NOW()
        )
        """
    )

    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS materials (
            id SERIAL PRIMARY KEY,
            subject TEXT NOT NULL,
            title TEXT NOT NULL,
            content_text TEXT DEFAULT '',
            external_url TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT NOW()
        )
        """
    )

    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS material_files (
            id SERIAL PRIMARY KEY,
            material_id INTEGER REFERENCES materials(id) ON DELETE CASCADE,
            file_name TEXT NOT NULL,
            mime_type TEXT DEFAULT 'application/octet-stream',
            file_size INTEGER DEFAULT 0,
            file_data BYTEA,
            telegram_file_id TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        )
        """
    )

    # Миграция старых одиночных file_id из таблицы tasks в task_files (если ещё не перенесены)
    legacy_rows = await conn.fetch(
        """
        SELECT t.id, t.file_id
        FROM tasks t
        WHERE t.file_id IS NOT NULL AND t.file_id != ''
          AND NOT EXISTS (SELECT 1 FROM task_files tf WHERE tf.task_id = t.id)
        """
    )
    for row in legacy_rows:
        await conn.execute(
            """
            INSERT INTO task_files (task_id, file_name, mime_type, telegram_file_id, file_kind)
            VALUES ($1, $2, 'application/octet-stream', $3, 'document')
            """,
            row["id"],
            f"attachment_{row['id']}",
            row["file_id"],
        )


async def get_all_subjects(conn) -> list[str]:
    """Возвращает объединённый список всех актуальных предметов."""
    subjects = list(DEFAULT_SUBJECTS)
    for table in ("tasks", "schedule", "materials"):
        rows = await conn.fetch(f"SELECT DISTINCT subject FROM {table} WHERE subject IS NOT NULL AND subject != ''")
        for r in rows:
            s = r["subject"].strip()
            if s and s not in subjects:
                subjects.append(s)
    return subjects


def build_submit_kb(submit_url: str | None) -> InlineKeyboardMarkup | None:
    """Строит инлайн-кнопку для сдачи, если в строке есть HTTP(S)-ссылка."""
    url_str = str(submit_url or "").strip()
    match = re.search(r"(https?://\S+)", url_str)
    if match:
        return InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="📥 Куда сдавать", url=match.group(1))]]
        )
    return None


def _format_dt_display(deadline_iso: str) -> str:
    try:
        return datetime.strptime(deadline_iso.strip(), "%Y-%m-%d %H:%M").strftime("%d.%m.%Y %H:%M")
    except Exception:
        return str(deadline_iso)


def build_task_post_text(
    subject: str,
    description: str,
    deadline_iso: str,
    submit_url: str,
    files_count: int = 0,
    is_edited: bool = False,
    old_values: dict | None = None,
) -> str:
    """Формирует HTML-текст поста задания для канала."""
    hashtag = get_hashtag_by_subject(subject)
    if hashtag == "#инфо":
        hashtag = "#дедлайн"

    dt_new = _format_dt_display(deadline_iso)
    header = "🔄 <b>Задание изменено!</b>" if is_edited else "📚 <b>Новое задание в расписании</b>"
    text = f"{header} {hashtag}\n\n📘 <b>Предмет:</b> {clean_html(subject)}\n"

    old_values = old_values or {}
    if old_values.get("description") and old_values["description"] != description:
        text += f"📝 <b>Что сделать:</b> <s>{clean_html(old_values['description'])}</s> ➡️ <b>{clean_html(description)}</b>\n"
    else:
        text += f"📝 <b>Что сделать:</b> {clean_html(description)}\n"

    if old_values.get("deadline") and old_values["deadline"] != deadline_iso:
        dt_old = _format_dt_display(old_values["deadline"])
        text += f"⏰ <b>Сдать до:</b> <s>{dt_old}</s> ➡️ <code>{dt_new}</code>\n"
    else:
        text += f"⏰ <b>Сдать до:</b> <code>{dt_new}</code>\n"

    url_str = str(submit_url or "").strip()
    has_http = bool(re.search(r"https?://\S+", url_str))
    if not has_http:
        if old_values.get("submit_url") and old_values["submit_url"] != url_str:
            text += f"📥 <b>Куда сдавать:</b> <s>{clean_html(old_values['submit_url'])}</s> ➡️ <b>{clean_html(url_str)}</b>\n"
        else:
            text += f"📥 <b>Куда сдавать:</b> {clean_html(url_str)}\n"
    else:
        match = re.search(r"(https?://\S+)", url_str)
        prefix = url_str[: match.start()].strip(" -:")
        if prefix:
            text += f"📥 <b>Куда сдавать:</b> {clean_html(prefix)}\n"

    if files_count > 0:
        text += f"📎 <b>Прикреплено файлов:</b> {files_count} (см. вложение ниже)\n"

    return text


async def _delete_messages_safe(bot, msg_ids_str: str | None):
    if not msg_ids_str:
        return
    for part in str(msg_ids_str).split(","):
        part = part.strip()
        if not part:
            continue
        try:
            await bot.delete_message(chat_id=CHANNEL_ID, message_id=int(part))
        except Exception as exc:
            logging.warning(f"Не удалось удалить сообщение {part}: {exc}")


async def _send_file_batch(bot, conn, file_rows: list, is_photo: bool, reply_to_msg_id: int | None) -> list[int]:
    """Отправляет пачку файлов (до 10 штук в альбоме) и сохраняет telegram_file_id."""
    if not file_rows:
        return []

    sent_ids: list[int] = []
    for i in range(0, len(file_rows), 10):
        chunk = file_rows[i : i + 10]
        if len(chunk) == 1:
            row = chunk[0]
            media = (
                BufferedInputFile(bytes(row["file_data"]), filename=row["file_name"])
                if row["file_data"] is not None
                else row["telegram_file_id"]
            )
            try:
                if is_photo:
                    msg = await bot.send_photo(
                        chat_id=CHANNEL_ID,
                        photo=media,
                        reply_to_message_id=reply_to_msg_id,
                    )
                    tg_id = msg.photo[-1].file_id if msg.photo else None
                else:
                    msg = await bot.send_document(
                        chat_id=CHANNEL_ID,
                        document=media,
                        reply_to_message_id=reply_to_msg_id,
                    )
                    tg_id = msg.document.file_id if msg.document else None
                sent_ids.append(msg.message_id)
                if tg_id and not row["telegram_file_id"]:
                    await conn.execute(
                        "UPDATE task_files SET telegram_file_id = $1 WHERE id = $2",
                        tg_id,
                        row["id"],
                    )
            except Exception as exc:
                logging.error(f"Ошибка отправки одиночного файла {row['id']}: {exc}")
        else:
            media_group = []
            for row in chunk:
                media = (
                    BufferedInputFile(bytes(row["file_data"]), filename=row["file_name"])
                    if row["file_data"] is not None
                    else row["telegram_file_id"]
                )
                if is_photo:
                    media_group.append(InputMediaPhoto(media=media))
                else:
                    media_group.append(InputMediaDocument(media=media))
            try:
                msgs = await bot.send_media_group(
                    chat_id=CHANNEL_ID,
                    media=media_group,
                    reply_to_message_id=reply_to_msg_id,
                )
                for row, msg in zip(chunk, msgs):
                    sent_ids.append(msg.message_id)
                    tg_id = None
                    if msg.document:
                        tg_id = msg.document.file_id
                    elif msg.photo:
                        tg_id = msg.photo[-1].file_id
                    if tg_id and not row["telegram_file_id"]:
                        await conn.execute(
                            "UPDATE task_files SET telegram_file_id = $1 WHERE id = $2",
                            tg_id,
                            row["id"],
                        )
            except Exception as exc:
                logging.error(f"Ошибка отправки альбома файлов: {exc}")
    return sent_ids


async def send_task_attachments(bot, conn, task_id: int, reply_to_msg_id: int | None = None) -> list[int]:
    """Отправляет все прикреплённые к заданию файлы в канал."""
    rows = await conn.fetch(
        """
        SELECT id, file_name, mime_type, file_data, telegram_file_id, file_kind
        FROM task_files
        WHERE task_id = $1
        ORDER BY id ASC
        """,
        task_id,
    )
    if not rows:
        return []

    photo_rows = [r for r in rows if r["file_kind"] == "photo" and r["file_data"] is None]
    doc_rows = [r for r in rows if r not in photo_rows]

    sent_ids: list[int] = []
    if doc_rows:
        sent_ids.extend(await _send_file_batch(bot, conn, doc_rows, is_photo=False, reply_to_msg_id=reply_to_msg_id))
    if photo_rows:
        sent_ids.extend(await _send_file_batch(bot, conn, photo_rows, is_photo=True, reply_to_msg_id=reply_to_msg_id))
    return sent_ids


async def publish_task_to_channel(bot, db_pool, task_id: int) -> int:
    """Публикует задание (текст + все прикреплённые файлы) в канал и обновляет закреп."""
    posted_msg_id = 0
    async with db_pool.acquire() as conn:
        task = await conn.fetchrow(
            "SELECT id, subject, description, deadline, submit_url FROM tasks WHERE id = $1",
            task_id,
        )
        if not task:
            return 0
        files_count = await conn.fetchval("SELECT count(*) FROM task_files WHERE task_id = $1", task_id)

        post_text = build_task_post_text(
            task["subject"],
            task["description"],
            task["deadline"],
            task["submit_url"],
            files_count=files_count,
        )
        kb = build_submit_kb(task["submit_url"])

        try:
            msg = await bot.send_message(
                chat_id=CHANNEL_ID,
                text=post_text,
                reply_markup=kb,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            posted_msg_id = msg.message_id
            attach_ids = await send_task_attachments(bot, conn, task_id, reply_to_msg_id=posted_msg_id)
            attach_str = ",".join(str(x) for x in attach_ids)
            await conn.execute(
                "UPDATE tasks SET message_id = $1, attachment_msg_ids = $2, is_published = TRUE WHERE id = $3",
                posted_msg_id,
                attach_str,
                task_id,
            )
        except Exception as exc:
            logging.error(f"Ошибка публикации задания {task_id} в канал: {exc}", exc_info=True)

    await update_pinned_post(bot, db_pool)
    return posted_msg_id


async def sync_task_attachments_in_channel(bot, db_pool, task_id: int):
    """Переотправляет файлы задания в канал при добавлении/удалении файлов в CRM или боте."""
    async with db_pool.acquire() as conn:
        task = await conn.fetchrow(
            "SELECT id, subject, description, deadline, submit_url, message_id, attachment_msg_ids FROM tasks WHERE id = $1",
            task_id,
        )
        if not task or not task["message_id"]:
            return

        # Удаляем старые сообщения-вложения (если не старше 48ч) и прикрепляем актуальные
        await _delete_messages_safe(bot, task["attachment_msg_ids"])
        files_count = await conn.fetchval("SELECT count(*) FROM task_files WHERE task_id = $1", task_id)
        attach_ids = await send_task_attachments(bot, conn, task_id, reply_to_msg_id=task["message_id"])
        await conn.execute(
            "UPDATE tasks SET attachment_msg_ids = $1 WHERE id = $2",
            ",".join(str(x) for x in attach_ids),
            task_id,
        )

        post_text = build_task_post_text(
            task["subject"],
            task["description"],
            task["deadline"],
            task["submit_url"],
            files_count=files_count,
            is_edited=True,
        )
        kb = build_submit_kb(task["submit_url"])
        try:
            await bot.edit_message_text(
                chat_id=CHANNEL_ID,
                message_id=task["message_id"],
                text=post_text,
                reply_markup=kb,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except Exception:
            try:
                await bot.edit_message_caption(
                    chat_id=CHANNEL_ID,
                    message_id=task["message_id"],
                    caption=post_text[:1024],
                    reply_markup=kb,
                    parse_mode=ParseMode.HTML,
                )
            except Exception as exc:
                logging.warning(f"Не удалось обновить пост задания {task_id}: {exc}")

    await update_pinned_post(bot, db_pool)


async def sync_task_edit_to_channel(
    bot,
    db_pool,
    task_id: int,
    old_task: dict,
    notify_channel: bool = True,
):
    """Синхронизирует отредактированное задание с постом в канале, уведомлением и закрепом."""
    async with db_pool.acquire() as conn:
        new_task = await conn.fetchrow(
            "SELECT id, subject, description, deadline, submit_url, message_id, edit_message_ids FROM tasks WHERE id = $1",
            task_id,
        )
        if not new_task:
            return
        files_count = await conn.fetchval("SELECT count(*) FROM task_files WHERE task_id = $1", task_id)

        msg_id = new_task["message_id"]
        edited_post_text = build_task_post_text(
            new_task["subject"],
            new_task["description"],
            new_task["deadline"],
            new_task["submit_url"],
            files_count=files_count,
            is_edited=True,
            old_values=old_task,
        )
        kb = build_submit_kb(new_task["submit_url"])

        if msg_id and msg_id != 0:
            try:
                await bot.edit_message_text(
                    chat_id=CHANNEL_ID,
                    message_id=msg_id,
                    text=edited_post_text,
                    reply_markup=kb,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
            except Exception as exc:
                if "no text in the message" in str(exc).lower():
                    try:
                        await bot.edit_message_caption(
                            chat_id=CHANNEL_ID,
                            message_id=msg_id,
                            caption=edited_post_text[:1024],
                            reply_markup=kb,
                            parse_mode=ParseMode.HTML,
                        )
                    except Exception as cap_exc:
                        logging.error(f"Не удалось изменить caption поста {msg_id}: {cap_exc}")
                else:
                    logging.error(f"Не удалось изменить пост {msg_id} в ленте канала: {exc}")

        if notify_channel:
            hashtag = get_hashtag_by_subject(new_task["subject"])
            if hashtag == "#инфо":
                hashtag = "#дедлайн"

            changes_lines = []
            if old_task.get("description") != new_task["description"]:
                changes_lines.append(
                    f"❌ <s>📝 <b>Было:</b> {clean_html(old_task.get('description'))}</s>\n"
                    f"✅ 📝 <b>Стало:</b> {clean_html(new_task['description'])}"
                )
            if old_task.get("deadline") != new_task["deadline"]:
                dt_old = _format_dt_display(old_task.get("deadline", ""))
                dt_new = _format_dt_display(new_task["deadline"])
                changes_lines.append(
                    f"❌ <s>⏰ <b>Сдать до было:</b> {dt_old}</s>\n"
                    f"✅ ⏰ <b>Сдать до стало:</b> <code>{dt_new}</code>"
                )
            if old_task.get("submit_url") != new_task["submit_url"]:
                changes_lines.append(
                    f"❌ <s>📥 <b>Сдача была:</b> {clean_html(old_task.get('submit_url'))}</s>\n"
                    f"✅ 📥 <b>Сдача стала:</b> {clean_html(new_task['submit_url'])}"
                )

            if changes_lines:
                alert_text = (
                    f"🔔 <b>Внимание! Задание изменено</b> {hashtag}\n\n"
                    f"📚 <b>Предмет:</b> {clean_html(new_task['subject'])}\n"
                    + "\n".join(changes_lines)
                    + "\n"
                )
                if msg_id and msg_id != 0:
                    alert_text += f"\n🔗 <a href='{task_link(msg_id)}'>📎 Перейти к заданию</a>\n"
                alert_text += "\n📋 Изменения внесены в закрепленный пост группы!"

                try:
                    msg_alert = await bot.send_message(
                        chat_id=CHANNEL_ID, text=alert_text, parse_mode=ParseMode.HTML
                    )
                    current_edits = new_task["edit_message_ids"] or ""
                    new_edits = f"{current_edits},{msg_alert.message_id}" if current_edits else str(msg_alert.message_id)
                    await conn.execute("UPDATE tasks SET edit_message_ids = $1 WHERE id = $2", new_edits, task_id)
                except Exception as exc:
                    logging.error(f"Ошибка отправки уведомления об изменении: {exc}")

    change_info = {
        "task_id": task_id,
        "old_desc": old_task.get("description", ""),
        "old_dead": _format_dt_display(old_task.get("deadline", "")),
        "old_url": old_task.get("submit_url", ""),
        "new_desc": new_task["description"],
        "new_dead": new_task["deadline"],
        "new_url": new_task["submit_url"],
    }
    await update_pinned_post(bot, db_pool, change_info=change_info if notify_channel else None)


async def delete_task_completely(bot, db_pool, task_id: int):
    """Удаляет задание, все связанные сообщения в канале, файлы и обновляет закреп."""
    async with db_pool.acquire() as conn:
        res = await conn.fetchrow(
            "SELECT message_id, edit_message_ids, attachment_msg_ids FROM tasks WHERE id = $1",
            task_id,
        )
        if res:
            if res["message_id"] and res["message_id"] != 0:
                await _delete_messages_safe(bot, str(res["message_id"]))
            await _delete_messages_safe(bot, res["edit_message_ids"])
            await _delete_messages_safe(bot, res["attachment_msg_ids"])
        await conn.execute("DELETE FROM tasks WHERE id = $1", task_id)

    await update_pinned_post(bot, db_pool)


async def update_pinned_post(bot, db_pool, change_info: dict | None = None):
    """Обновляет закреплённый пост актуальных дедлайнов в канале."""
    try:
        text = "📌 <b>Актуальные дедлайны</b> 📌\n\n"
        now = now_msk()
        two_weeks_ago_str = (now - timedelta(days=14)).strftime("%Y-%m-%d %H:%M")

        async with db_pool.acquire() as conn:
            db_subjects = await conn.fetch(
                "SELECT DISTINCT subject FROM tasks WHERE deadline >= $1 AND COALESCE(is_published, TRUE) = TRUE",
                two_weeks_ago_str,
            )
            extra_subjects = [r["subject"] for r in db_subjects if r["subject"] not in DEFAULT_SUBJECTS]
            subjects_to_display = list(DEFAULT_SUBJECTS) + extra_subjects

            for subj_name in subjects_to_display:
                hashtag = get_hashtag_by_subject(subj_name)
                if hashtag == "#инфо":
                    hashtag = "#дедлайн"

                text += f"📘 <b>Предмет:</b> {clean_html(subj_name)} {hashtag}\n\n"

                subj_tasks = await conn.fetch(
                    """
                    SELECT t.id, t.description, t.deadline, t.submit_url, t.message_id,
                           (SELECT count(*) FROM task_files tf WHERE tf.task_id = t.id) AS files_count
                    FROM tasks t
                    WHERE t.subject = $1 AND t.deadline >= $2 AND COALESCE(t.is_published, TRUE) = TRUE
                    ORDER BY t.deadline ASC
                    """,
                    subj_name,
                    two_weeks_ago_str,
                )

                if not subj_tasks:
                    text += "🔸 📝 <b>Что сделать:</b> Активных заданий нет 🎉\n"
                    text += "‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾\n\n"
                    continue

                for idx, task in enumerate(subj_tasks, start=1):
                    try:
                        t_id = task["id"]
                        desc = clean_html(task["description"])
                        dead_raw = task["deadline"]
                        url = task["submit_url"]
                        msg_id = task["message_id"]
                        f_cnt = task["files_count"] or 0

                        task_deadline = datetime.strptime(dead_raw.strip(), "%Y-%m-%d %H:%M")
                        dt = task_deadline.strftime("%d.%m.%Y %H:%M")
                        is_expired = task_deadline < now

                        if change_info and t_id == change_info.get("task_id"):
                            text += "🔄 <b>ЗАДАНИЕ ИЗМЕНЕНО СТАРОСТОЙ:</b>\n"
                            old_desc = change_info.get("old_desc", "")
                            old_dead = change_info.get("old_dead", "")
                            old_url = change_info.get("old_url", "")

                            if old_desc != task["description"]:
                                text += f"{idx}. 📝 <b>Что сделать:</b> <s>{clean_html(old_desc)}</s> ➡️ <b>{desc}</b>\n"
                            else:
                                text += f"{idx}. 📝 <b>Что сделать:</b> {desc}\n"

                            if old_dead != dt:
                                text += f"⏰ <b>Сдать до:</b> <s>{old_dead}</s> ➡️ <code>{dt}</code>\n"
                            else:
                                text += f"⏰ <b>Сдать до:</b> <code>{dt}</code>\n"

                            if old_url != url:
                                formatted_new = format_submit_url(url, is_expired=False).replace(
                                    "📥 <b>Куда сдавать:</b> ", ""
                                )
                                text += f"📥 <b>Куда сдавать:</b> <s>{clean_html(old_url)}</s> ➡️ {formatted_new}"
                            else:
                                text += format_submit_url(url, is_expired=False)
                        else:
                            if is_expired:
                                text += "❌ <b>(Дедлайн прошел)</b>\n"
                                text += f"{idx}. 📝 <b>Что сделать:</b> <s>{desc}</s>\n"
                                text += f"⏰ <b>Сдать до:</b> <s>{dt}</s>\n"
                                text += format_submit_url(url, is_expired=True)
                            else:
                                text += f"{idx}. 📝 <b>Что сделать:</b> {desc}\n"
                                text += f"⏰ <b>Сдать до:</b> <code>{dt}</code>\n"
                                text += format_submit_url(url, is_expired=False)

                        if f_cnt > 0:
                            text += f"📎 <b>Файлов:</b> {f_cnt} шт.\n"
                        if msg_id and msg_id != 0:
                            text += f"🔗 <a href='{task_link(msg_id)}'>📎 Открыть задание</a>\n"
                        text += "— — — — — — — — — — — — — —\n"
                    except Exception as exc:
                        logging.error(f"Ошибка форматирования строки в закрепе: {exc}")

                text += "\n"

        if len(text) > 4090:
            logging.warning(f"Закрепленный пост слишком длинный ({len(text)} символов), сокращаем...")
            text = text[:4085] + "\n..."

        try:
            await bot.edit_message_text(
                text=text,
                chat_id=CHANNEL_ID,
                message_id=PINNED_MESSAGE_ID,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except Exception as exc:
            if "message is not modified" in str(exc):
                logging.info("Закрепленный пост проверен: изменений нет.")
            else:
                logging.error(f"Ошибка обновления закрепа: {exc}")
    except Exception as exc:
        logging.error(f"Глобальная ошибка в update_pinned_post: {exc}", exc_info=True)
