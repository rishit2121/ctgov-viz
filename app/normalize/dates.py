"""Parse CT.gov partial dates ("2019", "2019-05", "2019-05-04") without inventing precision."""

from __future__ import annotations

import re
from typing import Any

from app.contracts.trial import DatePrecision, DateValue

_DATE_RE = re.compile(r"^(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?$")


def parse_date_struct(struct: dict[str, Any] | None) -> DateValue:
    if not struct:
        return DateValue()
    raw = struct.get("date")
    kind = struct.get("type")
    if not isinstance(raw, str):
        return DateValue(kind=kind)
    m = _DATE_RE.match(raw.strip())
    if not m:
        return DateValue(raw=raw, kind=kind)
    year, month, day = (int(g) if g else None for g in m.groups())
    if month is not None and not 1 <= month <= 12:
        return DateValue(raw=raw, kind=kind)
    precision: DatePrecision = "day" if day else "month" if month else "year"
    return DateValue(raw=raw, year=year, month=month, day=day, precision=precision, kind=kind)


def months_between(start: DateValue, end: DateValue) -> float | None:
    """Whole-month difference; requires at least month precision on both ends."""
    if start.year is None or end.year is None or start.month is None or end.month is None:
        return None
    months: float = (end.year - start.year) * 12 + (end.month - start.month)
    if start.day and end.day:
        months += (end.day - start.day) / 30.4375
    return round(months, 1)
