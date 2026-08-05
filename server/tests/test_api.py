import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import Settings, create_app


TOKEN = "test-token-with-at-least-24-characters"


class IngestApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temporary_directory.name)
        self.database_path = self.data_dir / "indexes.sqlite3"
        app = create_app(
            Settings(
                token=TOKEN,
                database_path=self.database_path,
                data_dir=self.data_dir,
            )
        )
        self.client_context = TestClient(app)
        self.client = self.client_context.__enter__()

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)
        self.temporary_directory.cleanup()

    @staticmethod
    def payload() -> dict:
        return {
            "source": "pc_a",
            "device_id": "windows_pc_a",
            "events": [
                {
                    "event_id": "event-001",
                    "type": "focused_window_ocr",
                    "ts": 1782355279.482,
                    "data": {
                        "app": "chrome.exe",
                        "title": "Google Chrome",
                        "text": "OCR content",
                    },
                }
            ],
        }

    def test_health(self) -> None:
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])

    def test_authentication_is_required(self) -> None:
        response = self.client.post("/ingest", json=self.payload())
        self.assertEqual(response.status_code, 401)

    def test_ingest_and_duplicate_are_idempotent(self) -> None:
        headers = {"Authorization": f"Bearer {TOKEN}"}
        first = self.client.post("/ingest", json=self.payload(), headers=headers)
        second = self.client.post("/ingest", json=self.payload(), headers=headers)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["count"], 1)
        self.assertEqual(first.json()["duplicates"], 0)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["count"], 0)
        self.assertEqual(second.json()["duplicates"], 1)

        with closing(sqlite3.connect(self.database_path)) as connection:
            row = connection.execute(
                "SELECT source, device_id, device_path FROM event_index"
            ).fetchone()
        self.assertEqual(row[0], "pc_a")
        self.assertEqual(row[1], "windows_pc_a")

        device_file = self.data_dir / row[2]
        self.assertTrue(device_file.exists())
        device_event = json.loads(device_file.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(device_event["data"]["app"], "chrome.exe")

        timeline_file = self.data_dir / "timeline.jsonl"
        self.assertTrue(timeline_file.exists())
        timeline_event = json.loads(
            timeline_file.read_text(encoding="utf-8").splitlines()[0]
        )
        self.assertEqual(timeline_event["device_id"], "windows_pc_a")

    def test_unknown_event_fields_are_rejected(self) -> None:
        payload = self.payload()
        payload["events"][0]["unexpected"] = True
        response = self.client.post(
            "/ingest",
            json=payload,
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        self.assertEqual(response.status_code, 422)

    def test_request_body_limit(self) -> None:
        app = create_app(
            Settings(
                token=TOKEN,
                database_path=self.database_path,
                max_body_bytes=10,
            )
        )
        with TestClient(app) as client:
            response = client.post(
                "/ingest",
                json=self.payload(),
                headers={"Authorization": f"Bearer {TOKEN}"},
            )
        self.assertEqual(response.status_code, 413)

    def test_events_requires_authentication(self) -> None:
        response = self.client.get("/events")
        self.assertEqual(response.status_code, 401)

    def test_events_are_paginated_by_sequence(self) -> None:
        payload = self.payload()
        payload["events"] = [
            {
                **payload["events"][0],
                "event_id": f"event-{index:03d}",
                "ts": 1782355279.482 + index,
                "data": {"index": index},
            }
            for index in range(1, 4)
        ]
        headers = {"Authorization": f"Bearer {TOKEN}"}
        inserted = self.client.post("/ingest", json=payload, headers=headers)
        self.assertEqual(inserted.status_code, 200)

        first = self.client.get(
            "/events?after_seq=0&limit=2",
            headers=headers,
        )
        self.assertEqual(first.status_code, 200)
        first_body = first.json()
        self.assertEqual([item["seq"] for item in first_body["events"]], [1, 2])
        self.assertEqual(first_body["last_seq"], 2)
        self.assertTrue(first_body["has_more"])
        self.assertEqual(first_body["events"][0]["data"], {"index": 1})

        second = self.client.get(
            "/events?after_seq=2&limit=2",
            headers=headers,
        )
        self.assertEqual(second.status_code, 200)
        second_body = second.json()
        self.assertEqual([item["seq"] for item in second_body["events"]], [3])
        self.assertEqual(second_body["last_seq"], 3)
        self.assertFalse(second_body["has_more"])

    def test_events_are_read_by_time_range(self) -> None:
        payload = self.payload()
        payload["events"] = [
            {
                **payload["events"][0],
                "event_id": "event-old",
                "ts": 100.0,
                "data": {"name": "old"},
            },
            {
                **payload["events"][0],
                "event_id": "event-middle-a",
                "ts": 200.0,
                "data": {"name": "middle-a"},
            },
            {
                **payload["events"][0],
                "event_id": "event-middle-b",
                "ts": 200.0,
                "data": {"name": "middle-b"},
            },
            {
                **payload["events"][0],
                "event_id": "event-new",
                "ts": 300.0,
                "data": {"name": "new"},
            },
        ]
        headers = {"Authorization": f"Bearer {TOKEN}"}
        inserted = self.client.post("/ingest", json=payload, headers=headers)
        self.assertEqual(inserted.status_code, 200)

        first = self.client.get(
            "/events/range?start_ts=150&end_ts=250&limit=1",
            headers=headers,
        )
        self.assertEqual(first.status_code, 200)
        first_body = first.json()
        self.assertEqual(
            [item["data"]["name"] for item in first_body["events"]],
            ["middle-a"],
        )
        self.assertTrue(first_body["has_more"])
        self.assertIsNotNone(first_body["next_cursor"])

        second = self.client.get(
            "/events/range?start_ts=150&end_ts=250&limit=10"
            f"&cursor={first_body['next_cursor']}",
            headers=headers,
        )
        self.assertEqual(second.status_code, 200)
        second_body = second.json()
        self.assertEqual(
            [item["data"]["name"] for item in second_body["events"]],
            ["middle-b"],
        )
        self.assertFalse(second_body["has_more"])


if __name__ == "__main__":
    unittest.main()
