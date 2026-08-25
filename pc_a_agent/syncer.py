"""
Incrementally upload events from focus_history/history.jsonl.

Run:
    python pc_a_agent/syncer.py --config sync_config.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener


ENVELOPE_FIELDS = {"event_id", "source", "device_id", "type", "ts"}


class RetryableSyncError(RuntimeError):
    pass


class FatalSyncError(RuntimeError):
    pass


class HistoryTruncatedError(RuntimeError):
    def __init__(self, file_size: int, cursor_offset: int) -> None:
        self.file_size = file_size
        self.cursor_offset = cursor_offset
        super().__init__(
            f"History file was truncated: size={file_size}, "
            f"cursor={cursor_offset}"
        )


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as stream:
        config = json.load(stream)

    required = ("server_url", "token", "source", "device_id", "history_file")
    missing = [key for key in required if not config.get(key)]
    if missing:
        raise ValueError(f"Missing config values: {', '.join(missing)}")

    base_dir = path.resolve().parent
    config["history_file"] = resolve_path(base_dir, config["history_file"])
    config["state_dir"] = resolve_path(
        base_dir, config.get("state_dir", "sync_state")
    )
    config.setdefault("batch_size", 20)
    config.setdefault("flush_interval_seconds", 1.0)
    config.setdefault("request_timeout_seconds", 15.0)
    config.setdefault("max_retry_seconds", 60.0)
    config.setdefault("use_proxy", False)
    return config


def resolve_path(base_dir: Path, value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()


def configure_logging(log_path: Path) -> logging.Logger:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("syncer")
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


def load_cursor(cursor_path: Path, history_path: Path) -> int:
    if not cursor_path.exists():
        return 0

    with cursor_path.open("r", encoding="utf-8") as stream:
        cursor = json.load(stream)

    recorded_path = cursor.get("path")
    if recorded_path and Path(recorded_path).resolve() != history_path.resolve():
        raise FatalSyncError(
            "Cursor belongs to a different history file: "
            f"{recorded_path}"
        )
    return int(cursor.get("offset", 0))


def save_cursor(cursor_path: Path, history_path: Path, offset: int) -> None:
    cursor_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = cursor_path.with_suffix(cursor_path.suffix + ".tmp")
    payload = {
        "path": str(history_path.resolve()),
        "offset": offset,
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    with temporary_path.open("w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary_path, cursor_path)


def read_complete_lines(
    history_path: Path,
    offset: int,
    limit: int,
) -> tuple[list[dict[str, Any]], int]:
    if not history_path.exists():
        return [], offset

    file_size = history_path.stat().st_size
    if file_size < offset:
        raise HistoryTruncatedError(file_size, offset)

    records: list[dict[str, Any]] = []
    confirmed_offset = offset
    with history_path.open("rb") as stream:
        stream.seek(offset)
        while len(records) < limit:
            line_start = stream.tell()
            raw_line = stream.readline()
            if not raw_line:
                break
            if not raw_line.endswith(b"\n"):
                break

            line_end = stream.tell()
            confirmed_offset = line_end
            try:
                event = json.loads(raw_line.decode("utf-8"))
                if not isinstance(event, dict):
                    raise ValueError("JSON value is not an object")
                error = None
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                event = None
                error = str(exc)

            records.append(
                {
                    "start": line_start,
                    "end": line_end,
                    "raw": raw_line.decode("utf-8", errors="replace").rstrip("\n"),
                    "event": event,
                    "error": error,
                }
            )

    return records, confirmed_offset


def normalize_event(
    event: dict[str, Any],
    raw_line: str,
    source: str,
    device_id: str,
) -> dict[str, Any]:
    normalized_source = str(event.get("source") or source)
    normalized_device_id = str(event.get("device_id") or device_id)
    event_id = event.get("event_id")
    if not event_id:
        digest = hashlib.sha256(
            f"{normalized_device_id}\0{raw_line}".encode("utf-8")
        ).hexdigest()
        event_id = f"legacy_{digest}"

    timestamp = event.get("ts")
    if timestamp is None:
        timestamp = parse_timestamp(event.get("timestamp"))

    return {
        "event_id": str(event_id),
        "type": str(event.get("type") or "focused_window_ocr"),
        "ts": float(timestamp),
        "data": {
            key: value
            for key, value in event.items()
            if key not in ENVELOPE_FIELDS
        },
    }


def parse_timestamp(value: Any) -> float:
    if not value:
        raise ValueError("Event has neither ts nor timestamp")
    parsed = datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S.%f")
    return parsed.timestamp()


def upload_events(
    config: dict[str, Any],
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    endpoint = config["server_url"].rstrip("/") + "/ingest"
    payload = json.dumps(
        {
            "source": config["source"],
            "device_id": config["device_id"],
            "events": events,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request = Request(
        endpoint,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {config['token']}",
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
        },
    )

    opener = (
        build_opener()
        if config.get("use_proxy")
        else build_opener(ProxyHandler({}))
    )
    try:
        with opener.open(
            request,
            timeout=float(config["request_timeout_seconds"]),
        ) as response:
            body = response.read().decode("utf-8")
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        if exc.code in (400, 422):
            raise ValueError(
                f"Server permanently rejected batch with HTTP {exc.code}: {body}"
            ) from exc
        if exc.code == 409:
            return {"ok": True, "duplicate": True}
        if exc.code in (401, 403):
            raise FatalSyncError(
                f"Authentication failed with HTTP {exc.code}: {body}"
            ) from exc
        raise RetryableSyncError(
            f"Server returned HTTP {exc.code}: {body}"
        ) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise RetryableSyncError(f"Cannot reach server: {exc}") from exc

    try:
        result = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RetryableSyncError("Server returned invalid JSON") from exc
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise RetryableSyncError(f"Server did not confirm batch: {result!r}")
    return result


def append_dead_letters(
    dead_letter_path: Path,
    records: list[dict[str, Any]],
    reason: str | None = None,
) -> None:
    if not records:
        return

    dead_letter_path.parent.mkdir(parents=True, exist_ok=True)
    with dead_letter_path.open("a", encoding="utf-8") as stream:
        for record in records:
            payload = {
                "recorded_at": datetime.now().astimezone().isoformat(
                    timespec="seconds"
                ),
                "byte_start": record["start"],
                "byte_end": record["end"],
                "error": reason or record["error"],
                "raw_line": record["raw"],
            }
            stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
        stream.flush()


def collect_batch(
    config: dict[str, Any],
    history_path: Path,
    offset: int,
) -> tuple[list[dict[str, Any]], int]:
    batch_size = int(config["batch_size"])
    records, end_offset = read_complete_lines(history_path, offset, batch_size)
    if records and len(records) < batch_size:
        time.sleep(float(config["flush_interval_seconds"]))
        records, end_offset = read_complete_lines(history_path, offset, batch_size)
    return records, end_offset


def run(config_path: Path) -> int:
    config = load_config(config_path)
    state_dir: Path = config["state_dir"]
    history_path: Path = config["history_file"]
    cursor_path = state_dir / "offset.cursor"
    dead_letter_path = state_dir / "dead_letter.jsonl"
    logger = configure_logging(state_dir / "syncer.log")
    offset = load_cursor(cursor_path, history_path)
    retry_delay = 3.0

    logger.info("Watching %s from byte %d", history_path, offset)
    while True:
        try:
            records, end_offset = collect_batch(config, history_path, offset)
            if not records:
                retry_delay = 3.0
                time.sleep(min(float(config["flush_interval_seconds"]), 1.0))
                continue

            invalid_records = [record for record in records if record["error"]]
            events: list[dict[str, Any]] = []
            for record in records:
                if record["error"]:
                    continue
                try:
                    events.append(
                        normalize_event(
                            record["event"],
                            record["raw"],
                            config["source"],
                            config["device_id"],
                        )
                    )
                except (TypeError, ValueError) as exc:
                    record["error"] = str(exc)
                    invalid_records.append(record)

            if events:
                try:
                    result = upload_events(config, events)
                except ValueError as exc:
                    append_dead_letters(dead_letter_path, records, str(exc))
                    save_cursor(cursor_path, history_path, end_offset)
                    logger.error(
                        "Discarded permanently rejected batch at bytes %d-%d",
                        offset,
                        end_offset,
                    )
                    offset = end_offset
                    retry_delay = 3.0
                    continue
                logger.info(
                    "Uploaded %d events through byte %d; response=%s",
                    len(events),
                    end_offset,
                    result,
                )

            append_dead_letters(dead_letter_path, invalid_records)
            save_cursor(cursor_path, history_path, end_offset)
            if invalid_records:
                logger.warning(
                    "Skipped %d invalid JSONL records through byte %d",
                    len(invalid_records),
                    end_offset,
                )
            offset = end_offset
            retry_delay = 3.0
        except RetryableSyncError as exc:
            logger.warning("%s; retrying in %.1f seconds", exc, retry_delay)
            time.sleep(retry_delay)
            retry_delay = min(
                retry_delay * 2,
                float(config["max_retry_seconds"]),
            )
        except HistoryTruncatedError as exc:
            logger.warning(
                "%s; resetting cursor to byte 0",
                exc,
            )
            offset = 0
            save_cursor(cursor_path, history_path, offset)
            retry_delay = 3.0
        except FatalSyncError as exc:
            logger.error("%s", exc)
            return 1
        except KeyboardInterrupt:
            logger.info("Stopped")
            return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Incrementally upload focused-window JSONL events."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("sync_config.json"),
        help="Path to the syncer JSON configuration.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return run(args.config)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
