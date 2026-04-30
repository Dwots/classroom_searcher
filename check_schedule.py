from __future__ import annotations

import json
import sys

from app import EralasScheduleClient, app


DEFAULT_LIMIT = 5


def print_schedule_samples(queries: list[str]) -> None:
    client = EralasScheduleClient()

    if not queries:
        groups = client.fetch_groups()
        print(f"Loaded groups: {len(groups)}")
        queries = groups[:DEFAULT_LIMIT]

    for query in queries:
        print(f"\n==== {query} ====")
        try:
            payload = client.fetch_schedule(query, 0)
            sample = {
                "group": payload.get("group"),
                "week_number": payload.get("week_number"),
                "week_start_date": payload.get("week_start_date"),
                "events_total": payload.get("events_total"),
                "days": payload.get("days", [])[:2],
            }
            print(json.dumps(sample, ensure_ascii=False, indent=2))
        except Exception as error:
            print(type(error).__name__, str(error))


def print_api_sample(queries: list[str]) -> None:
    classrooms = ",".join(queries)
    flask_client = app.test_client()
    query = f"?classrooms={classrooms}" if classrooms else ""
    response = flask_client.get(f"/api/free-classrooms{query}")

    print("\n==== /api/free-classrooms ====")
    print(response.status_code)
    print(response.get_data(as_text=True)[:5000])


def main() -> None:
    queries = sys.argv[1:]
    print_schedule_samples(queries)
    print_api_sample([])


if __name__ == "__main__":
    main()
