import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo

MSK = ZoneInfo("Europe/Moscow")


def now_msk() -> datetime:
    """Возвращает текущее московское время без tzinfo (naive MSK)."""
    return datetime.now(MSK).replace(tzinfo=None)


# --- НАСТРОЙКИ TELEGRAM БОТА ---
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8653801306:AAFfKR9d9D8bLYEArAHxov40_bi4b-N9BOM")
CHANNEL_ID = int(os.environ.get("CHANNEL_ID", "-1004330638807"))
ADMIN_ID = int(os.environ.get("ADMIN_ID", "987506862"))
PINNED_MESSAGE_ID = int(os.environ.get("PINNED_MESSAGE_ID", "3"))
CHANNEL_INTERNAL_ID = str(CHANNEL_ID).replace("-100", "")

# --- НАСТРОЙКИ ОБЛАЧНОЙ БАЗЫ SUPABASE ---
DB_USER = os.environ.get("DB_USER", "postgres.tqpaoezbovvanysghfvl")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "/-s56B3sbWw+L&L")
DB_HOST = os.environ.get("DB_HOST", "aws-1-eu-west-1.pooler.supabase.com")
DB_PORT = int(os.environ.get("DB_PORT", "5432"))
DB_NAME = os.environ.get("DB_NAME", "postgres")

# --- НАСТРОЙКИ ДОСТУПА К CRM ---
DEFAULT_ADMIN_PASSWORD = os.environ.get("CRM_PASSWORD", "340568")
DEFAULT_STUDENT_PASSWORD = os.environ.get("STUDENT_PASSWORD", "")

DEFAULT_SUBJECTS = [
    "Молекулярная биология и геномика в растениеводстве",
    "Генетические основы селекционного процесса в растениеводстве и животноводстве",
]


def get_crm_base_url() -> str:
    """Возвращает публичный базовый URL сервиса (без завершающего /)."""
    url = os.environ.get("CRM_BASE_URL") or os.environ.get("RENDER_EXTERNAL_URL") or ""
    return url.strip().rstrip("/")


def clean_html(text) -> str:
    """Экранирует спецсимволы для безопасного рендеринга HTML в Telegram."""
    if text is None:
        return ""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def task_link(message_id: int) -> str:
    """Возвращает ссылку на сообщение в канале."""
    return f"https://t.me/c/{CHANNEL_INTERNAL_ID}/{message_id}"


def format_submit_url(url: str, is_expired: bool = False) -> str:
    """Форматирует строку сдачи (ссылка/почта/текст)."""
    url_str = str(url or "").strip()
    match = re.search(r"(https?://\S+)", url_str)
    if match:
        actual_url = match.group(1)
        prefix = url_str[: match.start()].strip(" -:")
        prefix_text = f"{clean_html(prefix)}: " if prefix else ""
        link_html = f"<a href='{actual_url}'>Ссылка</a>"
        content = f"{prefix_text}{link_html}"
    else:
        content = clean_html(url_str)

    if is_expired:
        return f"📥 <b>Куда сдавать:</b> <s>{content}</s>\n"
    return f"📥 <b>Куда сдавать:</b> {content}\n"


def get_hashtag_by_subject(subj: str) -> str:
    """Подбирает хэштег по названию предмета."""
    s = (subj or "").lower()
    if "молекулярная биология" in s:
        return "#мол_биология"
    if "генетические основы селекц" in s:
        return "#селекция"
    if "семинар наставника" in s:
        return "#наставник"
    if "популяционн" in s:
        return "#популяционная_генетика"
    if "днк-технолог" in s:
        return "#днк_технологии"
    if "фитопатолог" in s:
        return "#фитопатология"
    if "биоинформат" in s:
        return "#биоинформатика"
    if "машинное обучение" in s:
        return "#ml_агро"
    if "секвенирован" in s or "омиксн" in s:
        return "#омикс_данные"
    return "#инфо"
