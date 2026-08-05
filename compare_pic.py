import argparse
import csv
import json
import os
import statistics
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import cv2

try:
    import psutil
except ImportError:  # pragma: no cover - useful on stripped-down target machines
    psutil = None

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


class RapidOCRTextRecognizer:
    def __init__(
        self,
        min_score=0.5,
        use_det=True,
        use_cls=False,
        use_rec=True,
        limit_side_len=960,
    ):
        self.min_score = min_score
        self.use_det = use_det
        self.use_cls = use_cls
        self.use_rec = use_rec
        self.limit_side_len = limit_side_len
        self.last_ocr_metrics = {}
        self.ocr = self._create_ocr()

    @staticmethod
    def _create_ocr():
        try:
            from rapidocr import RapidOCR
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "缺少 RapidOCR 依赖，请先安装："
                "pip install rapidocr onnxruntime opencv-python"
            ) from exc
        except Exception as exc:
            raise RuntimeError(
                "RapidOCR 导入失败，通常是 rapidocr、onnxruntime 或 opencv-python "
                "版本不兼容。"
                f" 原始错误：{exc}"
            ) from exc

        try:
            return RapidOCR()
        except Exception as exc:
            raise RuntimeError(
                "RapidOCR 初始化失败。若是首次运行，需要能下载模型或配置本地模型。"
                f" 原始错误：{exc}"
            ) from exc

    def recognize(self, image):
        self.last_ocr_metrics = {}
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

        self.last_ocr_metrics = {
            "provider": "rapidocr",
            "elapse_seconds": self._safe_round(getattr(result, "elapse", None)),
            "elapse_list_seconds": [
                self._safe_round(value)
                for value in self._as_sequence(getattr(result, "elapse_list", []))
            ],
            "use_det": self.use_det,
            "use_cls": self.use_cls,
            "use_rec": self.use_rec,
        }

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

    def _resize_for_ocr(self, image):
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
    def _as_sequence(value):
        if value is None:
            return []
        if hasattr(value, "tolist"):
            value = value.tolist()
        return list(value)

    @staticmethod
    def _score_at(scores, index):
        if index >= len(scores):
            return 1.0
        score = float(scores[index])
        if score > 1:
            score = score / 100
        return score

    @classmethod
    def _to_jsonable_box(cls, box):
        if hasattr(box, "tolist"):
            box = box.tolist()
        if isinstance(box, (list, tuple)):
            return [cls._to_jsonable_box(item) for item in box]
        try:
            return round(float(box), 3)
        except (TypeError, ValueError):
            return box

    @staticmethod
    def _safe_round(value):
        if value is None:
            return None
        return round(float(value), 6)


def extract_diff_region_data(
    img1_path,
    img2_path,
    output_dir="diff_regions",
    min_area=100,
    margin=5,
    save_images=True,
    text_recognizer=None,
):
    if save_images:
        os.makedirs(output_dir, exist_ok=True)

    img1 = cv2.imread(img1_path)
    img2 = cv2.imread(img2_path)

    if img1 is None or img2 is None:
        raise ValueError("图片读取失败，请检查路径")

    if img1.shape != img2.shape:
        img2 = cv2.resize(img2, (img1.shape[1], img1.shape[0]))

    gray1 = cv2.cvtColor(img1, cv2.COLOR_BGR2GRAY)
    gray2 = cv2.cvtColor(img2, cv2.COLOR_BGR2GRAY)
    diff = cv2.absdiff(gray1, gray2)

    _, thresh = cv2.threshold(diff, 80, 255, cv2.THRESH_BINARY)

    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (9, 9))
    thresh = cv2.dilate(thresh, kernel, iterations=5)

    contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    regions = []
    region_crops = []
    annotated = img2.copy()
    h, w = img2.shape[:2]

    for i, contour in enumerate(contours):
        area = cv2.contourArea(contour)

        if area < min_area:
            continue

        x, y, bw, bh = cv2.boundingRect(contour)
        x1 = max(0, x - margin)
        y1 = max(0, y - margin)
        x2 = min(w, x + bw + margin)
        y2 = min(h, y + bh + margin)

        crop = img2[y1:y2, x1:x2]
        output_path = None
        if save_images:
            output_path = os.path.join(output_dir, f"diff_{i}.png")
            cv2.imwrite(output_path, crop)

        region = {
            "index": i,
            "box": [x1, y1, x2, y2],
            "area": round(float(area), 3),
            "image_path": output_path,
            "text": "",
            "ocr_items": [],
            "ocr_metrics": {},
            "is_ocr_target": False,
        }
        regions.append(region)
        region_crops.append((region, crop))

        cv2.rectangle(annotated, (x1, y1), (x2, y2), (0, 0, 255), 2)

    if text_recognizer and region_crops:
        selected_region_crops = sorted(
            region_crops, key=lambda item: item[0]["area"], reverse=True
        )[:3]
        selected_regions = [item[0] for item in selected_region_crops]
        x1 = min(region["box"][0] for region in selected_regions)
        y1 = min(region["box"][1] for region in selected_regions)
        x2 = max(region["box"][2] for region in selected_regions)
        y2 = max(region["box"][3] for region in selected_regions)
        target_crop = img2[y1:y2, x1:x2]
        target_region = {
            "index": "top3_merged",
            "box": [x1, y1, x2, y2],
            "area": round(sum(region["area"] for region in selected_regions), 3),
            "image_path": None,
            "text": "",
            "ocr_items": [],
            "ocr_metrics": {},
            "is_ocr_target": True,
            "merged_region_count": len(selected_regions),
            "merged_regions": selected_regions,
        }
        if save_images:
            target_region["image_path"] = os.path.join(
                output_dir, "diff_top3_merged.png"
            )
            cv2.imwrite(target_region["image_path"], target_crop)

        text_items = text_recognizer.recognize(target_crop)
        text = "\n".join(item["text"] for item in text_items).strip()
        target_region["text"] = text
        target_region["ocr_items"] = text_items
        target_region["ocr_metrics"] = getattr(
            text_recognizer,
            "last_ocr_metrics",
            {},
        )
        regions.append(target_region)
        cv2.rectangle(annotated, (x1, y1), (x2, y2), (0, 0, 255), 3)

        if text:
            cv2.putText(
                annotated,
                text.splitlines()[0][:20],
                (x1, max(15, y1 - 6)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.5,
                (0, 0, 255),
                1,
                cv2.LINE_AA,
            )

    if save_images:
        cv2.imwrite(os.path.join(output_dir, "annotated.png"), annotated)
        cv2.imwrite(os.path.join(output_dir, "diff_mask.png"), thresh)

    return regions


def extract_diff_regions(
    img1_path,
    img2_path,
    output_dir="diff_regions",
    min_area=100,
    margin=5,
    save_images=True,
):
    regions = extract_diff_region_data(
        img1_path,
        img2_path,
        output_dir=output_dir,
        min_area=min_area,
        margin=margin,
        save_images=save_images,
        text_recognizer=None,
    )
    return [tuple(region["box"]) for region in regions]


def bytes_to_mb(value):
    if value is None:
        return None
    return round(value / 1024 / 1024, 3)


def percentile(values, percent):
    if not values:
        return None
    sorted_values = sorted(values)
    index = round((len(sorted_values) - 1) * percent / 100)
    return sorted_values[index]


class ResourceSampler:
    def __init__(self, process, interval):
        self.process = process
        self.interval = interval
        self.samples = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self):
        self._sample()
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self._stop.set()
        self._thread.join()
        self._sample()

    def _run(self):
        while not self._stop.wait(self.interval):
            self._sample()

    def _sample(self):
        try:
            memory = self.process.memory_info()
            self.samples.append(
                {
                    "timestamp": time.perf_counter(),
                    "rss_bytes": memory.rss,
                    "vms_bytes": memory.vms,
                    "system_cpu_percent": psutil.cpu_percent(interval=None),
                    "num_threads": self.process.num_threads(),
                }
            )
        except Exception:
            # Sampling should never make the benchmark itself fail.
            pass


def process_snapshot(process):
    if psutil is None:
        return {}

    memory = process.memory_info()
    cpu_times = process.cpu_times()
    snapshot = {
        "rss_bytes": memory.rss,
        "vms_bytes": memory.vms,
        "cpu_user_seconds": cpu_times.user,
        "cpu_system_seconds": cpu_times.system,
        "num_threads": process.num_threads(),
    }

    try:
        ctx = process.num_ctx_switches()
        snapshot["ctx_switches_voluntary"] = ctx.voluntary
        snapshot["ctx_switches_involuntary"] = ctx.involuntary
    except Exception:
        pass

    return snapshot


def collect_environment(process):
    env = {
        "python": os.sys.version,
        "opencv": cv2.__version__,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
    }

    if psutil is None:
        env["psutil_available"] = False
        return env

    env.update(
        {
            "psutil_available": True,
            "logical_cpu_count": psutil.cpu_count(logical=True),
            "physical_cpu_count": psutil.cpu_count(logical=False),
            "total_memory_mb": bytes_to_mb(psutil.virtual_memory().total),
            "process_priority": process.nice(),
        }
    )

    try:
        env["cpu_affinity"] = process.cpu_affinity()
    except Exception:
        pass

    return env


def diff_snapshot(before, after, key):
    if key not in before or key not in after:
        return None
    return after[key] - before[key]


def flatten_text(regions):
    return "\n".join(
        region["text"] for region in regions if region.get("text")
    ).strip()


def run_once(args, process, text_recognizer=None):
    if psutil is not None:
        process.cpu_percent(interval=None)
        psutil.cpu_percent(interval=None)
        before = process_snapshot(process)
    else:
        before = {}

    start = time.perf_counter()
    sampler = ResourceSampler(process, args.sample_interval) if psutil is not None else None

    if sampler is None:
        regions = extract_diff_region_data(
            args.img1,
            args.img2,
            output_dir=args.output_dir,
            min_area=args.min_area,
            margin=args.margin,
            save_images=not args.no_save_images,
            text_recognizer=text_recognizer,
        )
    else:
        with sampler:
            regions = extract_diff_region_data(
                args.img1,
                args.img2,
                output_dir=args.output_dir,
                min_area=args.min_area,
                margin=args.margin,
                save_images=not args.no_save_images,
                text_recognizer=text_recognizer,
            )

    elapsed = time.perf_counter() - start

    if psutil is not None:
        process_cpu_percent = process.cpu_percent(interval=None)
        after = process_snapshot(process)
    else:
        process_cpu_percent = None
        after = {}

    samples = sampler.samples if sampler is not None else []
    peak_rss = max((sample["rss_bytes"] for sample in samples), default=after.get("rss_bytes"))
    peak_vms = max((sample["vms_bytes"] for sample in samples), default=after.get("vms_bytes"))
    peak_threads = max((sample["num_threads"] for sample in samples), default=after.get("num_threads"))
    peak_system_cpu = max((sample["system_cpu_percent"] for sample in samples), default=None)

    cpu_user_delta = diff_snapshot(before, after, "cpu_user_seconds")
    cpu_system_delta = diff_snapshot(before, after, "cpu_system_seconds")
    cpu_total_delta = None
    if cpu_user_delta is not None and cpu_system_delta is not None:
        cpu_total_delta = cpu_user_delta + cpu_system_delta

    ocr_text = flatten_text(regions)

    return {
        "elapsed_seconds": round(elapsed, 6),
        "boxes_count": len(regions),
        "ocr_regions_count": sum(1 for region in regions if region.get("text")),
        "ocr_text": ocr_text,
        "regions": regions,
        "process_cpu_percent": process_cpu_percent,
        "process_cpu_seconds": round(cpu_total_delta, 6) if cpu_total_delta is not None else None,
        "process_cpu_user_seconds": round(cpu_user_delta, 6) if cpu_user_delta is not None else None,
        "process_cpu_system_seconds": round(cpu_system_delta, 6) if cpu_system_delta is not None else None,
        "rss_before_mb": bytes_to_mb(before.get("rss_bytes")),
        "rss_after_mb": bytes_to_mb(after.get("rss_bytes")),
        "rss_peak_mb": bytes_to_mb(peak_rss),
        "vms_peak_mb": bytes_to_mb(peak_vms),
        "num_threads_after": after.get("num_threads"),
        "num_threads_peak": peak_threads,
        "ctx_switches_voluntary_delta": diff_snapshot(before, after, "ctx_switches_voluntary"),
        "ctx_switches_involuntary_delta": diff_snapshot(before, after, "ctx_switches_involuntary"),
        "system_cpu_percent_peak": peak_system_cpu,
    }


def summarize(iterations):
    elapsed = [item["elapsed_seconds"] for item in iterations]
    rss_peak = [item["rss_peak_mb"] for item in iterations if item["rss_peak_mb"] is not None]
    cpu_percent = [
        item["process_cpu_percent"]
        for item in iterations
        if item["process_cpu_percent"] is not None
    ]
    cpu_seconds = [
        item["process_cpu_seconds"]
        for item in iterations
        if item["process_cpu_seconds"] is not None
    ]
    voluntary_ctx = [
        item["ctx_switches_voluntary_delta"]
        for item in iterations
        if item["ctx_switches_voluntary_delta"] is not None
    ]
    involuntary_ctx = [
        item["ctx_switches_involuntary_delta"]
        for item in iterations
        if item["ctx_switches_involuntary_delta"] is not None
    ]

    return {
        "runs": len(iterations),
        "elapsed_seconds_avg": round(statistics.mean(elapsed), 6) if elapsed else None,
        "elapsed_seconds_min": min(elapsed) if elapsed else None,
        "elapsed_seconds_p95": percentile(elapsed, 95),
        "elapsed_seconds_max": max(elapsed) if elapsed else None,
        "rss_peak_mb_max": max(rss_peak) if rss_peak else None,
        "process_cpu_percent_avg": round(statistics.mean(cpu_percent), 3) if cpu_percent else None,
        "process_cpu_percent_max": max(cpu_percent) if cpu_percent else None,
        "process_cpu_seconds_avg": round(statistics.mean(cpu_seconds), 6) if cpu_seconds else None,
        "ctx_switches_voluntary_avg": round(statistics.mean(voluntary_ctx), 3)
        if voluntary_ctx
        else None,
        "ctx_switches_involuntary_avg": round(statistics.mean(involuntary_ctx), 3)
        if involuntary_ctx
        else None,
    }


def write_csv(path, iterations):
    if not iterations:
        return

    csv_rows = []
    for item in iterations:
        row = dict(item)
        row["regions"] = json.dumps(row["regions"], ensure_ascii=False)
        csv_rows.append(row)

    with open(path, "w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=csv_rows[0].keys())
        writer.writeheader()
        writer.writerows(csv_rows)


def create_text_recognizer(args, process):
    if not args.ocr:
        return None, None

    before = process_snapshot(process) if process is not None else {}
    start = time.perf_counter()
    recognizer = RapidOCRTextRecognizer(
        min_score=args.ocr_min_score,
        use_det=args.rapidocr_use_det,
        use_cls=args.rapidocr_use_cls,
        use_rec=args.rapidocr_use_rec,
        limit_side_len=args.rapidocr_limit_side_len,
    )
    elapsed = time.perf_counter() - start
    after = process_snapshot(process) if process is not None else {}

    return recognizer, {
        "ocr_init_seconds": round(elapsed, 6),
        "ocr_init_rss_before_mb": bytes_to_mb(before.get("rss_bytes")),
        "ocr_init_rss_after_mb": bytes_to_mb(after.get("rss_bytes")),
        "ocr_init_rss_delta_mb": bytes_to_mb(
            diff_snapshot(before, after, "rss_bytes")
            if before and after
            else None
        ),
    }


def run_benchmark(args):
    process = psutil.Process(os.getpid()) if psutil is not None else None
    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)

    environment = collect_environment(process) if process is not None else collect_environment(None)
    text_recognizer, ocr_init = create_text_recognizer(args, process)
    if ocr_init is not None:
        print(
            "OCR initialized: "
            f"{ocr_init['ocr_init_seconds']}s, "
            f"rss_delta={ocr_init['ocr_init_rss_delta_mb']}MB"
        )

    warmup_results = []
    iteration_results = []

    for index in range(args.warmup):
        result = run_once(args, process, text_recognizer=text_recognizer)
        result["index"] = index + 1
        warmup_results.append(result)
        print(f"warmup {index + 1}/{args.warmup}: {result['elapsed_seconds']}s")

    for index in range(args.runs):
        result = run_once(args, process, text_recognizer=text_recognizer)
        result["index"] = index + 1
        iteration_results.append(result)
        print(
            f"run {index + 1}/{args.runs}: "
            f"{result['elapsed_seconds']}s, "
            f"rss_peak={result['rss_peak_mb']}MB, "
            f"cpu={result['process_cpu_percent']}%, "
            f"ctx=({result['ctx_switches_voluntary_delta']}/"
            f"{result['ctx_switches_involuntary_delta']}), "
            f"ocr_regions={result['ocr_regions_count']}"
        )

        if args.sleep > 0:
            time.sleep(args.sleep)

    report = {
        "config": {
            "img1": args.img1,
            "img2": args.img2,
            "runs": args.runs,
            "warmup": args.warmup,
            "min_area": args.min_area,
            "margin": args.margin,
            "save_images": not args.no_save_images,
            "sample_interval": args.sample_interval,
            "ocr": args.ocr,
            "ocr_min_score": args.ocr_min_score,
            "ocr_provider": "rapidocr",
            "rapidocr_use_det": args.rapidocr_use_det,
            "rapidocr_use_cls": args.rapidocr_use_cls,
            "rapidocr_use_rec": args.rapidocr_use_rec,
            "rapidocr_limit_side_len": args.rapidocr_limit_side_len,
        },
        "environment": environment,
        "ocr_init": ocr_init,
        "summary": summarize(iteration_results),
        "warmup": warmup_results,
        "iterations": iteration_results,
    }

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = report_dir / f"compare_pic_perf_{timestamp}.json"
    csv_path = report_dir / f"compare_pic_perf_{timestamp}.csv"

    with open(json_path, "w", encoding="utf-8") as file:
        json.dump(report, file, ensure_ascii=False, indent=2)

    write_csv(csv_path, iteration_results)

    return report, json_path, csv_path


def parse_args():
    parser = argparse.ArgumentParser(
        description="重复运行图片差异提取，并采集耗时、内存、CPU、调度和可选 OCR 指标。"
    )
    parser.add_argument("--img1", default="old.png", help="旧图片路径")
    parser.add_argument("--img2", default="new.png", help="新图片路径")
    parser.add_argument("--output-dir", default="diff_regions", help="差异图片输出目录")
    parser.add_argument("--report-dir", default="perf_reports", help="性能报告输出目录")
    parser.add_argument("--runs", type=int, default=10, help="正式运行次数")
    parser.add_argument("--warmup", type=int, default=1, help="预热次数，不计入汇总")
    parser.add_argument("--sleep", type=float, default=0.0, help="每轮结束后的等待秒数")
    parser.add_argument("--sample-interval", type=float, default=0.01, help="资源采样间隔秒数")
    parser.add_argument("--min-area", type=int, default=100, help="最小差异区域面积")
    parser.add_argument("--margin", type=int, default=8, help="差异区域裁剪边距")
    parser.add_argument(
        "--no-save-images",
        action="store_true",
        help="不写差异图片，只测计算和 OCR 性能",
    )
    parser.add_argument(
        "--ocr",
        action="store_true",
        help="对面积最大的差异区域执行文字识别",
    )
    parser.add_argument(
        "--ocr-provider",
        choices=["paddle", "rapidocr", "aliyun"],
        default="paddle",
        help=(
            "OCR 后端：paddle 使用本地 PaddleOCR，rapidocr 使用本地 RapidOCR，"
            "aliyun 使用阿里云 RecognizeGeneral"
        ),
    )
    parser.add_argument("--ocr-lang", default="ch", help="PaddleOCR 语言，ch 可识别中英文")
    parser.add_argument(
        "--ocr-min-score",
        type=float,
        default=0.5,
        help="OCR 结果最低置信度",
    )
    parser.add_argument(
        "--paddle-det-model",
        default="PP-OCRv5_mobile_det",
        help="PaddleOCR 检测模型名称",
    )
    parser.add_argument(
        "--paddle-rec-model",
        default="PP-OCRv5_mobile_rec",
        help="PaddleOCR 识别模型名称",
    )
    parser.add_argument(
        "--paddle-det-model-dir",
        default=None,
        help="PaddleOCR 检测模型本地目录；设置后优先使用本地模型",
    )
    parser.add_argument(
        "--paddle-rec-model-dir",
        default=None,
        help="PaddleOCR 识别模型本地目录；设置后优先使用本地模型",
    )
    parser.add_argument(
        "--paddle-enable-hpi",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="启用 PaddleOCR 高性能推理模式",
    )
    parser.add_argument(
        "--paddle-enable-mkldnn",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="启用 PaddleOCR oneDNN/MKLDNN；默认关闭以避开部分 Paddle 静态图兼容问题",
    )
    parser.add_argument(
        "--paddle-engine",
        default="",
        help="PaddleOCR 推理引擎；默认空字符串，让 enable_hpi 接管高性能模式",
    )
    parser.add_argument(
        "--paddle-limit-side-len",
        type=int,
        default=960,
        help="OCR 前将最大差异区域最长边缩放到该尺寸；0 表示不缩放",
    )
    parser.add_argument(
        "--paddle-limit-type",
        choices=["max", "min"],
        default="max",
        help="PaddleOCR 文本检测尺寸限制类型",
    )
    parser.add_argument(
        "--paddle-disable-model-source-check",
        action="store_true",
        help="设置 PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True，适合已配置本地模型目录的离线环境",
    )
    parser.add_argument(
        "--rapidocr-use-det",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="RapidOCR 是否启用文本检测",
    )
    parser.add_argument(
        "--rapidocr-use-cls",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="RapidOCR 是否启用方向分类；截图 OCR 默认关闭以减少耗时",
    )
    parser.add_argument(
        "--rapidocr-use-rec",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="RapidOCR 是否启用文本识别",
    )
    parser.add_argument(
        "--rapidocr-limit-side-len",
        type=int,
        default=960,
        help="RapidOCR 前将最大差异区域最长边缩放到该尺寸；0 表示不缩放",
    )
    parser.add_argument(
        "--aliyun-access-key-id",
        default=os.getenv("ALIBABA_CLOUD_ACCESS_KEY_ID")
        or os.getenv("ALIYUN_ACCESS_KEY_ID"),
        help="阿里云 AccessKey ID，也可用 ALIBABA_CLOUD_ACCESS_KEY_ID 环境变量",
    )
    parser.add_argument(
        "--aliyun-access-key-secret",
        default=os.getenv("ALIBABA_CLOUD_ACCESS_KEY_SECRET")
        or os.getenv("ALIYUN_ACCESS_KEY_SECRET"),
        help="阿里云 AccessKey Secret，也可用 ALIBABA_CLOUD_ACCESS_KEY_SECRET 环境变量",
    )
    parser.add_argument(
        "--aliyun-endpoint",
        default="ocr-api.cn-hangzhou.aliyuncs.com",
        help="阿里云 OCR API endpoint",
    )
    parser.add_argument(
        "--aliyun-image-format",
        choices=["png", "jpg", "jpeg"],
        default="png",
        help="提交给阿里云 OCR 的差异区域图片编码格式",
    )
    return parser.parse_args()


if __name__ == "__main__":
    benchmark_report, benchmark_json, benchmark_csv = run_benchmark(parse_args())

    last_iteration = benchmark_report["iterations"][-1]
    print("\n变化区域数量:", last_iteration["boxes_count"])
    if benchmark_report["config"]["ocr"]:
        print("识别到文字的区域数量:", last_iteration["ocr_regions_count"])
        if last_iteration["ocr_text"]:
            print("识别文字:")
            print(last_iteration["ocr_text"])

    print("汇总:")
    for key, value in benchmark_report["summary"].items():
        print(f"  {key}: {value}")
    print(f"JSON报告: {benchmark_json}")
    print(f"CSV报告: {benchmark_csv}")
    
