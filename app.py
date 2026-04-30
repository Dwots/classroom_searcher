from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
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
_classrooms_cache: tuple[float, list[str]] | None = None
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
    values = re.split(r"[\n,;]+", raw)
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


def build_busy_entry(event: dict[str, Any], source: str) -> dict[str, Any]:
    return {
        "source": source,
        "start_time": event.get("start_time"),
        "end_time": event.get("end_time"),
        "discipline": event.get("discipline"),
        "group": event.get("group"),
        "group_type": event.get("group_type"),
        "classroom": event.get("classroom"),
        "address": event.get("address"),
        "teachers": event.get("teachers") or [],
    }


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


def parse_availability_args() -> tuple[list[str], list[str], str, str, str, str, int, int]:
    classrooms = split_values(request.args.get("classrooms") or request.args.get("rooms"))
    groups = split_values(request.args.get("groups"))
    mode = request.args.get("mode", "rooms").strip().casefold()
    if mode not in {"rooms", "groups"}:
        raise ValueError("Mode must be 'rooms' or 'groups'")

    week_raw = request.args.get("week", "0").strip()
    try:
        week = int(week_raw)
    except ValueError as error:
        raise ValueError("Query parameter 'week' must be an integer") from error
    if week < 0:
        raise ValueError("Query parameter 'week' must be >= 0")

    date_raw = request.args.get("date", "").strip()
    target_date = parse_client_date(date_raw) if date_raw else datetime.now().strftime("%d.%m.%Y")

    slot = current_lesson_slot()
    start_time = request.args.get("start", "").strip() or slot["start"]
    end_time = request.args.get("end", "").strip() or slot["end"]
    start_minutes = parse_minutes(start_time)
    end_minutes = parse_minutes(end_time)
    if start_minutes >= end_minutes:
        raise ValueError("Start time must be earlier than end time")

    return classrooms, groups, mode, target_date, start_time, end_time, start_minutes, end_minutes


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


@app.get("/api/free-classrooms")
def get_free_classrooms() -> Any:
    try:
        (
            classrooms,
            groups,
            mode,
            target_date,
            start_time,
            end_time,
            start_minutes,
            end_minutes,
        ) = parse_availability_args()
    except ValueError as error:
        return jsonify({"error": str(error)}), 400

    if mode == "groups" and not groups:
        return jsonify({"error": "Query parameter 'groups' is required in groups mode"}), 400

    week = int(request.args.get("week", "0"))
    requested_classrooms = classrooms
    busy_by_room: dict[str, list[dict[str, Any]]] = {}
    errors: list[dict[str, str]] = []
    failed_rooms: set[str] = set()
    checked_sources: list[str] = []
    week_info: dict[str, Any] = {}

    if mode == "rooms":
        try:
            known_classrooms = cached_classrooms()
        except Exception as error:
            return jsonify({"error": "Failed to load classroom list", "reason": str(error)}), 502

        rooms_to_check = resolve_requested_classrooms(requested_classrooms, known_classrooms)
        busy_by_room = {room: [] for room in rooms_to_check}
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
            busy_by_room[room].extend(
                find_room_busy_entries(payload, room, target_date, start_minutes, end_minutes)
            )

    if mode == "groups":
        busy_by_room = {room: [] for room in requested_classrooms}
        payloads, fetch_errors = fetch_schedules_parallel(groups, week, GROUP_PAGE_URL)
        errors.extend(fetch_errors)

        for group, payload in payloads:
            checked_sources.append(group)
            if not week_info:
                week_info = {
                    "week_number": payload.get("week_number"),
                    "week_start_date": payload.get("week_start_date"),
                    "month": payload.get("month"),
                }
            for event in events_for_date(payload.get("days", []), target_date):
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
                    busy_by_room.setdefault(room, [])
                    if event_overlaps(event, start_minutes, end_minutes):
                        busy_by_room[room].append(build_busy_entry(event, group))

    rooms_payload: list[dict[str, Any]] = []
    error_by_room = {error["source"]: error["reason"] for error in errors}
    rooms = sorted(busy_by_room.keys(), key=classroom_sort_key)
    for room in rooms:
        busy_entries = sorted(
            busy_by_room.get(room, []),
            key=lambda item: (item.get("start_time") or "", item.get("discipline") or ""),
        )

        if room in failed_rooms:
            status = "error"
        else:
            status = "busy" if busy_entries else "free"

        rooms_payload.append(
            {
                "classroom": room,
                "status": status,
                "busy": busy_entries,
                "error": error_by_room.get(room),
            }
        )

    return jsonify(
        {
            "source": "schedule.siriusuniversity.ru",
            "mode": mode,
            "date": target_date,
            "start_time": start_time,
            "end_time": end_time,
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
      --bg: #f6f7f9;
      --panel: #ffffff;
      --panel-muted: #edf1f5;
      --text: #17202a;
      --muted: #66717f;
      --border: #d8dee6;
      --accent: #1d6f64;
      --accent-dark: #15534b;
      --danger: #ad2f2f;
      --free: #dff4e6;
      --busy: #ffe8df;
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
    textarea,
    select {
      width: 100%;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: #fff;
      color: var(--text);
      padding: 11px 12px;
      outline: none;
    }

    textarea {
      min-height: 112px;
      resize: vertical;
      line-height: 1.4;
    }

    input:focus,
    textarea:focus,
    select:focus {
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
      grid-template-columns: minmax(120px, 180px) 96px 1fr;
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

    .badge.error {
      background: #fff1c9;
      color: #7a5200;
    }

    .lessons {
      display: grid;
      gap: 8px;
      color: var(--muted);
      line-height: 1.35;
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
          <strong id="slot-title">Текущая пара</strong>
          <span id="slot-subtitle">Интервал будет выбран автоматически.</span>
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
          <div class="hint" id="mode-hint">Пустой список означает все аудитории, найденные в расписании групп.</div>
        </div>

        <div class="field">
          <label for="classrooms">Аудитории</label>
          <textarea id="classrooms" name="classrooms" placeholder="Оставьте пустым, чтобы показать все найденные аудитории">{{ default_classrooms }}</textarea>
          <div class="hint">Можно ограничить результат конкретными аудиториями через запятую или с новой строки.</div>
        </div>

        <div class="field" id="groups-field">
          <label for="groups">Группы</label>
          <textarea id="groups" name="groups" placeholder="Например: БИВТ-24-1&#10;БИВТ-24-2"></textarea>
          <div class="hint">В режиме групп аудитории считаются занятыми только по указанным группам.</div>
        </div>

        <div class="two">
          <div class="field">
            <label for="date">Дата</label>
            <input id="date" name="date" type="date" required>
          </div>
          <div class="field">
            <label for="week">Неделя</label>
            <select id="week" name="week">
              <option value="0">Текущая</option>
              <option value="1">Следующая</option>
              <option value="2">Через 2 недели</option>
              <option value="3">Через 3 недели</option>
            </select>
          </div>
        </div>

        <div class="two">
          <div class="field">
            <label for="start">С</label>
            <input id="start" name="start" type="time" value="08:00" required>
          </div>
          <div class="field">
            <label for="end">До</label>
            <input id="end" name="end" type="time" value="21:30" required>
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
    const lessonSlots = {{ lesson_slots_json | safe }};
    const serverCurrentSlot = {{ current_slot | safe }};

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

    function applyCurrentSlot() {
      const slot = lessonSlots.length ? resolveCurrentSlot() : serverCurrentSlot;
      document.querySelector("#date").value = formatDateInput(new Date());
      document.querySelector("#start").value = slot.start;
      document.querySelector("#end").value = slot.end;

      const stateText = slot.state === "active"
        ? "идет сейчас"
        : slot.state === "upcoming"
          ? "следующая пара"
          : "последняя пара на сегодня";
      slotTitle.textContent = `${slot.start}-${slot.end}`;
      slotSubtitle.textContent = stateText;
    }

    applyCurrentSlot();

    function currentMode() {
      return new FormData(form).get("mode");
    }

    function syncMode() {
      const mode = currentMode();
      groupsField.style.display = mode === "groups" ? "grid" : "none";
      modeHint.textContent = mode === "groups"
        ? "Занятость будет вычислена только по расписанию указанных групп."
        : "Пустой список означает все аудитории, найденные в расписании групп.";
    }

    form.addEventListener("change", syncMode);
    syncMode();

    function text(value) {
      return value == null || value === "" ? "Без названия" : String(value);
    }

    function renderRoom(room) {
      const row = document.createElement("article");
      row.className = "room";

      const name = document.createElement("div");
      name.className = "room-name";
      name.textContent = room.classroom;

      const badge = document.createElement("div");
      badge.className = `badge ${room.status}`;
      badge.textContent = room.status === "free" ? "Свободна" : room.status === "busy" ? "Занята" : "Ошибка";

      const lessons = document.createElement("div");
      lessons.className = "lessons";

      if (room.busy.length === 0) {
        lessons.textContent = room.status === "error"
          ? (room.error || "Не удалось проверить расписание.")
          : "На выбранный интервал занятий не найдено.";
      } else {
        room.busy.forEach((event) => {
          const lesson = document.createElement("div");
          lesson.className = "lesson";
          const teachers = Array.isArray(event.teachers) && event.teachers.length
            ? ` · ${event.teachers.join(", ")}`
            : "";
          const time = document.createElement("strong");
          time.textContent = `${text(event.start_time)}-${text(event.end_time)}`;
          lesson.append(time, ` ${text(event.discipline)}`, document.createElement("br"), `${text(event.group)}${teachers}`);
          lessons.appendChild(lesson);
        });
      }

      row.append(name, badge, lessons);
      return row;
    }

    function setNotice(message, isError = false) {
      notice.hidden = false;
      notice.className = isError ? "notice error" : "notice";
      notice.textContent = message;
    }

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
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

        document.querySelector("#metric-free").textContent = payload.summary.free;
        document.querySelector("#metric-busy").textContent = payload.summary.busy;
        document.querySelector("#metric-total").textContent = payload.summary.total;
        document.querySelector("#metric-errors").textContent = payload.summary.errors;
        summary.hidden = false;

        const weekText = payload.week && payload.week.week_start_date
          ? ` Неделя от ${payload.week.week_start_date}.`
          : "";
        const errorText = payload.errors.length
          ? ` Не удалось проверить: ${payload.errors.map((item) => item.source).join(", ")}.`
          : "";
        setNotice(`${payload.date}, ${payload.start_time}-${payload.end_time}. Проверено ${payload.summary.total}: свободно ${payload.summary.free}, занято ${payload.summary.busy}.${weekText}${errorText}`);

        payload.rooms
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
