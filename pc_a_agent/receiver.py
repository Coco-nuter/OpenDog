"""Long-poll server messages and show them with the Windows MessageBoxW API."""

from __future__ import annotations

import argparse
import ctypes
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener


class RetryableReceiverError(RuntimeError):
    pass


class FatalReceiverError(RuntimeError):
    pass


def resolve_path(base_dir: Path, value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()


def load_config(config_path: Path) -> dict[str, Any]:
    with config_path.open("r", encoding="utf-8") as stream:
        config = json.load(stream)

    required = ("server_url", "device_id")
    missing = [key for key in required if not config.get(key)]
    if missing:
        raise ValueError(f"Missing config values: {', '.join(missing)}")

    message_token = config.get("message_token") or config.get("token")
    if not message_token:
        raise ValueError("Missing config value: message_token or token")
    config["message_token"] = message_token

    base_dir = config_path.resolve().parent
    state_dir = resolve_path(base_dir, config.get("state_dir", "sync_state"))
    config["message_cursor_file"] = resolve_path(
        base_dir,
        config.get("message_cursor_file", str(state_dir / "message_seq.cursor")),
    )
    config["message_inbox_file"] = resolve_path(
        base_dir,
        config.get("message_inbox_file", str(state_dir / "message_inbox.jsonl")),
    )
    config["message_log_file"] = resolve_path(
        base_dir,
        config.get("message_log_file", str(state_dir / "receiver.log")),
    )
    config.setdefault("message_batch_size", 20)
    config.setdefault("message_poll_wait_seconds", 25.0)
    config.setdefault("request_timeout_seconds", 15.0)
    config.setdefault("max_retry_seconds", 60.0)
    config.setdefault("use_proxy", False)

    batch_size = int(config["message_batch_size"])
    wait_seconds = float(config["message_poll_wait_seconds"])
    if not 1 <= batch_size <= 100:
        raise ValueError("message_batch_size must be between 1 and 100")
    if not 0 <= wait_seconds <= 30:
        raise ValueError("message_poll_wait_seconds must be between 0 and 30")
    return config


def configure_logging(log_path: Path) -> logging.Logger:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("pc_a_receiver")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    return logger


def load_cursor(cursor_path: Path) -> int:
    if not cursor_path.exists():
        return 0
    value = cursor_path.read_text(encoding="utf-8").strip()
    sequence = int(value or "0")
    if sequence < 0:
        raise ValueError("message_seq.cursor cannot be negative")
    return sequence


def save_cursor(cursor_path: Path, sequence: int) -> None:
    cursor_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = cursor_path.with_suffix(cursor_path.suffix + ".tmp")
    with temporary_path.open("w", encoding="utf-8") as stream:
        stream.write(f"{sequence}\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary_path, cursor_path)


def build_http_opener(use_proxy: bool) -> Any:
    return build_opener() if use_proxy else build_opener(ProxyHandler({}))


def request_json(
    config: dict[str, Any],
    endpoint: str,
    method: str,
    payload: dict[str, Any] | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    body = None
    headers = {"Authorization": f"Bearer {config['message_token']}"}
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(endpoint, data=body, headers=headers, method=method)
    opener = build_http_opener(bool(config["use_proxy"]))
    try:
        with opener.open(
            request,
            timeout=timeout or float(config["request_timeout_seconds"]),
        ) as response:
            response_body = response.read().decode("utf-8")
    except HTTPError as exc:
        response_body = exc.read().decode("utf-8", errors="replace")
        if exc.code in (401, 403):
            raise FatalReceiverError(
                f"Authentication or device authorization failed: HTTP {exc.code}: "
                f"{response_body}"
            ) from exc
        if exc.code in (400, 404, 422):
            raise FatalReceiverError(
                f"Server rejected receiver request: HTTP {exc.code}: {response_body}"
            ) from exc
        raise RetryableReceiverError(
            f"Server returned HTTP {exc.code}: {response_body}"
        ) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise RetryableReceiverError(f"Cannot reach server: {exc}") from exc

    try:
        result = json.loads(response_body)
    except json.JSONDecodeError as exc:
        raise RetryableReceiverError("Server returned invalid JSON") from exc
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise RetryableReceiverError(f"Server did not confirm request: {result!r}")
    return result


def pull_messages(config: dict[str, Any], after_seq: int) -> dict[str, Any]:
    wait_seconds = float(config["message_poll_wait_seconds"])
    query = urlencode(
        {
            "target_device_id": config["device_id"],
            "after_seq": after_seq,
            "limit": int(config["message_batch_size"]),
            "wait_seconds": wait_seconds,
        }
    )
    endpoint = config["server_url"].rstrip("/") + "/messages/pull?" + query
    timeout = max(float(config["request_timeout_seconds"]), wait_seconds + 10.0)
    result = request_json(config, endpoint, "GET", timeout=timeout)
    if not isinstance(result.get("messages"), list):
        raise RetryableReceiverError("Server response has no messages list")
    return result


def acknowledge_message(config: dict[str, Any], message_id: str) -> dict[str, Any]:
    endpoint = config["server_url"].rstrip("/") + "/messages/ack"
    return request_json(
        config,
        endpoint,
        "POST",
        {
            "message_id": message_id,
            "target_device_id": config["device_id"],
            "status": "shown",
        },
    )


def append_inbox(inbox_path: Path, message: dict[str, Any]) -> None:
    inbox_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        **message,
        "stored_locally_at": datetime.now().astimezone().isoformat(
            timespec="milliseconds"
        ),
    }
    with inbox_path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def show_message_box(title: str, body: str) -> None:
    if os.name != "nt":
        raise FatalReceiverError("MessageBoxW is only available on Windows")
    flags = 0x00000040 | 0x00010000 | 0x00040000
    result = ctypes.windll.user32.MessageBoxW(None, body, title, flags)
    if result == 0:
        raise FatalReceiverError("MessageBoxW failed to display the message")


def retry_ack(
    config: dict[str, Any],
    message_id: str,
    logger: logging.Logger,
) -> None:
    retry_delay = 3.0
    while True:
        try:
            acknowledge_message(config, message_id)
            return
        except RetryableReceiverError as exc:
            logger.warning(
                "Acknowledgement failed for %s: %s; retrying in %.1f seconds",
                message_id,
                exc,
                retry_delay,
            )
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, float(config["max_retry_seconds"]))


def run(config_path: Path) -> int:
    config = load_config(config_path)
    logger = configure_logging(config["message_log_file"])
    last_seq = load_cursor(config["message_cursor_file"])
    retry_delay = 3.0
    logger.info(
        "Receiving messages for %s after seq %d",
        config["device_id"],
        last_seq,
    )

    while True:
        try:
            result = pull_messages(config, last_seq)
            retry_delay = 3.0
            for message in result["messages"]:
                if not isinstance(message, dict):
                    raise FatalReceiverError("Server returned a non-object message")
                sequence = message.get("msg_seq")
                message_id = message.get("message_id")
                if not isinstance(sequence, int) or sequence <= last_seq:
                    raise FatalReceiverError(
                        f"Server returned invalid message sequence {sequence!r}"
                    )
                if not isinstance(message_id, str) or not message_id:
                    raise FatalReceiverError("Server returned a message without message_id")

                append_inbox(config["message_inbox_file"], message)
                show_message_box(
                    str(message.get("title") or "OpenDog"),
                    str(message.get("body") or ""),
                )
                retry_ack(config, message_id, logger)
                save_cursor(config["message_cursor_file"], sequence)
                last_seq = sequence
                logger.info("Displayed and acknowledged message %s", message_id)
        except RetryableReceiverError as exc:
            logger.warning("%s; retrying in %.1f seconds", exc, retry_delay)
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, float(config["max_retry_seconds"]))
        except KeyboardInterrupt:
            logger.info("Stopped")
            return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Receive OpenDog messages and show Windows MessageBoxW popups."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("sync_config.json"),
        help="Path to the shared PC A sync configuration.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return run(args.config)
    except (FatalReceiverError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
