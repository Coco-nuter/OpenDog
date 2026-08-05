"""Pull new OpenDog events from the server into a local JSONL mirror."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener


class RetryableReaderError(RuntimeError):
    pass


class FatalReaderError(RuntimeError):
    pass


def resolve_path(base_dir: Path, value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()


def load_config(config_path: Path) -> dict[str, Any]:
    with config_path.open("r", encoding="utf-8") as stream:
        config = json.load(stream)

    required = ("server_url", "token")
    missing = [key for key in required if not config.get(key)]
    if missing:
        raise ValueError(f"Missing config values: {', '.join(missing)}")

    base_dir = config_path.resolve().parent
    config["mirror_file"] = resolve_path(
        base_dir,
        config.get("mirror_file", "mirror_events.jsonl"),
    )
    config["cursor_file"] = resolve_path(
        base_dir,
        config.get("cursor_file", "last_seq.cursor"),
    )
    config["log_file"] = resolve_path(
        base_dir,
        config.get("log_file", "logs/reader.log"),
    )
    config.setdefault("batch_size", 500)
    config.setdefault("request_timeout_seconds", 15.0)
    config.setdefault("max_retries", 5)
    config.setdefault("max_retry_seconds", 60.0)
    config.setdefault("use_proxy", False)

    batch_size = int(config["batch_size"])
    if not 1 <= batch_size <= 1000:
        raise ValueError("batch_size must be between 1 and 1000")
    return config


def configure_logging(log_path: Path) -> logging.Logger:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("pc_b_reader")
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
    if not value:
        return 0
    sequence = int(value)
    if sequence < 0:
        raise ValueError("last_seq.cursor cannot be negative")
    return sequence


def save_cursor(cursor_path: Path, sequence: int) -> None:
    cursor_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = cursor_path.with_suffix(cursor_path.suffix + ".tmp")
    with temporary_path.open("w", encoding="utf-8") as stream:
        stream.write(f"{sequence}\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary_path, cursor_path)


def load_mirror_last_seq(mirror_file: Path) -> int | None:
    if not mirror_file.exists() or mirror_file.stat().st_size == 0:
        return None

    last_line = ""
    with mirror_file.open("r", encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                last_line = line
    if not last_line:
        return None

    try:
        event = json.loads(last_line)
    except json.JSONDecodeError as exc:
        raise FatalReaderError(
            f"The last mirror line is incomplete or invalid: {exc}"
        ) from exc
    sequence = event.get("seq") if isinstance(event, dict) else None
    if not isinstance(sequence, int) or sequence < 0:
        raise FatalReaderError("The last mirror event has no valid seq")
    return sequence


def reconcile_cursor(
    cursor_file: Path,
    mirror_file: Path,
    logger: logging.Logger,
) -> int:
    cursor_seq = load_cursor(cursor_file)
    mirror_seq = load_mirror_last_seq(mirror_file)
    if mirror_seq is None:
        if cursor_seq > 0:
            raise FatalReaderError(
                "Cursor exists but mirror_events.jsonl is empty or missing; "
                "restore the mirror or delete last_seq.cursor to rebuild it"
            )
        return 0
    if cursor_seq > mirror_seq:
        raise FatalReaderError(
            f"Cursor seq {cursor_seq} is ahead of mirror seq {mirror_seq}"
        )
    if mirror_seq > cursor_seq:
        logger.warning(
            "Mirror seq %d is ahead of cursor seq %d; recovering cursor",
            mirror_seq,
            cursor_seq,
        )
        save_cursor(cursor_file, mirror_seq)
    return mirror_seq


def create_opener(use_proxy: bool) -> Any:
    if use_proxy:
        return build_opener()
    return build_opener(ProxyHandler({}))


def fetch_events(
    config: dict[str, Any],
    after_seq: int,
) -> dict[str, Any]:
    query = urlencode(
        {
            "after_seq": after_seq,
            "limit": int(config["batch_size"]),
        }
    )
    endpoint = config["server_url"].rstrip("/") + "/events?" + query
    request = Request(
        endpoint,
        method="GET",
        headers={
            "Authorization": f"Bearer {config['token']}",
            "Accept": "application/json",
        },
    )

    try:
        with create_opener(bool(config.get("use_proxy"))).open(
            request,
            timeout=float(config["request_timeout_seconds"]),
        ) as response:
            body = response.read().decode("utf-8")
    except HTTPError as exc:
        response_body = exc.read().decode("utf-8", errors="replace")
        if exc.code in (401, 403):
            raise FatalReaderError(
                f"Authentication failed with HTTP {exc.code}: {response_body}"
            ) from exc
        if 400 <= exc.code < 500:
            raise FatalReaderError(
                f"Server rejected request with HTTP {exc.code}: {response_body}"
            ) from exc
        raise RetryableReaderError(
            f"Server returned HTTP {exc.code}: {response_body}"
        ) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise RetryableReaderError(f"Cannot reach server: {exc}") from exc

    try:
        result = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RetryableReaderError("Server returned invalid JSON") from exc
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise RetryableReaderError(f"Server did not confirm request: {result!r}")
    if not isinstance(result.get("events"), list):
        raise RetryableReaderError("Server response has no events list")
    return result


def fetch_with_retry(
    config: dict[str, Any],
    after_seq: int,
    logger: logging.Logger,
) -> dict[str, Any]:
    retry_delay = 3.0
    attempts = 0
    while True:
        try:
            return fetch_events(config, after_seq)
        except RetryableReaderError as exc:
            attempts += 1
            if attempts > int(config["max_retries"]):
                raise FatalReaderError(
                    f"Download failed after {attempts} attempts: {exc}"
                ) from exc
            logger.warning("%s; retrying in %.1f seconds", exc, retry_delay)
            time.sleep(retry_delay)
            retry_delay = min(
                retry_delay * 2,
                float(config["max_retry_seconds"]),
            )


def append_event(mirror_file: Path, event: dict[str, Any]) -> None:
    mirror_file.parent.mkdir(parents=True, exist_ok=True)
    with mirror_file.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, ensure_ascii=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def run(config_path: Path) -> int:
    config = load_config(config_path)
    logger = configure_logging(config["log_file"])
    last_seq = reconcile_cursor(
        config["cursor_file"],
        config["mirror_file"],
        logger,
    )
    downloaded = 0
    logger.info("Pulling events after seq %d", last_seq)

    while True:
        result = fetch_with_retry(config, last_seq, logger)
        events = result["events"]
        if not events:
            logger.info(
                "Mirror is up to date at seq %d; downloaded %d events",
                last_seq,
                downloaded,
            )
            return 0

        for event in events:
            if not isinstance(event, dict):
                raise FatalReaderError("Server returned a non-object event")
            sequence = event.get("seq")
            if not isinstance(sequence, int) or sequence <= last_seq:
                raise FatalReaderError(
                    f"Server returned invalid sequence {sequence!r} after {last_seq}"
                )

            append_event(config["mirror_file"], event)
            save_cursor(config["cursor_file"], sequence)
            last_seq = sequence
            downloaded += 1

        logger.info(
            "Saved %d events in this page through seq %d",
            len(events),
            last_seq,
        )
        if not result.get("has_more", False):
            logger.info(
                "Mirror is up to date at seq %d; downloaded %d events",
                last_seq,
                downloaded,
            )
            return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download new OpenDog events into a local JSONL mirror."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config.json"),
        help="Path to the PC B reader configuration.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return run(args.config)
    except (FatalReaderError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
