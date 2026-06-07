"""
Detect changed screen regions and OCR only those regions.

Install runtime dependencies:
    pip install mss opencv-python numpy paddleocr paddlepaddle

Examples:
    python screen_change_ocr.py
    python screen_change_ocr.py --debug-crops --text-diff
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    import cv2
    import numpy as np
except ImportError as exc:
    raise SystemExit(
        "Missing image dependencies. Run: pip install opencv-python numpy"
    ) from exc


class ScreenChangeOCR:
    def __init__(
        self,
        interval: float = 1.0,
        monitor_index: int = 1,
        diff_threshold: int = 30,
        min_area: int = 800,
        merge_distance: int = 40,
        crop_margin: int = 10,
        min_change_ratio: float = 0.001,
        debug_crops: bool = False,
        debug_dir: str = "debug_crops",
        text_diff: bool = False,
    ) -> None:
        self.interval = interval
        self.monitor_index = monitor_index
        self.diff_threshold = diff_threshold
        self.min_area = min_area
        self.merge_distance = merge_distance
        self.crop_margin = crop_margin
        self.min_change_ratio = min_change_ratio
        self.debug_crops = debug_crops
        self.debug_dir = Path(debug_dir)
        self.text_diff = text_diff

        self.prev_frame: np.ndarray | None = None
        self._sct: Any = None
        self.ocr: Any = None

        if self.debug_crops:
            self.debug_dir.mkdir(parents=True, exist_ok=True)

    def initialize(self) -> None:
        try:
            import mss
        except ImportError as exc:
            raise RuntimeError(
                "Missing screenshot dependency. Run: pip install mss"
            ) from exc

        try:
            from paddleocr import PaddleOCR
        except ImportError as exc:
            raise RuntimeError(
                "Missing OCR dependency. Run: pip install paddleocr paddlepaddle"
            ) from exc

        self._sct = mss.MSS() if hasattr(mss, "MSS") else mss.mss()
        if not 0 < self.monitor_index < len(self._sct.monitors):
            monitor_count = len(self._sct.monitors) - 1
            raise RuntimeError(
                f"Monitor index {self.monitor_index} is invalid. "
                f"Available physical monitors: 1-{monitor_count}."
            )

        # lang="ch" recognizes both Chinese and English.
        try:
            try:
                # PaddleOCR 3.x API. Screen crops do not need document preprocessing.
                self.ocr = PaddleOCR(
                    lang="ch",
                    ocr_version="PP-OCRv4",
                    use_doc_orientation_classify=False,
                    use_doc_unwarping=False,
                    use_textline_orientation=False,
                    device="cpu",
                    enable_mkldnn=False,
                )
            except (TypeError, ValueError):
                # PaddleOCR 2.x compatibility.
                self.ocr = PaddleOCR(use_angle_cls=False, lang="ch", show_log=False)
        except Exception as exc:
            raise RuntimeError(
                "PaddleOCR initialization failed. On the first run it must download "
                "OCR models. Check network access to a PaddleOCR model hoster or "
                "configure local model directories."
            ) from exc

    def close(self) -> None:
        if self._sct is not None:
            self._sct.close()
            self._sct = None

    def capture_screen(self) -> np.ndarray:
        if self._sct is None:
            raise RuntimeError("Screen capture is not initialized.")

        monitor = self._sct.monitors[self.monitor_index]
        screenshot = self._sct.grab(monitor)
        frame = np.array(screenshot)
        return cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)

    def find_changed_boxes(
        self, previous: np.ndarray, current: np.ndarray
    ) -> list[list[int]]:
        if previous.shape != current.shape:
            previous = cv2.resize(previous, (current.shape[1], current.shape[0]))

        diff = cv2.absdiff(previous, current)
        gray = cv2.cvtColor(diff, cv2.COLOR_BGR2GRAY)
        _, mask = cv2.threshold(gray, self.diff_threshold, 255, cv2.THRESH_BINARY)

        kernel_small = np.ones((3, 3), np.uint8)
        kernel_large = np.ones((7, 7), np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel_small)
        mask = cv2.dilate(mask, kernel_large, iterations=2)

        contours, _ = cv2.findContours(
            mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )

        boxes = []
        for contour in contours:
            x, y, width, height = cv2.boundingRect(contour)
            if width * height >= self.min_area:
                boxes.append([x, y, width, height])

        return self.merge_boxes(boxes)

    def merge_boxes(self, boxes: list[list[int]]) -> list[list[int]]:
        boxes = [list(box) for box in boxes]
        changed = True

        # Repeat until stable so chained neighboring boxes are also merged.
        while changed:
            changed = False
            merged: list[list[int]] = []

            for box in boxes:
                for index, existing in enumerate(merged):
                    if self._boxes_are_close(box, existing):
                        merged[index] = self._union_box(box, existing)
                        changed = True
                        break
                else:
                    merged.append(box)

            boxes = merged

        return boxes

    def _boxes_are_close(self, first: list[int], second: list[int]) -> bool:
        x1, y1, width1, height1 = first
        x2, y2, width2, height2 = second
        distance = self.merge_distance
        return not (
            x1 + width1 < x2 - distance
            or x1 > x2 + width2 + distance
            or y1 + height1 < y2 - distance
            or y1 > y2 + height2 + distance
        )

    @staticmethod
    def _union_box(first: list[int], second: list[int]) -> list[int]:
        x1, y1, width1, height1 = first
        x2, y2, width2, height2 = second
        left = min(x1, x2)
        top = min(y1, y2)
        right = max(x1 + width1, x2 + width2)
        bottom = max(y1 + height1, y2 + height2)
        return [left, top, right - left, bottom - top]

    def crop_region(
        self, image: np.ndarray, box: list[int]
    ) -> tuple[np.ndarray, list[int]]:
        x, y, width, height = box
        image_height, image_width = image.shape[:2]
        left = max(0, x - self.crop_margin)
        top = max(0, y - self.crop_margin)
        right = min(image_width, x + width + self.crop_margin)
        bottom = min(image_height, y + height + self.crop_margin)
        return image[top:bottom, left:right], [
            left,
            top,
            right - left,
            bottom - top,
        ]

    def run_ocr(self, image: np.ndarray) -> str:
        if self.ocr is None:
            raise RuntimeError("OCR is not initialized.")

        if hasattr(self.ocr, "predict"):
            result = self.ocr.predict(image)
        else:
            result = self.ocr.ocr(image, cls=True)

        texts = []
        for group in result or []:
            # PaddleOCR 3.x returns mapping-like OCRResult objects.
            if hasattr(group, "get"):
                group_texts = group.get("rec_texts", [])
                group_scores = group.get("rec_scores", [])
                texts.extend(
                    text
                    for text, score in zip(group_texts, group_scores)
                    if score >= 0.5
                )
                continue

            # PaddleOCR 2.x returns nested lists of [box, (text, score)].
            for line in group or []:
                try:
                    text, score = line[1]
                except (IndexError, TypeError, ValueError):
                    continue
                if score >= 0.5:
                    texts.append(text)
        return "\n".join(texts).strip()

    @staticmethod
    def added_text(previous: str, current: str) -> str:
        diff = difflib.ndiff(previous.splitlines(), current.splitlines())
        return "\n".join(line[2:] for line in diff if line.startswith("+ ")).strip()

    def save_debug_crop(self, crop: np.ndarray, region_index: int) -> str | None:
        if not self.debug_crops:
            return None

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        path = self.debug_dir / f"crop_{timestamp}_{region_index}.png"
        cv2.imwrite(str(path), crop)
        return str(path)

    def process_once(self) -> dict[str, Any] | None:
        current = self.capture_screen()
        if self.prev_frame is None:
            self.prev_frame = current
            return None

        previous = self.prev_frame
        boxes = self.find_changed_boxes(previous, current)
        self.prev_frame = current
        if not boxes:
            return None

        screen_area = current.shape[0] * current.shape[1]
        change_area = sum(width * height for _, _, width, height in boxes)
        change_ratio = change_area / screen_area
        if change_ratio < self.min_change_ratio:
            return None

        regions = []
        for index, box in enumerate(boxes):
            current_crop, expanded_box = self.crop_region(current, box)
            debug_path = self.save_debug_crop(current_crop, index)
            current_text = self.run_ocr(current_crop)

            text = current_text
            if self.text_diff:
                previous_crop, _ = self.crop_region(previous, box)
                text = self.added_text(self.run_ocr(previous_crop), current_text)

            if text:
                region = {"box": expanded_box, "text": text}
                if debug_path:
                    region["debug_crop"] = debug_path
                regions.append(region)

        if not regions:
            return None

        return {
            "type": "screen_change_ocr",
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "source": "windows_agent",
            "change_ratio": round(change_ratio, 6),
            "regions": regions,
        }

    def loop(self) -> None:
        print("Monitoring screen changes. Press Ctrl+C to exit.")
        try:
            while True:
                result = self.process_once()
                if result:
                    print(json.dumps(result, ensure_ascii=False, indent=2))
                time.sleep(self.interval)
        except KeyboardInterrupt:
            print("\nStopped.")
        finally:
            self.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="OCR changed regions of the screen and print JSON events."
    )
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--monitor", type=int, default=1)
    parser.add_argument("--diff-threshold", type=int, default=30)
    parser.add_argument("--min-area", type=int, default=800)
    parser.add_argument("--merge-distance", type=int, default=40)
    parser.add_argument("--crop-margin", type=int, default=10)
    parser.add_argument("--min-change-ratio", type=float, default=0.001)
    parser.add_argument("--debug-crops", action="store_true")
    parser.add_argument("--debug-dir", default="debug_crops")
    parser.add_argument(
        "--text-diff",
        action="store_true",
        help="OCR previous and current crops, then emit only added lines.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    app = ScreenChangeOCR(
        interval=args.interval,
        monitor_index=args.monitor,
        diff_threshold=args.diff_threshold,
        min_area=args.min_area,
        merge_distance=args.merge_distance,
        crop_margin=args.crop_margin,
        min_change_ratio=args.min_change_ratio,
        debug_crops=args.debug_crops,
        debug_dir=args.debug_dir,
        text_diff=args.text_diff,
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
