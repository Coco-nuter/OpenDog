"""
Capture focused application windows on focus switches and after user activity.

Install dependencies:
    pip install uiautomation dxcam pillow opencv-python numpy pynput rapidocr onnxruntime

Run:
    python focused_app_history_ocr.py
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes
import json
import os
import re
import sys
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    import cv2
    import dxcam
    import numpy as np
    import uiautomation as auto
    from pynput import keyboard, mouse
except ImportError as exc:
    raise SystemExit(
        "Missing dependencies. Run: "
        "pip install uiautomation dxcam pillow opencv-python numpy pynput "
        "rapidocr onnxruntime"
    ) from exc


PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32


class FocusedAppHistoryOCR:
    def __init__(
        self,
        poll_interval: float = 0.1,
        idle_seconds: float = 1.0,
        output_dir: str = "focus_history",
        max_memory_history: int = 100,
        max_images_per_focus: int = 500,
        debug_all_captures: bool = False,
        max_debug_images_per_focus: int = 0,
        ocr_enabled: bool = True,
        diff_min_area: int = 100,
        diff_margin: int = 8,
        rapidocr_min_score: float = 0.5,
        rapidocr_limit_side_len: int = 960,
        excluded_apps: list[str] | None = None,
    ) -> None:
        self.poll_interval = poll_interval
        self.idle_seconds = idle_seconds
        self.output_dir = Path(output_dir)
        self.max_memory_history = max_memory_history
        self.max_images_per_focus = max_images_per_focus
        self.debug_all_captures = debug_all_captures
        self.max_debug_images_per_focus = max_debug_images_per_focus
        self.ocr_enabled = ocr_enabled
        self.diff_min_area = diff_min_area
        self.diff_margin = diff_margin
        self.rapidocr_min_score = rapidocr_min_score
        self.rapidocr_limit_side_len = rapidocr_limit_side_len
        self.excluded_apps = {app.lower() for app in excluded_apps or []}

        self.camera: Any = None
        self.ocr: Any = None
        self.keyboard_listener: Any = None
        self.mouse_listener: Any = None
        self.activity_lock = threading.Lock()
        self.pending_activity_hwnd: int | None = None
        self.last_activity_at = 0.0
        self.current_focus_id: str | None = None
        self.focus_library: dict[str, dict[str, Any]] = {}
        self.stats = {
            "samples": 0,
            "saved_events": 0,
            "debug_captures": 0,
            "skipped_excluded": 0,
            "errors": 0,
        }

    def initialize(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.camera = dxcam.create(output_idx=0, output_color="BGR")
        if self.camera is None:
            raise RuntimeError("DXCAM initialization failed.")

        if not self.ocr_enabled:
            self.start_input_listeners()
            return

        try:
            from compare_pic import RapidOCRTextRecognizer
        except ImportError as exc:
            raise RuntimeError(
                "Missing OCR dependency. Run: pip install rapidocr onnxruntime opencv-python"
            ) from exc

        try:
            self.ocr = RapidOCRTextRecognizer(
                min_score=self.rapidocr_min_score,
                use_det=True,
                use_cls=False,
                use_rec=True,
                limit_side_len=self.rapidocr_limit_side_len,
            )
        except Exception as exc:
            raise RuntimeError(
                "RapidOCR initialization failed. Check the rapidocr/onnxruntime "
                "installation and model availability."
            ) from exc
        self.start_input_listeners()

    def close(self) -> None:
        if self.keyboard_listener is not None:
            self.keyboard_listener.stop()
            self.keyboard_listener = None
        if self.mouse_listener is not None:
            self.mouse_listener.stop()
            self.mouse_listener = None
        self.camera = None

    def start_input_listeners(self) -> None:
        self.keyboard_listener = keyboard.Listener(on_press=self.on_key_press)
        self.mouse_listener = mouse.Listener(
            on_click=self.on_mouse_click,
            on_scroll=self.on_mouse_scroll,
        )
        self.keyboard_listener.start()
        self.mouse_listener.start()

    def mark_activity(self) -> None:
        hwnd = int(user32.GetForegroundWindow() or 0)
        if not hwnd:
            return
        with self.activity_lock:
            self.pending_activity_hwnd = hwnd
            self.last_activity_at = time.monotonic()

    def on_key_press(self, _key: Any) -> None:
        self.mark_activity()

    def on_mouse_click(
        self, _x: int, _y: int, _button: Any, pressed: bool
    ) -> None:
        if pressed:
            self.mark_activity()

    def on_mouse_scroll(self, _x: int, _y: int, _dx: int, _dy: int) -> None:
        self.mark_activity()

    @staticmethod
    def get_process_name(hwnd: int) -> str:
        if not hwnd:
            return "Unknown"

        pid = ctypes.wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return "Unknown"

        process = kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value
        )
        if not process:
            return "Unknown"

        try:
            path_buffer = ctypes.create_unicode_buffer(1024)
            size = ctypes.wintypes.DWORD(len(path_buffer))
            if kernel32.QueryFullProcessImageNameW(
                process, 0, path_buffer, ctypes.byref(size)
            ):
                return os.path.basename(path_buffer.value)
        finally:
            kernel32.CloseHandle(process)

        return "Unknown"

    @staticmethod
    def normalize_region(rect: Any, screen_width: int, screen_height: int) -> tuple[int, int, int, int] | None:
        left = max(0, int(rect.left))
        top = max(0, int(rect.top))
        right = min(screen_width, int(rect.right))
        bottom = min(screen_height, int(rect.bottom))
        if right <= left or bottom <= top:
            return None
        return left, top, right, bottom

    def get_focused_window(self) -> dict[str, Any] | None:
        control = auto.GetFocusedControl()
        if not control:
            return None

        window = control.GetTopLevelControl()
        if not window:
            return None

        hwnd = int(getattr(window, "NativeWindowHandle", 0) or 0)
        if not hwnd:
            hwnd = int(user32.GetForegroundWindow() or 0)

        app = self.get_process_name(hwnd)
        title = str(getattr(window, "Name", "") or "")
        focus_id = f"{app}:{hwnd}"
        region = self.normalize_region(
            window.BoundingRectangle, self.camera.width, self.camera.height
        )
        if region is None:
            return None

        return {
            "focus_id": focus_id,
            "app": app,
            "title": title,
            "hwnd": hwnd,
            "region": region,
        }

    def capture_region(self, region: tuple[int, int, int, int]) -> np.ndarray | None:
        frame = self.camera.grab(region=region)
        if frame is None:
            return None
        return frame

    def run_ocr(self, image: np.ndarray) -> dict[str, Any]:
        if not self.ocr_enabled:
            return {"text": "", "ocr_items": [], "ocr_metrics": {}}

        items = self.ocr.recognize(image)
        text = "\n".join(item["text"] for item in items if item.get("text")).strip()
        return {
            "text": text,
            "ocr_items": items,
            "ocr_metrics": getattr(self.ocr, "last_ocr_metrics", {}),
        }

    def find_largest_diff_region(
        self, old_frame: np.ndarray, new_frame: np.ndarray
    ) -> tuple[dict[str, Any], np.ndarray] | tuple[None, None]:
        if old_frame.shape != new_frame.shape:
            old_frame = cv2.resize(old_frame, (new_frame.shape[1], new_frame.shape[0]))

        gray1 = cv2.cvtColor(old_frame, cv2.COLOR_BGR2GRAY)
        gray2 = cv2.cvtColor(new_frame, cv2.COLOR_BGR2GRAY)
        diff = cv2.absdiff(gray1, gray2)

        _, thresh = cv2.threshold(diff, 40, 255, cv2.THRESH_BINARY)
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (7, 7))
        thresh = cv2.dilate(thresh, kernel, iterations=3)

        contours, _ = cv2.findContours(
            thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )
        if not contours:
            return None, None

        height, width = new_frame.shape[:2]
        regions = []
        for contour in contours:
            area = cv2.contourArea(contour)
            if area < self.diff_min_area:
                continue

            x, y, bw, bh = cv2.boundingRect(contour)
            x1 = max(0, x - self.diff_margin)
            y1 = max(0, y - self.diff_margin)
            x2 = min(width, x + bw + self.diff_margin)
            y2 = min(height, y + bh + self.diff_margin)
            regions.append(
                {
                    "box": [x1, y1, x2, y2],
                    "area": round(float(area), 3),
                }
            )

        if not regions:
            return None, None

        region = max(regions, key=lambda item: item["area"])
        x1, y1, x2, y2 = region["box"]
        return region, new_frame[y1:y2, x1:x2]

    @staticmethod
    def sanitize_path_component(value: str, max_length: int = 80) -> str:
        sanitized = INVALID_FILENAME_CHARS.sub("_", value).strip(" .")
        return (sanitized or "unknown")[:max_length]

    def focus_directory(self, focus_id: str, app: str) -> Path:
        safe_id = self.sanitize_path_component(focus_id)
        safe_app = self.sanitize_path_component(app)
        path = self.output_dir / f"{safe_app}_{safe_id}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def save_event(
        self,
        window_info: dict[str, Any],
        frame: np.ndarray,
        trigger: str,
    ) -> dict[str, Any]:
        focus_id = window_info["focus_id"]
        state = self.focus_library.get(focus_id)
        directory = self.focus_directory(focus_id, window_info["app"])
        timestamp = datetime.now()
        stem = f"{timestamp.strftime('%Y%m%d_%H%M%S_%f')}_{trigger}"
        image_path = directory / f"{stem}.png"

        cv2.imwrite(str(image_path), frame)

        diff_box = None
        diff_area = None
        diff_crop_path = None
        ocr_mode = "disabled" if not self.ocr_enabled else "full_frame"
        ocr_image = frame

        old_frame = state.get("last_frame") if state is not None else None
        if self.ocr_enabled and old_frame is not None:
            diff_region, diff_crop = self.find_largest_diff_region(old_frame, frame)
            if diff_region is None or diff_crop is None:
                ocr_mode = "no_diff"
                ocr_image = None
            else:
                ocr_mode = "largest_diff"
                diff_box = diff_region["box"]
                diff_area = diff_region["area"]
                ocr_image = diff_crop

                diff_directory = directory / "diff_regions"
                diff_directory.mkdir(parents=True, exist_ok=True)
                diff_crop_path = diff_directory / f"{stem}_largest_diff.png"
                cv2.imwrite(str(diff_crop_path), diff_crop)

        if ocr_image is None:
            ocr_result = {"text": "", "ocr_items": [], "ocr_metrics": {}}
        else:
            ocr_result = self.run_ocr(ocr_image)

        event = {
            "timestamp": timestamp.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            "trigger": trigger,
            "focus_id": focus_id,
            "app": window_info["app"],
            "title": window_info["title"],
            "hwnd": window_info["hwnd"],
            "region": list(window_info["region"]),
            "ocr_enabled": self.ocr_enabled,
            "ocr_mode": ocr_mode,
            "text": ocr_result["text"],
            "ocr_items": ocr_result["ocr_items"],
            "ocr_metrics": ocr_result["ocr_metrics"],
            "image_path": str(image_path),
            "diff_box": diff_box,
            "diff_area": diff_area,
            "diff_crop_path": str(diff_crop_path) if diff_crop_path else None,
        }

        with (directory / "history.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")

        if state is not None:
            state["history"].append(event)
            state["last_frame"] = frame.copy()
            state["last_image_path"] = str(image_path)
            self.remove_old_images(directory)

        self.stats["saved_events"] += 1
        print(json.dumps(event, ensure_ascii=False, indent=2))
        return event

    def save_debug_capture(
        self,
        window_info: dict[str, Any],
        frame: np.ndarray,
        trigger: str,
    ) -> None:
        focus_id = window_info["focus_id"]
        directory = self.focus_directory(focus_id, window_info["app"])
        debug_directory = directory / "debug_captures"
        debug_directory.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now()
        stem = f"{timestamp.strftime('%Y%m%d_%H%M%S_%f')}_sample"
        image_path = debug_directory / f"{stem}.png"
        cv2.imwrite(str(image_path), frame)
        record = {
            "timestamp": timestamp.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            "record_type": "debug_capture",
            "trigger": trigger,
            "focus_id": focus_id,
            "app": window_info["app"],
            "title": window_info["title"],
            "hwnd": window_info["hwnd"],
            "region": list(window_info["region"]),
            "ocr_enabled": self.ocr_enabled,
            "image_path": str(image_path),
        }

        with (directory / "debug_captures.jsonl").open(
            "a", encoding="utf-8"
        ) as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")

        if self.max_debug_images_per_focus > 0:
            self.remove_old_images(
                debug_directory, self.max_debug_images_per_focus
            )

        self.stats["debug_captures"] += 1
        return None

    def remove_old_images(self, directory: Path, limit: int | None = None) -> None:
        if limit is None:
            limit = self.max_images_per_focus
        images = sorted(directory.glob("*.png"), key=lambda path: path.stat().st_mtime)
        for path in images[:-limit]:
            try:
                path.unlink()
            except OSError:
                pass

    def capture_event(
        self, window_info: dict[str, Any], trigger: str
    ) -> dict[str, Any] | None:
        frame = self.capture_region(window_info["region"])
        if frame is None:
            return None

        if self.debug_all_captures:
            self.save_debug_capture(window_info, frame, trigger)
        return self.save_event(window_info, frame, trigger)

    def process_once(self, now: float | None = None) -> dict[str, Any] | None:
        self.stats["samples"] += 1
        window_info = self.get_focused_window()
        if not window_info:
            return None

        if window_info["app"].lower() in self.excluded_apps:
            self.current_focus_id = f"excluded:{window_info['focus_id']}"
            self.stats["skipped_excluded"] += 1
            return None

        focus_id = window_info["focus_id"]
        state = self.focus_library.get(focus_id)
        if state is None:
            state = {
                "app": window_info["app"],
                "title": window_info["title"],
                "hwnd": window_info["hwnd"],
                "region": window_info["region"],
                "last_text": "",
                "last_frame": None,
                "last_image_path": None,
                "last_seen_at": time.time(),
                "history": deque(maxlen=self.max_memory_history),
            }
            self.focus_library[focus_id] = state
        state["last_seen_at"] = time.time()
        state["title"] = window_info["title"]
        state["region"] = window_info["region"]

        if focus_id != self.current_focus_id:
            self.current_focus_id = focus_id
            with self.activity_lock:
                if self.pending_activity_hwnd == window_info["hwnd"]:
                    self.pending_activity_hwnd = None
            event = self.capture_event(window_info, "focus_switch")
            if event:
                state["last_text"] = event["text"]
            return event

        if now is None:
            now = time.monotonic()
        with self.activity_lock:
            should_capture_idle = (
                self.pending_activity_hwnd == window_info["hwnd"]
                and now - self.last_activity_at >= self.idle_seconds
            )
            if should_capture_idle:
                self.pending_activity_hwnd = None

        if not should_capture_idle:
            return None

        event = self.capture_event(window_info, "interaction_idle")
        if event:
            state["last_text"] = event["text"]
        return event

    def print_summary(self) -> None:
        summary = {
            **self.stats,
            "tracked_focus_ids": len(self.focus_library),
        }
        print("\nSummary:")
        print(json.dumps(summary, ensure_ascii=False, indent=2))

    def loop(self) -> None:
        print("Monitoring focused application windows. Press Ctrl+C to exit.")
        try:
            while True:
                try:
                    self.process_once()
                except Exception as exc:
                    self.stats["errors"] += 1
                    print(f"[WARN] Capture cycle failed: {exc}", file=sys.stderr)
                time.sleep(self.poll_interval)
        except KeyboardInterrupt:
            print("\nStopped.")
        finally:
            self.print_summary()
            self.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Capture focused application windows and keep OCR history."
    )
    parser.add_argument("--poll-interval", type=float, default=0.1)
    parser.add_argument("--idle-seconds", type=float, default=1.0)
    parser.add_argument("--output-dir", default="focus_history")
    parser.add_argument("--max-memory-history", type=int, default=100)
    parser.add_argument("--max-images-per-focus", type=int, default=500)
    parser.add_argument(
        "--debug-all-captures",
        action="store_true",
        help="Save a debug copy for every triggered screenshot.",
    )
    parser.add_argument(
        "--max-debug-images-per-focus",
        type=int,
        default=0,
        help="Maximum debug images per focus ID. Use 0 to keep all images.",
    )
    parser.add_argument(
        "--disable-ocr",
        action="store_true",
        help="Skip RapidOCR initialization and text recognition.",
    )
    parser.add_argument(
        "--diff-min-area",
        type=int,
        default=100,
        help="Minimum contour area for a screenshot diff region.",
    )
    parser.add_argument(
        "--diff-margin",
        type=int,
        default=8,
        help="Pixels to expand around the largest diff region before OCR.",
    )
    parser.add_argument(
        "--rapidocr-min-score",
        type=float,
        default=0.5,
        help="Minimum RapidOCR confidence score to keep.",
    )
    parser.add_argument(
        "--rapidocr-limit-side-len",
        type=int,
        default=960,
        help="Resize OCR input so its longest side is at most this value. Use 0 to disable.",
    )
    parser.add_argument(
        "--exclude-app",
        action="append",
        default=[],
        help="Skip an application executable. May be specified multiple times.",
    )
    return parser


def main() -> int: 
    args = build_parser().parse_args()
    app = FocusedAppHistoryOCR(
        poll_interval=args.poll_interval,
        idle_seconds=args.idle_seconds,
        output_dir=args.output_dir,
        max_memory_history=args.max_memory_history,
        max_images_per_focus=args.max_images_per_focus,
        debug_all_captures=args.debug_all_captures,
        max_debug_images_per_focus=args.max_debug_images_per_focus,
        ocr_enabled=not args.disable_ocr,
        diff_min_area=args.diff_min_area,
        diff_margin=args.diff_margin,
        rapidocr_min_score=args.rapidocr_min_score,
        rapidocr_limit_side_len=args.rapidocr_limit_side_len,
        excluded_apps=args.exclude_app,
    )
    try:
        app.initialize()
    except RuntimeError as exc:
        app.close()
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1

    app.loop()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
