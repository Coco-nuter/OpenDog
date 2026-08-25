import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path

import uvicorn

from app.main import Settings, create_app
from pc_a_agent.receiver import acknowledge_message, append_inbox, pull_messages
from pc_b_reader.sender import send_message


EVENT_TOKEN = "integration-event-token-1234567890"
PC_A_TOKEN = "integration-pc-a-token-1234567890"
PC_B_TOKEN = "integration-pc-b-token-1234567890"


def available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class MessageClientIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temporary_directory.name)
        self.port = available_port()
        app = create_app(
            Settings(
                token=EVENT_TOKEN,
                database_path=self.data_dir / "indexes.sqlite3",
                data_dir=self.data_dir,
                pc_a_token=PC_A_TOKEN,
                pc_b_token=PC_B_TOKEN,
                pc_a_device_id="windows_pc_a",
            )
        )
        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=self.port,
            log_level="error",
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 5
        while not self.server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        if not self.server.started:
            self.fail("Test HTTP server did not start")

    def tearDown(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=5)
        self.temporary_directory.cleanup()

    def test_sender_and_receiver_over_http(self) -> None:
        server_url = f"http://127.0.0.1:{self.port}"
        sender_config = {
            "server_url": server_url,
            "message_token": PC_B_TOKEN,
            "request_timeout_seconds": 5,
            "use_proxy": False,
        }
        receiver_config = {
            "server_url": server_url,
            "message_token": PC_A_TOKEN,
            "device_id": "windows_pc_a",
            "message_batch_size": 20,
            "message_poll_wait_seconds": 0,
            "request_timeout_seconds": 5,
            "use_proxy": False,
        }
        payload = {
            "message_id": "integration-message-001",
            "sender_id": "pc_b",
            "target_device_id": "windows_pc_a",
            "message_type": "popup_text",
            "title": "Integration test",
            "body": "Message transport works",
            "payload": {},
            "expires_at": None,
        }

        sent = send_message(sender_config, payload)
        self.assertTrue(sent["ok"])
        self.assertFalse(sent["duplicate"])

        pulled = pull_messages(receiver_config, 0)
        self.assertEqual(len(pulled["messages"]), 1)
        message = pulled["messages"][0]
        self.assertEqual(message["body"], "Message transport works")

        inbox = self.data_dir / "local-inbox.jsonl"
        append_inbox(inbox, message)
        acknowledged = acknowledge_message(
            receiver_config,
            message["message_id"],
        )
        self.assertEqual(acknowledged["status"], "acknowledged")
        self.assertEqual(len(inbox.read_text(encoding="utf-8").splitlines()), 1)

        pulled_again = pull_messages(receiver_config, 0)
        self.assertEqual(pulled_again["messages"], [])


if __name__ == "__main__":
    unittest.main()
