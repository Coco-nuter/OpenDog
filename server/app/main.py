from __future__ import annotations

import hmac
import json
import math
import os
import re
import sqlite3
from contextlib import asynccontextmanager, closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.responses import JSONResponse


DEVICE_ID_PATTERN = re.compile(r"[^A-Za-z0-9_.-]+")


@dataclass(frozen=True)
class Settings:
    token: str
    database_path: Path
    data_dir: Path | None = None
    max_batch_size: int = 100
    max_body_bytes: int = 2 * 1024 * 1024

    @classmethod
    def from_environment(cls) -> "Settings":
        token = os.environ.get("OPENDOG_TOKEN", "")
        if len(token) < 24:
            raise RuntimeError("OPENDOG_TOKEN must contain at least 24 characters")

        max_batch_size = int(os.environ.get("OPENDOG_MAX_BATCH_SIZE", "100"))
        max_body_bytes = int(os.environ.get("OPENDOG_MAX_BODY_BYTES", "2097152"))
        if max_batch_size <= 0 or max_body_bytes <= 0:
            raise RuntimeError("Batch and body limits must be positive")

        database_path = Path(
            os.environ.get(
                "OPENDOG_DATABASE_PATH",
                "/var/lib/opendog-ingest/indexes.sqlite3",
            )
        )
        data_dir_value = os.environ.get("OPENDOG_DATA_DIR")

        return cls(
            token=token,
            database_path=database_path,
            data_dir=Path(data_dir_value) if data_dir_value else database_path.parent,
            max_batch_size=max_batch_size,
            max_body_bytes=max_body_bytes,
        )

    @property
    def storage_dir(self) -> Path:
        return self.data_dir or self.database_path.parent

    @property
    def timeline_path(self) -> Path:
        return self.storage_dir / "timeline.jsonl"

    @property
    def devices_dir(self) -> Path:
        return self.storage_dir / "devices"


class EventInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1, max_length=128)
    type: str = Field(min_length=1, max_length=128)
    ts: float
    data: dict[str, Any]

    @field_validator("ts")
    @classmethod
    def timestamp_must_be_finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("ts must be finite")
        return value


class IngestInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str = Field(min_length=1, max_length=128)
    device_id: str = Field(min_length=1, max_length=128)
    events: list[EventInput] = Field(min_length=1)


class IngestOutput(BaseModel):
    ok: bool = True
    count: int
    duplicates: int
    last_seq: int


class EventOutput(BaseModel):
    seq: int
    event_id: str
    source: str
    device_id: str
    type: str
    ts: float
    data: dict[str, Any]
    received_at: str


class EventsOutput(BaseModel):
    ok: bool = True
    events: list[EventOutput]
    last_seq: int
    has_more: bool


class RangeEventsOutput(BaseModel):
    ok: bool = True
    events: list[EventOutput]
    next_cursor: str | None
    has_more: bool


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def connect_database(database_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 10000")
    return connection


def sanitize_device_id(device_id: str) -> str:
    sanitized = DEVICE_ID_PATTERN.sub("_", device_id).strip("._-")
    return sanitized or "unknown_device"


def device_events_path(settings: Settings, device_id: str) -> Path:
    return settings.devices_dir / sanitize_device_id(device_id) / "events.jsonl"


def append_jsonl(path: Path, payload: dict[str, Any]) -> tuple[int, int]:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    with path.open("ab") as handle:
        offset = handle.tell()
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    return offset, len(encoded)


def read_jsonl_at(path: Path, offset: int, length: int) -> dict[str, Any]:
    with path.open("rb") as handle:
        handle.seek(offset)
        raw = handle.read(length)
    return json.loads(raw.decode("utf-8"))


def initialize_storage(settings: Settings) -> None:
    settings.storage_dir.mkdir(parents=True, exist_ok=True)
    settings.devices_dir.mkdir(parents=True, exist_ok=True)
    settings.timeline_path.parent.mkdir(parents=True, exist_ok=True)
    settings.timeline_path.touch(exist_ok=True)
    initialize_database(settings.database_path)


def initialize_database(database_path: Path) -> None:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(connect_database(database_path)) as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS event_index (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                source TEXT NOT NULL,
                device_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                event_ts REAL NOT NULL,
                received_at TEXT NOT NULL,
                device_path TEXT NOT NULL,
                device_offset INTEGER NOT NULL,
                device_length INTEGER NOT NULL,
                timeline_offset INTEGER NOT NULL,
                timeline_length INTEGER NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_event_index_event_ts_seq
                ON event_index(event_ts, seq);
            CREATE INDEX IF NOT EXISTS idx_event_index_device_seq
                ON event_index(device_id, seq);
            CREATE INDEX IF NOT EXISTS idx_event_index_received_at
                ON event_index(received_at);
            """
        )


def reserve_event_seq(
    connection: sqlite3.Connection,
    source: str,
    device_id: str,
    event: EventInput,
    received_at: str,
) -> tuple[int, bool]:
    cursor = connection.execute(
        """
        INSERT OR IGNORE INTO event_index (
            event_id,
            source,
            device_id,
            event_type,
            event_ts,
            received_at,
            device_path,
            device_offset,
            device_length,
            timeline_offset,
            timeline_length
        ) VALUES (?, ?, ?, ?, ?, ?, '', 0, 0, 0, 0)
        """,
        (event.event_id, source, device_id, event.type, event.ts, received_at),
    )
    if cursor.rowcount == 0:
        seq = int(
            connection.execute(
                "SELECT seq FROM event_index WHERE event_id = ?",
                (event.event_id,),
            ).fetchone()["seq"]
        )
        return seq, False

    seq = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
    return seq, True


def store_events(
    settings: Settings,
    source: str,
    device_id: str,
    events: list[EventInput],
) -> tuple[int, int]:
    inserted = 0
    last_seq = 0

    with closing(connect_database(settings.database_path)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        for event in events:
            received_at = utc_now()
            seq, is_new = reserve_event_seq(
                connection,
                source,
                device_id,
                event,
                received_at,
            )
            last_seq = max(last_seq, seq)
            if not is_new:
                continue

            full_event = {
                "seq": seq,
                "event_id": event.event_id,
                "source": source,
                "device_id": device_id,
                "type": event.type,
                "ts": event.ts,
                "data": event.data,
                "received_at": received_at,
            }
            device_path = device_events_path(settings, device_id)
            device_offset, device_length = append_jsonl(device_path, full_event)

            timeline_event = {
                "seq": seq,
                "received_at": received_at,
                "device_id": device_id,
                "source": source,
                "event_id": event.event_id,
                "type": event.type,
                "event_ts": event.ts,
                "device_path": str(device_path.relative_to(settings.storage_dir)),
                "offset": device_offset,
                "length": device_length,
            }
            timeline_offset, timeline_length = append_jsonl(
                settings.timeline_path,
                timeline_event,
            )

            connection.execute(
                """
                UPDATE event_index
                SET
                    device_path = ?,
                    device_offset = ?,
                    device_length = ?,
                    timeline_offset = ?,
                    timeline_length = ?
                WHERE seq = ?
                """,
                (
                    str(device_path.relative_to(settings.storage_dir)),
                    device_offset,
                    device_length,
                    timeline_offset,
                    timeline_length,
                    seq,
                ),
            )
            inserted += 1

        if last_seq == 0:
            last_seq = int(
                connection.execute(
                    "SELECT COALESCE(MAX(seq), 0) FROM event_index"
                ).fetchone()[0]
            )
        connection.commit()

    return inserted, last_seq


def row_to_event(settings: Settings, row: sqlite3.Row) -> EventOutput:
    payload = read_jsonl_at(
        settings.storage_dir / row["device_path"],
        int(row["device_offset"]),
        int(row["device_length"]),
    )
    return EventOutput(
        seq=int(row["seq"]),
        event_id=payload["event_id"],
        source=payload["source"],
        device_id=payload["device_id"],
        type=payload["type"],
        ts=float(payload["ts"]),
        data=payload["data"],
        received_at=payload["received_at"],
    )


def read_events(
    settings: Settings,
    after_seq: int,
    limit: int,
) -> tuple[list[EventOutput], bool]:
    with closing(connect_database(settings.database_path)) as connection:
        rows = connection.execute(
            """
            SELECT
                seq,
                device_path,
                device_offset,
                device_length
            FROM event_index
            WHERE seq > ?
            ORDER BY seq ASC
            LIMIT ?
            """,
            (after_seq, limit + 1),
        ).fetchall()

    has_more = len(rows) > limit
    events = [row_to_event(settings, row) for row in rows[:limit]]
    return events, has_more


def encode_range_cursor(event_ts: float, seq: int) -> str:
    return f"{event_ts:.6f}:{seq}"


def decode_range_cursor(cursor: str | None) -> tuple[float, int] | None:
    if not cursor:
        return None
    try:
        event_ts_text, seq_text = cursor.split(":", 1)
        event_ts = float(event_ts_text)
        seq = int(seq_text)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid cursor",
        ) from exc
    if not math.isfinite(event_ts) or seq < 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid cursor",
        )
    return event_ts, seq


def read_events_by_time_range(
    settings: Settings,
    start_ts: float,
    end_ts: float,
    limit: int,
    cursor: str | None,
) -> tuple[list[EventOutput], str | None, bool]:
    decoded_cursor = decode_range_cursor(cursor)
    params: tuple[Any, ...]
    cursor_clause = ""
    if decoded_cursor:
        cursor_ts, cursor_seq = decoded_cursor
        cursor_clause = "AND (event_ts > ? OR (event_ts = ? AND seq > ?))"
        params = (start_ts, end_ts, cursor_ts, cursor_ts, cursor_seq, limit + 1)
    else:
        params = (start_ts, end_ts, limit + 1)

    with closing(connect_database(settings.database_path)) as connection:
        rows = connection.execute(
            f"""
            SELECT
                seq,
                event_ts,
                device_path,
                device_offset,
                device_length
            FROM event_index
            WHERE event_ts >= ?
              AND event_ts <= ?
              {cursor_clause}
            ORDER BY event_ts ASC, seq ASC
            LIMIT ?
            """,
            params,
        ).fetchall()

    has_more = len(rows) > limit
    page_rows = rows[:limit]
    events = [row_to_event(settings, row) for row in page_rows]
    next_cursor = None
    if has_more and page_rows:
        last_row = page_rows[-1]
        next_cursor = encode_range_cursor(
            float(last_row["event_ts"]),
            int(last_row["seq"]),
        )
    return events, next_cursor, has_more


def create_app(settings: Settings | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        active_settings = settings or Settings.from_environment()
        initialize_storage(active_settings)
        application.state.settings = active_settings
        yield

    application = FastAPI(
        title="OpenDog Ingest API",
        version="2.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    @application.middleware("http")
    async def limit_request_size(request: Request, call_next: Any) -> Any:
        active_settings: Settings = request.app.state.settings
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                size = int(content_length)
            except ValueError:
                return JSONResponse(
                    status_code=400,
                    content={"detail": "Invalid Content-Length"},
                )
            if size > active_settings.max_body_bytes:
                return JSONResponse(
                    status_code=413,
                    content={"detail": "Request body is too large"},
                )
        return await call_next(request)

    def authorize(
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> None:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing bearer token",
            )
        supplied_token = authorization.removeprefix("Bearer ")
        active_settings: Settings = request.app.state.settings
        if not hmac.compare_digest(supplied_token, active_settings.token):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Invalid bearer token",
            )

    @application.get("/health")
    def health(request: Request) -> dict[str, Any]:
        active_settings: Settings = request.app.state.settings
        with closing(connect_database(active_settings.database_path)) as connection:
            connection.execute("SELECT 1").fetchone()
        return {
            "ok": True,
            "service": "opendog-ingest",
            "storage": "jsonl-with-sqlite-index",
        }

    @application.post(
        "/ingest",
        response_model=IngestOutput,
        dependencies=[Depends(authorize)],
    )
    def ingest(request: Request, payload: IngestInput) -> IngestOutput:
        active_settings: Settings = request.app.state.settings
        if len(payload.events) > active_settings.max_batch_size:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"At most {active_settings.max_batch_size} events are allowed",
            )

        inserted, last_seq = store_events(
            active_settings,
            payload.source,
            payload.device_id,
            payload.events,
        )
        return IngestOutput(
            count=inserted,
            duplicates=len(payload.events) - inserted,
            last_seq=last_seq,
        )

    @application.get(
        "/events",
        response_model=EventsOutput,
        dependencies=[Depends(authorize)],
    )
    def events(
        request: Request,
        after_seq: int = Query(default=0, ge=0),
        limit: int = Query(default=500, ge=1, le=1000),
    ) -> EventsOutput:
        active_settings: Settings = request.app.state.settings
        items, has_more = read_events(
            active_settings,
            after_seq,
            limit,
        )
        last_seq = items[-1].seq if items else after_seq
        return EventsOutput(
            events=items,
            last_seq=last_seq,
            has_more=has_more,
        )

    @application.get(
        "/events/range",
        response_model=RangeEventsOutput,
        dependencies=[Depends(authorize)],
    )
    def events_range(
        request: Request,
        start_ts: float = Query(...),
        end_ts: float = Query(...),
        limit: int = Query(default=500, ge=1, le=1000),
        cursor: str | None = Query(default=None),
    ) -> RangeEventsOutput:
        if not math.isfinite(start_ts) or not math.isfinite(end_ts):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="start_ts and end_ts must be finite",
            )
        if end_ts < start_ts:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="end_ts must be greater than or equal to start_ts",
            )

        active_settings: Settings = request.app.state.settings
        items, next_cursor, has_more = read_events_by_time_range(
            active_settings,
            start_ts,
            end_ts,
            limit,
            cursor,
        )
        return RangeEventsOutput(
            events=items,
            next_cursor=next_cursor,
            has_more=has_more,
        )

    return application


app = create_app()
