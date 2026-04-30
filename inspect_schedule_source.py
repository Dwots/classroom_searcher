from __future__ import annotations

from http.cookiejar import CookieJar
import html
import json
import re
import sys
from typing import Any
from urllib.request import HTTPCookieProcessor, build_opener

from app import ROOT_URL, SOURCE_TIMEOUT


STATE_FILE = "debug_livewire_state{suffix}.json"
HTML_FILE = "debug_schedule_page{suffix}.html"
KEYWORDS = (
    "ауд",
    "аудит",
    "кабин",
    "classroom",
    "room",
    "place",
    "address",
    "group",
    "search",
    "select",
)


def compact(value: Any, depth: int = 0) -> Any:
    if depth > 5:
        return "..."

    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 40:
                result["..."] = f"{len(value) - index} more keys"
                break
            result[str(key)] = compact(item, depth + 1)
        return result

    if isinstance(value, list):
        result = [compact(item, depth + 1) for item in value[:10]]
        if len(value) > 10:
            result.append(f"... {len(value) - 10} more items")
        return result

    if isinstance(value, str) and len(value) > 500:
        return value[:500] + f"... ({len(value)} chars total)"

    return value


def build_url(path: str) -> str:
    clean_path = path.strip()
    if clean_path.startswith("http://") or clean_path.startswith("https://"):
        return clean_path
    if not clean_path or clean_path == "/":
        return ROOT_URL
    return ROOT_URL.rstrip("/") + "/" + clean_path.strip("/")


def file_suffix(path: str) -> str:
    clean_path = path.strip().strip("/")
    if not clean_path:
        return ""
    return "_" + re.sub(r"[^a-zA-Z0-9_-]+", "_", clean_path)


def fetch_page(url: str, html_file: str) -> str:
    opener = build_opener(HTTPCookieProcessor(CookieJar()))
    with opener.open(url, timeout=SOURCE_TIMEOUT) as response:
        page_html = response.read().decode("utf-8")

    with open(html_file, "w", encoding="utf-8") as file:
        file.write(page_html)

    return page_html


def extract_state(page_html: str, state_file: str) -> dict[str, Any]:
    state_match = re.search(r'wire:initial-data="([^"]+)"', page_html)
    if state_match is None:
        raise RuntimeError("Cannot find Livewire initial state")

    state = json.loads(html.unescape(state_match.group(1)))
    with open(state_file, "w", encoding="utf-8") as file:
        json.dump(state, file, ensure_ascii=False, indent=2)

    return state


def print_json(title: str, value: Any) -> None:
    print(f"\n==== {title} ====")
    print(json.dumps(compact(value), ensure_ascii=False, indent=2))


def print_unique(title: str, values: list[str], limit: int = 120) -> None:
    print(f"\n==== {title} ====")
    seen: set[str] = set()
    unique_values: list[str] = []
    for value in values:
        cleaned = re.sub(r"\s+", " ", html.unescape(value)).strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            unique_values.append(cleaned)

    for value in unique_values[:limit]:
        print(value)

    if len(unique_values) > limit:
        print(f"... {len(unique_values) - limit} more")


def print_keyword_snippets(page_html: str) -> None:
    print("\n==== keyword snippets ====")
    lowered = page_html.casefold()
    printed = 0

    for keyword in KEYWORDS:
        start = 0
        while True:
            index = lowered.find(keyword.casefold(), start)
            if index == -1:
                break
            snippet_start = max(0, index - 180)
            snippet_end = min(len(page_html), index + 260)
            snippet = html.unescape(page_html[snippet_start:snippet_end])
            snippet = re.sub(r"\s+", " ", snippet).strip()
            print(f"\n-- {keyword} --")
            print(snippet)
            printed += 1
            start = index + len(keyword)
            if printed >= 40:
                print("\n... snippet limit reached")
                return

    if printed == 0:
        print("No keyword snippets found")


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else "/"
    url = build_url(path)
    suffix = file_suffix(path)
    html_file = HTML_FILE.format(suffix=suffix)
    state_file = STATE_FILE.format(suffix=suffix)

    page_html = fetch_page(url, html_file)
    state = extract_state(page_html, state_file)
    data = state.get("serverMemo", {}).get("data", {})

    print(f"Fetched {url}")
    print(f"Saved HTML to {html_file}")
    print(f"Saved Livewire state to {state_file}")

    print_json("fingerprint", state.get("fingerprint"))
    print_json("serverMemo.data keys", list(data.keys()) if isinstance(data, dict) else data)
    print_json("serverMemo.data", data)

    wire_attrs = re.findall(r'wire:[\w.-]+="[^"]*"', page_html)
    print_unique("wire attributes", wire_attrs)

    form_tags = re.findall(r"<(?:input|select|option|button|textarea)\b[^>]*>", page_html, flags=re.IGNORECASE)
    print_unique("form tags", form_tags)

    print_keyword_snippets(page_html)


if __name__ == "__main__":
    main()
