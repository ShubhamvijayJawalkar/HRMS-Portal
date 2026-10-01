"""Holiday calendar maintenance (FR-HOL-01/02).

The `holidays` table is the reference data every attendance classification, leave
eligibility check and payroll calendar reads, so the rules about *what may
exist* in it matter more than the routes that write to it. The SRS asks for
"CRUD (HR/Admin, holidays permission), duplicate (name, date) per location
prevented by a unique constraint; copy year-to-year with Feb-29 handling;
import/export; iCal feed".

Three decisions here are not obvious and are recorded in place rather than left
for someone to rediscover:

**The duplicate is per location, and a NULL location is a real value.** The SRS
says "duplicate (name, date) *per location*", so the same named holiday on the
same day in two locations is legitimate, while two org-wide (NULL location)
holidays with the same name and date are not. A plain
``UNIQUE (name, holiday_date, location)`` gets that *wrong*: in SQL, NULL is
distinct from NULL, so it would happily accept any number of duplicate org-wide
holidays. The constraint is therefore on ``COALESCE(location, '')`` (Alembic
0007), and :func:`duplicate_key` builds the same triple so the application and
the index agree on what "the same holiday" means.

**A Feb-29 holiday is skipped on copy, not shifted.** Copying year-to-year has
to decide what a 29 February holiday becomes in a non-leap year, and the
tempting answers are both wrong: placing it on 28 February silently invents a
company holiday on a day nobody agreed to, and placing it on 1 March is worse
(the date means nothing then). A holiday named for the 29th is an observance of
that date, so when the date does not exist the honest result is to leave it out
and *report* it, and let an admin add it deliberately. :func:`copy_year` returns
the skipped ones by name so the response can name them rather than quietly
dropping two of five holidays.

**Copy is idempotent per the same duplicate rule.** Re-running a copy skips what
is already there and says so in the response, so a retried request converges
instead of producing 409s or duplicate rows.

This module takes plain values so it is testable without a request context; the
routes own the connection and the writes.
"""

from __future__ import annotations

import calendar
import csv
import io
from datetime import date, datetime, timedelta, timezone

TYPES = ('National', 'Optional')
MAX_NAME = 120
MAX_LOCATION = 80
MAX_IMPORT_ROWS = 1000
MAX_IMPORT_BYTES = 2 * 1024 * 1024

EXPORT_COLUMNS = ('name', 'date', 'type', 'location')
IMPORT_COLUMNS = ('name', 'date', 'type', 'location')

# VEVENT requires a stable, unique identifier. The iCal spec is explicit that the
# UID must not change when an event's properties change, so it is derived from the
# holiday's identity and never from its name or date.
ICS_PRODID = '-//HRMS Portal//Holiday Calendar//EN'


class HolidayError(ValueError):
    """A holiday payload is not valid or not permitted."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _as_date(value):
    if value in (None, ''):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    for fmt in ('%Y-%m-%d', '%d-%m-%Y', '%d/%m/%Y', '%Y/%m/%d'):
        try:
            return datetime.strptime(str(value).strip()[:10], fmt).date()
        except ValueError:
            continue
    return None


def _clean(value, limit):
    """Trim, and refuse a value that is only whitespace or over-long."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if len(text) > limit:
        raise HolidayError(f'Value is too long (max {limit} characters)')
    return text


def check_payload(payload: dict, existing_holiday_id=None) -> tuple:
    """Validate a create/update body. Returns ``(name, holiday_date, type, location)``.

    ``existing_holiday_id`` lets an update reuse its own row without tripping the
    duplicate check, since an update that changes nothing must not collide with
    itself.
    """
    if not isinstance(payload, dict):
        raise HolidayError('A JSON object is required')
    unknown = set(payload) - {'name', 'date', 'type', 'location'}
    if unknown:
        raise HolidayError(f'Unknown field(s): {", ".join(sorted(unknown))}')

    name = _clean(payload.get('name'), MAX_NAME)
    if not name:
        raise HolidayError('name is required')
    when = _as_date(payload.get('date'))
    if when is None:
        raise HolidayError('date is required and must be a real date (YYYY-MM-DD)')
    htype = (payload.get('type') or 'National')
    if htype not in TYPES:
        raise HolidayError(f'type must be one of {", ".join(TYPES)}')
    location = _clean(payload.get('location'), MAX_LOCATION)
    # Case is not the discriminator here; a location is a label that an admin
    # retypes, and a second "Mumbai" differing only in case is a mistake worth
    # catching rather than a second location.
    if location is not None:
        location = ' '.join(location.split())
    return name, when, htype, location


def duplicate_key(name: str, holiday_date, location):
    """The triple the unique index is built on, for a comparable check.

    Mirrors ``COALESCE(location, '')`` so the application and the database index
    define "the same holiday" identically.
    """
    return name, holiday_date, (location or '').strip().lower()


def is_leap(year: int) -> bool:
    return calendar.isleap(year)


def copy_plan(holiday_rows, target_year: int):
    """Decide what a year-to-year copy would create, and what it would skip.

    ``holiday_rows`` is ``(name, holiday_date, type, location)`` from the source
    year. Returns ``(plan, skipped)`` where each plan entry is
    ``(name, new_date, type, location)``.

    The Feb-29 rule lives here rather than in the route so it is stated once:
    a holiday whose date does not exist in the target year is **skipped and
    reported**, never shifted. A fixed-date holiday (1 January) moves to the same
    month and day in the target year, which is what a copy is for.
    """
    plan, skipped = [], []
    for name, when, htype, location in holiday_rows:
        try:
            month, day = when.month, when.day
        except AttributeError:
            skipped.append((name, str(when), 'unreadable date'))
            continue
        if month == 2 and day == 29 and not is_leap(target_year):
            skipped.append((
                name,
                f'{date(when.year, 2, 29).isoformat()} does not exist in {target_year}',
                '29 February is not a date in a non-leap year, so it is not shifted '
                'onto another day',
            ))
            continue
        plan.append((name, date(target_year, month, day), htype, location))
    return plan, skipped


def parse_csv(text: str):
    """Parse a holiday CSV. Returns ``(rows, errors)`` with spreadsheet row numbers.

    Reported rather than raised per row: a calendar import is usually mostly good,
    and an admin needs to know which lines failed and why, not a single abort that
    loses the rest.
    """
    rows, errors = [], []
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise HolidayError('The CSV has no header row', 400)
    headers = {name.strip().lower(): name for name in reader.fieldnames if name}
    missing = [c for c in IMPORT_COLUMNS if c not in headers]
    if missing:
        raise HolidayError(
            f'CSV header must include {", ".join(IMPORT_COLUMNS)} '
            f'(missing: {", ".join(missing)})', 400)
    for number, raw in enumerate(reader, start=2):  # row 1 is the header
        if number > MAX_IMPORT_ROWS + 1:
            errors.append((number, f'the file has more than {MAX_IMPORT_ROWS} rows'))
            break
        if not any((raw.get(original) or '').strip() for original in headers.values()):
            continue  # a blank line, not a failed row
        body = {key: raw.get(original) for key, original in headers.items()}
        try:
            rows.append((number, check_payload(body)))
        except HolidayError as exc:
            errors.append((number, str(exc)))
    return rows, errors


def to_csv(holiday_rows) -> str:
    """Render holidays as CSV, with the same headers :func:`parse_csv` accepts."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator='\n')
    writer.writerow(EXPORT_COLUMNS)
    for name, when, htype, location in holiday_rows:
        writer.writerow([name, when.isoformat() if hasattr(when, 'isoformat') else when,
                         htype, location or ''])
    return out.getvalue()


def _ics_escape(text: str) -> str:
    return (str(text).replace('\\', '\\\\').replace(';', r'\;')
            .replace(',', r'\,').replace('\n', r'\n'))


# RFC 5545 §3.1: a content line is at most 75 octets, excluding the line break,
# and anything longer is split with a continuation line starting with one space.
# A holiday name is user input, so a long one is not an edge case to be hoped away
# — and a client that receives an over-long line may silently drop the property
# (the calendar simply has no holidays in it) rather than reporting an error.
_ICS_MAX_OCTETS = 75


def _ics_fold(line: str) -> list[str]:
    """Fold one content line to 75 octets with RFC 5545 continuations.

    Folds on octets, not characters, and never splits a multi-byte sequence: a
    name in a non-Latin script would otherwise be cut mid-character and the line
    would be undecodable, which is worse than an over-long line.
    """
    encoded = line.encode('utf-8')
    if len(encoded) <= _ICS_MAX_OCTETS:
        return [line]
    chunks, current, used = [], [], 0
    # The first line has a 75-octet budget; each continuation spends one octet on
    # the leading space, so it has 74.
    budget = _ICS_MAX_OCTETS
    for char in line:
        size = len(char.encode('utf-8'))
        if used + size > budget:
            chunks.append(''.join(current))
            current, used, budget = [], 0, _ICS_MAX_OCTETS - 1
        current.append(char)
        used += size
    if current:
        chunks.append(''.join(current))
    return [chunks[0]] + [' ' + chunk for chunk in chunks[1:]]


def to_ics(holiday_rows, calendar_name='HRMS Holidays') -> str:
    """Render holidays as an RFC 5545 iCalendar feed.

    **All-day events use ``DTSTART;VALUE=DATE``** — a bare ``YYYYMMDD``, with no
    time and no ``Z``. This is the detail that matters: an all-day event given a
    date-time is imported by calendar clients as a timed event, so the 1 January
    holiday shows up at 00:00 in one timezone and 05:30 in another, and some
    clients drop it. RFC 5545 §3.8.2.4 requires the ``VALUE=DATE`` parameter for
    a floating date.

    The ``UID`` is derived from the holiday's own identity rather than its name or
    date, because the spec requires a UID to be stable: an admin renaming a
    holiday must not turn it into a "deleted" and a "new" event in every
    subscribed calendar.

    Every content line is folded to the 75-octet limit, which matters because a
    holiday name is user input and a client handed an over-long line may drop the
    property rather than complain.
    """
    # DTSTAMP is a UTC instant (RFC 5545 §3.8.7.2), so this is explicitly
    # timezone-aware rather than relying on a naive local clock.
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    lines = [
        'BEGIN:VCALENDAR',
        'VERSION:2.0',
        f'PRODID:{ICS_PRODID}',
        'CALSCALE:GREGORIAN',
        'METHOD:PUBLISH',
        f'X-WR-CALNAME:{_ics_escape(calendar_name)}',
    ]
    for name, when, htype, location in holiday_rows:
        if not hasattr(when, 'isoformat'):
            continue
        summary = f'{name} ({htype})' if htype == 'Optional' else str(name)
        day = when.strftime('%Y%m%d')
        # The next day, so a single-day all-day event does not read as zero-length
        # in a client that expects an exclusive DTEND.
        following = when + timedelta(days=1)
        uid_seed = f'{name}|{when.isoformat()}|{(location or "").strip().lower()}'
        event = [
            'BEGIN:VEVENT',
            f'UID:{_ics_escape(uid_seed)}@hrms',
            f'DTSTAMP:{stamp}',
            f'DTSTART;VALUE=DATE:{day}',
            f'DTEND;VALUE=DATE:{following.strftime("%Y%m%d")}',
            f'SUMMARY:{_ics_escape(summary)}',
            'TRANSP:TRANSPARENT',
        ]
        if htype == 'Optional':
            # An Optional holiday is a holiday only for the employees who opted
            # in, so a subscriber who has not opted in should not see it silently
            # vanish — the description says who it is for.
            event.append(
                'DESCRIPTION:Optional holiday - applies to employees with an approved '
                'opt-in only')
        if location:
            event.append(f'LOCATION:{_ics_escape(location)}')
        event.append('END:VEVENT')
        for line in event:
            lines.extend(_ics_fold(line))
    lines.append('END:VCALENDAR')
    return '\r\n'.join(lines) + '\r\n'
