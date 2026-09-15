import io
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import URLError

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from canvas_agent import (  # noqa: E402
    AgentError,
    CanvasClient,
    Config,
    build_report,
    next_page_url,
    normalize_items,
    validate_base_url,
    write_report,
)


class FakeResponse:
    def __init__(self, body, link=None):
        self.body = body.encode("utf-8")
        self.headers = {"Link": link} if link else {}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self.body


class FakeOpener:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        if not self.responses:
            raise URLError("no response")
        return self.responses.pop(0)


class CanvasAgentTests(unittest.TestCase):
    def setUp(self):
        self.config = Config("https://canvas.example.edu", "super-secret-token")

    def test_base_url_requires_clean_https_origin(self):
        with self.assertRaises(AgentError):
            validate_base_url("http://canvas.example.edu")
        with self.assertRaises(AgentError):
            validate_base_url("https://canvas.example.edu/courses")

    def test_client_rejects_writes_before_network(self):
        opener = FakeOpener([])
        client = CanvasClient(self.config, opener=opener)
        with self.assertRaises(AgentError):
            client.request_json("POST", "https://canvas.example.edu/api/v1/planner/items")
        self.assertEqual(opener.requests, [])

    def test_client_rejects_wrong_host_path_and_url_token(self):
        client = CanvasClient(self.config, opener=FakeOpener([]))
        blocked = [
            "https://evil.example/api/v1/planner/items",
            "https://canvas.example.edu/api/v1/courses",
            "https://canvas.example.edu/api/v1/planner/items?access_token=secret",
        ]
        for url in blocked:
            with self.subTest(url=url), self.assertRaises(AgentError):
                client._validate_url(url)

    def test_pagination_follows_safe_next_link(self):
        next_url = "https://canvas.example.edu/api/v1/planner/items?page=2"
        opener = FakeOpener([
            FakeResponse('[{"id": 1}]', f'<{next_url}>; rel="next"'),
            FakeResponse('[{"id": 2}]'),
        ])
        client = CanvasClient(self.config, opener=opener)
        now = datetime(2026, 9, 15, tzinfo=timezone.utc)
        items = client.get_planner_items(now)
        self.assertEqual([item["id"] for item in items], [1, 2])
        self.assertEqual(len(opener.requests), 2)
        auth_header = opener.requests[0][0].get_header("Authorization")
        self.assertEqual(auth_header, "Bearer super-secret-token")
        self.assertNotIn("super-secret-token", opener.requests[0][0].full_url)

    def test_next_link_parser(self):
        header = '<https://canvas.example.edu/api/v1/planner/items?page=1>; rel="current", <https://canvas.example.edu/api/v1/planner/items?page=2>; rel="next"'
        self.assertEqual(next_page_url(header), "https://canvas.example.edu/api/v1/planner/items?page=2")

    def test_items_are_filtered_deduplicated_and_sorted(self):
        now = datetime(2026, 9, 15, 0, 0, tzinfo=timezone.utc)
        raw = [
            {"plannable_id": 2, "context_name": "B", "plannable_date": "2026-09-17T00:00:00Z", "plannable": {"title": "Later"}},
            {"plannable_id": 1, "context_name": "A", "plannable_date": "2026-09-16T00:00:00Z", "plannable": {"title": "Soon"}},
            {"plannable_id": 1, "context_name": "A", "plannable_date": "2026-09-16T00:00:00Z", "plannable": {"title": "Soon"}},
            {"plannable_id": 3, "context_name": "C", "plannable_date": "2026-10-01T00:00:00Z", "plannable": {"title": "Too late"}},
        ]
        items = normalize_items(raw, now)
        self.assertEqual([item["title"] for item in items], ["Soon", "Later"])

    def test_relative_canvas_link_becomes_absolute(self):
        now = datetime(2026, 9, 15, 0, 0, tzinfo=timezone.utc)
        raw = [{
            "plannable_id": 1,
            "context_name": "MKT 382",
            "plannable_date": "2026-09-16T00:00:00Z",
            "plannable": {"title": "Quiz", "html_url": "/courses/1/quizzes/2"},
        }]
        items = normalize_items(raw, now, "https://canvas.example.edu")
        self.assertEqual(items[0]["link"], "https://canvas.example.edu/courses/1/quizzes/2")

    def test_report_writes_expected_content(self):
        now = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
        items = [{"title": "Quiz", "course": "MKT 382", "due": now, "status": "Incomplete", "link": "https://canvas.example/quiz"}]
        report = build_report(items, now, "test")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "latest.md"
            write_report(report, path)
            content = path.read_text(encoding="utf-8")
        self.assertIn("Quiz", content)
        self.assertIn("MKT 382", content)
        self.assertIn("Open in Canvas", content)


if __name__ == "__main__":
    unittest.main()
