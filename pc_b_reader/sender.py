"""Send a popup message from PC B to a target OpenDog device."""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener


class RetryableSenderError(RuntimeError):
    pass


class FatalSenderError(RuntimeError):
    pass


def load_config(config_path: Path) -> dict[str, Any]:
    with config_path.open("r", encoding="utf-8") as stream:
        config = json.load(stream)

    required = ("server_url",)
    missing = [key for key in required if not config.get(key)]
    if missing:
        raise ValueError(f"Missing config values: {', '.join(missing)}")
    message_token = config.get("message_token") or config.get("token")
    if not message_token:
        raise ValueError("Missing config value: message_token or token")
    config["message_token"] = message_token
    config.setdefault("sender_id", "pc_b")
    config.setdefault("target_device_id", "windows_pc_a")
    config.setdefault("request_timeout_seconds", 15.0)
    config.setdefault("max_retries", 5)
    config.setdefault("max_retry_seconds", 60.0)
    config.setdefault("use_proxy", False)
    return config


def send_message(config: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    endpoint = config["server_url"].rstrip("/") + "/messages"
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(
        endpoint,
        data=body,
        headers={
            "Authorization": f"Bearer {config['message_token']}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    opener = (
        build_opener()
        if bool(config["use_proxy"])
        else build_opener(ProxyHandler({}))
    )
    try:
        with opener.open(
            request,
            timeout=float(config["request_timeout_seconds"]),
        ) as response:
            response_body = response.read().decode("utf-8")
    except HTTPError as exc:
        response_body = exc.read().decode("utf-8", errors="replace")
        if exc.code in (400, 401, 403, 404, 422):
            raise FatalSenderError(
                f"Server rejected message: HTTP {exc.code}: {response_body}"
            ) from exc
        raise RetryableSenderError(
            f"Server returned HTTP {exc.code}: {response_body}"
        ) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise RetryableSenderError(f"Cannot reach server: {exc}") from exc

    try:
        result = json.loads(response_body)
    except json.JSONDecodeError as exc:
        raise RetryableSenderError("Server returned invalid JSON") from exc
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise RetryableSenderError(f"Server did not confirm message: {result!r}")
    return result


def send_with_retry(
    config: dict[str, Any],
    payload: dict[str, Any],
) -> dict[str, Any]:
    retry_delay = 3.0
    attempts = 0
    while True:
        try:
            return send_message(config, payload)
        except RetryableSenderError as exc:
            attempts += 1
            if attempts > int(config["max_retries"]):
                raise FatalSenderError(
                    f"Message send failed after {attempts} attempts: {exc}"
                ) from exc
            print(
                f"[WARNING] {exc}; retrying in {retry_delay:.1f} seconds",
                file=sys.stderr,
            )
            time.sleep(retry_delay)
            retry_delay = min(
                retry_delay * 2,
                float(config["max_retry_seconds"]),
            )


def parse_payload(value: str) -> dict[str, Any]:
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError(f"payload is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise argparse.ArgumentTypeError("payload must be a JSON object")
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Send a Windows popup message through the OpenDog server."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config.json"),
        help="Path to the PC B configuration.",
    )
    parser.add_argument("--body", required=True, help="Popup message text.")
    parser.add_argument("--title", default="OpenDog", help="Popup title.")
    parser.add_argument("--target", help="Target device ID; defaults to config.")
    parser.add_argument("--message-id", help="Stable UUID for manual retry.")
    parser.add_argument(
        "--payload",
        type=parse_payload,
        default={},
        help="Optional JSON object attached to the message.",
    )
    parser.add_argument(
        "--expires-in",
        type=float,
        help="Optional number of seconds before the server expires the message.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        config = load_config(args.config)
        if args.expires_in is not None and args.expires_in <= 0:
            raise ValueError("--expires-in must be positive")
        payload = {
            "message_id": args.message_id or str(uuid.uuid4()),
            "sender_id": config["sender_id"],
            "target_device_id": args.target or config["target_device_id"],
            "message_type": "popup_text",
            "title": args.title,
            "body": args.body,
            "payload": args.payload,
            "expires_at": (
                time.time() + args.expires_in
                if args.expires_in is not None
                else None
            ),
        }
        result = send_with_retry(config, payload)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (FatalSenderError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
