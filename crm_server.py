import datetime as dt_mod
import hashlib
import hmac
import io
import json
import logging
import os
import time
from urllib.parse import quote

from aiohttp import web
from aiogram.enums import ParseMode
from aiogram.types import BufferedInputFile
from aiogram.utils.web_app import safe_parse_webapp_init_data

import schedule_parser
from config import (
    ADMIN_ID,
    BOT_TOKEN,
    CHANNEL_ID,
    DEFAULT_ADMIN_PASSWORD,
    DEFAULT_STUDENT_PASSWORD,
    MSK,
    clean_html,
    get_crm_base_url,
    now_msk,
)
from task_sync import (
    delete_task_completely,
    get_all_subjects,
    publish_task_to_channel,
    sync_task_attachments_in_channel,
    sync_task_edit_to_channel,
    update_pinned_post,
)

MAX_UPLOAD_MB = 25
MAX_FILE_BYTES = 15 * 1024 * 1024
SESSION_TTL_SEC = 60 * 60 * 24 * 14  # 14 дней
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_HTML_PATH = os.path.join(_BASE_DIR, "web", "crm.html")
FALLBACK_WEB_HTML_PATH = os.path.join(_BASE_DIR, "crm.html")


def _hmac_secret() -> bytes:
    return hashlib.sha256(f"crm-secret:{BOT_TOKEN}".encode("utf-8")).digest()


def make_signed_token(role: str, ttl_seconds: int = SESSION_TTL_SEC, user_id: int = 0, label: str = "") -> str:
    exp = int(time.time()) + ttl_seconds
    safe_label = label.replace("|", " ").strip()[:40]
    payload = f"{role}|{user_id}|{exp}|{safe_label}"
    sig = hmac.new(_hmac_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload}|{sig}"


def verify_signed_token(token: str) -> tuple[str, int, str] | None:
    if not token or token.count("|") != 4:
        return None
    role, uid_str, exp_str, label, sig = token.split("|", 4)
    if role not in ("admin", "student"):
        return None
    payload = f"{role}|{uid_str}|{exp_str}|{label}"
    expected_sig = hmac.new(_hmac_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected_sig):
        return None
    try:
        exp = int(exp_str)
        uid = int(uid_str)
    except ValueError:
        return None
    if exp < int(time.time()):
        return None
    return role, uid, label


def build_admin_magic_link() -> str | None:
    base = get_crm_base_url()
    if not base:
        return None
    token = make_signed_token("admin", ttl_seconds=60 * 60 * 24 * 7, user_id=ADMIN_ID, label="Староста")
    return f"{base}/crm?token={quote(token, safe='')}"


async def _get_passwords(conn) -> tuple[str, str]:
    rows = await conn.fetch(
        "SELECT key, value FROM bot_metadata WHERE key IN ('crm_admin_password', 'crm_student_password')"
    )
    data = {r["key"]: r["value"] for r in rows}
    admin_pw = data["crm_admin_password"] if data.get("crm_admin_password") else DEFAULT_ADMIN_PASSWORD
    student_pw = data["crm_student_password"] if "crm_student_password" in data else DEFAULT_STUDENT_PASSWORD
    return admin_pw, student_pw


async def _resolve_viewer_auth(request: web.Request, pool) -> dict:
    """Возвращает авторизацию пользователя; если пароль группы пуст, пускает всех в режиме студента."""
    auth = _resolve_auth(request)
    if auth["authenticated"] or not pool:
        return auth
    async with pool.acquire() as conn:
        _, student_pw = await _get_passwords(conn)
    if not student_pw:
        return {
            "authenticated": True,
            "role": "student",
            "user_id": 0,
            "user_name": "Гость (просмотр)",
            "method": "public",
        }
    return auth


def _resolve_auth(request: web.Request) -> dict:
    """Определяет роль пользователя по заголовку Telegram InitData, query dl-токену или cookie."""
    init_data = request.headers.get("X-Telegram-Init-Data") or ""
    if init_data:
        try:
            parsed = safe_parse_webapp_init_data(token=BOT_TOKEN, init_data=init_data)
            if parsed.user:
                uid = parsed.user.id
                name_parts = [parsed.user.first_name or "", parsed.user.last_name or ""]
                full_name = " ".join(p for p in name_parts if p).strip() or parsed.user.username or str(uid)
                role = "admin" if uid == ADMIN_ID else "student"
                return {
                    "authenticated": True,
                    "role": role,
                    "user_id": uid,
                    "user_name": full_name,
                    "method": "telegram",
                }
        except Exception as exc:
            logging.warning(f"Невалидный Telegram initData: {exc}")

    for raw_token, method in (
        (request.query.get("dl", ""), "dl_token"),
        (request.cookies.get("crm_session", ""), "cookie"),
    ):
        if not raw_token:
            continue
        verified = verify_signed_token(raw_token)
        if verified:
            role, uid, label = verified
            return {
                "authenticated": True,
                "role": role,
                "user_id": uid,
                "user_name": label or ("Староста" if role == "admin" else "Студент"),
                "method": method,
            }

    return {"authenticated": False, "role": "guest", "user_id": 0, "user_name": None, "method": None}


def _require_auth(request: web.Request, admin_only: bool = False) -> dict:
    auth = _resolve_auth(request)
    if not auth["authenticated"]:
        raise web.HTTPUnauthorized(
            text=json.dumps({"error": "Требуется авторизация"}, ensure_ascii=False),
            content_type="application/json",
        )
    if admin_only and auth["role"] != "admin":
        raise web.HTTPForbidden(
            text=json.dumps({"error": "Доступ только для старосты"}, ensure_ascii=False),
            content_type="application/json",
        )
    return auth


def _normalize_deadline_input(raw: str) -> str:
    s = (raw or "").strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%d.%m.%Y %H:%M"):
        try:
            dt = dt_mod.datetime.strptime(s, fmt)
            return dt.strftime("%Y-%m-%d %H:%M")
        except ValueError:
            continue
    raise ValueError("Неверный формат дедлайна. Используйте ДД.ММ.ГГГГ ЧЧ:ММ или YYYY-MM-DD HH:MM")


async def _read_multipart(request: web.Request) -> tuple[dict[str, str], list[dict]]:
    """Читает поля формы и загруженные файлы из multipart/form-data."""
    reader = await request.multipart()
    fields: dict[str, str] = {}
    files: list[dict] = []

    while True:
        part = await reader.next()
        if part is None:
            break
        if part.filename:
            raw_name = os.path.basename(part.filename).strip() or "file"
            data = bytearray()
            while True:
                chunk = await part.read_chunk(65536)
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > MAX_FILE_BYTES:
                    raise web.HTTPRequestEntityTooLarge(
                        max_size=MAX_FILE_BYTES,
                        actual_size=len(data),
                        text=json.dumps(
                            {"error": f"Файл «{raw_name}» больше лимита 15 МБ"},
                            ensure_ascii=False,
                        ),
                        content_type="application/json",
                    )
            mime = part.headers.get("Content-Type", "application/octet-stream")
            file_kind = "photo" if mime.startswith("image/") else "document"
            files.append(
                {
                    "file_name": raw_name,
                    "mime_type": mime,
                    "file_size": len(data),
                    "file_data": bytes(data),
                    "file_kind": file_kind,
                }
            )
        else:
            fields[part.name] = (await part.read(decode=True)).decode("utf-8", errors="replace")
    return fields, files


def _json(data, status: int = 200) -> web.Response:
    return web.Response(
        text=json.dumps(data, ensure_ascii=False, default=str),
        status=status,
        content_type="application/json",
    )


async def _serve_file_bytes(bot, row) -> web.Response:
    file_name = row["file_name"] or "file"
    mime_type = row["mime_type"] or "application/octet-stream"
    file_bytes = row["file_data"]

    if file_bytes is None and row["telegram_file_id"]:
        tg_file = await bot.get_file(row["telegram_file_id"])
        bio = io.BytesIO()
        await bot.download_file(tg_file.file_path, destination=bio)
        file_bytes = bio.getvalue()
        if "." not in file_name and tg_file.file_path and "." in tg_file.file_path:
            ext = tg_file.file_path.rsplit(".", 1)[-1]
            file_name = f"{file_name}.{ext}"

    if file_bytes is None:
        return _json({"error": "Содержимое файла не найдено"}, status=404)

    encoded_name = quote(file_name)
    headers = {
        "Content-Disposition": f"attachment; filename=\"file\"; filename*=UTF-8''{encoded_name}",
        "Cache-Control": "private, max-age=3600",
    }
    return web.Response(body=bytes(file_bytes), content_type=mime_type, headers=headers)


def create_web_app(bot, get_db_pool) -> web.Application:
    app = web.Application(client_max_size=MAX_UPLOAD_MB * 1024 * 1024)

    async def handle_root(request: web.Request):
        return web.Response(text="Бот активен и работает 24/7!")

    async def handle_crm_page(request: web.Request):
        magic_token = request.query.get("token", "")
        if magic_token:
            verified = verify_signed_token(magic_token)
            if verified:
                role, uid, label = verified
                session_val = make_signed_token(role, user_id=uid, label=label)
                resp = web.HTTPFound("/crm")
                resp.set_cookie(
                    "crm_session",
                    session_val,
                    max_age=SESSION_TTL_SEC,
                    httponly=True,
                    samesite="Lax",
                )
                return resp

        html_path = WEB_HTML_PATH if os.path.exists(WEB_HTML_PATH) else FALLBACK_WEB_HTML_PATH
        if not os.path.exists(html_path):
            return web.Response(text="CRM интерфейс не найден (web/crm.html)", status=404)
        with open(html_path, "r", encoding="utf-8") as f:
            return web.Response(text=f.read(), content_type="text/html", charset="utf-8")

    # --- AUTH ---
    async def api_me(request: web.Request):
        auth = await _resolve_viewer_auth(request, get_db_pool())
        dl_token = ""
        if auth["authenticated"]:
            dl_token = make_signed_token(
                auth["role"], ttl_seconds=60 * 60 * 6, user_id=auth["user_id"], label=auth["user_name"] or ""
            )
        return _json({**auth, "dl_token": dl_token})

    async def api_tg_session(request: web.Request):
        """Обменивает Telegram initData на HttpOnly cookie + dl_token для скачивания файлов."""
        auth = _resolve_auth(request)
        if not auth["authenticated"]:
            return _json({"error": "Неверная подпись Telegram WebApp"}, status=401)
        session_val = make_signed_token(
            auth["role"], user_id=auth["user_id"], label=auth["user_name"] or ""
        )
        dl_token = make_signed_token(
            auth["role"], ttl_seconds=60 * 60 * 6, user_id=auth["user_id"], label=auth["user_name"] or ""
        )
        resp = _json({**auth, "dl_token": dl_token})
        resp.set_cookie("crm_session", session_val, max_age=SESSION_TTL_SEC, httponly=True, samesite="Lax")
        return resp

    async def api_login(request: web.Request):
        pool = get_db_pool()
        if not pool:
            return _json({"error": "БД ещё подключается"}, status=503)
        body = await request.json()
        password = (body.get("password") or "").strip()

        async with pool.acquire() as conn:
            admin_pw, student_pw = await _get_passwords(conn)

        if password == admin_pw:
            role, label, uid = "admin", "Староста", ADMIN_ID
        elif student_pw and password == student_pw:
            role, label, uid = "student", "Студент группы", 0
        else:
            return _json({"error": "Неверный пароль"}, status=401)

        session_val = make_signed_token(role, user_id=uid, label=label)
        dl_token = make_signed_token(role, ttl_seconds=60 * 60 * 6, user_id=uid, label=label)
        resp = _json({"authenticated": True, "role": role, "user_name": label, "dl_token": dl_token})
        resp.set_cookie("crm_session", session_val, max_age=SESSION_TTL_SEC, httponly=True, samesite="Lax")
        return resp

    async def api_logout(request: web.Request):
        resp = _json({"ok": True})
        resp.del_cookie("crm_session")
        return resp

    # --- BOOTSTRAP ---
    async def api_bootstrap(request: web.Request):
        pool = get_db_pool()
        if not pool:
            return _json({"error": "База данных недоступна"}, status=503)

        auth = await _resolve_viewer_auth(request, pool)
        if not auth["authenticated"]:
            return _json({"error": "Требуется авторизация"}, status=401)

        async with pool.acquire() as conn:
            subjects = await get_all_subjects(conn)

            # Расписание
            sched_rows = await conn.fetch(
                """
                SELECT id, lesson_date, start_time, end_time, subject, kind, url, module, COALESCE(teacher, '') AS teacher
                FROM schedule
                ORDER BY lesson_date ASC, start_time ASC
                """
            )
            schedule_list = [
                {
                    "id": r["id"],
                    "lesson_date": r["lesson_date"].isoformat(),
                    "start_time": r["start_time"],
                    "end_time": r["end_time"],
                    "subject": r["subject"],
                    "kind": r["kind"],
                    "url": r["url"],
                    "module": r["module"] or 1,
                    "teacher": r["teacher"],
                }
                for r in sched_rows
            ]

            # Задания + файлы (без file_data байтов!; черновики видны только старосте)
            if auth["role"] == "admin":
                task_rows = await conn.fetch(
                    """
                    SELECT id, subject, description, deadline, submit_url, message_id,
                           COALESCE(is_published, TRUE) AS is_published
                    FROM tasks
                    ORDER BY deadline ASC
                    """
                )
            else:
                task_rows = await conn.fetch(
                    """
                    SELECT id, subject, description, deadline, submit_url, message_id,
                           COALESCE(is_published, TRUE) AS is_published
                    FROM tasks
                    WHERE COALESCE(is_published, TRUE) = TRUE
                    ORDER BY deadline ASC
                    """
                )
            tfile_rows = await conn.fetch(
                "SELECT id, task_id, file_name, mime_type, file_size, file_kind FROM task_files ORDER BY id ASC"
            )
            files_by_task: dict[int, list[dict]] = {}
            for tf in tfile_rows:
                files_by_task.setdefault(tf["task_id"], []).append(
                    {
                        "id": tf["id"],
                        "file_name": tf["file_name"],
                        "mime_type": tf["mime_type"],
                        "file_size": tf["file_size"] or 0,
                        "file_kind": tf["file_kind"] or "document",
                    }
                )
            tasks_list = [
                {
                    "id": t["id"],
                    "subject": t["subject"],
                    "description": t["description"],
                    "deadline": t["deadline"],
                    "submit_url": t["submit_url"] or "",
                    "message_id": t["message_id"] or 0,
                    "is_published": bool(t["is_published"]),
                    "files": files_by_task.get(t["id"], []),
                }
                for t in task_rows
            ]

            # База материалов + файлы
            mat_rows = await conn.fetch(
                "SELECT id, subject, title, content_text, external_url, created_at FROM materials ORDER BY created_at DESC"
            )
            mfile_rows = await conn.fetch(
                "SELECT id, material_id, file_name, mime_type, file_size FROM material_files ORDER BY id ASC"
            )
            mfiles_by_mat: dict[int, list[dict]] = {}
            for mf in mfile_rows:
                mfiles_by_mat.setdefault(mf["material_id"], []).append(
                    {
                        "id": mf["id"],
                        "file_name": mf["file_name"],
                        "mime_type": mf["mime_type"],
                        "file_size": mf["file_size"] or 0,
                    }
                )
            materials_list = [
                {
                    "id": m["id"],
                    "subject": m["subject"],
                    "title": m["title"],
                    "content_text": m["content_text"] or "",
                    "external_url": m["external_url"] or "",
                    "created_at": m["created_at"].strftime("%d.%m.%Y %H:%M") if m["created_at"] else "",
                    "files": mfiles_by_mat.get(m["id"], []),
                }
                for m in mat_rows
            ]

            ann_rows = await conn.fetch(
                """
                SELECT id, title, body, is_pinned, channel_message_id, created_at
                FROM announcements
                ORDER BY is_pinned DESC, created_at DESC, id DESC
                """
            )
            announcements_list = [
                {
                    "id": a["id"],
                    "title": a["title"] or "",
                    "body": a["body"] or "",
                    "is_pinned": bool(a["is_pinned"]),
                    "channel_message_id": a["channel_message_id"],
                    "created_at": a["created_at"].strftime("%d.%m.%Y %H:%M") if a["created_at"] else "",
                }
                for a in ann_rows
            ]

            table_rows = await conn.fetch(
                """
                SELECT id, title, url, subject, category, description, sort_order, created_at
                FROM tables_links
                ORDER BY sort_order ASC, id DESC
                """
            )
            tables_list = [
                {
                    "id": tb["id"],
                    "title": tb["title"] or "",
                    "url": tb["url"] or "",
                    "subject": tb["subject"] or "",
                    "category": tb["category"] or "",
                    "description": tb["description"] or "",
                    "sort_order": tb["sort_order"] or 0,
                    "created_at": tb["created_at"].strftime("%d.%m.%Y %H:%M") if tb["created_at"] else "",
                }
                for tb in table_rows
            ]

            db_bytes = await conn.fetchval("SELECT pg_database_size(current_database())")
            admin_info = {}
            if auth["role"] == "admin":
                admin_pw, student_pw = await _get_passwords(conn)
                admin_info = {
                    "student_password": student_pw,
                    "admin_password_set": bool(admin_pw),
                    "crm_url": f"{get_crm_base_url()}/crm" if get_crm_base_url() else "/crm",
                }

        dl_token = make_signed_token(
            auth["role"], ttl_seconds=60 * 60 * 6, user_id=auth["user_id"], label=auth["user_name"] or ""
        )
        return _json(
            {
                "auth": {**auth, "dl_token": dl_token},
                "today": now_msk().strftime("%Y-%m-%d"),
                "subjects": subjects,
                "schedule": schedule_list,
                "tasks": tasks_list,
                "materials": materials_list,
                "announcements": announcements_list,
                "tables_links": tables_list,
                "db_size_mb": round((db_bytes or 0) / (1024 * 1024), 2),
                "admin_info": admin_info,
            }
        )

    # --- TASKS & FILES ---
    async def api_create_task(request: web.Request):
        _require_auth(request, admin_only=True)
        pool = get_db_pool()
        fields, files = await _read_multipart(request)

        subject = (fields.get("subject") or "").strip()
        description = (fields.get("description") or "").strip()
        deadline_raw = (fields.get("deadline") or "").strip()
        submit_url = (fields.get("submit_url") or "").strip() or "Уточняется (будет добавлено позже)"
        publish = fields.get("publish", "true").lower() in ("1", "true", "yes", "on")

        if not subject or not description or not deadline_raw:
            return _json({"error": "Заполните предмет, описание и дедлайн"}, status=400)
        try:
            deadline_iso = _normalize_deadline_input(deadline_raw)
        except ValueError as exc:
            return _json({"error": str(exc)}, status=400)

        async with pool.acquire() as conn:
            task_id = await conn.fetchval(
                """
                INSERT INTO tasks (subject, description, deadline, submit_url, file_id, message_id, is_published)
                VALUES ($1, $2, $3, $4, NULL, 0, $5)
                RETURNING id
                """,
                subject,
                description,
                deadline_iso,
                submit_url,
                publish,
            )
            for f in files:
                await conn.execute(
                    """
                    INSERT INTO task_files (task_id, file_name, mime_type, file_size, file_data, file_kind)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    """,
                    task_id,
                    f["file_name"],
                    f["mime_type"],
                    f["file_size"],
                    f["file_data"],
                    f["file_kind"],
                )

        if publish:
            await publish_task_to_channel(bot, pool, task_id)

        return _json({"ok": True, "id": task_id}, status=201)

    async def api_update_task(request: web.Request):
        _require_auth(request, admin_only=True)
        task_id = int(request.match_info["id"])
        pool = get_db_pool()
        body = await request.json()

        subject = (body.get("subject") or "").strip()
        description = (body.get("description") or "").strip()
        deadline_raw = (body.get("deadline") or "").strip()
        submit_url = (body.get("submit_url") or "").strip() or "Уточняется (будет добавлено позже)"
        notify_channel = bool(body.get("notify_channel", True))

        if not subject or not description or not deadline_raw:
            return _json({"error": "Заполните предмет, описание и дедлайн"}, status=400)
        try:
            deadline_iso = _normalize_deadline_input(deadline_raw)
        except ValueError as exc:
            return _json({"error": str(exc)}, status=400)

        async with pool.acquire() as conn:
            old_row = await conn.fetchrow(
                """
                SELECT subject, description, deadline, submit_url, message_id,
                       COALESCE(is_published, TRUE) AS is_published
                FROM tasks WHERE id = $1
                """,
                task_id,
            )
            if not old_row:
                return _json({"error": "Задание не найдено"}, status=404)
            old_dict = dict(old_row)
            was_published = bool(old_row["is_published"])

            deadline_changed = old_dict["deadline"] != deadline_iso
            await conn.execute(
                """
                UPDATE tasks
                SET subject = $1,
                    description = $2,
                    deadline = $3,
                    submit_url = $4,
                    notified = CASE WHEN $5 THEN 0 ELSE notified END
                WHERE id = $6
                """,
                subject,
                description,
                deadline_iso,
                submit_url,
                deadline_changed,
                task_id,
            )

        if not was_published:
            if notify_channel:
                await publish_task_to_channel(bot, pool, task_id)
        else:
            await sync_task_edit_to_channel(bot, pool, task_id, old_dict, notify_channel=notify_channel)
        return _json({"ok": True})

    async def api_add_task_files(request: web.Request):
        _require_auth(request, admin_only=True)
        task_id = int(request.match_info["id"])
        pool = get_db_pool()
        fields, files = await _read_multipart(request)
        if not files:
            return _json({"error": "Не выбрано ни одного файла"}, status=400)

        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT COALESCE(is_published, TRUE) AS is_published FROM tasks WHERE id = $1",
                task_id,
            )
            if not row:
                return _json({"error": "Задание не найдено"}, status=404)
            is_pub = bool(row["is_published"])
            for f in files:
                await conn.execute(
                    """
                    INSERT INTO task_files (task_id, file_name, mime_type, file_size, file_data, file_kind)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    """,
                    task_id,
                    f["file_name"],
                    f["mime_type"],
                    f["file_size"],
                    f["file_data"],
                    f["file_kind"],
                )

        if is_pub:
            if fields.get("sync_channel", "true").lower() in ("1", "true", "yes"):
                await sync_task_attachments_in_channel(bot, pool, task_id)
            else:
                await update_pinned_post(bot, pool)
        return _json({"ok": True})

    async def api_delete_task_file(request: web.Request):
        _require_auth(request, admin_only=True)
        task_id = int(request.match_info["id"])
        file_id = int(request.match_info["fid"])
        pool = get_db_pool()

        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM task_files WHERE id = $1 AND task_id = $2", file_id, task_id)
            is_pub = await conn.fetchval(
                "SELECT COALESCE(is_published, TRUE) FROM tasks WHERE id = $1",
                task_id,
            )

        if is_pub:
            await sync_task_attachments_in_channel(bot, pool, task_id)
        return _json({"ok": True})

    async def api_delete_task(request: web.Request):
        _require_auth(request, admin_only=True)
        task_id = int(request.match_info["id"])
        pool = get_db_pool()
        async with pool.acquire() as conn:
            is_pub = await conn.fetchval(
                "SELECT COALESCE(is_published, TRUE) FROM tasks WHERE id = $1",
                task_id,
            )
            if not is_pub:
                await conn.execute("DELETE FROM tasks WHERE id = $1", task_id)
                return _json({"ok": True})

        await delete_task_completely(bot, pool, task_id)
        return _json({"ok": True})

    async def api_download_task_file(request: web.Request):
        pool = get_db_pool()
        auth = await _resolve_viewer_auth(request, pool)
        if not auth["authenticated"]:
            raise web.HTTPUnauthorized(
                text=json.dumps({"error": "Требуется авторизация"}, ensure_ascii=False),
                content_type="application/json",
            )
        file_id = int(request.match_info["fid"])
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, file_name, mime_type, file_data, telegram_file_id FROM task_files WHERE id = $1",
                file_id,
            )
        if not row:
            return _json({"error": "Файл не найден"}, status=404)
        return await _serve_file_bytes(bot, row)

    async def api_send_file_to_me(request: web.Request):
        """Отправляет файл пользователю в ЛС бота (удобно внутри Telegram Mini App)."""
        auth = _require_auth(request, admin_only=False)
        if not auth["user_id"]:
            return _json({"error": "Доступно при открытии через Telegram"}, status=400)

        kind = request.match_info["kind"]  # task | material
        fid = int(request.match_info["fid"])
        table = "task_files" if kind == "task" else "material_files"
        pool = get_db_pool()

        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT id, file_name, file_data, telegram_file_id FROM {table} WHERE id = $1",
                fid,
            )
            if not row:
                return _json({"error": "Файл не найден"}, status=404)

            media = (
                BufferedInputFile(bytes(row["file_data"]), filename=row["file_name"])
                if row["file_data"] is not None
                else row["telegram_file_id"]
            )
            try:
                msg = await bot.send_document(
                    chat_id=auth["user_id"],
                    document=media,
                    caption=f"📎 {clean_html(row['file_name'])}",
                    parse_mode=ParseMode.HTML,
                )
                if msg.document and not row["telegram_file_id"]:
                    await conn.execute(
                        f"UPDATE {table} SET telegram_file_id = $1 WHERE id = $2",
                        msg.document.file_id,
                        fid,
                    )
            except Exception as exc:
                return _json({"error": f"Не удалось отправить в ЛС (нажмите /start в боте): {exc}"}, status=400)

        return _json({"ok": True})

    # --- SCHEDULE & CSV ---
    async def api_create_lesson(request: web.Request):
        _require_auth(request, admin_only=True)
        pool = get_db_pool()
        body = await request.json()
        try:
            lesson_date = dt_mod.date.fromisoformat((body.get("lesson_date") or "").strip())
        except ValueError:
            return _json({"error": "Неверный формат даты (YYYY-MM-DD)"}, status=400)

        start_time = (body.get("start_time") or "").strip()
        end_time = (body.get("end_time") or "").strip()
        subject = (body.get("subject") or "").strip()
        kind = (body.get("kind") or "Семинар").strip()
        teacher = (body.get("teacher") or "").strip()
        url = (body.get("url") or "https://telemost.yandex.ru").strip()
        module = int(body.get("module") or 2)

        if not start_time or not end_time or not subject:
            return _json({"error": "Укажите дату, время и предмет"}, status=400)

        now = now_msk()
        is_past = (lesson_date < now.date()) or (lesson_date == now.date() and end_time <= now.strftime("%H:%M"))
        async with pool.acquire() as conn:
            lid = await conn.fetchval(
                """
                INSERT INTO schedule (lesson_date, start_time, end_time, subject, kind, url, notified, message_id, module, teacher)
                VALUES ($1, $2, $3, $4, $5, $6, $7, 0, $8, $9)
                RETURNING id
                """,
                lesson_date,
                start_time,
                end_time,
                subject,
                kind,
                url,
                is_past,
                module,
                teacher,
            )
        return _json({"ok": True, "id": lid}, status=201)

    async def api_update_lesson(request: web.Request):
        _require_auth(request, admin_only=True)
        lid = int(request.match_info["id"])
        pool = get_db_pool()
        body = await request.json()

        try:
            lesson_date = dt_mod.date.fromisoformat((body.get("lesson_date") or "").strip())
        except ValueError:
            return _json({"error": "Неверный формат даты (YYYY-MM-DD)"}, status=400)

        start_time = (body.get("start_time") or "").strip()
        end_time = (body.get("end_time") or "").strip()
        subject = (body.get("subject") or "").strip()
        kind = (body.get("kind") or "Семинар").strip()
        teacher = (body.get("teacher") or "").strip()
        url = (body.get("url") or "https://telemost.yandex.ru").strip()
        module = int(body.get("module") or 2)

        now = now_msk()
        is_future = (lesson_date > now.date()) or (lesson_date == now.date() and end_time > now.strftime("%H:%M"))

        async with pool.acquire() as conn:
            res = await conn.execute(
                """
                UPDATE schedule
                SET lesson_date = $1, start_time = $2, end_time = $3, subject = $4,
                    kind = $5, url = $6, module = $7, teacher = $8,
                    notified = CASE WHEN $9 THEN FALSE ELSE notified END
                WHERE id = $10
                """,
                lesson_date,
                start_time,
                end_time,
                subject,
                kind,
                url,
                module,
                teacher,
                is_future,
                lid,
            )
        if res == "UPDATE 0":
            return _json({"error": "Пара не найдена"}, status=404)
        return _json({"ok": True})

    async def api_delete_lesson(request: web.Request):
        _require_auth(request, admin_only=True)
        lid = int(request.match_info["id"])
        pool = get_db_pool()
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM schedule WHERE id = $1", lid)
        return _json({"ok": True})

    async def api_preview_csv(request: web.Request):
        _require_auth(request, admin_only=True)
        pool = get_db_pool()

        content_type = request.headers.get("Content-Type", "")
        if "multipart/form-data" in content_type:
            fields, files = await _read_multipart(request)
            default_mod = int(fields.get("module") or 2)
            raw_bytes = files[0]["file_data"] if files else fields.get("csv_text", "").encode("utf-8")
        else:
            body = await request.json()
            default_mod = int(body.get("module") or 2)
            raw_bytes = (body.get("csv_text") or "").encode("utf-8")

        if not raw_bytes:
            return _json({"error": "Пустой CSV"}, status=400)

        parsed = schedule_parser.parse_schedule_csv(raw_bytes, default_module=default_mod)
        async with pool.acquire() as conn:
            existing_rows = await conn.fetch("SELECT lesson_date, start_time FROM schedule")
            existing_set = {(r["lesson_date"].isoformat(), r["start_time"]) for r in existing_rows}

        new_count = sum(1 for item in parsed if (item["lesson_date"], item["start_time"]) not in existing_set)
        return _json(
            {
                "lessons": parsed,
                "total": len(parsed),
                "new_count": new_count,
                "existing_count": len(parsed) - new_count,
            }
        )

    async def api_import_csv(request: web.Request):
        _require_auth(request, admin_only=True)
        pool = get_db_pool()
        body = await request.json()
        lessons = body.get("lessons") or []
        mode = body.get("mode", "add_missing")  # add_missing | replace_module
        target_module = body.get("target_module")

        if not lessons:
            return _json({"error": "Нет пар для импорта"}, status=400)

        now = now_msk()
        today = now.date()
        now_hm = now.strftime("%H:%M")

        inserted = 0
        updated = 0
        async with pool.acquire() as conn:
            async with conn.transaction():
                if mode == "replace_module" and target_module:
                    await conn.execute("DELETE FROM schedule WHERE module = $1", int(target_module))

                existing_rows = await conn.fetch("SELECT id, lesson_date, start_time FROM schedule")
                existing_map = {(r["lesson_date"].isoformat(), r["start_time"]): r["id"] for r in existing_rows}

                for item in lessons:
                    dt = dt_mod.date.fromisoformat(item["lesson_date"])
                    st = item["start_time"]
                    et = item["end_time"]
                    is_past = (dt < today) or (dt == today and et <= now_hm)
                    mod_val = int(item.get("module") or target_module or 2)
                    key = (item["lesson_date"], st)

                    if key in existing_map:
                        await conn.execute(
                            """
                            UPDATE schedule
                            SET end_time = $1, subject = $2, kind = $3, url = $4, module = $5, teacher = $6
                            WHERE id = $7
                            """,
                            et,
                            item["subject"],
                            item["kind"],
                            item["url"],
                            mod_val,
                            item.get("teacher", ""),
                            existing_map[key],
                        )
                        updated += 1
                    else:
                        await conn.execute(
                            """
                            INSERT INTO schedule (lesson_date, start_time, end_time, subject, kind, url, notified, message_id, module, teacher)
                            VALUES ($1, $2, $3, $4, $5, $6, $7, 0, $8, $9)
                            """,
                            dt,
                            st,
                            et,
                            item["subject"],
                            item["kind"],
                            item["url"],
                            is_past,
                            mod_val,
                            item.get("teacher", ""),
                        )
                        inserted += 1

        return _json({"ok": True, "inserted": inserted, "updated": updated})

    # --- MATERIALS & TEXTS ---
    async def api_create_material(request: web.Request):
        _require_auth(request, admin_only=True)
        pool = get_db_pool()
        fields, files = await _read_multipart(request)

        subject = (fields.get("subject") or "").strip() or "Общее / Организационное"
        title = (fields.get("title") or "").strip()
        content_text = (fields.get("content_text") or "").strip()
        external_url = (fields.get("external_url") or "").strip()

        if not title:
            return _json({"error": "Укажите заголовок материала"}, status=400)

        async with pool.acquire() as conn:
            mid = await conn.fetchval(
                """
                INSERT INTO materials (subject, title, content_text, external_url)
                VALUES ($1, $2, $3, $4)
                RETURNING id
                """,
                subject,
                title,
                content_text,
                external_url,
            )
            for f in files:
                await conn.execute(
                    """
                    INSERT INTO material_files (material_id, file_name, mime_type, file_size, file_data)
                    VALUES ($1, $2, $3, $4, $5)
                    """,
                    mid,
                    f["file_name"],
                    f["mime_type"],
                    f["file_size"],
                    f["file_data"],
                )
        return _json({"ok": True, "id": mid}, status=201)

    async def api_update_material(request: web.Request):
        _require_auth(request, admin_only=True)
        mid = int(request.match_info["id"])
        pool = get_db_pool()
        body = await request.json()

        subject = (body.get("subject") or "").strip() or "Общее / Организационное"
        title = (body.get("title") or "").strip()
        content_text = (body.get("content_text") or "").strip()
        external_url = (body.get("external_url") or "").strip()
        if not title:
            return _json({"error": "Укажите заголовок"}, status=400)

        async with pool.acquire() as conn:
            res = await conn.execute(
                """
                UPDATE materials
                SET subject = $1, title = $2, content_text = $3, external_url = $4
                WHERE id = $5
                """,
                subject,
                title,
                content_text,
                external_url,
                mid,
            )
        if res == "UPDATE 0":
            return _json({"error": "Материал не найден"}, status=404)
        return _json({"ok": True})

    async def api_add_material_files(request: web.Request):
        _require_auth(request, admin_only=True)
        mid = int(request.match_info["id"])
        pool = get_db_pool()
        _, files = await _read_multipart(request)
        if not files:
            return _json({"error": "Выберите файлы"}, status=400)

        async with pool.acquire() as conn:
            for f in files:
                await conn.execute(
                    """
                    INSERT INTO material_files (material_id, file_name, mime_type, file_size, file_data)
                    VALUES ($1, $2, $3, $4, $5)
                    """,
                    mid,
                    f["file_name"],
                    f["mime_type"],
                    f["file_size"],
                    f["file_data"],
                )
        return _json({"ok": True})

    async def api_delete_material_file(request: web.Request):
        _require_auth(request, admin_only=True)
        mid = int(request.match_info["id"])
        fid = int(request.match_info["fid"])
        pool = get_db_pool()
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM material_files WHERE id = $1 AND material_id = $2", fid, mid)
        return _json({"ok": True})

    async def api_delete_material(request: web.Request):
        _require_auth(request, admin_only=True)
        mid = int(request.match_info["id"])
        pool = get_db_pool()
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM materials WHERE id = $1", mid)
        return _json({"ok": True})

    async def api_download_material_file(request: web.Request):
        pool = get_db_pool()
        auth = await _resolve_viewer_auth(request, pool)
        if not auth["authenticated"]:
            raise web.HTTPUnauthorized(
                text=json.dumps({"error": "Требуется авторизация"}, ensure_ascii=False),
                content_type="application/json",
            )
        fid = int(request.match_info["fid"])
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, file_name, mime_type, file_data, telegram_file_id FROM material_files WHERE id = $1",
                fid,
            )
        if not row:
            return _json({"error": "Файл не найден"}, status=404)
        return await _serve_file_bytes(bot, row)

    # --- ANNOUNCEMENTS ---
    def _normalize_url(raw_url: str) -> str:
        val = (raw_url or "").strip()
        if val and not val.startswith(("http://", "https://")):
            return f"https://{val}"
        return val

    async def api_create_announcement(request: web.Request):
        _require_auth(request, admin_only=True)
        pool = get_db_pool()
        body = await request.json()
        title = (body.get("title") or "").strip() or "Важное объявление"
        text_body = (body.get("body") or body.get("text") or "").strip()
        is_pinned = bool(body.get("is_pinned", False))
        send_to_channel = bool(body.get("send_to_channel", False))
        pin_in_channel = bool(body.get("pin_in_channel", False))
        if not text_body:
            return _json({"error": "Введите текст объявления"}, status=400)

        channel_msg_id = None
        if send_to_channel:
            header = clean_html(title) if title else "Важное объявление"
            full_text = (
                f"🚨 <b>{header}</b> #инфо\n\n"
                f"{clean_html(text_body)}\n\n"
                f"Просьба ознакомиться!"
            )
            msg = await bot.send_message(chat_id=CHANNEL_ID, text=full_text, parse_mode=ParseMode.HTML)
            channel_msg_id = msg.message_id
            if pin_in_channel:
                await bot.pin_chat_message(chat_id=CHANNEL_ID, message_id=msg.message_id)

        async with pool.acquire() as conn:
            ann_id = await conn.fetchval(
                """
                INSERT INTO announcements (title, body, is_pinned, channel_message_id)
                VALUES ($1, $2, $3, $4)
                RETURNING id
                """,
                title,
                text_body,
                is_pinned,
                channel_msg_id,
            )
        return _json({"ok": True, "id": ann_id, "channel_message_id": channel_msg_id})

    async def api_update_announcement(request: web.Request):
        _require_auth(request, admin_only=True)
        pool = get_db_pool()
        ann_id = int(request.match_info["id"])
        body = await request.json()
        title = (body.get("title") or "").strip() or "Важное объявление"
        text_body = (body.get("body") or body.get("text") or "").strip()
        is_pinned = bool(body.get("is_pinned", False))
        if not text_body:
            return _json({"error": "Введите текст объявления"}, status=400)
        async with pool.acquire() as conn:
            res = await conn.execute(
                """
                UPDATE announcements
                SET title = $1, body = $2, is_pinned = $3
                WHERE id = $4
                """,
                title,
                text_body,
                is_pinned,
                ann_id,
            )
        if res == "UPDATE 0":
            return _json({"error": "Объявление не найдено"}, status=404)
        return _json({"ok": True})

    async def api_delete_announcement(request: web.Request):
        _require_auth(request, admin_only=True)
        pool = get_db_pool()
        ann_id = int(request.match_info["id"])
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM announcements WHERE id = $1", ann_id)
        return _json({"ok": True})

    # --- TABLES LINKS ---
    async def api_create_table_link(request: web.Request):
        _require_auth(request, admin_only=True)
        pool = get_db_pool()
        body = await request.json()
        title = (body.get("title") or "").strip()
        url = _normalize_url(body.get("url") or "")
        subject = (body.get("subject") or "").strip()
        category = (body.get("category") or "").strip()
        description = (body.get("description") or "").strip()
        try:
            sort_order = int(body.get("sort_order") or 0)
        except (ValueError, TypeError):
            sort_order = 0

        if not title or not url:
            return _json({"error": "Укажите название таблицы и ссылку"}, status=400)

        async with pool.acquire() as conn:
            tid = await conn.fetchval(
                """
                INSERT INTO tables_links (title, url, subject, category, description, sort_order)
                VALUES ($1, $2, $3, $4, $5, $6)
                RETURNING id
                """,
                title,
                url,
                subject,
                category,
                description,
                sort_order,
            )
        return _json({"ok": True, "id": tid})

    async def api_update_table_link(request: web.Request):
        _require_auth(request, admin_only=True)
        pool = get_db_pool()
        tid = int(request.match_info["id"])
        body = await request.json()
        title = (body.get("title") or "").strip()
        url = _normalize_url(body.get("url") or "")
        subject = (body.get("subject") or "").strip()
        category = (body.get("category") or "").strip()
        description = (body.get("description") or "").strip()
        try:
            sort_order = int(body.get("sort_order") or 0)
        except (ValueError, TypeError):
            sort_order = 0

        if not title or not url:
            return _json({"error": "Укажите название таблицы и ссылку"}, status=400)

        async with pool.acquire() as conn:
            res = await conn.execute(
                """
                UPDATE tables_links
                SET title = $1, url = $2, subject = $3, category = $4, description = $5, sort_order = $6
                WHERE id = $7
                """,
                title,
                url,
                subject,
                category,
                description,
                sort_order,
                tid,
            )
        if res == "UPDATE 0":
            return _json({"error": "Таблица не найдена"}, status=404)
        return _json({"ok": True})

    async def api_delete_table_link(request: web.Request):
        _require_auth(request, admin_only=True)
        pool = get_db_pool()
        tid = int(request.match_info["id"])
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM tables_links WHERE id = $1", tid)
        return _json({"ok": True})

    # --- ADMIN TOOLS ---
    async def api_admin_refresh_pin(request: web.Request):
        _require_auth(request, admin_only=True)
        pool = get_db_pool()
        await update_pinned_post(bot, pool)
        return _json({"ok": True})

    async def api_admin_broadcast(request: web.Request):
        _require_auth(request, admin_only=True)
        pool = get_db_pool()
        body = await request.json()
        title = (body.get("title") or "").strip() or "Важное объявление"
        raw_text = (body.get("text") or "").strip()
        pin = bool(body.get("pin", False))
        if not raw_text:
            return _json({"error": "Введите текст объявления"}, status=400)

        full_text = (
            f"🚨 <b>{clean_html(title)}</b> #инфо\n\n"
            f"{clean_html(raw_text)}\n\n"
            f"Просьба ознакомиться!"
        )
        msg = await bot.send_message(chat_id=CHANNEL_ID, text=full_text, parse_mode=ParseMode.HTML)
        if pin:
            await bot.pin_chat_message(chat_id=CHANNEL_ID, message_id=msg.message_id)

        async with pool.acquire() as conn:
            ann_id = await conn.fetchval(
                """
                INSERT INTO announcements (title, body, is_pinned, channel_message_id)
                VALUES ($1, $2, $3, $4)
                RETURNING id
                """,
                title,
                raw_text,
                pin,
                msg.message_id,
            )
        return _json({"ok": True, "message_id": msg.message_id, "announcement_id": ann_id})

    async def api_admin_passwords(request: web.Request):
        _require_auth(request, admin_only=True)
        pool = get_db_pool()
        body = await request.json()
        new_student_pw = (body.get("student_password") or "").strip()
        new_admin_pw = (body.get("admin_password") or "").strip()

        async with pool.acquire() as conn:
            if "student_password" in body:
                await conn.execute(
                    """
                    INSERT INTO bot_metadata (key, value) VALUES ('crm_student_password', $1)
                    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
                    """,
                    new_student_pw,
                )
            if new_admin_pw:
                await conn.execute(
                    """
                    INSERT INTO bot_metadata (key, value) VALUES ('crm_admin_password', $1)
                    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
                    """,
                    new_admin_pw,
                )
        return _json({"ok": True})

    app.router.add_get("/", handle_root)
    app.router.add_get("/health", handle_root)
    app.router.add_get("/crm", handle_crm_page)

    app.router.add_get("/api/me", api_me)
    app.router.add_post("/api/auth/tg-session", api_tg_session)
    app.router.add_post("/api/auth/login", api_login)
    app.router.add_post("/api/auth/logout", api_logout)
    app.router.add_get("/api/bootstrap", api_bootstrap)

    app.router.add_post("/api/tasks", api_create_task)
    app.router.add_put("/api/tasks/{id}", api_update_task)
    app.router.add_post("/api/tasks/{id}/files", api_add_task_files)
    app.router.add_delete("/api/tasks/{id}/files/{fid}", api_delete_task_file)
    app.router.add_delete("/api/tasks/{id}", api_delete_task)
    app.router.add_get("/api/task-files/{fid}", api_download_task_file)

    app.router.add_post("/api/files/{kind}/{fid}/send-to-me", api_send_file_to_me)

    app.router.add_post("/api/schedule", api_create_lesson)
    app.router.add_put("/api/schedule/{id}", api_update_lesson)
    app.router.add_delete("/api/schedule/{id}", api_delete_lesson)
    app.router.add_post("/api/schedule/preview-csv", api_preview_csv)
    app.router.add_post("/api/schedule/import-csv", api_import_csv)

    app.router.add_post("/api/materials", api_create_material)
    app.router.add_put("/api/materials/{id}", api_update_material)
    app.router.add_post("/api/materials/{id}/files", api_add_material_files)
    app.router.add_delete("/api/materials/{id}/files/{fid}", api_delete_material_file)
    app.router.add_delete("/api/materials/{id}", api_delete_material)
    app.router.add_get("/api/material-files/{fid}", api_download_material_file)

    app.router.add_post("/api/announcements", api_create_announcement)
    app.router.add_put("/api/announcements/{id}", api_update_announcement)
    app.router.add_delete("/api/announcements/{id}", api_delete_announcement)

    app.router.add_post("/api/tables", api_create_table_link)
    app.router.add_put("/api/tables/{id}", api_update_table_link)
    app.router.add_delete("/api/tables/{id}", api_delete_table_link)

    app.router.add_post("/api/admin/refresh-pin", api_admin_refresh_pin)
    app.router.add_post("/api/admin/broadcast", api_admin_broadcast)
    app.router.add_post("/api/admin/passwords", api_admin_passwords)

    return app
