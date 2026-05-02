from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta
import html
from http.cookiejar import CookieJar
import json
import re
import time
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.request import HTTPCookieProcessor, Request, build_opener

from flask import Flask, jsonify, render_template_string, request

ROOT_URL = "https://schedule.siriusuniversity.ru/"
GROUP_PAGE_URL = ROOT_URL
CLASSROOM_PAGE_URL = f"{ROOT_URL}classroom"
GROUP_LIVEWIRE_ENDPOINT = "https://schedule.siriusuniversity.ru/livewire/message/main-grid"
CLASSROOM_LIVEWIRE_ENDPOINT = "https://schedule.siriusuniversity.ru/livewire/message/classroom.classroom-main-grid"
SOURCE_TIMEOUT = 8
CACHE_TTL_SECONDS = 600
DEFAULT_DAY_START = "08:00"
DEFAULT_DAY_END = "21:30"
DEFAULT_CLASSROOMS: tuple[str, ...] = ()
LESSON_SLOTS = (
    ("08:45", "10:05"),
    ("10:20", "11:40"),
    ("11:55", "13:15"),
    ("13:30", "14:50"),
    ("15:05", "16:25"),
    ("16:40", "18:00"),
    ("18:15", "19:35"),
    ("19:50", "21:10"),
)
WEEKDAY_LABELS = ("ПН", "ВТ", "СР", "ЧТ", "ПТ", "СБ")
_classrooms_cache: tuple[float, list[str]] | None = None
_groups_cache: dict[str, tuple[float, list[str]]] = {}
_classroom_schedules_cache: dict[tuple[str, int], tuple[float, dict[str, Any]]] = {}
CLASSROOM_SEARCH_TERMS = (
    "",
    "К_",
    "1.",
    "1,",
    "2.",
    "3.",
    "Аль",
    "Альфа",
    "Бета",
    "Гамма",
    "Дельта",
    "ЛК",
    "Газ",
    "Рос",
    "Лаб",
    "дистан",
)


@dataclass
class LivewireState:
    token: str
    fingerprint: dict[str, Any]
    server_memo: dict[str, Any]


class SiriusScheduleClient:
    def __init__(self, page_url: str = GROUP_PAGE_URL) -> None:
        cookie_jar = CookieJar()
        self._page_url = page_url
        self._opener = build_opener(HTTPCookieProcessor(cookie_jar))

    def fetch_schedule(self, query: str, week_offset: int) -> dict[str, Any]:
        state = self._get_initial_state()
        updates = self._build_updates(state.fingerprint["id"], query, week_offset)
        payload = {
            "_token": state.token,
            "fingerprint": state.fingerprint,
            "serverMemo": state.server_memo,
            "updates": updates,
        }
        response = self._post_livewire(payload)
        data = response.get("serverMemo", {}).get("data", {})
        return self._normalize_response(query, week_offset, data)

    def search_groups(self, search: str) -> list[str]:
        state = self._get_initial_state()
        response = self._post_livewire(
            payload={
                "_token": state.token,
                "fingerprint": state.fingerprint,
                "serverMemo": state.server_memo,
                "updates": [
                    {
                        "type": "syncInput",
                        "payload": {
                            "id": state.fingerprint["id"],
                            "name": "search",
                            "value": search,
                        },
                    }
                ],
            }
        )
        html_fragment = response.get("effects", {}).get("html", "")
        return self._parse_groups_from_html(html_fragment)

    def _get_initial_state(self) -> LivewireState:
        with self._opener.open(self._page_url, timeout=SOURCE_TIMEOUT) as response:
            page_html = response.read().decode("utf-8")

        state_match = re.search(r'wire:initial-data="([^"]+)"', page_html)
        if state_match is None:
            raise RuntimeError("Cannot find Livewire initial state")

        token_match = re.search(r"window\.livewire_token = '([^']+)';", page_html)
        if token_match is None:
            raise RuntimeError("Cannot find Livewire token")

        state = json.loads(html.unescape(state_match.group(1)))
        return LivewireState(
            token=token_match.group(1),
            fingerprint=state["fingerprint"],
            server_memo=state["serverMemo"],
        )

    def _build_updates(self, component_id: str, group: str, week_offset: int) -> list[dict[str, Any]]:
        updates: list[dict[str, Any]] = [
            {
                "type": "callMethod",
                "payload": {
                    "id": component_id,
                    "method": "set",
                    "params": [group],
                },
            }
        ]

        for _ in range(week_offset):
            updates.append(
                {
                    "type": "callMethod",
                    "payload": {
                        "id": component_id,
                        "method": "addWeek",
                        "params": [],
                    },
                }
            )

        return updates

    def _post_livewire(self, payload: dict[str, Any]) -> dict[str, Any]:
        req = Request(
            GROUP_LIVEWIRE_ENDPOINT,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/plain, */*",
                "X-Livewire": "true",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": self._page_url,
            },
            method="POST",
        )

        with self._opener.open(req, timeout=SOURCE_TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))

    def _normalize_response(self, requested_group: str, week_offset: int, data: dict[str, Any]) -> dict[str, Any]:
        raw_events = data.get("events") or {}
        grouped: dict[str, dict[str, Any]] = {}

        if isinstance(raw_events, dict):
            event_lists = raw_events.values()
        elif isinstance(raw_events, list):
            event_lists = [raw_events]
        else:
            event_lists = []

        for event_list in event_lists:
            if not isinstance(event_list, list):
                continue

            for event in event_list:
                if not isinstance(event, dict):
                    continue

                date = event.get("date")
                day_week = event.get("dayWeek")
                if not date:
                    continue

                day_bucket = grouped.setdefault(
                    date,
                    {
                        "date": date,
                        "day_week": day_week,
                        "events": [],
                    },
                )

                teachers = event.get("teachers") or {}
                if isinstance(teachers, dict):
                    teacher_list = list(teachers.values())
                else:
                    teacher_list = []

                day_bucket["events"].append(
                    {
                        "start_time": event.get("startTime"),
                        "end_time": event.get("endTime"),
                        "number_pair": event.get("numberPair"),
                        "discipline": event.get("discipline"),
                        "group_type": event.get("groupType"),
                        "address": event.get("address"),
                        "classroom": event.get("classroom"),
                        "comment": event.get("comment"),
                        "place": event.get("place"),
                        "url_online": event.get("urlOnline"),
                        "group": event.get("group"),
                        "code": event.get("code"),
                        "color": event.get("color"),
                        "teachers": teacher_list,
                    }
                )

        days = sorted(grouped.values(), key=lambda item: self._parse_date(item.get("date")))
        for day in days:
            day["events"].sort(
                key=lambda item: (
                    self._parse_time(item.get("start_time")),
                    item.get("number_pair") or 0,
                )
            )

        return {
            "source": "schedule.siriusuniversity.ru",
            "group": data.get("group") or requested_group,
            "week_offset": week_offset,
            "week_number": data.get("numWeek"),
            "week_start_date": data.get("date"),
            "month": data.get("month"),
            "events_total": data.get("count", 0),
            "days": days,
        }

    @staticmethod
    def _parse_date(value: Any) -> datetime:
        if not isinstance(value, str):
            return datetime.min
        try:
            return datetime.strptime(value, "%d.%m.%Y")
        except ValueError:
            return datetime.min

    @staticmethod
    def _parse_time(value: Any) -> datetime:
        if not isinstance(value, str):
            return datetime.min
        try:
            return datetime.strptime(value, "%H:%M")
        except ValueError:
            return datetime.min

    @staticmethod
    def _normalize_group_list(value: Any) -> list[str]:
        if isinstance(value, dict):
            raw_items = value.values()
        elif isinstance(value, list):
            raw_items = value
        else:
            raw_items = []

        result: list[str] = []
        seen: set[str] = set()
        for item in raw_items:
            if isinstance(item, dict):
                group = item.get("name") or item.get("group") or item.get("title") or ""
            else:
                group = str(item)
            clean_group = " ".join(group.split())
            key = normalize_lookup(clean_group)
            if clean_group and key not in seen:
                result.append(clean_group)
                seen.add(key)
        return result

    @staticmethod
    def _parse_groups_from_html(value: Any) -> list[str]:
        if not isinstance(value, str):
            return []

        matches = re.findall(
            r"data-group=\"([^\"]+)\"|wire:click=\"set\('([^']+)'\)\"",
            value,
        )
        result: list[str] = []
        seen: set[str] = set()
        for first, second in matches:
            group = html.unescape(first or second or "")
            clean_group = " ".join(group.split())
            key = normalize_lookup(clean_group)
            if clean_group and key not in seen:
                result.append(clean_group)
                seen.add(key)
        return result


class OfficialClassroomClient:
    def __init__(self) -> None:
        cookie_jar = CookieJar()
        self._opener = build_opener(HTTPCookieProcessor(cookie_jar))

    def search_classrooms(self, search: str) -> list[str]:
        state = self._get_initial_state()
        response = self._post_livewire(
            state,
            [
                {
                    "type": "syncInput",
                    "payload": {
                        "id": state.fingerprint["id"],
                        "name": "search",
                        "value": search,
                    },
                }
            ],
        )
        data = response.get("serverMemo", {}).get("data", {})
        return self._normalize_classroom_list(data.get("classroomsList"))

    def fetch_schedule(self, classroom: str, week_offset: int) -> dict[str, Any]:
        state = self._get_initial_state()
        updates = self._build_updates(state.fingerprint["id"], classroom, week_offset)
        response = self._post_livewire(state, updates)
        data = response.get("serverMemo", {}).get("data", {})
        return self._normalize_response(classroom, week_offset, data)

    def _get_initial_state(self) -> LivewireState:
        with self._opener.open(CLASSROOM_PAGE_URL, timeout=SOURCE_TIMEOUT) as response:
            page_html = response.read().decode("utf-8")

        state_match = re.search(r'wire:initial-data="([^"]+)"', page_html)
        if state_match is None:
            raise RuntimeError("Cannot find classroom Livewire initial state")

        token_match = re.search(r"window\.livewire_token = '([^']+)';", page_html)
        if token_match is None:
            raise RuntimeError("Cannot find Livewire token")

        state = json.loads(html.unescape(state_match.group(1)))
        return LivewireState(
            token=token_match.group(1),
            fingerprint=state["fingerprint"],
            server_memo=state["serverMemo"],
        )

    def _build_updates(self, component_id: str, classroom: str, week_offset: int) -> list[dict[str, Any]]:
        updates: list[dict[str, Any]] = [
            {
                "type": "callMethod",
                "payload": {
                    "id": component_id,
                    "method": "set",
                    "params": [classroom],
                },
            }
        ]

        for _ in range(week_offset):
            updates.append(
                {
                    "type": "callMethod",
                    "payload": {
                        "id": component_id,
                        "method": "addWeek",
                        "params": [],
                    },
                }
            )

        return updates

    def _post_livewire(self, state: LivewireState, updates: list[dict[str, Any]]) -> dict[str, Any]:
        req = Request(
            CLASSROOM_LIVEWIRE_ENDPOINT,
            data=json.dumps(
                {
                    "_token": state.token,
                    "fingerprint": state.fingerprint,
                    "serverMemo": state.server_memo,
                    "updates": updates,
                }
            ).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/plain, */*",
                "X-Livewire": "true",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": CLASSROOM_PAGE_URL,
            },
            method="POST",
        )

        with self._opener.open(req, timeout=SOURCE_TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))

    def _normalize_response(self, requested_classroom: str, week_offset: int, data: dict[str, Any]) -> dict[str, Any]:
        raw_events = self._flatten_events(data.get("events"))
        grouped: dict[str, dict[str, Any]] = {}

        for event in raw_events:
            date = event.get("date")
            if not isinstance(date, str) or not date.strip():
                continue

            day_bucket = grouped.setdefault(
                date,
                {
                    "date": date,
                    "day_week": event.get("dayWeek"),
                    "events": [],
                },
            )
            day_bucket["events"].append(self._normalize_event(event, data.get("classroom") or requested_classroom))

        days = sorted(grouped.values(), key=lambda item: SiriusScheduleClient._parse_date(item.get("date")))
        for day in days:
            day["events"].sort(
                key=lambda item: (
                    SiriusScheduleClient._parse_time(item.get("start_time")),
                    item.get("number_pair") or 0,
                )
            )

        return {
            "source": "schedule.siriusuniversity.ru",
            "classroom": data.get("classroom") or requested_classroom,
            "week_offset": week_offset,
            "week_number": data.get("numWeek"),
            "week_start_date": data.get("date"),
            "month": data.get("month"),
            "events_total": data.get("count", len(raw_events)),
            "days": days,
        }

    def _normalize_event(self, event: dict[str, Any], fallback_classroom: Any) -> dict[str, Any]:
        number_pair = self._safe_int(event.get("numberPair"))
        start_time = event.get("startTime")
        end_time = event.get("endTime")

        if not isinstance(start_time, str) or not start_time.strip() or not isinstance(end_time, str) or not end_time.strip():
            start_time, end_time = lesson_times_by_pair(number_pair)

        teachers = event.get("teachers") or {}
        if isinstance(teachers, dict):
            teacher_list = list(teachers.values())
        else:
            teacher_list = []

        return {
            "start_time": start_time,
            "end_time": end_time,
            "number_pair": number_pair,
            "discipline": event.get("discipline"),
            "group_type": event.get("groupType"),
            "address": event.get("address"),
            "classroom": event.get("classroom") or fallback_classroom,
            "comment": event.get("comment"),
            "place": event.get("place"),
            "url_online": event.get("urlOnline"),
            "group": event.get("group"),
            "code": event.get("code"),
            "color": event.get("color"),
            "teachers": teacher_list,
        }

    @staticmethod
    def _flatten_events(events: Any) -> list[dict[str, Any]]:
        if isinstance(events, dict):
            event_lists = events.values()
        elif isinstance(events, list):
            event_lists = [events]
        else:
            event_lists = []

        result: list[dict[str, Any]] = []
        for event_list in event_lists:
            if not isinstance(event_list, list):
                continue
            for event in event_list:
                if isinstance(event, dict):
                    result.append(event)
        return result

    @staticmethod
    def _normalize_classroom_list(value: Any) -> list[str]:
        if isinstance(value, dict):
            raw_items = value.values()
        elif isinstance(value, list):
            raw_items = value
        else:
            raw_items = []

        result: list[str] = []
        seen: set[str] = set()
        for item in raw_items:
            classroom = " ".join(str(item).split())
            key = normalize_lookup(classroom)
            if classroom and key not in seen:
                result.append(classroom)
                seen.add(key)
        return result

    @staticmethod
    def _safe_int(value: Any) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0


app = Flask(__name__)


def split_values(raw: str | None) -> list[str]:
    if not raw:
        return []
    values = re.split(r"[\n;]+", raw)
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        clean_value = " ".join(value.strip().split())
        key = clean_value.casefold()
        if clean_value and key not in seen:
            result.append(clean_value)
            seen.add(key)
    return result


def normalize_lookup(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value).strip().casefold()


def classroom_matches(actual: Any, requested: str) -> bool:
    actual_normalized = normalize_lookup(actual)
    requested_normalized = normalize_lookup(requested)
    if not actual_normalized or not requested_normalized:
        return False
    return (
        actual_normalized == requested_normalized
        or requested_normalized in actual_normalized
        or actual_normalized in requested_normalized
    )


def parse_client_date(value: str) -> str:
    value = value.strip()
    for date_format in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, date_format).strftime("%d.%m.%Y")
        except ValueError:
            continue
    raise ValueError("Date must be in DD.MM.YYYY or YYYY-MM-DD format")


def parse_minutes(value: str) -> int:
    try:
        parsed = datetime.strptime(value.strip(), "%H:%M")
    except ValueError as error:
        raise ValueError("Time must be in HH:MM format") from error
    return parsed.hour * 60 + parsed.minute


def lesson_times_by_pair(number_pair: int) -> tuple[str, str]:
    if 1 <= number_pair <= len(LESSON_SLOTS):
        return LESSON_SLOTS[number_pair - 1]
    return DEFAULT_DAY_START, DEFAULT_DAY_END


def current_lesson_slot(now: datetime | None = None) -> dict[str, str]:
    current_time = now or datetime.now()
    current_minutes = current_time.hour * 60 + current_time.minute
    parsed_slots = [(start, end, parse_minutes(start), parse_minutes(end)) for start, end in LESSON_SLOTS]

    for start, end, start_minutes, end_minutes in parsed_slots:
        if start_minutes <= current_minutes < end_minutes:
            return {"start": start, "end": end, "state": "active"}
        if current_minutes < start_minutes:
            return {"start": start, "end": end, "state": "upcoming"}

    start, end, _, _ = parsed_slots[-1]
    return {"start": start, "end": end, "state": "finished"}


def start_of_week(now: datetime | None = None) -> datetime:
    current = now or datetime.now()
    return current - timedelta(days=current.weekday())


def week_dates(week_offset: int, now: datetime | None = None) -> list[dict[str, str]]:
    monday = start_of_week(now) + timedelta(days=week_offset * 7)
    result: list[dict[str, str]] = []
    for day_offset, day_label in enumerate(WEEKDAY_LABELS):
        current_day = monday + timedelta(days=day_offset)
        result.append(
            {
                "date": current_day.strftime("%d.%m.%Y"),
                "day_label": day_label,
            }
        )
    return result


def event_overlaps(event: dict[str, Any], start_minutes: int, end_minutes: int) -> bool:
    start_time = event.get("start_time")
    end_time = event.get("end_time")
    if not isinstance(start_time, str) or not isinstance(end_time, str):
        return False

    try:
        event_start = parse_minutes(start_time)
        event_end = parse_minutes(end_time)
    except ValueError:
        return False

    return event_start < end_minutes and event_end > start_minutes


def events_for_date(days: Iterable[dict[str, Any]], target_date: str) -> list[dict[str, Any]]:
    for day in days:
        if day.get("date") == target_date:
            events = day.get("events")
            return events if isinstance(events, list) else []
    return []


def format_teacher_name(teacher: Any) -> str:
    if isinstance(teacher, str):
        return " ".join(teacher.split())
    if not isinstance(teacher, dict):
        return ""

    fio = teacher.get("fio")
    if isinstance(fio, str) and fio.strip():
        return " ".join(fio.split())

    parts = [
        teacher.get("lastName"),
        teacher.get("firstName"),
        teacher.get("middleName"),
    ]
    clean_parts = [" ".join(str(part).split()) for part in parts if isinstance(part, str) and part.strip()]
    return " ".join(clean_parts)


def build_busy_entry(event: dict[str, Any], source: str) -> dict[str, Any]:
    raw_teachers = event.get("teachers") or []
    if isinstance(raw_teachers, list):
        teachers = [name for name in (format_teacher_name(item) for item in raw_teachers) if name]
    else:
        teachers = []

    return {
        "source": source,
        "start_time": event.get("start_time"),
        "end_time": event.get("end_time"),
        "discipline": event.get("discipline"),
        "group": event.get("group"),
        "group_type": event.get("group_type"),
        "classroom": event.get("classroom"),
        "address": event.get("address"),
        "teachers": teachers,
    }


def busy_entry_key(entry: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(entry.get("start_time") or ""),
        str(entry.get("end_time") or ""),
        str(entry.get("discipline") or ""),
        str(entry.get("group") or ""),
    )


def find_room_busy_entries(
    schedule_payload: dict[str, Any],
    room: str,
    target_date: str,
    start_minutes: int,
    end_minutes: int,
) -> list[dict[str, Any]]:
    busy_entries: list[dict[str, Any]] = []
    for event in events_for_date(schedule_payload.get("days", []), target_date):
        if not isinstance(event, dict) or not event_overlaps(event, start_minutes, end_minutes):
            continue

        event_classroom = event.get("classroom")
        if event_classroom and not classroom_matches(event_classroom, room):
            continue

        busy_entries.append(build_busy_entry(event, room))

    return busy_entries


def find_group_busy_by_room(
    schedule_payload: dict[str, Any],
    target_date: str,
    start_minutes: int,
    end_minutes: int,
    requested_rooms: list[str],
) -> dict[str, list[dict[str, Any]]]:
    busy_by_room: dict[str, list[dict[str, Any]]] = {}
    group_name = str(schedule_payload.get("group") or "")

    for event in events_for_date(schedule_payload.get("days", []), target_date):
        if not isinstance(event, dict) or not event_overlaps(event, start_minutes, end_minutes):
            continue

        classroom = event.get("classroom")
        if not isinstance(classroom, str) or not classroom.strip():
            continue

        if requested_rooms:
            matched_rooms = [room for room in requested_rooms if classroom_matches(classroom, room)]
        else:
            matched_rooms = [" ".join(classroom.split())]

        for room in matched_rooms:
            busy_by_room.setdefault(room, []).append(build_busy_entry(event, group_name))

    return busy_by_room


def fetch_schedule_source(source: str, week: int, page_url: str) -> dict[str, Any]:
    return SiriusScheduleClient(page_url=page_url).fetch_schedule(source, week)


def fetch_schedules_parallel(
    sources: list[str],
    week: int,
    page_url: str,
) -> tuple[list[tuple[str, dict[str, Any]]], list[dict[str, str]]]:
    if not sources:
        return [], []

    payloads: list[tuple[str, dict[str, Any]]] = []
    errors: list[dict[str, str]] = []
    max_workers = min(len(sources), 8)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_by_source = {
            executor.submit(fetch_schedule_source, source, week, page_url): source
            for source in sources
        }
        for future in as_completed(future_by_source):
            source = future_by_source[future]
            try:
                payloads.append((source, future.result()))
            except Exception as error:
                errors.append({"source": source, "reason": str(error)})

    return payloads, errors


def cached_classrooms() -> list[str]:
    global _classrooms_cache

    now = time.monotonic()
    if _classrooms_cache and now - _classrooms_cache[0] < CACHE_TTL_SECONDS:
        return _classrooms_cache[1]

    client = OfficialClassroomClient()
    classrooms_by_key: dict[str, str] = {}
    for term in CLASSROOM_SEARCH_TERMS:
        for classroom in client.search_classrooms(term):
            classrooms_by_key.setdefault(normalize_lookup(classroom), classroom)

    classrooms = sorted(classrooms_by_key.values(), key=classroom_sort_key)
    _classrooms_cache = (now, classrooms)
    return classrooms


def cached_group_search(search: str) -> list[str]:
    key = normalize_lookup(search)
    now = time.monotonic()
    cached = _groups_cache.get(key)
    if cached and now - cached[0] < CACHE_TTL_SECONDS:
        return cached[1]

    groups = SiriusScheduleClient().search_groups(search)
    _groups_cache[key] = (now, groups)
    return groups


def resolve_requested_classrooms(requested_classrooms: list[str], known_classrooms: list[str]) -> list[str]:
    if not requested_classrooms:
        return known_classrooms

    resolved: list[str] = []
    seen: set[str] = set()
    for requested in requested_classrooms:
        matches = [classroom for classroom in known_classrooms if classroom_matches(classroom, requested)]
        if not matches:
            matches = [requested]

        for classroom in matches:
            key = normalize_lookup(classroom)
            if key not in seen:
                resolved.append(classroom)
                seen.add(key)

    return resolved


def cached_classroom_schedule(classroom: str, week: int) -> dict[str, Any]:
    key = (classroom, week)
    now = time.monotonic()
    cached = _classroom_schedules_cache.get(key)
    if cached and now - cached[0] < CACHE_TTL_SECONDS:
        return cached[1]

    payload = OfficialClassroomClient().fetch_schedule(classroom, week)
    _classroom_schedules_cache[key] = (now, payload)
    return payload


def fetch_classroom_schedules_parallel(
    classrooms: list[str],
    week: int,
) -> tuple[list[tuple[str, dict[str, Any]]], list[dict[str, str]]]:
    if not classrooms:
        return [], []

    payloads: list[tuple[str, dict[str, Any]]] = []
    errors: list[dict[str, str]] = []
    max_workers = min(len(classrooms), 12)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_by_classroom = {
            executor.submit(cached_classroom_schedule, classroom, week): classroom
            for classroom in classrooms
        }
        for future in as_completed(future_by_classroom):
            classroom = future_by_classroom[future]
            try:
                payloads.append((classroom, future.result()))
            except Exception as error:
                errors.append({"source": classroom, "reason": str(error)})

    return payloads, errors


def classroom_sort_key(value: str) -> tuple[int, str]:
    normalized = normalize_lookup(value)
    number_match = re.search(r"\d+", normalized)
    number = int(number_match.group(0)) if number_match else 10_000
    return number, normalized


def parse_interval_values(raw: str | None, fallback_start: str, fallback_end: str) -> list[dict[str, Any]]:
    intervals: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    for value in split_values(raw):
        parts = [part.strip() for part in value.split("-", 1)]
        if len(parts) != 2:
            raise ValueError("Each slot must be in HH:MM-HH:MM format")
        start_time, end_time = parts
        start_minutes = parse_minutes(start_time)
        end_minutes = parse_minutes(end_time)
        if start_minutes >= end_minutes:
            raise ValueError("Start time must be earlier than end time")
        key = (start_time, end_time)
        if key not in seen:
            intervals.append(
                {
                    "start_time": start_time,
                    "end_time": end_time,
                    "start_minutes": start_minutes,
                    "end_minutes": end_minutes,
                }
            )
            seen.add(key)

    if intervals:
        return sorted(intervals, key=lambda item: (item["start_minutes"], item["end_minutes"]))

    start_minutes = parse_minutes(fallback_start)
    end_minutes = parse_minutes(fallback_end)
    if start_minutes >= end_minutes:
        raise ValueError("Start time must be earlier than end time")
    return [
        {
            "start_time": fallback_start,
            "end_time": fallback_end,
            "start_minutes": start_minutes,
            "end_minutes": end_minutes,
        }
    ]


def parse_availability_args() -> tuple[list[str], list[str], str, str, int, list[dict[str, str]], list[dict[str, Any]]]:
    classrooms = split_values(request.args.get("classrooms") or request.args.get("rooms"))
    groups = split_values(request.args.get("groups"))
    mode = request.args.get("mode", "rooms").strip().casefold()
    if mode not in {"rooms", "groups"}:
        raise ValueError("Mode must be 'rooms' or 'groups'")
    view = request.args.get("view", "day").strip().casefold()
    if view not in {"day", "week"}:
        raise ValueError("Query parameter 'view' must be 'day' or 'week'")

    week_raw = request.args.get("week", "0").strip()
    try:
        week = int(week_raw)
    except ValueError as error:
        raise ValueError("Query parameter 'week' must be an integer") from error
    if week < 0:
        raise ValueError("Query parameter 'week' must be >= 0")

    slot = current_lesson_slot()
    start_time = request.args.get("start", "").strip() or slot["start"]
    end_time = request.args.get("end", "").strip() or slot["end"]
    intervals = parse_interval_values(request.args.get("slots"), start_time, end_time)
    if view == "week":
        target_days = week_dates(week)
    else:
        date_raw = request.args.get("date", "").strip()
        target_date = parse_client_date(date_raw) if date_raw else datetime.now().strftime("%d.%m.%Y")
        target_days = [{"date": target_date, "day_label": ""}]

    return classrooms, groups, mode, view, week, target_days, intervals


@app.get("/")
def index() -> str:
    slot = current_lesson_slot()
    return render_template_string(
        INDEX_HTML,
        default_classrooms="\n".join(DEFAULT_CLASSROOMS),
        default_classrooms_json=json.dumps(list(DEFAULT_CLASSROOMS), ensure_ascii=False),
        lesson_slots_json=json.dumps(
            [{"start": start, "end": end} for start, end in LESSON_SLOTS],
            ensure_ascii=False,
        ),
        current_slot=json.dumps(slot, ensure_ascii=False),
    )


@app.get("/api/schedule")
def get_schedule() -> Any:
    classroom = request.args.get("classroom", request.args.get("room", "")).strip()
    group = request.args.get("group", "").strip()
    if not classroom and not group:
        return jsonify({"error": "Query parameter 'classroom' or 'group' is required"}), 400

    week_raw = request.args.get("week", "0").strip()
    try:
        week = int(week_raw)
    except ValueError:
        return jsonify({"error": "Query parameter 'week' must be an integer"}), 400

    if week < 0:
        return jsonify({"error": "Query parameter 'week' must be >= 0"}), 400

    try:
        if classroom:
            payload = OfficialClassroomClient().fetch_schedule(classroom, week)
        else:
            payload = SiriusScheduleClient().fetch_schedule(group, week)
        return jsonify(payload)
    except HTTPError as error:
        return (
            jsonify(
                {"error": "Schedule source returned an error", "status": error.code}
            ),
            502,
        )
    except URLError as error:
        return jsonify({"error": "Failed to connect to schedule source", "reason": str(error)}), 502
    except Exception as error:
        return jsonify({"error": "Unexpected parser error", "reason": str(error)}), 500


@app.get("/api/classrooms")
def get_classroom_options() -> Any:
    query = request.args.get("q", "").strip()
    limit_raw = request.args.get("limit", "20").strip()
    try:
        limit = max(1, min(int(limit_raw), 50))
    except ValueError:
        return jsonify({"error": "Query parameter 'limit' must be an integer"}), 400

    try:
        classrooms = cached_classrooms()
    except HTTPError as error:
        return jsonify({"error": "Schedule source returned an error", "status": error.code}), 502
    except URLError as error:
        return jsonify({"error": "Failed to connect to schedule source", "reason": str(error)}), 502
    except Exception as error:
        return jsonify({"error": "Unexpected parser error", "reason": str(error)}), 500

    normalized_query = normalize_lookup(query)
    if normalized_query:
        classrooms = [
            classroom
            for classroom in classrooms
            if normalized_query in normalize_lookup(classroom)
        ]

    return jsonify({"items": classrooms[:limit]})


@app.get("/api/groups")
def get_group_options() -> Any:
    query = request.args.get("q", "").strip()
    if len(query) < 1:
        return jsonify({"items": []})

    limit_raw = request.args.get("limit", "20").strip()
    try:
        limit = max(1, min(int(limit_raw), 50))
    except ValueError:
        return jsonify({"error": "Query parameter 'limit' must be an integer"}), 400

    try:
        groups = cached_group_search(query)
    except HTTPError as error:
        return jsonify({"error": "Schedule source returned an error", "status": error.code}), 502
    except URLError as error:
        return jsonify({"error": "Failed to connect to schedule source", "reason": str(error)}), 502
    except Exception as error:
        return jsonify({"error": "Unexpected parser error", "reason": str(error)}), 500

    return jsonify({"items": groups[:limit]})


@app.get("/api/free-classrooms")
def get_free_classrooms() -> Any:
    try:
        (
            classrooms,
            groups,
            mode,
            view,
            week,
            target_days,
            intervals,
        ) = parse_availability_args()
    except ValueError as error:
        return jsonify({"error": str(error)}), 400

    if mode == "groups" and not groups:
        return jsonify({"error": "Query parameter 'groups' is required in groups mode"}), 400

    requested_classrooms = classrooms
    busy_by_room: dict[str, dict[str, dict[tuple[str, str], list[dict[str, Any]]]]] = {}
    errors: list[dict[str, str]] = []
    failed_rooms: set[str] = set()
    checked_sources: list[str] = []
    week_info: dict[str, Any] = {}
    interval_keys = [(item["start_time"], item["end_time"]) for item in intervals]
    day_items = [(day["date"], day["day_label"]) for day in target_days]

    if mode == "rooms":
        try:
            known_classrooms = cached_classrooms()
        except Exception as error:
            return jsonify({"error": "Failed to load classroom list", "reason": str(error)}), 502

        rooms_to_check = resolve_requested_classrooms(requested_classrooms, known_classrooms)
        busy_by_room = {
            room: {
                day_date: {interval_key: [] for interval_key in interval_keys}
                for day_date, _ in day_items
            }
            for room in rooms_to_check
        }
        payloads, fetch_errors = fetch_classroom_schedules_parallel(rooms_to_check, week)
        errors.extend(fetch_errors)
        failed_rooms.update(error["source"] for error in fetch_errors)

        for room, payload in payloads:
            checked_sources.append(room)
            if not week_info:
                week_info = {
                    "week_number": payload.get("week_number"),
                    "week_start_date": payload.get("week_start_date"),
                    "month": payload.get("month"),
                }
            for day_date, _ in day_items:
                for interval in intervals:
                    interval_key = (interval["start_time"], interval["end_time"])
                    busy_by_room[room][day_date][interval_key].extend(
                        find_room_busy_entries(
                            payload,
                            room,
                            day_date,
                            interval["start_minutes"],
                            interval["end_minutes"],
                        )
                    )

    if mode == "groups":
        busy_by_room = {
            room: {
                day_date: {interval_key: [] for interval_key in interval_keys}
                for day_date, _ in day_items
            }
            for room in requested_classrooms
        }
        payloads, fetch_errors = fetch_schedules_parallel(groups, week, GROUP_PAGE_URL)
        errors.extend(fetch_errors)

        room_payloads: list[tuple[str, dict[str, Any]]] = []
        room_fetch_errors: list[dict[str, str]] = []
        if requested_classrooms:
            try:
                resolved_rooms = resolve_requested_classrooms(requested_classrooms, cached_classrooms())
            except Exception as error:
                return jsonify({"error": "Failed to load classroom list", "reason": str(error)}), 502
            room_payloads, room_fetch_errors = fetch_classroom_schedules_parallel(resolved_rooms, week)
            errors.extend(room_fetch_errors)
            requested_to_resolved: dict[str, str] = {}
            for requested_room in requested_classrooms:
                for resolved_room in resolved_rooms:
                    if classroom_matches(resolved_room, requested_room):
                        requested_to_resolved[requested_room] = resolved_room
                        break
            for room_error in room_fetch_errors:
                for requested_room, resolved_room in requested_to_resolved.items():
                    if resolved_room == room_error["source"]:
                        failed_rooms.add(requested_room)
            room_payload_by_requested = {
                requested_room: next(
                    (payload for resolved_room, payload in room_payloads if resolved_room == requested_to_resolved.get(requested_room)),
                    None,
                )
                for requested_room in requested_classrooms
            }
        else:
            room_payload_by_requested = {}

        for group, payload in payloads:
            checked_sources.append(group)
            if not week_info:
                week_info = {
                    "week_number": payload.get("week_number"),
                    "week_start_date": payload.get("week_start_date"),
                    "month": payload.get("month"),
                }
            for day_date, _ in day_items:
                for event in events_for_date(payload.get("days", []), day_date):
                    if not isinstance(event, dict):
                        continue

                    classroom = event.get("classroom")
                    if not isinstance(classroom, str) or not classroom.strip():
                        continue

                    clean_room = " ".join(classroom.split())
                    if requested_classrooms:
                        matched_rooms = [room for room in requested_classrooms if classroom_matches(clean_room, room)]
                    else:
                        matched_rooms = [clean_room]

                    for room in matched_rooms:
                        room_days = busy_by_room.setdefault(
                            room,
                            {
                                date_value: {interval_key: [] for interval_key in interval_keys}
                                for date_value, _ in day_items
                            },
                        )
                        room_intervals = room_days[day_date]
                        for interval in intervals:
                            if event_overlaps(event, interval["start_minutes"], interval["end_minutes"]):
                                room_intervals[(interval["start_time"], interval["end_time"])].append(
                                    build_busy_entry(event, group)
                                )

    rooms_payload: list[dict[str, Any]] = []
    error_by_room = {error["source"]: error["reason"] for error in errors}
    rooms = sorted(busy_by_room.keys(), key=classroom_sort_key)
    for room in rooms:
        if room in failed_rooms:
            status = "error"
        else:
            status = "free"

        day_payloads: list[dict[str, Any]] = []
        for day_date, day_label in day_items:
            interval_payloads: list[dict[str, Any]] = []
            for interval in intervals:
                interval_key = (interval["start_time"], interval["end_time"])
                busy_entries = sorted(
                    busy_by_room.get(room, {}).get(day_date, {}).get(interval_key, []),
                    key=lambda item: (item.get("start_time") or "", item.get("discipline") or ""),
                )
                other_busy_entries: list[dict[str, Any]] = []

                if mode == "groups":
                    room_schedule_payload = room_payload_by_requested.get(room)
                    if isinstance(room_schedule_payload, dict):
                        selected_keys = {busy_entry_key(item) for item in busy_entries}
                        other_busy_entries = [
                            entry
                            for entry in find_room_busy_entries(
                                room_schedule_payload,
                                room,
                                day_date,
                                interval["start_minutes"],
                                interval["end_minutes"],
                            )
                            if busy_entry_key(entry) not in selected_keys
                        ]
                        other_busy_entries.sort(
                            key=lambda item: (item.get("start_time") or "", item.get("discipline") or "")
                        )

                if room in failed_rooms:
                    interval_status = "error"
                else:
                    if busy_entries:
                        interval_status = "selected_busy"
                        status = "busy"
                    elif other_busy_entries:
                        interval_status = "other_busy"
                        status = "busy"
                    else:
                        interval_status = "free"

                interval_payloads.append(
                    {
                        "start_time": interval["start_time"],
                        "end_time": interval["end_time"],
                        "status": interval_status,
                        "busy": busy_entries,
                        "other_busy": other_busy_entries,
                        "error": error_by_room.get(room) if room in failed_rooms else None,
                    }
                )

            day_payloads.append(
                {
                    "date": day_date,
                    "day_label": day_label,
                    "intervals": interval_payloads,
                }
            )

        rooms_payload.append(
            {
                "classroom": room,
                "status": status,
                "days": day_payloads,
                "error": error_by_room.get(room),
            }
        )

    return jsonify(
        {
            "source": "schedule.siriusuniversity.ru",
            "mode": mode,
            "view": view,
            "days": target_days,
            "date": target_days[0]["date"] if target_days else "",
            "intervals": [
                {
                    "start_time": interval["start_time"],
                    "end_time": interval["end_time"],
                }
                for interval in intervals
            ],
            "week_offset": week,
            "checked_sources": checked_sources,
            "errors": errors,
            "week": week_info,
            "summary": {
                "total": len(rooms_payload),
                "free": sum(1 for room in rooms_payload if room["status"] == "free"),
                "busy": sum(1 for room in rooms_payload if room["status"] == "busy"),
                "errors": sum(1 for room in rooms_payload if room["status"] == "error") or len(errors),
            },
            "rooms": rooms_payload,
        }
    )


INDEX_HTML = """
<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Свободные аудитории</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f4f6f8;
      --panel: #ffffff;
      --panel-muted: #eef2f6;
      --text: #17202a;
      --muted: #66717f;
      --border: #d8dee6;
      --accent: #1d6f64;
      --accent-dark: #15534b;
      --danger: #ad2f2f;
      --free: #dff4e6;
      --busy: #ffe8df;
      --chip: #e6eef8;
      --chip-text: #25435e;
      --shadow: 0 12px 32px rgba(23, 32, 42, 0.08);
    }

    * {
      box-sizing: border-box;
    }

    body {
      margin: 0;
      min-height: 100vh;
      background: var(--bg);
      color: var(--text);
      font-family: Inter, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      letter-spacing: 0;
    }

    button,
    input,
    textarea,
    select {
      font: inherit;
    }

    .shell {
      display: grid;
      grid-template-columns: minmax(320px, 400px) 1fr;
      min-height: 100vh;
    }

    aside {
      background: var(--panel);
      border-right: 1px solid var(--border);
      padding: 28px;
      position: sticky;
      top: 0;
      height: 100vh;
      overflow-y: auto;
    }

    main {
      padding: 28px;
    }

    h1 {
      margin: 0 0 6px;
      font-size: 28px;
      line-height: 1.15;
    }

    .lead {
      margin: 0 0 24px;
      color: var(--muted);
      line-height: 1.45;
    }

    .field {
      display: grid;
      gap: 8px;
      margin-bottom: 18px;
    }

    label {
      font-weight: 650;
      font-size: 14px;
    }

    input,
    select,
    button {
      width: 100%;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: #fff;
      color: var(--text);
      padding: 11px 12px;
      outline: none;
    }

    input:focus,
    select:focus,
    button:focus-visible {
      border-color: var(--accent);
      box-shadow: 0 0 0 3px rgba(29, 111, 100, 0.12);
    }

    .two {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 12px;
    }

    .mode {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 6px;
      padding: 4px;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--panel-muted);
    }

    .mode input {
      position: absolute;
      opacity: 0;
      pointer-events: none;
    }

    .mode span {
      display: block;
      text-align: center;
      border-radius: 6px;
      padding: 9px 8px;
      cursor: pointer;
      color: var(--muted);
      font-weight: 650;
      font-size: 13px;
      white-space: nowrap;
    }

    .mode input:checked + span {
      background: #fff;
      color: var(--text);
      box-shadow: 0 2px 8px rgba(23, 32, 42, 0.08);
    }

    .hint {
      color: var(--muted);
      font-size: 13px;
      line-height: 1.35;
    }

    .inline-row {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
    }

    .picker {
      position: relative;
    }

    .picker-surface {
      min-height: 50px;
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 8px;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: #fff;
      padding: 8px;
      cursor: text;
    }

    .picker.open .picker-surface {
      border-color: var(--accent);
      box-shadow: 0 0 0 3px rgba(29, 111, 100, 0.12);
    }

    .picker-input {
      flex: 1 1 120px;
      min-width: 96px;
      border: 0;
      padding: 4px 2px;
      background: transparent;
      box-shadow: none;
    }

    .picker-input:focus {
      box-shadow: none;
    }

    .picker-input::placeholder {
      color: #8b96a5;
    }

    .chip {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      max-width: 100%;
      padding: 7px 10px;
      border-radius: 999px;
      background: var(--chip);
      color: var(--chip-text);
      font-size: 13px;
      line-height: 1;
    }

    .chip.toggle {
      cursor: pointer;
      border: 0;
    }

    .chip span {
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    .chip button {
      width: 20px;
      height: 20px;
      min-width: 20px;
      display: inline-grid;
      place-items: center;
      padding: 0;
      border: 0;
      border-radius: 999px;
      background: rgba(37, 67, 94, 0.14);
      color: inherit;
      cursor: pointer;
    }

    .picker-menu {
      position: absolute;
      top: calc(100% + 6px);
      left: 0;
      right: 0;
      z-index: 20;
      display: none;
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 8px;
      box-shadow: var(--shadow);
      overflow: hidden;
    }

    .picker.open .picker-menu {
      display: block;
    }

    .picker-status {
      padding: 10px 12px 6px;
      color: var(--muted);
      font-size: 12px;
    }

    .picker-options {
      max-height: 240px;
      overflow-y: auto;
      padding: 4px;
    }

    .picker-option {
      width: 100%;
      border: 0;
      border-radius: 6px;
      background: transparent;
      padding: 9px 10px;
      text-align: left;
      cursor: pointer;
    }

    .picker-option:hover,
    .picker-option:focus-visible {
      background: var(--panel-muted);
      box-shadow: none;
    }

    .current-slot {
      display: grid;
      gap: 4px;
      margin-bottom: 18px;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--panel-muted);
      padding: 12px;
    }

    .current-slot strong {
      font-size: 18px;
      line-height: 1.2;
    }

    .current-slot span {
      color: var(--muted);
      font-size: 13px;
    }

    .primary {
      width: 100%;
      border: 0;
      border-radius: 8px;
      background: var(--accent);
      color: #fff;
      font-weight: 750;
      padding: 12px 16px;
      cursor: pointer;
    }

    .primary:hover {
      background: var(--accent-dark);
    }

    .primary:disabled {
      cursor: wait;
      opacity: 0.75;
    }

    .toolbar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      margin-bottom: 20px;
    }

    .toolbar h2 {
      margin: 0;
      font-size: 22px;
    }

    .summary {
      display: grid;
      grid-template-columns: repeat(4, minmax(120px, 1fr));
      gap: 12px;
      margin-bottom: 18px;
    }

    .metric {
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 14px;
      box-shadow: var(--shadow);
    }

    .metric strong {
      display: block;
      font-size: 24px;
      line-height: 1.1;
    }

    .metric span {
      display: block;
      color: var(--muted);
      font-size: 13px;
      margin-top: 4px;
    }

    .notice {
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--panel);
      padding: 14px 16px;
      color: var(--muted);
      line-height: 1.45;
      margin-bottom: 18px;
    }

    .notice.error {
      border-color: rgba(173, 47, 47, 0.28);
      color: var(--danger);
      background: #fff4f2;
    }

    .results {
      display: grid;
      gap: 10px;
    }

    .room {
      display: grid;
      grid-template-columns: minmax(140px, 220px) 1fr;
      align-items: start;
      gap: 14px;
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 14px;
      box-shadow: var(--shadow);
    }

    .room-name {
      font-weight: 800;
      font-size: 18px;
      overflow-wrap: anywhere;
    }

    .badge {
      display: inline-flex;
      justify-content: center;
      min-width: 82px;
      border-radius: 999px;
      padding: 6px 10px;
      font-weight: 750;
      font-size: 13px;
    }

    .badge.free {
      background: var(--free);
      color: #17622c;
    }

    .badge.busy {
      background: var(--busy);
      color: #8c351b;
    }

    .badge.other-busy {
      background: #fff2d8;
      color: #8a5a00;
    }

    .badge.error {
      background: #fff1c9;
      color: #7a5200;
    }

    .lessons {
      display: grid;
      gap: 12px;
      color: var(--muted);
      line-height: 1.35;
    }

    .day-block {
      display: grid;
      gap: 10px;
      padding-top: 6px;
      border-top: 1px solid var(--border);
    }

    .day-title {
      font-weight: 750;
      color: var(--text);
      font-size: 15px;
    }

    .interval-block {
      display: grid;
      gap: 8px;
      border-left: 3px solid var(--border);
      padding-left: 10px;
    }

    .interval-head {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 10px;
    }

    .interval-head strong {
      color: var(--text);
      font-size: 14px;
    }

    .interval-note {
      color: var(--muted);
      font-size: 13px;
    }

    .lesson {
      border-left: 3px solid var(--accent);
      padding-left: 10px;
    }

    .lesson strong {
      color: var(--text);
    }

    .empty {
      min-height: 48vh;
      display: grid;
      place-items: center;
      border: 1px dashed var(--border);
      border-radius: 8px;
      color: var(--muted);
      text-align: center;
      padding: 24px;
    }

    @media (max-width: 900px) {
      .shell {
        grid-template-columns: 1fr;
      }

      aside {
        position: static;
        height: auto;
      }

      .summary {
        grid-template-columns: repeat(2, 1fr);
      }

      .room {
        grid-template-columns: 1fr;
      }
    }

    @media (max-width: 540px) {
      aside,
      main {
        padding: 18px;
      }

      .two,
      .summary {
        grid-template-columns: 1fr;
      }
    }
  </style>
</head>
<body>
  <div class="shell">
    <aside>
      <h1>Свободные аудитории</h1>
      <p class="lead">Автоматическая проверка текущей пары по расписанию Университета «Сириус».</p>

      <form id="availability-form">
        <div class="current-slot">
          <strong id="slot-title">Выбранные интервалы</strong>
          <span id="slot-subtitle">По умолчанию выбрана текущая пара.</span>
        </div>

        <div class="field">
          <label>Режим проверки</label>
          <div class="mode">
            <label>
              <input type="radio" name="mode" value="rooms" checked>
              <span>Аудитории</span>
            </label>
            <label>
              <input type="radio" name="mode" value="groups">
              <span>Группы</span>
            </label>
          </div>
          <div class="hint" id="mode-hint">Если аудитории не выбраны, будут проверены все найденные аудитории.</div>
        </div>

        <div class="field">
          <label for="classroom-input">Аудитории</label>
          <div class="picker" id="classroom-picker" data-type="classrooms">
            <div class="picker-surface" data-surface>
              <div class="picker-chips" data-chips></div>
              <input id="classroom-input" class="picker-input" type="text" autocomplete="off" placeholder="Начните вводить аудиторию">
            </div>
            <div class="picker-menu" data-menu>
              <div class="picker-status" data-status>Загрузка аудиторий...</div>
              <div class="picker-options" data-options></div>
            </div>
          </div>
          <input id="classrooms" name="classrooms" type="hidden" value="{{ default_classrooms }}">
          <div class="hint">Выбранные аудитории добавляются в виде меток. Если список пуст, проверяются все найденные аудитории.</div>
        </div>

        <div class="field" id="groups-field">
          <label for="group-input">Группы</label>
          <div class="picker" id="group-picker" data-type="groups">
            <div class="picker-surface" data-surface>
              <div class="picker-chips" data-chips></div>
              <input id="group-input" class="picker-input" type="text" autocomplete="off" placeholder="Начните вводить группу">
            </div>
            <div class="picker-menu" data-menu>
              <div class="picker-status" data-status>Начните вводить название группы.</div>
              <div class="picker-options" data-options></div>
            </div>
          </div>
          <input id="groups" name="groups" type="hidden" value="">
          <div class="hint">В режиме групп занятость считается только по выбранным группам.</div>
        </div>

        <div class="two">
          <div class="field">
            <label for="date">День</label>
            <input id="date" name="date" type="date" required>
            <div class="hint">По умолчанию показывается сегодняшний день.</div>
          </div>
          <div class="field">
            <label for="availability-filter">Показывать</label>
            <select id="availability-filter" name="availability_filter">
              <option value="free" selected>Только свободные</option>
              <option value="all">Все аудитории</option>
            </select>
          </div>
        </div>

        <div class="field">
          <label>Неделя</label>
          <div class="inline-row">
            <button id="enable-week-view" class="chip toggle" type="button">Вся неделя</button>
            <div id="week-chip" class="chip" hidden>
              <span id="week-chip-label">Неделя</span>
              <button id="disable-week-view" type="button" aria-label="Выключить недельный режим">×</button>
            </div>
          </div>
          <div id="week-field" hidden>
            <select id="week" name="week">
              <option value="0">Текущая</option>
              <option value="1">Следующая</option>
              <option value="2">Через 2 недели</option>
              <option value="3">Через 3 недели</option>
            </select>
            <div class="hint">В недельном режиме проверка идет сразу по ПН-СБ.</div>
          </div>
          <input id="view" name="view" type="hidden" value="day">
        </div>

        <div class="two">
          <div class="field">
            <label for="slot-input">Пары</label>
            <div class="picker" id="slot-picker">
              <div class="picker-surface" data-surface>
                <div class="picker-chips" data-chips></div>
                <input id="slot-input" class="picker-input" type="text" autocomplete="off" placeholder="Выберите одну или несколько пар">
              </div>
              <div class="picker-menu" data-menu>
                <div class="picker-status" data-status>Доступные интервалы пар</div>
                <div class="picker-options" data-options></div>
              </div>
            </div>
            <input id="slots" name="slots" type="hidden" value="">
            <input id="start" name="start" type="hidden" value="08:00">
            <input id="end" name="end" type="hidden" value="21:30">
          </div>
        </div>

        <button class="primary" type="submit">Обновить</button>
      </form>
    </aside>

    <main>
      <div class="toolbar">
        <h2>Результат</h2>
      </div>

      <section class="summary" id="summary" hidden>
        <div class="metric"><strong id="metric-free">0</strong><span>свободно</span></div>
        <div class="metric"><strong id="metric-busy">0</strong><span>занято</span></div>
        <div class="metric"><strong id="metric-total">0</strong><span>проверено</span></div>
        <div class="metric"><strong id="metric-errors">0</strong><span>ошибок</span></div>
      </section>

      <div id="notice" class="notice">Проверяю текущую пару...</div>
      <section id="results" class="results"></section>
    </main>
  </div>

  <script>
    const form = document.querySelector("#availability-form");
    const results = document.querySelector("#results");
    const notice = document.querySelector("#notice");
    const summary = document.querySelector("#summary");
    const groupsField = document.querySelector("#groups-field");
    const modeHint = document.querySelector("#mode-hint");
    const submitButton = form.querySelector("button[type='submit']");
    const slotTitle = document.querySelector("#slot-title");
    const slotSubtitle = document.querySelector("#slot-subtitle");
    const dateInput = document.querySelector("#date");
    const weekInput = document.querySelector("#week");
    const viewInput = document.querySelector("#view");
    const weekField = document.querySelector("#week-field");
    const weekChip = document.querySelector("#week-chip");
    const weekChipLabel = document.querySelector("#week-chip-label");
    const enableWeekViewButton = document.querySelector("#enable-week-view");
    const disableWeekViewButton = document.querySelector("#disable-week-view");
    const availabilityFilterInput = document.querySelector("#availability-filter");
    const lessonSlots = {{ lesson_slots_json | safe }};
    const serverCurrentSlot = {{ current_slot | safe }};
    const defaultClassrooms = {{ default_classrooms_json | safe }};
    const slotPicker = document.querySelector("#slot-picker");
    const startInput = document.querySelector("#start");
    const endInput = document.querySelector("#end");

    function formatDateInput(date) {
      const year = date.getFullYear();
      const month = String(date.getMonth() + 1).padStart(2, "0");
      const day = String(date.getDate()).padStart(2, "0");
      return `${year}-${month}-${day}`;
    }

    function minutesOfDay(date) {
      return date.getHours() * 60 + date.getMinutes();
    }

    function timeToMinutes(value) {
      const [hours, minutes] = value.split(":").map(Number);
      return hours * 60 + minutes;
    }

    function resolveCurrentSlot() {
      const now = new Date();
      const current = minutesOfDay(now);

      for (const slot of lessonSlots) {
        if (current >= timeToMinutes(slot.start) && current < timeToMinutes(slot.end)) {
          return { ...slot, state: "active" };
        }
        if (current < timeToMinutes(slot.start)) {
          return { ...slot, state: "upcoming" };
        }
      }

      return { ...lessonSlots[lessonSlots.length - 1], state: "finished" };
    }

    function pairLabel(index) {
      return `${index + 1} пара`;
    }

    function slotValue(slot) {
      return `${slot.start}-${slot.end}`;
    }

    function slotDisplay(slot) {
      const slotIndex = lessonSlots.findIndex((item) => item.start === slot.start && item.end === slot.end);
      return `${pairLabel(slotIndex)} · ${slot.start}-${slot.end}`;
    }

    function syncSlotSummary(values) {
      if (!values.length) {
        slotTitle.textContent = "Выбранные интервалы";
        slotSubtitle.textContent = "Выберите хотя бы одну пару.";
        return;
      }
      slotTitle.textContent = values.length === 1 ? "Выбран 1 интервал" : `Выбрано интервалов: ${values.length}`;
      slotSubtitle.textContent = values.join(", ");
    }

    function applyCurrentSlot() {
      dateInput.value = formatDateInput(new Date());
    }

    applyCurrentSlot();

    function weekLabel(value) {
      return weekInput.querySelector(`option[value="${value}"]`)?.textContent || "Неделя";
    }

    function syncViewMode() {
      const isWeekView = viewInput.value === "week";
      weekField.hidden = !isWeekView;
      weekChip.hidden = !isWeekView;
      enableWeekViewButton.hidden = isWeekView;
      dateInput.disabled = isWeekView;
      weekChipLabel.textContent = weekLabel(weekInput.value);
    }

    function currentMode() {
      return new FormData(form).get("mode");
    }

    function syncMode() {
      const mode = currentMode();
      groupsField.style.display = mode === "groups" ? "grid" : "none";
      modeHint.textContent = mode === "groups"
        ? "Занятость будет вычислена только по расписанию указанных групп."
        : "Если аудитории не выбраны, будут проверены все найденные аудитории.";
    }

    form.addEventListener("change", syncMode);
    syncMode();
    syncViewMode();

    enableWeekViewButton.addEventListener("click", () => {
      viewInput.value = "week";
      syncViewMode();
    });

    disableWeekViewButton.addEventListener("click", () => {
      viewInput.value = "day";
      syncViewMode();
    });

    weekInput.addEventListener("change", syncViewMode);

    function text(value) {
      return value == null || value === "" ? "Без названия" : String(value);
    }

    function renderRoom(room) {
      const row = document.createElement("article");
      row.className = "room";

      const name = document.createElement("div");
      name.className = "room-name";
      name.textContent = room.classroom;

      const lessons = document.createElement("div");
      lessons.className = "lessons";
      const isWeekView = Array.isArray(room.days) && room.days.length > 1;

      const roomDays = Array.isArray(room.days) ? room.days : [];
      roomDays.forEach((day) => {
        const dayBlock = document.createElement("div");
        dayBlock.className = "day-block";
        if (isWeekView) {
          const dayTitle = document.createElement("div");
          dayTitle.className = "day-title";
          dayTitle.textContent = `${day.day_label} · ${day.date}`;
          dayBlock.appendChild(dayTitle);
        }

        const dayIntervals = Array.isArray(day.intervals) ? day.intervals : [];
        dayIntervals.forEach((interval) => {
          const block = document.createElement("div");
          block.className = "interval-block";

          const head = document.createElement("div");
          head.className = "interval-head";
          const badge = document.createElement("div");
          const isGroupsMode = currentMode() === "groups";
          let badgeClass = "free";
          let badgeText = "Свободно";
          if (interval.status === "error") {
            badgeClass = "error";
            badgeText = "Ошибка";
          } else if (interval.status === "selected_busy") {
            badgeClass = "busy";
            badgeText = isGroupsMode ? "Занята выбранной группой" : "Занята";
          } else if (interval.status === "other_busy") {
            badgeClass = "other-busy";
            badgeText = "Занята другой группой";
          } else if (interval.status === "busy") {
            badgeClass = "busy";
            badgeText = "Занята";
          }
          badge.className = `badge ${badgeClass}`;
          badge.textContent = badgeText;
          const intervalTitle = document.createElement("strong");
          intervalTitle.textContent = `${interval.start_time}-${interval.end_time}`;
          head.append(badge, intervalTitle);
          block.appendChild(head);

          const selectedBusy = Array.isArray(interval.busy) ? interval.busy : [];
          const otherBusy = Array.isArray(interval.other_busy) ? interval.other_busy : [];

          if (selectedBusy.length === 0 && otherBusy.length === 0) {
            const note = document.createElement("div");
            note.className = "interval-note";
            note.textContent = interval.status === "error"
              ? (interval.error || room.error || "Не удалось проверить расписание.")
              : currentMode() === "groups"
                ? "Выбранные группы и другие занятия в этой аудитории на этот интервал не найдены."
                : "На этот интервал занятий не найдено.";
            block.appendChild(note);
          } else {
            selectedBusy.forEach((event) => {
              const lesson = document.createElement("div");
              lesson.className = "lesson";
              const teachers = Array.isArray(event.teachers) && event.teachers.length
                ? ` · ${event.teachers.join(", ")}`
                : "";
              const discipline = document.createElement("strong");
              discipline.textContent = text(event.discipline);
              lesson.append(discipline, document.createElement("br"), `${text(event.group)}${teachers}`);
              block.appendChild(lesson);
            });

            if (otherBusy.length) {
              const note = document.createElement("div");
              note.className = "interval-note";
              note.textContent = "Аудитория занята другой группой.";
              block.appendChild(note);
            }
          }

          dayBlock.appendChild(block);
        });

        lessons.appendChild(dayBlock);
      });

      row.append(name, lessons);
      return row;
    }

    function setNotice(message, isError = false) {
      notice.hidden = false;
      notice.className = isError ? "notice error" : "notice";
      notice.textContent = message;
    }

    function serializeValues(values) {
      return values.join("\\n");
    }

    function debounce(fn, delay = 250) {
      let timeoutId = null;
      return (...args) => {
        window.clearTimeout(timeoutId);
        timeoutId = window.setTimeout(() => fn(...args), delay);
      };
    }

    function closePicker(picker) {
      picker.classList.remove("open");
    }

    function closeAllPickers(except = null) {
      document.querySelectorAll(".picker.open").forEach((picker) => {
        if (picker !== except) {
          closePicker(picker);
        }
      });
    }

    function createMultiSelectPicker(config) {
      const root = document.querySelector(config.root);
      const surface = root.querySelector("[data-surface]");
      const input = root.querySelector(".picker-input");
      const menu = root.querySelector("[data-menu]");
      const status = root.querySelector("[data-status]");
      const options = root.querySelector("[data-options]");
      const chips = root.querySelector("[data-chips]");
      const hiddenInput = document.querySelector(config.hiddenInput);
      const state = {
        values: [...(config.initialValues || [])],
        items: [],
        loading: false,
        query: "",
      };

      function syncHiddenInput() {
        hiddenInput.value = serializeValues(state.values);
      }

      function renderChips() {
        chips.innerHTML = "";
        state.values.forEach((value) => {
          const chip = document.createElement("div");
          chip.className = "chip";
          const displayValue = typeof config.displayValue === "function" ? config.displayValue(value) : value;
          chip.innerHTML = `<span>${displayValue}</span>`;
          const remove = document.createElement("button");
          remove.type = "button";
          remove.setAttribute("aria-label", `Убрать ${value}`);
          remove.textContent = "×";
          remove.addEventListener("click", (event) => {
            event.stopPropagation();
            state.values = state.values.filter((item) => item !== value);
            renderChips();
            renderOptions();
            syncHiddenInput();
            if (typeof config.onChange === "function") {
              config.onChange([...state.values]);
            }
          });
          chip.appendChild(remove);
          chips.appendChild(chip);
        });
      }

      function renderOptions() {
        options.innerHTML = "";
        const availableItems = state.items.filter((item) => !state.values.includes(item));

        if (state.loading) {
          status.textContent = "Загрузка...";
          return;
        }

        if (!availableItems.length) {
          status.textContent = state.query
            ? "Ничего не найдено."
            : config.emptyPrompt;
          return;
        }

        status.textContent = config.caption;
        availableItems.forEach((item) => {
          const option = document.createElement("button");
          option.type = "button";
          option.className = "picker-option";
          option.textContent = typeof config.displayValue === "function" ? config.displayValue(item) : item;
          option.addEventListener("click", () => {
            state.values.push(item);
            state.query = "";
            input.value = "";
            renderChips();
            renderOptions();
            syncHiddenInput();
            if (typeof config.onChange === "function") {
              config.onChange([...state.values]);
            }
            if (config.keepOpen) {
              input.focus();
            } else {
              closePicker(root);
            }
          });
          options.appendChild(option);
        });
      }

      async function loadOptions(query) {
        if (Array.isArray(config.staticItems)) {
          const normalizedQuery = normalizeSearch(query);
          state.items = config.staticItems.filter((item) => {
            if (!normalizedQuery) {
              return true;
            }
            const displayValue = typeof config.displayValue === "function" ? config.displayValue(item) : item;
            return normalizeSearch(displayValue).includes(normalizedQuery);
          });
          state.loading = false;
          renderOptions();
          return;
        }

        state.loading = true;
        state.query = query;
        renderOptions();

        try {
          const params = new URLSearchParams();
          if (query) {
            params.set("q", query);
          }
          params.set("limit", "20");
          const response = await fetch(`${config.endpoint}?${params.toString()}`);
          const payload = await response.json();
          if (!response.ok) {
            throw new Error(payload.error || "Не удалось загрузить список.");
          }
          state.items = Array.isArray(payload.items) ? payload.items : [];
        } catch (error) {
          state.items = [];
          status.textContent = error.message;
        } finally {
          state.loading = false;
          renderOptions();
        }
      }

      const debouncedLoad = debounce((value) => {
        if (!value && config.requireQuery) {
          state.items = [];
          state.loading = false;
          state.query = "";
          renderOptions();
          return;
        }
        loadOptions(value);
      }, 220);

      surface.addEventListener("click", () => {
        closeAllPickers(root);
        root.classList.add("open");
        input.focus();
        if (!state.items.length && (!config.requireQuery || input.value.trim())) {
          loadOptions(input.value.trim());
        } else {
          renderOptions();
        }
      });

      input.addEventListener("input", () => {
        root.classList.add("open");
        debouncedLoad(input.value.trim());
      });

      input.addEventListener("focus", () => {
        closeAllPickers(root);
        root.classList.add("open");
        if (!state.items.length && Array.isArray(config.staticItems)) {
          loadOptions(input.value.trim());
        } else {
          renderOptions();
        }
      });

      root.addEventListener("keydown", (event) => {
        if (event.key === "Escape") {
          closePicker(root);
          input.blur();
        }
      });

      state.values = Array.from(new Set(state.values.filter(Boolean)));
      state.items = Array.isArray(config.staticItems) ? [...config.staticItems] : state.items;
      renderChips();
      renderOptions();
      syncHiddenInput();
      if (typeof config.onChange === "function") {
        config.onChange([...state.values]);
      }
    }

    function normalizeSearch(value) {
      return String(value || "").trim().toLowerCase();
    }

    createMultiSelectPicker({
      root: "#classroom-picker",
      hiddenInput: "#classrooms",
      endpoint: "/api/classrooms",
      caption: "Выберите аудиторию",
      emptyPrompt: "Введите название аудитории или оставьте поле пустым для всех аудиторий.",
      requireQuery: false,
      keepOpen: true,
      initialValues: defaultClassrooms,
    });

    createMultiSelectPicker({
      root: "#group-picker",
      hiddenInput: "#groups",
      endpoint: "/api/groups",
      caption: "Выберите группу",
      emptyPrompt: "Начните вводить название группы.",
      requireQuery: true,
      keepOpen: true,
      initialValues: [],
    });

    const defaultSlot = lessonSlots.length ? resolveCurrentSlot() : serverCurrentSlot;
    createMultiSelectPicker({
      root: "#slot-picker",
      hiddenInput: "#slots",
      endpoint: "",
      caption: "Выберите пары",
      emptyPrompt: "Выберите одну или несколько пар.",
      requireQuery: false,
      keepOpen: true,
      initialValues: [slotValue(defaultSlot)],
      staticItems: lessonSlots.map((slot) => slotValue(slot)),
      displayValue: (value) => {
        const [start, end] = value.split("-");
        return slotDisplay({ start, end });
      },
      onChange: (values) => {
        syncSlotSummary(values);
        if (values.length) {
          const [start, end] = values[0].split("-");
          startInput.value = start;
          endInput.value = end;
        } else {
          startInput.value = "";
          endInput.value = "";
        }
      },
    });

    document.addEventListener("click", (event) => {
      if (!event.target.closest(".picker")) {
        closeAllPickers();
      }
    });

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!document.querySelector("#slots").value.trim()) {
        setNotice("Выберите хотя бы одну пару.", true);
        return;
      }
      submitButton.disabled = true;
      submitButton.textContent = "Проверяю...";
      results.innerHTML = "";
      summary.hidden = true;
      setNotice("Идет запрос к расписанию...");

      const params = new URLSearchParams(new FormData(form));

      try {
        const response = await fetch(`/api/free-classrooms?${params.toString()}`);
        const payload = await response.json();

        if (!response.ok) {
          setNotice(payload.error || "Не удалось получить данные.", true);
          return;
        }

        const filterMode = availabilityFilterInput.value;
        let visibleRooms = Array.isArray(payload.rooms) ? [...payload.rooms] : [];
        if (filterMode === "free") {
          visibleRooms = visibleRooms.filter((room) => room.status === "free");
        }
        document.querySelector("#metric-free").textContent = visibleRooms.filter((room) => room.status === "free").length;
        document.querySelector("#metric-busy").textContent = visibleRooms.filter((room) => room.status === "busy").length;
        document.querySelector("#metric-total").textContent = visibleRooms.length;
        document.querySelector("#metric-errors").textContent = visibleRooms.filter((room) => room.status === "error").length;
        summary.hidden = false;

        const weekText = payload.week && payload.week.week_start_date
          ? ` Неделя: ${payload.week.week_start_date}.`
          : "";
        const intervalsText = Array.isArray(payload.intervals) && payload.intervals.length
          ? ` Интервалы: ${payload.intervals.map((item) => `${item.start_time}-${item.end_time}`).join(", ")}.`
          : "";
        const daysText = Array.isArray(payload.days) && payload.days.length > 1
          ? ` Дни: ${payload.days[0].date} - ${payload.days[payload.days.length - 1].date}.`
          : Array.isArray(payload.days) && payload.days.length === 1
            ? ` День: ${payload.days[0].date}.`
          : "";
        const errorText = payload.errors.length
          ? ` Не удалось проверить: ${payload.errors.map((item) => item.source).join(", ")}.`
          : "";
        setNotice(`${daysText}${intervalsText} Показано ${visibleRooms.length}.${weekText}${errorText}`);

        if (!visibleRooms.length) {
          results.innerHTML = `<div class="empty">${filterMode === "free" ? "По выбранным параметрам свободные аудитории не найдены." : "По выбранным параметрам аудитории не найдены."}</div>`;
          return;
        }

        visibleRooms
          .sort((a, b) => a.status.localeCompare(b.status) || a.classroom.localeCompare(b.classroom, "ru"))
          .forEach((room) => results.appendChild(renderRoom(room)));
      } catch (error) {
        setNotice(`Ошибка запроса: ${error.message}`, true);
      } finally {
        submitButton.disabled = false;
        submitButton.textContent = "Обновить";
      }
    });

    setTimeout(() => form.requestSubmit(), 0);
  </script>
</body>
</html>
"""


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
