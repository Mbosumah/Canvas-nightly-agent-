#!/usr/bin/env python3
"""Read-only Canvas deadline briefing agent."""

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo


PROJECT_DIR = Path(__file__).resolve().parent
ENV_PATH = PROJECT_DIR / ".env"
REPORT_PATH = PROJECT_DIR / "reports" / "latest.md"
LOG_PATH = PROJECT_DIR / "logs" / "agent.log"
CENTRAL = ZoneInfo("America/Chicago")
PLANNER_PATH = "/api/v1/planner/items"
ALLOWED_PATHS = frozenset({PLANNER_PATH})
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
NEXT_LINK_RE = re.compile(r'<([^>]+)>\s*;\s*rel="?next"?', re.IGNORECASE)


class AgentError(Exception):
    """An expected, safely reportable agent error."""


@dataclass(frozen=True)
class Config:
    base_url: str
    token: str

    @classmethod
    def from_env(cls, path: Path = ENV_PATH) -> "Config":
        values = load_env_file(path)
        base_url = os.environ.get("CANVAS_BASE_URL", values.get("CANVAS_BASE_URL", "")).strip()
        token = os.environ.get("CANVAS_TOKEN", values.get("CANVAS_TOKEN", "")).strip()
        if not base_url:
            raise AgentError(f"CANVAS_BASE_URL is missing from {path}")
        if not token:
            raise AgentError(f"CANVAS_TOKEN is missing from {path}")
        validate_base_url(base_url)
        return cls(base_url=base_url.rstrip("/"), token=token)


def load_env_file(path: Path) -> Dict[str, str]:
    """Load the two simple KEY=VALUE settings without third-party packages."""
    if not path.exists():
        raise AgentError(f"Configuration file not found: {path}")
    values: Dict[str, str] = {}
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise AgentError(f"Invalid configuration line {line_number} in {path}")
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key] = value
    return values


def validate_base_url(base_url: str) -> None:
    parsed = urlsplit(base_url)
    if parsed.scheme.lower() != "https":
        raise AgentError("CANVAS_BASE_URL must use HTTPS")
    if not parsed.hostname or parsed.username or parsed.password:
        raise AgentError("CANVAS_BASE_URL must be a plain HTTPS Canvas origin")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise AgentError("CANVAS_BASE_URL must not include a path, query, or fragment")


class SafeRedirectHandler(HTTPRedirectHandler):
    """Allow redirects only when they remain within the guarded Canvas boundary."""

    def __init__(self, validator):
        super().__init__()
        self.validator = validator

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if req.get_method().upper() != "GET":
            raise AgentError("Redirect blocked: only GET is permitted")
        self.validator(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class CanvasClient:
    """A deliberately narrow Canvas client that cannot perform writes."""

    def __init__(self, config: Config, opener=None, sleeper=time.sleep):
        validate_base_url(config.base_url)
        self.config = config
        self._origin = urlsplit(config.base_url)
        self._opener = opener or build_opener(SafeRedirectHandler(self._validate_url))
        self._sleep = sleeper

    def _validate_url(self, url: str) -> None:
        parsed = urlsplit(url)
        expected_port = self._origin.port or 443
        actual_port = parsed.port or 443
        if parsed.scheme.lower() != "https":
            raise AgentError("Request blocked: HTTPS is required")
        if parsed.hostname != self._origin.hostname or actual_port != expected_port:
            raise AgentError("Request blocked: unexpected host")
        if parsed.username or parsed.password:
            raise AgentError("Request blocked: credentials are not allowed in URLs")
        if parsed.path not in ALLOWED_PATHS:
            raise AgentError("Request blocked: API path is not allowlisted")
        if any(key.lower() == "access_token" for key, _ in parse_qsl(parsed.query, keep_blank_values=True)):
            raise AgentError("Request blocked: tokens are not allowed in URLs")

    def request_json(self, method: str, url: str) -> Tuple[Any, Optional[str]]:
        if method.upper() != "GET":
            raise AgentError("Request blocked: this agent only permits GET")
        self._validate_url(url)
        request = Request(
            url,
            method="GET",
            headers={
                "Authorization": f"Bearer {self.config.token}",
                "Accept": "application/json",
                "User-Agent": "MKT382-Canvas-Briefing/1.0",
            },
        )
        for attempt in range(3):
            try:
                with self._opener.open(request, timeout=15) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                    return payload, response.headers.get("Link")
            except HTTPError as exc:
                if exc.code in RETRYABLE_STATUS and attempt < 2:
                    retry_after = exc.headers.get("Retry-After", "") if exc.headers else ""
                    delay = min(float(retry_after), 10.0) if retry_after.isdigit() else float(2 ** attempt)
                    self._sleep(delay)
                    continue
                if exc.code == 401:
                    raise AgentError("Canvas rejected the credentials (HTTP 401)") from None
                if exc.code == 403:
                    raise AgentError("Canvas denied access to this read-only endpoint (HTTP 403)") from None
                if exc.code == 429:
                    raise AgentError("Canvas rate limit persisted after retries (HTTP 429)") from None
                raise AgentError(f"Canvas returned HTTP {exc.code}") from None
            except (URLError, TimeoutError):
                if attempt < 2:
                    self._sleep(float(2 ** attempt))
                    continue
                raise AgentError("Canvas could not be reached after three attempts") from None
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise AgentError("Canvas returned an unreadable response") from None
        raise AgentError("Canvas request failed")

    def get_planner_items(self, now: datetime) -> List[Dict[str, Any]]:
        end = now + timedelta(days=7)
        params = {
            "start_date": now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "end_date": end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "filter": "incomplete_items",
            "per_page": 100,
        }
        url = urljoin(self.config.base_url + "/", PLANNER_PATH.lstrip("/")) + "?" + urlencode(params)
        items: List[Dict[str, Any]] = []
        seen_urls = set()
        while url:
            if url in seen_urls or len(seen_urls) >= 50:
                raise AgentError("Canvas pagination was invalid or unexpectedly large")
            seen_urls.add(url)
            payload, link_header = self.request_json("GET", url)
            if not isinstance(payload, list):
                raise AgentError("Canvas planner response was not a list")
            items.extend(item for item in payload if isinstance(item, dict))
            url = next_page_url(link_header)
            if url:
                self._validate_url(url)
        return items


def next_page_url(link_header: Optional[str]) -> Optional[str]:
    if not link_header:
        return None
    match = NEXT_LINK_RE.search(link_header)
    return match.group(1) if match else None


def parse_canvas_datetime(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def normalize_items(
    raw_items: Iterable[Dict[str, Any]], now: datetime, base_url: str = ""
) -> List[Dict[str, Any]]:
    end = now + timedelta(days=7)
    normalized: List[Dict[str, Any]] = []
    seen = set()
    for raw in raw_items:
        plannable = raw.get("plannable") if isinstance(raw.get("plannable"), dict) else {}
        due_raw = plannable.get("due_at") or raw.get("plannable_date") or raw.get("due_at")
        due = parse_canvas_datetime(due_raw)
        if due is None or not (now <= due <= end):
            continue
        title = str(plannable.get("title") or plannable.get("name") or raw.get("title") or "Untitled Canvas item")
        course = str(raw.get("context_name") or plannable.get("context_name") or raw.get("course_name") or "Course not provided")
        link = str(plannable.get("html_url") or raw.get("html_url") or raw.get("plannable_url") or "")
        if link.startswith("/") and base_url:
            link = urljoin(base_url.rstrip("/") + "/", link.lstrip("/"))
        override = raw.get("planner_override") if isinstance(raw.get("planner_override"), dict) else {}
        submission = raw.get("submissions") if isinstance(raw.get("submissions"), dict) else {}
        if override.get("marked_complete"):
            status = "Completed"
        elif submission.get("submitted_at") or submission.get("workflow_state") in {"submitted", "graded", "pending_review"}:
            status = "Submitted"
        else:
            status = "Incomplete"
        item_id = raw.get("plannable_id") or plannable.get("id") or raw.get("id")
        identity = (str(item_id), title, due.isoformat())
        if identity in seen:
            continue
        seen.add(identity)
        normalized.append({"title": title, "course": course, "due": due, "status": status, "link": link})
    return sorted(normalized, key=lambda item: (item["due"], item["course"], item["title"]))


def markdown_text(value: str) -> str:
    return value.replace("\n", " ").replace("\r", " ").strip()


def build_report(items: Sequence[Dict[str, Any]], generated_at: datetime, source: str) -> str:
    local_now = generated_at.astimezone(CENTRAL)
    lines = [
        "# Canvas: Due in the Next 7 Days",
        "",
        f"Generated: {local_now.strftime('%A, %B %-d, %Y at %-I:%M %p %Z')}",
        f"Source: {source}",
        "",
    ]
    if not items:
        lines.extend(["No incomplete Canvas items are due within the next seven days.", ""])
        return "\n".join(lines)
    lines.extend([f"## {len(items)} incomplete item{'s' if len(items) != 1 else ''}", ""])
    for item in items:
        due_local = item["due"].astimezone(CENTRAL)
        lines.append(f"- **{markdown_text(item['title'])}** — {markdown_text(item['course'])}")
        lines.append(f"  - Due: {due_local.strftime('%A, %B %-d at %-I:%M %p %Z')}")
        lines.append(f"  - Status: {markdown_text(item['status'])}")
        if item["link"]:
            lines.append(f"  - [Open in Canvas]({item['link']})")
        lines.append("")
    return "\n".join(lines)


def write_report(report: str, path: Path = REPORT_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(report, encoding="utf-8")
    temporary.replace(path)


def notification_message(items: Sequence[Dict[str, Any]]) -> str:
    if not items:
        return "No incomplete items are due in the next 7 days."
    next_item = items[0]
    due = next_item["due"].astimezone(CENTRAL).strftime("%a at %-I:%M %p")
    count = len(items)
    return f"{count} item{'s' if count != 1 else ''} due this week. Next: {next_item['title']} — {due}."


def applescript_escape(value: str) -> str:
    clean = " ".join(value.split())[:240]
    return clean.replace("\\", "\\\\").replace('"', '\\"')


def send_notification(message: str) -> bool:
    script = f'display notification "{applescript_escape(message)}" with title "Canvas Briefing"'
    try:
        result = subprocess.run(
            ["/usr/bin/osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def setup_logging() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8"), logging.StreamHandler()],
    )


SAMPLE_ITEMS = [
    {
        "plannable_id": 101,
        "context_name": "MKT 382",
        "plannable_date": "2099-01-16T05:59:00Z",
        "plannable": {
            "title": "AI Agent Reflection",
            "html_url": "https://canvas.example.edu/courses/1/assignments/101",
        },
    },
    {
        "plannable_id": 102,
        "context_name": "Business Analytics",
        "plannable_date": "2099-01-18T18:00:00Z",
        "plannable": {
            "title": "Weekly Quiz",
            "html_url": "https://canvas.example.edu/courses/2/quizzes/102",
        },
    },
]


def dry_run() -> List[Dict[str, Any]]:
    fixture_now = datetime(2099, 1, 14, 12, 0, tzinfo=timezone.utc)
    items = normalize_items(SAMPLE_ITEMS, fixture_now)
    write_report(build_report(items, fixture_now, "fixture data (dry run)"))
    return items


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create a read-only Canvas deadline briefing.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Generate a report from built-in fixture data.")
    mode.add_argument("--check-config", action="store_true", help="Validate .env without contacting Canvas.")
    mode.add_argument("--test-notification", action="store_true", help="Display a test macOS notification.")
    parser.add_argument("--no-notify", action="store_true", help="Do not display a notification after a run.")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    setup_logging()
    try:
        if args.test_notification:
            if not send_notification("Test successful. Your Canvas briefing notifications are ready."):
                raise AgentError("The macOS test notification could not be displayed")
            print("Test notification sent.")
            return 0
        if args.check_config:
            config = Config.from_env()
            print(f"Configuration is valid for {urlsplit(config.base_url).hostname}; token is present and hidden.")
            return 0
        if args.dry_run:
            items = dry_run()
            logging.info("Dry run completed with %d fixture items", len(items))
        else:
            config = Config.from_env()
            now = datetime.now(timezone.utc)
            raw_items = CanvasClient(config).get_planner_items(now)
            items = normalize_items(raw_items, now, config.base_url)
            write_report(build_report(items, now, "live Canvas API"))
            logging.info("Live run completed with %d upcoming items", len(items))
        if not args.no_notify and not send_notification(notification_message(items)):
            logging.warning("Report was written, but the macOS notification failed")
        print(f"Report written to {REPORT_PATH}")
        return 0
    except AgentError as exc:
        logging.error("%s", exc)
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
