#!/usr/bin/env python3
"""
Img2Img Batch Avatar Generator (powered by Nano Banana)
=======================================================

Loops through every image in an input directory, sends it to the Nano Banana
image-editing API (model: ``nano2``) together with a style prompt, polls the
task until it finishes, and saves the result to an output directory using
sequential file names (avatar_0001.png, avatar_0002.png, ...).

Workflow per image:
    1. Validate/normalize the local image with Pillow.
    2. Upload it to a temporary public host (the Nano Banana API only accepts
       publicly accessible image URLs).
    3. POST  https://nanobnana.com/api/edit    -> task_id
    4. GET   https://nanobnana.com/api/status  -> poll until status_code 1/2
    5. Download the generated image and save it to output_avatars/.

API docs:  https://nanobnana.com/docs/api/image-to-image
Tutorial:  https://christopherlazok.com/blog/recreating-lensa-ai-stable-diffusion
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import os
import random
import re
import sys
import time
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

import requests
from dotenv import load_dotenv
from PIL import Image, ImageOps, UnidentifiedImageError

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
load_dotenv()

API_BASE = os.getenv("NANO_BANANA_API_BASE", "https://nanobnana.com").rstrip("/")
EDIT_URL = f"{API_BASE}/api/edit"
STATUS_URL = f"{API_BASE}/api/status"
DEFAULT_MODEL = "nano2"

STYLE_PROMPT = (
    "highly detailed flat vector art illustration, pop art style, "
    "professional cartoon illustration, masterpiece, clean lines, solid background."
)

# Status codes documented at https://nanobnana.com/docs/api/query-tasks-result
STATUS_SUCCESS, STATUS_FAILED, STATUS_IN_PROGRESS = 1, 2, 3

# Temporary public host used to give the API a reachable URL for local files.
LITTERBOX_URL = "https://litterbox.catbox.moe/resources/internals/api.php"

SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
OUTPUT_PREFIX = "avatar_"
OUTPUT_PATTERN = re.compile(rf"^{OUTPUT_PREFIX}(\d+)\.png$", re.IGNORECASE)
IMAGE_URL_PATTERN = re.compile(r"https?://\S+", re.IGNORECASE)

HTTP_TIMEOUT = 60
RETRYABLE_STATUS = {429, 500, 502, 503, 504}

log = logging.getLogger("batch_ai_avatars")


class TaskFailed(RuntimeError):
    """Raised when the API reports a task as failed or it times out."""


class FatalAPIError(RuntimeError):
    """Raised for errors that will affect every image (bad key, no credits)."""


def env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


# --------------------------------------------------------------------------- #
# File helpers
# --------------------------------------------------------------------------- #
def iter_input_images(input_dir: Path) -> Iterable[Path]:
    """Yield candidate image files in a stable, sorted order."""
    for path in sorted(input_dir.iterdir()):
        if not path.is_file() or path.name.startswith("."):
            continue
        if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            log.info("Skipping non-image file: %s", path.name)
            continue
        yield path


def next_output_index(output_dir: Path) -> int:
    """Return the next sequential index so existing avatars are never overwritten."""
    indices = [
        int(m.group(1))
        for p in output_dir.glob(f"{OUTPUT_PREFIX}*.png")
        if (m := OUTPUT_PATTERN.match(p.name))
    ]
    return max(indices, default=0) + 1


def prepare_image(path: Path, max_side: int) -> bytes:
    """
    Validate and normalize an image (EXIF rotation, RGB, downscale) and return
    JPEG bytes. Raises UnidentifiedImageError / OSError for corrupt files.
    """
    with Image.open(path) as img:
        img.verify()

    with Image.open(path) as img:
        img = ImageOps.exif_transpose(img).convert("RGB")
        img.thumbnail((max_side, max_side), Image.LANCZOS)
        buffer = io.BytesIO()
        img.save(buffer, format="JPEG", quality=92)
        return buffer.getvalue()


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #
def request_with_retries(
    session: requests.Session,
    method: str,
    url: str,
    *,
    max_retries: int,
    base_delay: float,
    **kwargs: Any,
) -> requests.Response:
    """Send an HTTP request, retrying on rate limits (429), 5xx, and network errors."""
    kwargs.setdefault("timeout", HTTP_TIMEOUT)
    for attempt in range(1, max_retries + 1):
        try:
            resp = session.request(method, url, **kwargs)
            if resp.status_code not in RETRYABLE_STATUS:
                return resp
            reason = "Rate limited (429)" if resp.status_code == 429 else f"Server error ({resp.status_code})"
            retry_after = resp.headers.get("Retry-After")
        except (requests.ConnectionError, requests.Timeout) as exc:
            reason, retry_after = f"Network error ({exc.__class__.__name__})", None

        if attempt == max_retries:
            raise RuntimeError(f"{reason} after {max_retries} attempts: {method} {url}")

        if retry_after and retry_after.isdigit():
            delay = float(retry_after)
        else:
            delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0, 1)
        log.warning("  %s. Retry %d/%d in %.1fs...", reason, attempt, max_retries - 1, delay)
        time.sleep(delay)

    raise RuntimeError("Unreachable")  # pragma: no cover


def parse_json(resp: requests.Response) -> dict:
    try:
        payload = resp.json()
    except ValueError:
        raise RuntimeError(f"Non-JSON response (HTTP {resp.status_code}): {resp.text[:200]}")
    return payload if isinstance(payload, dict) else {"data": payload}


def raise_for_api_error(resp: requests.Response, payload: dict) -> None:
    """Translate HTTP / body-level error codes into exceptions."""
    code = payload.get("code", resp.status_code)
    message = payload.get("message") or resp.reason
    try:
        code = int(code)
    except (TypeError, ValueError):
        code = resp.status_code

    if resp.status_code == 401 or code == 401:
        raise FatalAPIError(f"Unauthorized: check NANO_BANANA_API_KEY ({message})")
    if resp.status_code == 402 or code == 402:
        raise FatalAPIError(f"Insufficient credits ({message})")
    if resp.status_code >= 400 or code >= 400:
        raise RuntimeError(f"API error {code}: {message}")


# --------------------------------------------------------------------------- #
# Pipeline steps
# --------------------------------------------------------------------------- #
def upload_image(session: requests.Session, data: bytes, filename: str, args: argparse.Namespace) -> str:
    """Return a publicly accessible URL for the given image."""
    if args.public_base_url:
        # Images are already hosted (e.g. S3 bucket / CDN mirroring input_images/).
        return f"{args.public_base_url.rstrip('/')}/{quote(filename)}"

    resp = request_with_retries(
        session, "POST", LITTERBOX_URL,
        max_retries=args.max_retries, base_delay=args.retry_delay,
        data={"reqtype": "fileupload", "time": args.upload_expiry},
        files={"fileToUpload": (f"{Path(filename).stem}.jpg", data, "image/jpeg")},
        timeout=120,
    )
    url = resp.text.strip()
    if resp.status_code != 200 or not url.startswith("http"):
        raise RuntimeError(f"Temporary upload failed (HTTP {resp.status_code}): {url[:200]}")
    return url


def submit_edit_task(session: requests.Session, image_url: str, args: argparse.Namespace) -> str:
    body: dict[str, Any] = {
        "prompt": args.prompt,
        "images": [image_url],
        "model": args.model,
        "aspect_ratio": args.aspect_ratio,
        "resolution": args.resolution,
        "output_format": "png",
    }
    resp = request_with_retries(
        session, "POST", EDIT_URL,
        max_retries=args.max_retries, base_delay=args.retry_delay, json=body,
    )
    payload = parse_json(resp)
    raise_for_api_error(resp, payload)

    task_id = payload.get("task_id") or (payload.get("data") or {}).get("task_id")
    if not task_id:
        raise RuntimeError(f"No task_id in response: {json.dumps(payload)[:300]}")
    return task_id


def poll_task(session: requests.Session, task_id: str, args: argparse.Namespace) -> Any:
    """Poll /api/status until the task succeeds, fails, or times out."""
    deadline = time.monotonic() + args.poll_timeout
    while True:
        resp = request_with_retries(
            session, "GET", STATUS_URL,
            max_retries=args.max_retries, base_delay=args.retry_delay,
            params={"task_id": task_id},
        )
        payload = parse_json(resp)
        raise_for_api_error(resp, payload)
        task = payload.get("data") or payload

        status_code = task.get("status_code")
        status_text = str(task.get("status_text") or task.get("status") or "").upper()

        if status_code == STATUS_SUCCESS or status_text in {"SUCCESS", "COMPLETED", "SUCCEEDED"}:
            return task.get("response")
        if status_code == STATUS_FAILED or status_text in {"FAILED", "FAIL", "ERROR", "NSFW"}:
            raise TaskFailed(task.get("error_message") or task.get("message") or f"Task {task_id} failed")

        if time.monotonic() > deadline:
            raise TaskFailed(f"Timed out after {args.poll_timeout:.0f}s waiting for task {task_id}")

        log.debug("  Task %s: %s", task_id, status_text or "IN_PROGRESS")
        time.sleep(args.poll_interval)


def extract_image_urls(value: Any, depth: int = 0) -> list[str]:
    """Recursively pull image URLs out of the task 'response' field."""
    if value is None or depth > 6:
        return []
    if isinstance(value, str):
        text = value.strip()
        if text[:1] in "[{":
            try:
                return extract_image_urls(json.loads(text), depth + 1)
            except ValueError:
                pass
        return [m.rstrip("\"',)]}") for m in IMAGE_URL_PATTERN.findall(text)]
    if isinstance(value, list):
        return [u for item in value for u in extract_image_urls(item, depth + 1)]
    if isinstance(value, dict):
        preferred = ("resultUrls", "result_urls", "images", "urls", "image_urls",
                     "output", "output_url", "image_url", "url", "result", "data")
        urls: list[str] = []
        for key in preferred:
            urls.extend(extract_image_urls(value.get(key), depth + 1))
        if not urls:  # fall back to scanning every value
            for item in value.values():
                urls.extend(extract_image_urls(item, depth + 1))
        return urls
    return []


def save_result(session: requests.Session, url: str, out_path: Path, args: argparse.Namespace) -> None:
    resp = request_with_retries(
        session, "GET", url, max_retries=args.max_retries, base_delay=args.retry_delay, timeout=120,
    )
    resp.raise_for_status()
    with Image.open(io.BytesIO(resp.content)) as result:
        result.save(out_path, format="PNG")


# --------------------------------------------------------------------------- #
# Main pipeline
# --------------------------------------------------------------------------- #
def process_batch(args: argparse.Namespace) -> int:
    api_key = os.getenv("NANO_BANANA_API_KEY", "").strip()
    if not api_key:
        log.error("NANO_BANANA_API_KEY is not set. Copy .env.example to .env and add your key.")
        return 1

    input_dir: Path = args.input
    output_dir: Path = args.output
    if not input_dir.is_dir():
        input_dir.mkdir(parents=True, exist_ok=True)
        log.error("Input folder '%s' was missing. Created it — add images and re-run.", input_dir)
        return 1
    output_dir.mkdir(parents=True, exist_ok=True)

    images = list(iter_input_images(input_dir))
    if not images:
        log.warning("No supported images found in '%s'.", input_dir)
        return 0

    # Separate sessions so the API key is never sent to the upload host / CDN.
    api = requests.Session()
    api.headers.update({
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    })
    public = requests.Session()

    index = next_output_index(output_dir)
    succeeded = failed = 0
    log.info("Found %d image(s). Model: %s | Endpoint: %s", len(images), args.model, API_BASE)

    for n, path in enumerate(images, start=1):
        log.info("[%d/%d] Processing %s", n, len(images), path.name)
        try:
            data = prepare_image(path, args.max_side)
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            log.warning("  Skipping unreadable/corrupt image %s: %s", path.name, exc)
            failed += 1
            continue

        try:
            image_url = upload_image(public, data, path.name, args)
            log.debug("  Reference image URL: %s", image_url)

            task_id = submit_edit_task(api, image_url, args)
            log.info("  Submitted task %s — polling...", task_id)

            response = poll_task(api, task_id, args)
            urls = list(dict.fromkeys(extract_image_urls(response)))  # de-dupe, keep order
            if not urls:
                raise TaskFailed(f"Task succeeded but no image URL found: {str(response)[:200]}")

            for url in urls:
                out_path = output_dir / f"{OUTPUT_PREFIX}{index:04d}.png"
                save_result(public, url, out_path, args)
                log.info("  Saved %s", out_path.name)
                index += 1
            succeeded += 1
        except FatalAPIError as exc:
            log.error("  %s — aborting batch.", exc)
            failed += len(images) - n + 1
            break
        except KeyboardInterrupt:
            log.warning("Interrupted by user.")
            break
        except Exception as exc:
            log.error("  Failed to process %s: %s", path.name, exc)
            failed += 1

        if args.delay > 0 and n < len(images):
            time.sleep(args.delay)  # gentle throttling between tasks

    log.info("Done. %d succeeded, %d failed/skipped. Output: %s",
             succeeded, failed, output_dir.resolve())
    return 0 if failed == 0 else 2


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Batch-convert photos into stylized AI avatars using the Nano Banana API.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("-i", "--input", type=Path, default=Path(os.getenv("INPUT_DIR", "input_images")),
                        help="Folder containing source images.")
    parser.add_argument("-o", "--output", type=Path, default=Path(os.getenv("OUTPUT_DIR", "output_avatars")),
                        help="Folder where avatars are saved.")
    parser.add_argument("--model", default=os.getenv("NANO_BANANA_MODEL", DEFAULT_MODEL),
                        choices=["nano", "nano2", "nano2pro"])
    parser.add_argument("--prompt", default=os.getenv("STYLE_PROMPT", STYLE_PROMPT))
    parser.add_argument("--aspect-ratio", default=os.getenv("ASPECT_RATIO", "1:1"))
    parser.add_argument("--resolution", default=os.getenv("RESOLUTION", "1K"), choices=["1K", "2K", "4K"])
    parser.add_argument("--max-side", type=int, default=env_int("MAX_IMAGE_SIDE", 1536),
                        help="Downscale inputs so the longest side is at most this many pixels.")
    parser.add_argument("--public-base-url", default=os.getenv("PUBLIC_IMAGE_BASE_URL", ""),
                        help="If your input images are already hosted, the base URL to them. "
                             "Skips the temporary upload step.")
    parser.add_argument("--upload-expiry", default=os.getenv("UPLOAD_EXPIRY", "1h"),
                        choices=["1h", "12h", "24h", "72h"],
                        help="How long temporary uploads stay online.")
    parser.add_argument("--poll-interval", type=float, default=env_float("POLL_INTERVAL", 3.0),
                        help="Seconds between status checks.")
    parser.add_argument("--poll-timeout", type=float, default=env_float("POLL_TIMEOUT", 300.0),
                        help="Max seconds to wait for a single task.")
    parser.add_argument("--max-retries", type=int, default=env_int("MAX_RETRIES", 5))
    parser.add_argument("--retry-delay", type=float, default=env_float("RETRY_BASE_DELAY", 5.0),
                        help="Base delay (seconds) for exponential backoff.")
    parser.add_argument("--delay", type=float, default=env_float("REQUEST_DELAY", 1.0),
                        help="Pause (seconds) between images to avoid rate limits.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%H:%M:%S",
    )
    return process_batch(args)


if __name__ == "__main__":
    sys.exit(main())
