#!/usr/bin/env python3
"""
Img2Img Batch Avatar Generator
==============================

Loops through every image in an input directory, sends it to a Stable Diffusion
Img2Img model hosted on Replicate, and saves the stylized results to an output
directory using sequential file names (avatar_0001.png, avatar_0002.png, ...).

Full tutorial:
    https://christopherlazok.com/blog/recreating-lensa-ai-stable-diffusion
"""

from __future__ import annotations

import argparse
import io
import logging
import os
import random
import re
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Iterable

from dotenv import load_dotenv
from PIL import Image, ImageOps, UnidentifiedImageError

try:
    import replicate
    from replicate.exceptions import ReplicateError
except ImportError:  # pragma: no cover
    sys.exit("The 'replicate' package is missing. Run: pip install -r requirements.txt")


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
load_dotenv()

DEFAULT_MODEL = (
    "stability-ai/sdxl:"
    "39ed52f2a78e934b3ba6e2a89f5b1c712de7dfea535525255b1aa35c5565e08b"
)

STYLE_PROMPT = (
    "highly detailed flat vector art illustration, pop art style, "
    "professional cartoon illustration, masterpiece, clean lines."
)

NEGATIVE_PROMPT = (
    "blurry, low quality, deformed, disfigured, extra limbs, bad anatomy, "
    "watermark, text, signature, jpeg artifacts, photorealistic"
)

SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
OUTPUT_PREFIX = "avatar_"
OUTPUT_PATTERN = re.compile(rf"^{OUTPUT_PREFIX}(\d+)\.png$", re.IGNORECASE)

log = logging.getLogger("batch_ai_avatars")


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
# Helpers
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


def prepare_image(path: Path, max_side: int) -> tuple[io.BytesIO, tuple[int, int]]:
    """
    Validate and normalize an image:
      * honours EXIF orientation
      * converts to RGB
      * downsizes so the longest side <= max_side (dims rounded to multiples of 8)
    Raises UnidentifiedImageError / OSError for unreadable or corrupt files.
    """
    with Image.open(path) as img:
        img.verify()  # cheap integrity check

    with Image.open(path) as img:
        img = ImageOps.exif_transpose(img).convert("RGB")
        scale = min(1.0, max_side / max(img.size))
        width = max(64, int(img.width * scale) // 8 * 8)
        height = max(64, int(img.height * scale) // 8 * 8)
        if (width, height) != img.size:
            img = img.resize((width, height), Image.LANCZOS)

        buffer = io.BytesIO()
        img.save(buffer, format="PNG")
        buffer.seek(0)
        buffer.name = f"{path.stem}.png"  # some HTTP clients use this for MIME type
        return buffer, (width, height)


def output_to_bytes(item: Any) -> bytes:
    """Convert a Replicate output item (FileOutput, URL string, or bytes) to bytes."""
    if isinstance(item, (bytes, bytearray)):
        return bytes(item)
    if hasattr(item, "read"):  # replicate>=1.0 FileOutput
        return item.read()
    url = str(item)
    if url.startswith(("http://", "https://")):
        with urllib.request.urlopen(url, timeout=120) as resp:
            return resp.read()
    raise ValueError(f"Unrecognized model output: {item!r}")


def is_rate_limit(exc: Exception) -> bool:
    status = getattr(exc, "status", None) or getattr(
        getattr(exc, "response", None), "status_code", None
    )
    return status == 429 or "rate limit" in str(exc).lower() or "429" in str(exc)


def run_with_retries(model: str, model_input: dict, max_retries: int, base_delay: float) -> Any:
    """Call replicate.run with exponential backoff on rate limits / transient errors."""
    for attempt in range(1, max_retries + 1):
        try:
            # Rewind the file handle in case a previous attempt consumed it.
            if hasattr(model_input.get("image"), "seek"):
                model_input["image"].seek(0)
            return replicate.run(model, input=model_input)
        except ReplicateError as exc:
            status = getattr(exc, "status", None)
            retryable = is_rate_limit(exc) or (status is not None and status >= 500)
            if not retryable or attempt == max_retries:
                raise
            reason = "Rate limited" if is_rate_limit(exc) else f"Server error ({status})"
        except (ConnectionError, TimeoutError, OSError) as exc:
            if attempt == max_retries:
                raise
            reason = f"Network error ({exc.__class__.__name__})"
        except Exception as exc:  # httpx errors etc.
            if not is_rate_limit(exc) or attempt == max_retries:
                raise
            reason = "Rate limited"

        delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0, 1)
        log.warning("%s. Retry %d/%d in %.1fs...", reason, attempt, max_retries - 1, delay)
        time.sleep(delay)

    raise RuntimeError("Unreachable")  # pragma: no cover


# --------------------------------------------------------------------------- #
# Main pipeline
# --------------------------------------------------------------------------- #
def process_batch(args: argparse.Namespace) -> int:
    input_dir: Path = args.input
    output_dir: Path = args.output

    if not os.getenv("REPLICATE_API_TOKEN"):
        log.error("REPLICATE_API_TOKEN is not set. Copy .env.example to .env and add your token.")
        return 1
    if not input_dir.is_dir():
        input_dir.mkdir(parents=True, exist_ok=True)
        log.error("Input folder '%s' was empty/missing. Created it — add images and re-run.", input_dir)
        return 1
    output_dir.mkdir(parents=True, exist_ok=True)

    images = list(iter_input_images(input_dir))
    if not images:
        log.warning("No supported images found in '%s'.", input_dir)
        return 0

    index = next_output_index(output_dir)
    succeeded, failed = 0, 0
    log.info("Found %d image(s). Model: %s", len(images), args.model)

    for n, path in enumerate(images, start=1):
        log.info("[%d/%d] Processing %s", n, len(images), path.name)

        try:
            image_buffer, (width, height) = prepare_image(path, args.max_side)
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            log.warning("  Skipping unreadable/corrupt image %s: %s", path.name, exc)
            failed += 1
            continue

        model_input = {
            "image": image_buffer,
            "prompt": args.prompt,
            "negative_prompt": args.negative_prompt,
            "prompt_strength": args.prompt_strength,
            "guidance_scale": args.guidance_scale,
            "num_inference_steps": args.steps,
            "num_outputs": args.num_outputs,
            "width": width,
            "height": height,
        }
        if args.seed is not None:
            model_input["seed"] = args.seed

        try:
            output = run_with_retries(args.model, model_input, args.max_retries, args.retry_delay)
            items = output if isinstance(output, (list, tuple)) else [output]
            if not items:
                raise ValueError("Model returned no output.")

            for item in items:
                data = output_to_bytes(item)
                out_path = output_dir / f"{OUTPUT_PREFIX}{index:04d}.png"
                with Image.open(io.BytesIO(data)) as result:
                    result.save(out_path, format="PNG")
                log.info("  Saved %s", out_path.name)
                index += 1
            succeeded += 1
        except KeyboardInterrupt:
            log.warning("Interrupted by user.")
            break
        except Exception as exc:
            log.error("  Failed to process %s: %s", path.name, exc)
            failed += 1
        finally:
            image_buffer.close()

        if args.delay > 0 and n < len(images):
            time.sleep(args.delay)  # gentle throttling between requests

    log.info("Done. %d succeeded, %d failed/skipped. Output: %s", succeeded, failed, output_dir.resolve())
    return 0 if failed == 0 else 2


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Batch-convert photos into stylized AI avatars using Stable Diffusion Img2Img.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("-i", "--input", type=Path, default=Path(os.getenv("INPUT_DIR", "input_images")),
                        help="Folder containing source images.")
    parser.add_argument("-o", "--output", type=Path, default=Path(os.getenv("OUTPUT_DIR", "output_avatars")),
                        help="Folder where avatars are saved.")
    parser.add_argument("--model", default=os.getenv("REPLICATE_MODEL", DEFAULT_MODEL),
                        help="Replicate model identifier (owner/name:version).")
    parser.add_argument("--prompt", default=os.getenv("STYLE_PROMPT", STYLE_PROMPT))
    parser.add_argument("--negative-prompt", default=os.getenv("NEGATIVE_PROMPT", NEGATIVE_PROMPT))
    parser.add_argument("--prompt-strength", type=float, default=env_float("PROMPT_STRENGTH", 0.6),
                        help="0.0 keeps the original image, 1.0 ignores it entirely.")
    parser.add_argument("--guidance-scale", type=float, default=env_float("GUIDANCE_SCALE", 7.5))
    parser.add_argument("--steps", type=int, default=env_int("NUM_INFERENCE_STEPS", 30))
    parser.add_argument("--num-outputs", type=int, default=env_int("NUM_OUTPUTS", 1),
                        help="Avatars to generate per input image.")
    parser.add_argument("--seed", type=int, default=None, help="Fixed seed for reproducible results.")
    parser.add_argument("--max-side", type=int, default=env_int("MAX_IMAGE_SIDE", 1024),
                        help="Downscale inputs so the longest side is at most this many pixels.")
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
