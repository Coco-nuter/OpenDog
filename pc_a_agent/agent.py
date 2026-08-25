"""Start and supervise the PC A collector, syncer, and message receiver."""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Worker:
    name: str
    command: list[str]
    process: subprocess.Popen[bytes] | None = None
    restart_at: float = 0.0


def start_worker(worker: Worker, working_directory: Path) -> None:
    print(f"[agent] Starting {worker.name}: {' '.join(worker.command)}")
    worker.process = subprocess.Popen(worker.command, cwd=working_directory)
    worker.restart_at = 0.0


def stop_workers(workers: list[Worker]) -> None:
    for worker in workers:
        if worker.process is not None and worker.process.poll() is None:
            worker.process.terminate()
    for worker in workers:
        if worker.process is None:
            continue
        try:
            worker.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            worker.process.kill()
            worker.process.wait(timeout=5)


def run(config_path: Path, save_images: bool, restart_delay: float) -> int:
    agent_directory = Path(__file__).resolve().parent
    project_root = agent_directory.parent
    resolved_config = config_path.resolve()
    if not resolved_config.exists():
        raise FileNotFoundError(f"Config file does not exist: {resolved_config}")

    collector_command = [
        sys.executable,
        str(agent_directory / "focused_app_history_ocr.py"),
    ]
    if not save_images:
        collector_command.append("--no-save-images")

    workers = [
        Worker("collector", collector_command),
        Worker(
            "syncer",
            [
                sys.executable,
                str(agent_directory / "syncer.py"),
                "--config",
                str(resolved_config),
            ],
        ),
        Worker(
            "receiver",
            [
                sys.executable,
                str(agent_directory / "receiver.py"),
                "--config",
                str(resolved_config),
            ],
        ),
    ]

    for worker in workers:
        start_worker(worker, project_root)

    try:
        while True:
            now = time.monotonic()
            for worker in workers:
                if worker.process is None:
                    continue
                exit_code = worker.process.poll()
                if exit_code is None:
                    continue
                if worker.restart_at == 0.0:
                    worker.restart_at = now + restart_delay
                    print(
                        f"[agent] {worker.name} exited with code {exit_code}; "
                        f"restarting in {restart_delay:.1f} seconds"
                    )
                elif now >= worker.restart_at:
                    start_worker(worker, project_root)
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("[agent] Stopping all workers")
        stop_workers(workers)
        return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run all OpenDog PC A components with one command."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("sync_config.json"),
        help="Path to sync_config.json.",
    )
    parser.add_argument(
        "--save-images",
        action="store_true",
        help="Save capture images. Images are disabled by default.",
    )
    parser.add_argument(
        "--restart-delay",
        type=float,
        default=3.0,
        help="Seconds before restarting an exited worker.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.restart_delay < 0:
        print("[ERROR] --restart-delay cannot be negative", file=sys.stderr)
        return 1
    try:
        return run(args.config, args.save_images, args.restart_delay)
    except (OSError, ValueError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
