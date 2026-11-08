"""Publication dates with explicit precision; never use filesystem timestamps."""
from __future__ import annotations

import calendar
import re
from datetime import date


def normalize_date(value: str) -> str:
    value = str(value or "").strip()
    if not re.fullmatch(r"\d{4}(?:-\d{2}(?:-\d{2})?)?", value):
        return ""
    parts = [int(part) for part in value.split("-")]
    try:
        date(parts[0], parts[1] if len(parts) > 1 else 1, parts[2] if len(parts) > 2 else 1)
    except ValueError:
        return ""
    return value


def date_bounds(value: str) -> tuple[str, str]:
    value = normalize_date(value)
    if not value:
        raise ValueError("时间请输入 YYYY、YYYY-MM 或 YYYY-MM-DD 格式的有效日期")
    parts = [int(part) for part in value.split("-")]
    year, month = parts[0], parts[1] if len(parts) > 1 else 1
    lower = value + ("-01-01" if len(parts) == 1 else "-01" if len(parts) == 2 else "")
    upper = (f"{year:04d}-12-31" if len(parts) == 1 else
             f"{year:04d}-{month:02d}-{calendar.monthrange(year, month)[1]}" if len(parts) == 2 else value)
    return lower, upper


MONTHS = {name.lower(): index for index in range(1, 13)
          for name in (calendar.month_name[index], calendar.month_abbr[index])}
MONTH_PATTERN = "(?:" + "|".join(sorted(MONTHS, key=len, reverse=True)) + r")\.?"


def parse_date(value: str) -> str:
    """Read a date immediately following a publication label."""
    value = value.strip()
    match = re.match(r"(\d{4})(?:[-/.年](\d{1,2})(?:[-/.月](\d{1,2})日?)?)?(?!\d)", value)
    if match:
        return normalize_date("-".join(f"{int(part):0{4 if i == 0 else 2}d}"
                                       for i, part in enumerate(match.groups()) if part is not None))
    for pattern, order in (
        (rf"(\d{{1,2}})\s+({MONTH_PATTERN})\s*,?\s*(\d{{4}})", (2, 1, 0)),
        (rf"({MONTH_PATTERN})\s+(\d{{1,2}})(?:st|nd|rd|th)?\s*,?\s*(\d{{4}})", (2, 0, 1)),
        (rf"({MONTH_PATTERN})\s*,?\s*(\d{{4}})", (1, 0, None)),
    ):
        match = re.match(pattern, value, re.I)
        if match:
            year, month, day = order
            result = f"{int(match[year + 1]):04d}-{MONTHS[match[month + 1].lower().rstrip('.')]:02d}"
            if day is not None:
                result += f"-{int(match[day + 1]):02d}"
            return normalize_date(result)
    return ""


def publication_date(record: dict, front: str, arxiv_id: str = "") -> tuple[str, str]:
    # Crossref created/indexed/deposited are database dates, not publication dates.
    for key in ("published-online", "published-print", "published", "issued"):
        parts = (record.get(key) or {}).get("date-parts") or []
        if parts and isinstance(parts[0], list) and 1 <= len(parts[0]) <= 3:
            values = parts[0]
            if all(isinstance(part, int) and not isinstance(part, bool) for part in values):
                result = normalize_date("-".join(f"{part:0{4 if i == 0 else 2}d}" for i, part in enumerate(values)))
                if result:
                    return result, "arXiv 首次提交时间" if arxiv_id else "Crossref " + key
    if arxiv_id:
        # The PDF margin can show a later revision. The identifier gives the original month.
        match = re.match(r"(?:[a-z.-]+/)?(\d{2})(\d{2})(?:\.|\d)", arxiv_id, re.I)
        if match:
            year = int(match[1])
            result = normalize_date(f"{1900 + year if year >= 91 else 2000 + year:04d}-{match[2]}")
            if result:
                return result, "arXiv 编号中的首次提交年月（仅精确到月）"
        return "", ""
    # Publication statements only; avoid received/accepted dates and reference years.
    front = re.split(r"\b(?:references|bibliography)\b|参考文献", front, flags=re.I)[0]
    label = r"\b(?:(?:first\s+)?published(?:\s+(?:online|date))?(?:\s+on)?|available\s+online|publication\s+date|(?:first\s+)?posted(?:\s+on)?)\b|(?:在线发表|发表|出版|发布)(?:时间|日期)?"
    for match in re.finditer("(?:" + label + r")\s*[:：]?\s*", front, re.I):
        result = parse_date(front[match.end():match.end() + 60])
        if result:
            return result, "PDF 发表时间标注"
    return "", ""
