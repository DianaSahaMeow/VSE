import csv
import datetime
import io
import re

MONTHS_RU = {
    "августа": 8,
    "август": 8,
    "сентября": 9,
    "сентябрь": 9,
    "октября": 10,
    "октябрь": 10,
    "ноября": 11,
    "ноябрь": 11,
    "декабря": 12,
    "декабрь": 12,
    "января": 1,
    "январь": 1,
    "февраля": 2,
    "февраль": 2,
    "феврала": 2,
    "марта": 3,
    "март": 3,
    "апреля": 4,
    "апрель": 4,
    "мая": 5,
    "май": 5,
    "июня": 6,
    "июнь": 6,
    "июля": 7,
    "июль": 7,
}

DAY_COL_MAP = {
    "понедельник": 1,
    "вторник": 3,
    "среда": 5,
    "четверг": 7,
    "пятница": 9,
    "суббота": 11,
}

SKIP_WORDS = ("перерыв", "праздничный", "каникулы", "сессия")


def _decode_csv_bytes(raw_bytes: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "cp1251"):
        try:
            return raw_bytes.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw_bytes.decode("utf-8", errors="replace")


def _detect_delimiter(text: str) -> str:
    sample = "\n".join(text.splitlines()[:15])
    if sample.count(";") > sample.count(","):
        return ";"
    return ","


def _infer_academic_start_year(today: datetime.date | None = None) -> int:
    today = today or datetime.date.today()
    return today.year if today.month >= 8 else today.year - 1


def _extract_lesson_fields(cell: str, teacher: str) -> tuple[str, str, str]:
    """Извлекает тип занятия, название предмета и ссылку на Телемост."""
    lower = cell.lower()
    kind = "Семинар"
    if "лекция" in lower:
        kind = "Лекция"
    elif "экзамен" in lower:
        kind = "Экзамен"
    elif "практика" in lower:
        kind = "Практика"
    elif "встреча" in lower:
        kind = "Встреча"

    url_match = re.search(r"(https?://\S+)", f"{cell} {teacher}")
    url = url_match.group(1).rstrip(".,;)\"'") if url_match else "https://telemost.yandex.ru"

    subj = cell
    subj = re.sub(
        r"(Лекция|Семинары|Семинар|Экзамен|Практика|Организационная встреча в 17\.00)\s*",
        "",
        subj,
        flags=re.IGNORECASE,
    )
    subj = re.sub(r"Ссылка на видеовстречу:.*", "", subj, flags=re.IGNORECASE)
    subj = re.sub(r"https?://\S+", "", subj)
    subj = subj.strip(" .,\n\r\t\"'")
    subj = re.sub(r"\s+", " ", subj)

    if not subj:
        subj = cell.strip()
    return kind, subj, url


def _try_parse_flat_csv(rows: list[list[str]], default_module: int) -> list[dict] | None:
    """Поддержка простого табличного CSV формата: date,start_time,end_time,subject,kind,teacher,url."""
    if not rows:
        return None
    header = [c.strip().lower() for c in rows[0]]
    if not any(k in header for k in ("date", "lesson_date", "дата")):
        return None

    col_idx = {name: i for i, name in enumerate(header)}

    def get_col(row: list[str], names: tuple[str, ...], default: str = "") -> str:
        for n in names:
            idx = col_idx.get(n)
            if idx is not None and idx < len(row):
                return row[idx].strip()
        return default

    parsed = []
    for row in rows[1:]:
        if not row or not any(row):
            continue
        date_str = get_col(row, ("lesson_date", "date", "дата"))
        if not date_str:
            continue
        dt = None
        for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
            try:
                dt = datetime.datetime.strptime(date_str, fmt).date()
                break
            except ValueError:
                continue
        if not dt:
            continue

        start_time = get_col(row, ("start_time", "начало", "start")).replace(".", ":")
        end_time = get_col(row, ("end_time", "конец", "end")).replace(".", ":")
        time_range = get_col(row, ("time", "время"))
        if (not start_time or not end_time) and time_range:
            m = re.match(r"(\d{1,2})[.:](\d{2})\s*-\s*(\d{1,2})[.:](\d{2})", time_range)
            if m:
                start_time = f"{int(m.group(1)):02d}:{m.group(2)}"
                end_time = f"{int(m.group(3)):02d}:{m.group(4)}"
        if not start_time or not end_time:
            continue

        subj = get_col(row, ("subject", "предмет"))
        if not subj:
            continue
        kind = get_col(row, ("kind", "тип"), "Семинар") or "Семинар"
        teacher = get_col(row, ("teacher", "преподаватель"), "")
        url = get_col(row, ("url", "ссылка", "telemost"), "https://telemost.yandex.ru") or "https://telemost.yandex.ru"
        mod_raw = get_col(row, ("module", "модуль"), str(default_module))
        try:
            module_num = int(mod_raw)
        except ValueError:
            module_num = default_module

        parsed.append(
            {
                "module": module_num,
                "lesson_date": dt.isoformat(),
                "start_time": start_time.zfill(5),
                "end_time": end_time.zfill(5),
                "subject": subj,
                "kind": kind,
                "teacher": teacher,
                "url": url,
            }
        )
    return parsed


def parse_schedule_csv(raw_bytes: bytes, default_module: int = 2, start_year: int | None = None) -> list[dict]:
    """Парсит университетскую сетку расписания (CSV) или плоский CSV в список словарей."""
    text = _decode_csv_bytes(raw_bytes)
    delim = _detect_delimiter(text)
    rows = list(csv.reader(io.StringIO(text), delimiter=delim))

    flat_result = _try_parse_flat_csv(rows, default_module=default_module)
    if flat_result is not None:
        return flat_result

    acad_year = start_year or _infer_academic_start_year()
    current_module = default_module
    current_week_days: dict[int, datetime.date] = {}
    parsed: list[dict] = []
    seen_keys: set[tuple[str, str]] = set()

    for row in rows:
        if not row or not any(cell.strip() for cell in row):
            continue

        row_joined = " ".join(row).lower()
        mod_match = re.search(r"([1-4])\s*модуль", row_joined)
        if mod_match and len(" ".join(c.strip() for c in row if c.strip())) < 25:
            current_module = int(mod_match.group(1))
            continue

        # Ищем заголовок недели ("Понедельник, 9 ноября", "Вторник, 10 ноября", ...)
        temp_days: dict[int, datetime.date] = {}
        for col_idx, cell in enumerate(row):
            m = re.search(
                r"(понедельник|вторник|среда|четверг|пятница|суббота)[,\s]+(\d{1,2})\s+([а-яё]+)",
                cell.lower(),
            )
            if not m:
                continue
            day_name, day_num_str, month_name = m.groups()
            month_num = MONTHS_RU.get(month_name)
            if not month_num:
                continue
            year = acad_year if month_num >= 8 else acad_year + 1
            try:
                dt = datetime.date(year, month_num, int(day_num_str))
            except ValueError:
                continue

            # Если стандартная двухколоночная сетка университета (Понедельник -> 1, Вторник -> 3...)
            # сохраняем маппинг, но также проверяем, не сместилась ли колонка
            target_col = DAY_COL_MAP.get(day_name, col_idx)
            temp_days[target_col] = dt

        if temp_days:
            current_week_days = temp_days
            continue

        time_cell = row[0].strip()
        m_time = re.match(r"(\d{1,2})[.:](\d{2})\s*[-–]\s*(\d{1,2})[.:](\d{2})", time_cell)
        if not (m_time and current_week_days):
            continue

        sh, sm, eh, em = m_time.groups()
        start_time = f"{int(sh):02d}:{sm}"
        end_time = f"{int(eh):02d}:{em}"

        for data_col, dt in current_week_days.items():
            if data_col >= len(row):
                continue
            cell = row[data_col].strip()
            teacher = row[data_col + 1].strip() if data_col + 1 < len(row) else ""
            if not cell:
                continue
            if any(skip in cell.lower() for skip in SKIP_WORDS):
                continue

            kind, subj, url = _extract_lesson_fields(cell, teacher)
            key = (dt.isoformat(), start_time)
            if key in seen_keys:
                continue
            seen_keys.add(key)

            parsed.append(
                {
                    "module": current_module,
                    "lesson_date": dt.isoformat(),
                    "start_time": start_time,
                    "end_time": end_time,
                    "subject": subj,
                    "kind": kind,
                    "teacher": re.sub(r"\s+", " ", teacher).strip(),
                    "url": url,
                }
            )

    parsed.sort(key=lambda item: (item["lesson_date"], item["start_time"]))
    return parsed
