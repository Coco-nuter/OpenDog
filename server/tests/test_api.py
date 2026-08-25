import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import Settings, create_app


TOKEN = "test-token-with-at-least-24-characters"
PC_A_TOKEN = "pc-a-token-with-at-least-24-characters"
PC_B_TOKEN = "pc-b-token-with-at-least-24-characters"
ANDROID_TOKEN = "android-token-with-at-least-24-characters"
ANDROID_DEVICE_ID = "android_8d5dbc33-9dbe-4a94-aa33-726e2a3458aa"


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
                pc_b_token=PC_B_TOKEN,
                message_receivers={
                    "windows_pc_a": PC_A_TOKEN,
                    ANDROID_DEVICE_ID: ANDROID_TOKEN,
                },
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

    @staticmethod
    def message_payload(message_id: str = "message-001") -> dict:
        return {
            "message_id": message_id,
            "sender_id": "pc_b",
            "target_device_id": "windows_pc_a",
            "message_type": "popup_text",
            "title": "Test message",
            "body": "Visible message body",
            "payload": {"origin": "unit-test"},
        }

    def test_health(self) -> None:
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(response.json()["messages"], "enabled")

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
                pc_b_token=PC_B_TOKEN,
                message_receivers={"windows_pc_a": PC_A_TOKEN},
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

    def test_message_send_pull_and_ack(self) -> None:
        sender_headers = {"Authorization": f"Bearer {PC_B_TOKEN}"}
        receiver_headers = {"Authorization": f"Bearer {PC_A_TOKEN}"}
        payload = self.message_payload()

        first = self.client.post("/messages", json=payload, headers=sender_headers)
        duplicate = self.client.post(
            "/messages",
            json=payload,
            headers=sender_headers,
        )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json(), {"ok": True, "msg_seq": 1, "duplicate": False})
        self.assertEqual(duplicate.status_code, 200)
        self.assertTrue(duplicate.json()["duplicate"])

        pulled = self.client.get(
            "/messages/pull"
            "?target_device_id=windows_pc_a&after_seq=0&limit=20&wait_seconds=0",
            headers=receiver_headers,
        )
        self.assertEqual(pulled.status_code, 200)
        message = pulled.json()["messages"][0]
        self.assertEqual(message["body"], "Visible message body")
        self.assertEqual(message["payload"], {"origin": "unit-test"})

        acknowledged = self.client.post(
            "/messages/ack",
            json={
                "message_id": "message-001",
                "target_device_id": "windows_pc_a",
                "status": "shown",
            },
            headers=receiver_headers,
        )
        self.assertEqual(acknowledged.status_code, 200)
        self.assertEqual(acknowledged.json()["status"], "acknowledged")

        pulled_again = self.client.get(
            "/messages/pull"
            "?target_device_id=windows_pc_a&after_seq=0&limit=20&wait_seconds=0",
            headers=receiver_headers,
        )
        self.assertEqual(pulled_again.status_code, 200)
        self.assertEqual(pulled_again.json()["messages"], [])

        message_file = (
            self.data_dir
            / "messages"
            / "targets"
            / "windows_pc_a"
            / "messages.jsonl"
        )
        self.assertTrue(message_file.exists())
        self.assertEqual(len(message_file.read_text(encoding="utf-8").splitlines()), 1)
        self.assertTrue((self.data_dir / "messages" / "timeline.jsonl").exists())

    def test_message_tokens_and_target_are_restricted(self) -> None:
        payload = self.message_payload()
        missing_auth = self.client.post("/messages", json=payload)
        wrong_role = self.client.post(
            "/messages",
            json=payload,
            headers={"Authorization": f"Bearer {PC_A_TOKEN}"},
        )
        self.assertEqual(missing_auth.status_code, 401)
        self.assertEqual(wrong_role.status_code, 403)

        payload["target_device_id"] = "another_device"
        wrong_target = self.client.post(
            "/messages",
            json=payload,
            headers={"Authorization": f"Bearer {PC_B_TOKEN}"},
        )
        self.assertEqual(wrong_target.status_code, 403)

        read_wrong_target = self.client.get(
            "/messages/pull"
            "?target_device_id=another_device&after_seq=0&wait_seconds=0",
            headers={"Authorization": f"Bearer {PC_A_TOKEN}"},
        )
        self.assertEqual(read_wrong_target.status_code, 403)

        android_payload = self.message_payload("message-android-001")
        android_payload["target_device_id"] = ANDROID_DEVICE_ID
        sent_to_android = self.client.post(
            "/messages",
            json=android_payload,
            headers={"Authorization": f"Bearer {PC_B_TOKEN}"},
        )
        self.assertEqual(sent_to_android.status_code, 200)

        android_pull = self.client.get(
            "/messages/pull"
            f"?target_device_id={ANDROID_DEVICE_ID}&after_seq=0&wait_seconds=0",
            headers={"Authorization": f"Bearer {ANDROID_TOKEN}"},
        )
        self.assertEqual(android_pull.status_code, 200)
        self.assertEqual(
            android_pull.json()["messages"][0]["message_id"],
            "message-android-001",
        )

        android_reading_pc_a = self.client.get(
            "/messages/pull"
            "?target_device_id=windows_pc_a&after_seq=0&wait_seconds=0",
            headers={"Authorization": f"Bearer {ANDROID_TOKEN}"},
        )
        pc_a_reading_android = self.client.get(
            "/messages/pull"
            f"?target_device_id={ANDROID_DEVICE_ID}&after_seq=0&wait_seconds=0",
            headers={"Authorization": f"Bearer {PC_A_TOKEN}"},
        )
        self.assertEqual(android_reading_pc_a.status_code, 403)
        self.assertEqual(pc_a_reading_android.status_code, 403)

        cross_device_ack = self.client.post(
            "/messages/ack",
            json={
                "message_id": "message-android-001",
                "target_device_id": ANDROID_DEVICE_ID,
                "status": "shown",
            },
            headers={"Authorization": f"Bearer {PC_A_TOKEN}"},
        )
        self.assertEqual(cross_device_ack.status_code, 403)

    def test_receiver_configuration_is_required_and_strict(self) -> None:
        receivers_path = self.data_dir / "receivers.json"
        receivers_path.write_text(
            json.dumps({"windows_pc_a": PC_A_TOKEN}),
            encoding="utf-8",
        )
        environment = {
            "OPENDOG_TOKEN": TOKEN,
            "OPENDOG_DATABASE_PATH": str(self.database_path),
            "OPENDOG_PC_B_TOKEN": PC_B_TOKEN,
            "OPENDOG_MESSAGE_RECEIVERS_FILE": str(receivers_path),
        }
        with patch.dict(os.environ, environment, clear=True):
            loaded = Settings.from_environment()
        self.assertEqual(loaded.message_receivers, {"windows_pc_a": PC_A_TOKEN})

        missing_file_environment = {
            key: value
            for key, value in environment.items()
            if key != "OPENDOG_MESSAGE_RECEIVERS_FILE"
        }
        with patch.dict(os.environ, missing_file_environment, clear=True):
            with self.assertRaises(RuntimeError):
                Settings.from_environment()

        receivers_path.write_text(
            json.dumps(
                {"windows_pc_a": PC_A_TOKEN, "android_device": PC_A_TOKEN}
            ),
            encoding="utf-8",
        )
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaises(RuntimeError):
                Settings.from_environment()

if __name__ == "__main__":
    unittest.main()
