from __future__ import annotations

from collections import Counter
import json
import sys
from typing import Any
from urllib.parse import quote
from urllib.request import Request, urlopen


BASE_URL = "https://eralas.ru/api"
TIMEOUT = 20
CLASSROOM_KEYS = ("classroom", "auditory", "room", "place", "address")


def fetch_json(url: str) -> Any:
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "ScheduleAuditoryInspector/1.0"})
    with urlopen(request, timeout=TIMEOUT) as response:
        raw = response.read().decode("utf-8")
    return json.loads(raw)


def short(value: Any, limit: int = 1200) -> str:
    text = json.dumps(value, ensure_ascii=False, indent=2)
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... ({len(text)} chars total)"


def as_group_name(item: Any) -> str | None:
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        for key in ("name", "group", "title"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def extract_classroom(item: dict[str, Any]) -> str | None:
    for key in CLASSROOM_KEYS:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())
    return None


def inspect_endpoint(path: str) -> None:
    url = f"{BASE_URL}{path}"
    print(f"\n==== {url} ====")
    try:
        payload = fetch_json(url)
        print(short(payload))
    except Exception as error:
        print(type(error).__name__, str(error))


def main() -> None:
    inspect_endpoint("/groups")
    inspect_endpoint("/teachers")
    inspect_endpoint("/classrooms")
    inspect_endpoint("/rooms")

    try:
        groups_payload = fetch_json(f"{BASE_URL}/groups")
    except Exception as error:
        print("\nCannot fetch groups:", type(error).__name__, str(error))
        return

    if not isinstance(groups_payload, list):
        print("\n/groups did not return a list")
        return

    cli_groups = sys.argv[1:]
    groups = cli_groups or [name for item in groups_payload if (name := as_group_name(item))][:8]
    classroom_counter: Counter[str] = Counter()
    schedule_samples: list[dict[str, Any]] = []

    for group in groups:
        url = f"{BASE_URL}/schedule?group={quote(group)}&week=0"
        print(f"\n==== schedule: {group} ====")
        try:
            schedule = fetch_json(url)
        except Exception as error:
            print(type(error).__name__, str(error))
            continue

        if not isinstance(schedule, list):
            print(short(schedule))
            continue

        print(f"items: {len(schedule)}")
        if schedule:
            print("first item:")
            print(short(schedule[0]))

        for item in schedule:
            if not isinstance(item, dict):
                continue
            classroom = extract_classroom(item)
            if classroom:
                classroom_counter[classroom] += 1
            if len(schedule_samples) < 10:
                schedule_samples.append(item)

    print("\n==== classroom candidates ====")
    for classroom, count in classroom_counter.most_common(100):
        print(f"{classroom}: {count}")

    print("\n==== observed schedule keys ====")
    keys = sorted({key for item in schedule_samples for key in item.keys()})
    print(json.dumps(keys, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
