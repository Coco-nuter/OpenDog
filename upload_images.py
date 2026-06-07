import argparse
import getpass
import os
import posixpath
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    import paramiko
except ImportError:  # pragma: no cover - dependency check happens at runtime
    paramiko = None


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tif", ".tiff"}


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Upload images to an Aliyun server over SFTP and measure upload speed."
    )
    parser.add_argument(
        "images",
        nargs="+",
        help="Image files or folders. Folders are scanned recursively by default.",
    )
    parser.add_argument("--host", default="10.0.8.1", help="Server host/IP. Default: 10.0.8.1")
    parser.add_argument("--port", type=int, default=22, help="SSH port. Default: 22")
    parser.add_argument("--username", "-u", required=True, help="SSH username")
    parser.add_argument(
        "--remote-dir",
        "-r",
        required=True,
        help="Target folder on the server, for example /data/images",
    )
    parser.add_argument(
        "--password",
        default=os.getenv("ALIYUN_SSH_PASSWORD"),
        help="SSH password. Can also be set by ALIYUN_SSH_PASSWORD.",
    )
    parser.add_argument(
        "--key-file",
        default=os.getenv("ALIYUN_SSH_KEY_FILE"),
        help="SSH private key file. Can also be set by ALIYUN_SSH_KEY_FILE.",
    )
    parser.add_argument(
        "--key-passphrase",
        default=os.getenv("ALIYUN_SSH_KEY_PASSPHRASE"),
        help="SSH private key passphrase. Can also be set by ALIYUN_SSH_KEY_PASSPHRASE.",
    )
    parser.add_argument(
        "--no-recursive",
        action="store_true",
        help="Do not recursively scan folders.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite files if the same remote filename already exists.",
    )
    parser.add_argument(
        "--repeat",
        "-n",
        type=int,
        default=1,
        help="Upload the same image set multiple times for speed testing. Default: 1",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.0,
        help="Seconds to wait between repeat rounds. Default: 0",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="SSH connection timeout in seconds. Default: 30",
    )
    return parser.parse_args()


def collect_images(paths, recursive=True):
    images = []
    for raw_path in paths:
        path = Path(raw_path).expanduser()
        if not path.exists():
            raise FileNotFoundError(f"Path does not exist: {path}")

        if path.is_file():
            if path.suffix.lower() in IMAGE_EXTENSIONS:
                images.append(path)
            continue

        iterator = path.rglob("*") if recursive else path.glob("*")
        images.extend(
            item
            for item in iterator
            if item.is_file() and item.suffix.lower() in IMAGE_EXTENSIONS
        )

    unique_images = []
    seen = set()
    for image in images:
        resolved = image.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique_images.append(resolved)
    return unique_images


def connect_sftp(args):
    if paramiko is None:
        raise RuntimeError("Missing dependency: run `pip install paramiko` first.")

    if not args.password and not args.key_file:
        args.password = getpass.getpass("SSH password: ")

    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.WarningPolicy())

    connect_kwargs = {
        "hostname": args.host,
        "port": args.port,
        "username": args.username,
        "timeout": args.timeout,
        "banner_timeout": args.timeout,
        "auth_timeout": args.timeout,
    }

    if args.key_file:
        connect_kwargs["key_filename"] = str(Path(args.key_file).expanduser())
        connect_kwargs["passphrase"] = args.key_passphrase
    else:
        connect_kwargs["password"] = args.password

    client.connect(**connect_kwargs)
    return client, client.open_sftp()


def ensure_remote_dir(sftp, remote_dir):
    remote_dir = normalize_remote_dir(remote_dir)
    current = ""
    for part in remote_dir.split("/"):
        if not part:
            current = "/"
            continue

        current = posixpath.join(current, part)
        if not remote_exists(sftp, current):
            sftp.mkdir(current)
    return remote_dir


def normalize_remote_dir(remote_dir):
    normalized = remote_dir.replace("\\", "/").rstrip("/")
    if not normalized:
        raise ValueError("Remote directory cannot be empty.")
    if not normalized.startswith("/"):
        normalized = "/" + normalized
    return normalized


def unique_remote_path(sftp, remote_path):
    stem, ext = posixpath.splitext(remote_path)
    candidate = remote_path
    index = 1
    while True:
        if not remote_exists(sftp, candidate):
            return candidate
        candidate = f"{stem}_{index}{ext}"
        index += 1


def remote_exists(sftp, remote_path):
    try:
        sftp.stat(remote_path)
        return True
    except OSError:
        return False


def build_remote_filename(local_path, run_id=None):
    if run_id is None:
        return local_path.name

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    return f"{local_path.stem}_run{run_id:03d}_{timestamp}{local_path.suffix}"


def upload_one(sftp, local_path, remote_dir, overwrite=False, run_id=None):
    remote_filename = build_remote_filename(local_path, None if overwrite else run_id)
    remote_path = posixpath.join(remote_dir, remote_filename)
    if not overwrite:
        remote_path = unique_remote_path(sftp, remote_path)

    size = local_path.stat().st_size
    started = time.perf_counter()
    sftp.put(str(local_path), remote_path)
    elapsed = time.perf_counter() - started
    mb = size / 1024 / 1024
    speed = mb / elapsed if elapsed > 0 else 0.0
    return {
        "local": str(local_path),
        "remote": remote_path,
        "bytes": size,
        "elapsed": elapsed,
        "speed_mbps": speed,
        "run_id": run_id,
    }


def format_size(num_bytes):
    mb = num_bytes / 1024 / 1024
    return f"{mb:.2f} MB"


def main():
    args = parse_args()
    if args.repeat < 1:
        print("--repeat must be greater than or equal to 1.")
        return 1
    if args.delay < 0:
        print("--delay cannot be negative.")
        return 1

    images = collect_images(args.images, recursive=not args.no_recursive)
    if not images:
        print("No image files found.")
        return 1

    print(
        f"Found {len(images)} image(s). Repeat: {args.repeat}. "
        f"Connecting to {args.username}@{args.host}:{args.port} ..."
    )

    client = None
    sftp = None
    results = []
    bytes_per_run = sum(path.stat().st_size for path in images)
    total_started = time.perf_counter()

    try:
        client, sftp = connect_sftp(args)
        remote_dir = ensure_remote_dir(sftp, args.remote_dir)
        print(f"Remote folder: {remote_dir}")

        for run_id in range(1, args.repeat + 1):
            run_started = time.perf_counter()
            run_results = []
            print(f"\nRun {run_id}/{args.repeat}")

            for index, image in enumerate(images, start=1):
                result = upload_one(
                    sftp,
                    image,
                    remote_dir,
                    overwrite=args.overwrite,
                    run_id=run_id,
                )
                results.append(result)
                run_results.append(result)
                print(
                    f"[{index}/{len(images)}] {image.name} -> {result['remote']} | "
                    f"{format_size(result['bytes'])} in {result['elapsed']:.2f}s | "
                    f"{result['speed_mbps']:.2f} MB/s"
                )

            run_wall_time = time.perf_counter() - run_started
            run_upload_time = sum(item["elapsed"] for item in run_results)
            run_speed = (
                (bytes_per_run / 1024 / 1024) / run_upload_time
                if run_upload_time > 0
                else 0.0
            )
            print(
                f"Run {run_id} summary: {format_size(bytes_per_run)} uploaded in "
                f"{run_upload_time:.2f}s, average {run_speed:.2f} MB/s "
                f"(wall {run_wall_time:.2f}s)"
            )

            if run_id < args.repeat and args.delay > 0:
                time.sleep(args.delay)
    finally:
        if sftp is not None:
            sftp.close()
        if client is not None:
            client.close()

    total_elapsed = time.perf_counter() - total_started
    upload_elapsed = sum(item["elapsed"] for item in results)
    total_bytes = bytes_per_run * args.repeat
    average_speed = (total_bytes / 1024 / 1024) / upload_elapsed if upload_elapsed > 0 else 0.0
    fastest = max(results, key=lambda item: item["speed_mbps"]) if results else None
    slowest = min(results, key=lambda item: item["speed_mbps"]) if results else None

    print("\nUpload summary")
    print(f"Runs: {args.repeat}")
    print(f"Files uploaded: {len(results)}")
    print(f"Files per run: {len(images)}")
    print(f"Total size: {format_size(total_bytes)}")
    print(f"Upload time: {upload_elapsed:.2f}s")
    print(f"Wall time: {total_elapsed:.2f}s")
    print(f"Average speed: {average_speed:.2f} MB/s")
    if fastest and slowest:
        print(f"Fastest file: {Path(fastest['local']).name} ({fastest['speed_mbps']:.2f} MB/s)")
        print(f"Slowest file: {Path(slowest['local']).name} ({slowest['speed_mbps']:.2f} MB/s)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
