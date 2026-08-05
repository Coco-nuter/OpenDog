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
import uuid
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


class RapidOCRTextRecognizer:
    def __init__(
        self,
        min_score: float = 0.5,
        use_det: bool = True,
        use_cls: bool = False,
        use_rec: bool = True,
        limit_side_len: int = 960,
    ) -> None:
        self.min_score = min_score
        self.use_det = use_det
        self.use_cls = use_cls
        self.use_rec = use_rec
        self.limit_side_len = limit_side_len
        self.ocr = self._create_ocr()

    @staticmethod
    def _create_ocr() -> Any:
        try:
            from rapidocr import RapidOCR
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "Missing RapidOCR dependency. Run: "
                "pip install rapidocr onnxruntime opencv-python"
            ) from exc
        except Exception as exc:
            raise RuntimeError(
                "RapidOCR import failed. Check rapidocr, onnxruntime, and "
                f"opencv-python versions. Original error: {exc}"
            ) from exc

        try:
            return RapidOCR()
        except Exception as exc:
            raise RuntimeError(
                "RapidOCR initialization failed. Check model availability and "
                f"network access for the first download. Original error: {exc}"
            ) from exc

    def recognize(self, image: np.ndarray) -> list[dict[str, Any]]:
        if image is None or image.size == 0:
            return []

        image = self._resize_for_ocr(image)
        result = self.ocr(
            image,
            use_det=self.use_det,
            use_cls=self.use_cls,
            use_rec=self.use_rec,
        )

        texts = self._as_sequence(getattr(result, "txts", []))
        scores = self._as_sequence(getattr(result, "scores", []))
        boxes = self._as_sequence(getattr(result, "boxes", []))

        items = []
        for index, text in enumerate(texts):
            if not text:
                continue

            score = self._score_at(scores, index)
            if score < self.min_score:
                continue

            item = {"text": text, "score": round(score, 4)}
            if index < len(boxes):
                item["box"] = self._to_jsonable_box(boxes[index])
            items.append(item)

        return items

    def _resize_for_ocr(self, image: np.ndarray) -> np.ndarray:
        if not self.limit_side_len or self.limit_side_len <= 0:
            return image

        height, width = image.shape[:2]
        long_side = max(height, width)
        if long_side <= self.limit_side_len:
            return image

        scale = self.limit_side_len / long_side
        resized_width = max(1, int(width * scale))
        resized_height = max(1, int(height * scale))
        return cv2.resize(
            image,
            (resized_width, resized_height),
            interpolation=cv2.INTER_AREA,
        )

    @staticmethod
    def _as_sequence(value: Any) -> list[Any]:
        if value is None:
            return []
        if hasattr(value, "tolist"):
            value = value.tolist()
        return list(value)

    @staticmethod
    def _score_at(scores: list[Any], index: int) -> float:
        if index >= len(scores):
            return 1.0
        score = float(scores[index])
        if score > 1:
            score = score / 100
        return score

    @classmethod
    def _to_jsonable_box(cls, box: Any) -> Any:
        if hasattr(box, "tolist"):
            box = box.tolist()
        if isinstance(box, (list, tuple)):
            return [cls._to_jsonable_box(item) for item in box]
        try:
            return round(float(box), 3)
        except (TypeError, ValueError):
            return box


class FocusedAppHistoryOCR:
    def __init__(
        self,
        poll_interval: float = 0.1,
        idle_seconds: float = 1.0,
        focus_switch_settle_seconds: float = 0.2,
        output_dir: str = "focus_history",
        max_memory_history: int = 100,
        max_images_per_focus: int = 500,
        save_images: bool = True,
        debug_all_captures: bool = False,
        max_debug_images_per_focus: int = 0,
        ocr_enabled: bool = True,
        compare_screenshots: bool = True,
        diff_min_area: int = 100,
        diff_margin: int = 8,
        diff_min_ratio: float = 0.2,
        min_window_width: int = 200,
        min_window_height: int = 120,
        min_diff_width: int = 100,
        min_diff_height: int = 80,
        excluded_apps: list[str] | None = None,
        source: str = "pc_a",
    ) -> None:
        self.poll_interval = poll_interval
        self.idle_seconds = idle_seconds
        self.focus_switch_settle_seconds = focus_switch_settle_seconds
        self.output_dir = Path(output_dir)
        self.max_memory_history = max_memory_history
        self.max_images_per_focus = max_images_per_focus
        self.save_images = save_images
        self.debug_all_captures = debug_all_captures
        self.max_debug_images_per_focus = max_debug_images_per_focus
        self.ocr_enabled = ocr_enabled
        self.compare_screenshots = compare_screenshots
        self.diff_min_area = diff_min_area
        self.diff_margin = diff_margin
        self.diff_min_ratio = diff_min_ratio
        self.min_window_width = min_window_width
        self.min_window_height = min_window_height
        self.min_diff_width = min_diff_width
        self.min_diff_height = min_diff_height
        self.excluded_apps = {app.lower() for app in excluded_apps or []}
        self.source = source

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

        self.ocr = RapidOCRTextRecognizer()
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

    def normalize_region(
        self, rect: Any, screen_width: int, screen_height: int
    ) -> tuple[int, int, int, int] | None:
        left = max(0, int(rect.left))
        top = max(0, int(rect.top))
        right = min(screen_width, int(rect.right))
        bottom = min(screen_height, int(rect.bottom))
        if right <= left or bottom <= top:
            return None
        if right - left < self.min_window_width or bottom - top < self.min_window_height:
            return None
        return left, top, right, bottom

    def get_window_rect_region(self, hwnd: int) -> tuple[int, int, int, int] | None:
        rect = ctypes.wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
        return self.normalize_region(rect, self.camera.width, self.camera.height)

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
            region = self.get_window_rect_region(hwnd)
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

    def run_ocr(self, image: np.ndarray) -> str:
        if not self.ocr_enabled:
            return ""

        items = self.ocr.recognize(image)
        return "\n".join(item["text"] for item in items).strip()

    def find_largest_diff_region(
        self, old_frame: np.ndarray, new_frame: np.ndarray
    ) -> tuple[dict[str, Any], np.ndarray] | tuple[None, None]:
        if old_frame.shape != new_frame.shape:
            old_frame = cv2.resize(old_frame, (new_frame.shape[1], new_frame.shape[0]))

        gray1 = cv2.cvtColor(old_frame, cv2.COLOR_BGR2GRAY)
        gray2 = cv2.cvtColor(new_frame, cv2.COLOR_BGR2GRAY)
        diff = cv2.absdiff(gray1, gray2)

        _, thresh = cv2.threshold(diff, 80, 255, cv2.THRESH_BINARY)
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (9, 9))
        thresh = cv2.dilate(thresh, kernel, iterations=5)

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
                    "area_ratio": round(float(area) / float(width * height), 6),
                }
            )

        if not regions:
            return None, None

        regions = sorted(regions, key=lambda item: item["area"], reverse=True)
        total_area = sum(item["area"] for item in regions)
        total_area_ratio = total_area / float(width * height)
        selected_regions = regions[:3]
        selected_area = sum(item["area"] for item in selected_regions)
        selected_x1 = min(item["box"][0] for item in selected_regions)
        selected_y1 = min(item["box"][1] for item in selected_regions)
        selected_x2 = max(item["box"][2] for item in selected_regions)
        selected_y2 = max(item["box"][3] for item in selected_regions)
        selected_box = [selected_x1, selected_y1, selected_x2, selected_y2]
        region = {
            "box": selected_box,
            "area": round(float(selected_area), 3),
            "area_ratio": round(float(selected_area) / float(width * height), 6),
            "selected_region_count": len(selected_regions),
            "selected_regions": selected_regions,
        }
        region["total_area"] = round(float(total_area), 3)
        region["total_area_ratio"] = round(float(total_area_ratio), 6)
        region["region_count"] = len(regions)
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

    def append_history_event(self, directory: Path, event: dict[str, Any]) -> None:
        line = json.dumps(event, ensure_ascii=False) + "\n"
        for history_path in (
            directory / "history.jsonl",
            self.output_dir / "history.jsonl",
        ):
            with history_path.open("a", encoding="utf-8") as stream:
                stream.write(line)
                stream.flush()

    def save_event(
        self,
        window_info: dict[str, Any],
        frame: np.ndarray,
        trigger: str,
        text: str | None = None,
    ) -> dict[str, Any] | None:
        focus_id = window_info["focus_id"]
        state = self.focus_library.get(focus_id)
        directory = self.focus_directory(focus_id, window_info["app"])
        timestamp = datetime.now()
        stem = f"{timestamp.strftime('%Y%m%d_%H%M%S_%f')}_{trigger}"

        image_to_save = frame
        diff_box = None
        diff_area = None
        diff_area_ratio = None
        diff_total_area = None
        diff_total_area_ratio = None
        diff_region_count = None
        diff_selected_region_count = None
        diff_selected_regions = None
        diff_debug_image_path = None
        diff_compare_previous_image_path = None
        diff_compare_current_image_path = None

        old_frame = state.get("last_frame") if state is not None else None
        if self.compare_screenshots and old_frame is not None:
            diff_region, diff_crop = self.find_largest_diff_region(old_frame, frame)
            if diff_region is None or diff_crop is None:
                print(
                    "[DEBUG] compare-screenshots: "
                    f"focus_id={focus_id}, trigger={trigger}, result=no_diff"
                )
                return None

            diff_height, diff_width = diff_crop.shape[:2]
            diff_area = diff_region["area"]
            diff_area_ratio = diff_region["area_ratio"]
            diff_total_area = diff_region["total_area"]
            diff_total_area_ratio = diff_region["total_area_ratio"]
            diff_region_count = diff_region["region_count"]
            diff_selected_region_count = diff_region["selected_region_count"]
            diff_selected_regions = diff_region["selected_regions"]
            diff_box = diff_region["box"]
            if self.save_images and self.debug_all_captures:
                diff_debug_paths = self.save_diff_debug_capture(
                    window_info,
                    old_frame,
                    frame,
                    trigger,
                    stem,
                    diff_box,
                    diff_area,
                    diff_area_ratio,
                    diff_total_area,
                    diff_total_area_ratio,
                    diff_region_count,
                    diff_selected_region_count,
                    diff_selected_regions,
                )
                diff_debug_image_path = diff_debug_paths["annotated"]
                diff_compare_previous_image_path = diff_debug_paths["previous"]
                diff_compare_current_image_path = diff_debug_paths["current"]
            print(
                "[DEBUG] compare-screenshots: "
                f"focus_id={focus_id}, trigger={trigger}, "
                f"diff_box={diff_box}, diff_width={diff_width}, "
                f"diff_height={diff_height}, diff_area={diff_area}, "
                f"diff_area_ratio={diff_area_ratio}, "
                f"diff_total_area={diff_total_area}, "
                f"diff_total_area_ratio={diff_total_area_ratio}, "
                f"diff_region_count={diff_region_count}, "
                f"diff_selected_region_count={diff_selected_region_count}, "
                f"diff_selected_regions={diff_selected_regions}, "
                f"min_diff_size=[{self.min_diff_width}, {self.min_diff_height}], "
                f"diff_min_ratio={self.diff_min_ratio}, "
                f"compare_previous_image_path={diff_compare_previous_image_path}, "
                f"compare_current_image_path={diff_compare_current_image_path}, "
                f"diff_debug_image_path={diff_debug_image_path}"
            )
            if diff_width < self.min_diff_width or diff_height < self.min_diff_height:
                return None

            if diff_total_area_ratio < self.diff_min_ratio:
                return None

            image_to_save = diff_crop

        image_path = None
        if self.save_images:
            image_path = directory / f"{stem}.png"
            cv2.imwrite(str(image_path), image_to_save)
        if text is None:
            text = self.run_ocr(image_to_save)
        event = {
            "event_id": str(uuid.uuid4()),
            "source": self.source,
            "type": "focused_window_ocr",
            "timestamp": timestamp.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            "trigger": trigger,
            "focus_id": focus_id,
            "app": window_info["app"],
            "title": window_info["title"],
            "text": text,
        }

        self.append_history_event(directory, event)

        if state is not None:
            state["history"].append(event)
            state["last_frame"] = frame.copy()
            state["last_image_path"] = (
                str(image_path) if image_path is not None else None
            )
            if self.save_images:
                self.remove_old_images(directory)

        self.stats["saved_events"] += 1
        print(json.dumps(event, ensure_ascii=False, indent=2))
        return event

    def save_diff_debug_capture(
        self,
        window_info: dict[str, Any],
        old_frame: np.ndarray,
        frame: np.ndarray,
        trigger: str,
        stem: str,
        diff_box: list[int],
        diff_area: float,
        diff_area_ratio: float,
        diff_total_area: float,
        diff_total_area_ratio: float,
        diff_region_count: int,
        diff_selected_region_count: int,
        diff_selected_regions: list[dict[str, Any]],
    ) -> dict[str, str]:
        focus_id = window_info["focus_id"]
        directory = self.focus_directory(focus_id, window_info["app"])
        debug_directory = directory / "debug_captures"
        debug_directory.mkdir(parents=True, exist_ok=True)

        annotated = frame.copy()
        x1, y1, x2, y2 = diff_box
        cv2.rectangle(annotated, (x1, y1), (x2, y2), (0, 0, 255), 3)

        previous_image_path = debug_directory / f"{stem}_compare_previous.png"
        current_image_path = debug_directory / f"{stem}_compare_current.png"
        annotated_image_path = debug_directory / f"{stem}_diff_debug.png"
        cv2.imwrite(str(previous_image_path), old_frame)
        cv2.imwrite(str(current_image_path), frame)
        cv2.imwrite(str(annotated_image_path), annotated)
        record = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            "record_type": "compare_screenshot_diff_debug",
            "trigger": trigger,
            "focus_id": focus_id,
            "app": window_info["app"],
            "title": window_info["title"],
            "hwnd": window_info["hwnd"],
            "region": list(window_info["region"]),
            "image_path": str(annotated_image_path),
            "previous_image_path": str(previous_image_path),
            "current_image_path": str(current_image_path),
            "diff_box": diff_box,
            "diff_area": diff_area,
            "diff_area_ratio": diff_area_ratio,
            "diff_total_area": diff_total_area,
            "diff_total_area_ratio": diff_total_area_ratio,
            "diff_region_count": diff_region_count,
            "diff_selected_region_count": diff_selected_region_count,
            "diff_selected_regions": diff_selected_regions,
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
        return {
            "previous": str(previous_image_path),
            "current": str(current_image_path),
            "annotated": str(annotated_image_path),
        }

    def save_debug_capture(
        self,
        window_info: dict[str, Any],
        frame: np.ndarray,
        trigger: str,
        text: str | None = None,
    ) -> str:
        focus_id = window_info["focus_id"]
        directory = self.focus_directory(focus_id, window_info["app"])
        debug_directory = directory / "debug_captures"
        debug_directory.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now()
        stem = f"{timestamp.strftime('%Y%m%d_%H%M%S_%f')}_sample"
        image_path = debug_directory / f"{stem}.png"
        cv2.imwrite(str(image_path), frame)
        if text is None:
            text = self.run_ocr(frame)
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
            "text": text,
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
        return text

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

        event = self.save_event(window_info, frame, trigger)
        if event and self.save_images and self.debug_all_captures:
            self.save_debug_capture(window_info, frame, trigger, text=event["text"])
        return event

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
        if (
            focus_id != self.current_focus_id
            and self.focus_switch_settle_seconds > 0
        ):
            time.sleep(self.focus_switch_settle_seconds)
            settled_window_info = self.get_focused_window()
            if not settled_window_info:
                return None

            if settled_window_info["app"].lower() in self.excluded_apps:
                self.current_focus_id = f"excluded:{settled_window_info['focus_id']}"
                self.stats["skipped_excluded"] += 1
                return None

            if settled_window_info["focus_id"] != focus_id:
                print(
                    "[DEBUG] focus_switch not stable: "
                    f"first_focus_id={focus_id}, "
                    f"settled_focus_id={settled_window_info['focus_id']}"
                )
                return None

            window_info = settled_window_info
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
    parser.add_argument("--poll-interval", type=float, default=0.3)
    parser.add_argument("--idle-seconds", type=float, default=2.0)
    parser.add_argument(
        "--focus-switch-settle-seconds",
        type=float,
        default=0.2,
        help=(
            "Wait this many seconds after a focus switch, then re-read the "
            "focused window. Capture only if the focus ID is stable."
        ),
    )
    parser.add_argument("--output-dir", default="focus_history")
    parser.add_argument("--max-memory-history", type=int, default=100)
    parser.add_argument("--max-images-per-focus", type=int, default=500)
    parser.add_argument(
        "--no-save-images",
        action="store_true",
        help="Keep OCR and history JSONL records without writing PNG files.",
    )
    parser.add_argument(
        "--debug-all-captures",
        action="store_true",
        help=(
            "Save debug captures, including every triggered screenshot and "
            "compare-screenshot previous/current/annotated images."
        ),
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
        "--compare-screenshots",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Compare same-window screenshots and save only the largest diff crop.",
    )
    parser.add_argument(
        "--diff-min-area",
        type=int,
        default=100,
        help="Minimum contour area for screenshot diff extraction.",
    )
    parser.add_argument(
        "--diff-margin",
        type=int,
        default=8,
        help="Pixels to expand around the largest diff region.",
    )
    parser.add_argument(
        "--diff-min-ratio",
        type=float,
        default=0.2,
        help="Skip same-window screenshots when the largest diff area is below this ratio.",
    )
    parser.add_argument(
        "--min-window-width",
        type=int,
        default=200,
        help="Skip or fallback focused window regions narrower than this value.",
    )
    parser.add_argument(
        "--min-window-height",
        type=int,
        default=120,
        help="Skip or fallback focused window regions shorter than this value.",
    )
    parser.add_argument(
        "--min-diff-width",
        type=int,
        default=100,
        help="Skip largest-diff crops narrower than this value.",
    )
    parser.add_argument(
        "--min-diff-height",
        type=int,
        default=80,
        help="Skip largest-diff crops shorter than this value.",
    )
    parser.add_argument(
        "--exclude-app",
        action="append",
        default=[],
        help="Skip an application executable. May be specified multiple times.",
    )
    parser.add_argument(
        "--source",
        default="pc_a",
        help="Logical event source included in each history record.",
    )
    return parser


def main() -> int: 
    args = build_parser().parse_args()
    app = FocusedAppHistoryOCR(
        poll_interval=args.poll_interval,
        idle_seconds=args.idle_seconds,
        focus_switch_settle_seconds=args.focus_switch_settle_seconds,
        output_dir=args.output_dir,
        max_memory_history=args.max_memory_history,
        max_images_per_focus=args.max_images_per_focus,
        save_images=not args.no_save_images,
        debug_all_captures=args.debug_all_captures,
        max_debug_images_per_focus=args.max_debug_images_per_focus,
        ocr_enabled=not args.disable_ocr,
        compare_screenshots=args.compare_screenshots,
        diff_min_area=args.diff_min_area,
        diff_margin=args.diff_margin,
        diff_min_ratio=args.diff_min_ratio,
        min_window_width=args.min_window_width,
        min_window_height=args.min_window_height,
        min_diff_width=args.min_diff_width,
        min_diff_height=args.min_diff_height,
        excluded_apps=args.exclude_app,
        source=args.source,
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
