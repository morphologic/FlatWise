import base64
import json
import mimetypes
import shlex
import subprocess
import tempfile
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import fitz
import httpx
from fastapi import HTTPException, UploadFile
from PIL import Image, ImageFilter, ImageOps, ImageStat
from pypdf import PdfReader

from config import settings
from flatwise.constants import CUBICASA5K_SCHEMA, HTTPX_VERIFY
from flatwise.text import clean_text, floor_plan_score


async def read_uploaded_file(upload: UploadFile | None) -> dict[str, Any] | None:
    if not upload:
        return None

    content = await upload.read()
    if len(content) > settings.max_upload_bytes:
        raise HTTPException(status_code=413, detail="Plik z rzutem jest za duży.")

    content_type = upload.content_type or "application/octet-stream"
    result: dict[str, Any] = {
        "filename": upload.filename,
        "content_type": content_type,
        "size_bytes": len(content),
    }

    if content_type.startswith("image/"):
        image = prepare_uploaded_plan_image(content)
        encoded = base64.b64encode(image["data"]).decode("ascii")
        result["image_data_url"] = f"data:{content_type};base64,{base64.b64encode(content).decode('ascii')}"
        result["orientation_image_data_url"] = result["image_data_url"]
        result["orientation_image_source"] = "uploaded_original_image"
        result["floor_plan_image_data_url"] = f"data:{image['mime_type']};base64,{encoded}"
        result["floor_plan_image_source"] = image["source"]
        result["floor_plan_image_name"] = image["name"]
        result["floor_plan_image_size_bytes"] = len(image["data"])
        result["floor_plan_analysis"] = analyze_floor_plan_geometry(image["data"])
    elif content_type == "application/pdf" or (upload.filename or "").lower().endswith(".pdf"):
        pdf_result = extract_pdf_content(content)
        result.update(pdf_result)
    else:
        raise HTTPException(
            status_code=400,
            detail="Dodaj rzut jako PNG, JPG, WebP albo PDF.",
        )

    return result


async def find_linked_floor_plan(listing: dict[str, Any]) -> dict[str, Any] | None:
    candidates = [image for image in listing.get("images", []) if image.get("url")]
    for image in candidates[:12]:
        candidate_score = int(image.get("score") or 0)
        if image.get("source") == "playwright_gallery_scan" and candidate_score <= 0:
            continue
        linked = await fetch_floor_plan_from_url(image["url"], image.get("alt") or "")
        if linked and is_likely_linked_floor_plan(linked, candidate_score):
            return linked
    return None


async def fetch_floor_plan_from_url(url: str, alt: str = "") -> dict[str, Any] | None:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ),
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        "Referer": url,
    }
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=settings.request_timeout_seconds,
            headers=headers,
            verify=HTTPX_VERIFY,
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
    except httpx.HTTPError:
        return None

    content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
    if not content_type.startswith("image/"):
        return None
    if len(response.content) > settings.max_upload_bytes:
        return None

    try:
        image = prepare_uploaded_plan_image(response.content)
    except HTTPException:
        return None

    encoded = base64.b64encode(image["data"]).decode("ascii")
    source_encoded = base64.b64encode(response.content).decode("ascii")
    filename = Path(urlparse(str(response.url)).path).name or image["name"]
    return {
        "filename": filename,
        "content_type": content_type,
        "size_bytes": len(response.content),
        "linked_image_url": str(response.url),
        "linked_image_alt": alt,
        "image_data_url": f"data:{content_type};base64,{source_encoded}",
        "orientation_image_data_url": f"data:{content_type};base64,{source_encoded}",
        "orientation_image_source": "listing_original_image",
        "floor_plan_image_data_url": f"data:{image['mime_type']};base64,{encoded}",
        "floor_plan_image_source": "listing_image_crop",
        "floor_plan_image_name": image["name"],
        "floor_plan_image_size_bytes": len(image["data"]),
        "floor_plan_analysis": analyze_floor_plan_geometry(image["data"]),
    }


def summarize_linked_floor_plan(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if not value:
        return None
    return {
        "filename": value.get("filename"),
        "content_type": value.get("content_type"),
        "size_bytes": value.get("size_bytes"),
        "linked_image_url": value.get("linked_image_url"),
        "linked_image_alt": value.get("linked_image_alt"),
        "floor_plan_image_source": value.get("floor_plan_image_source"),
        "floor_plan_analysis": value.get("floor_plan_analysis"),
    }


def is_likely_linked_floor_plan(value: dict[str, Any], text_score: int = 0) -> bool:
    analysis = value.get("floor_plan_analysis") or {}
    if analysis.get("status") != "ready":
        return False

    ink_ratio = float(analysis.get("ink_ratio") or 0)
    rooms = int(analysis.get("estimated_room_like_spaces") or 0)
    walls = analysis.get("wall_run_summary") or {}
    horizontal_runs = int(walls.get("horizontal_long_runs") or 0)
    vertical_runs = int(walls.get("vertical_long_runs") or 0)
    visual_profile = analysis.get("visual_profile") or {}
    if visual_profile.get("photo_like"):
        return False

    light_background_ratio = float(visual_profile.get("light_background_ratio") or 0)
    white_background_ratio = float(visual_profile.get("white_background_ratio") or 0)
    has_plan_background = light_background_ratio >= 0.18 or white_background_ratio >= 0.08

    if text_score >= 2:
        return has_plan_background and 0.015 <= ink_ratio <= 0.7

    return (
        has_plan_background
        and 0.02 <= ink_ratio <= 0.5
        and rooms >= 1
        and horizontal_runs >= 4
        and vertical_runs >= 4
    )


def prepare_uploaded_plan_image(content: bytes) -> dict[str, Any]:
    try:
        image = Image.open(BytesIO(content))
        width, height = image.size
        if width * height > settings.max_image_pixels:
            raise HTTPException(
                status_code=413,
                detail="Obraz rzutu ma zbyt duza rozdzielczosc.",
            )
        image = ImageOps.exif_transpose(image).convert("RGB")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Nie udało się odczytać przesłanego obrazu rzutu.") from exc

    cropped = crop_architectural_drawing(image)
    normalized = normalize_plan_image(cropped)
    return {
        "data": image_to_png_bytes(normalized),
        "mime_type": "image/png",
        "name": "uploaded-plan-crop.png",
        "source": "uploaded_image_crop",
    }


def extract_pdf_content(content: bytes) -> dict[str, Any]:
    try:
        reader = PdfReader(BytesIO(content))
    except Exception:
        return {"pdf_text": "", "floor_plan_image_error": "Nie udało się odczytać PDF."}

    pages = []
    for page in reader.pages[: settings.max_pdf_pages]:
        pages.append(page.extract_text() or "")

    result: dict[str, Any] = {"pdf_text": clean_text("\n".join(pages))[:8000]}
    image = render_likely_floor_plan_page(content)

    if image:
        encoded = base64.b64encode(image["data"]).decode("ascii")
        result.update(
            {
                "floor_plan_image_data_url": f"data:{image['mime_type']};base64,{encoded}",
                "orientation_image_data_url": f"data:{image.get('sheet_mime_type', image['mime_type'])};base64,{base64.b64encode(image.get('sheet_data', image['data'])).decode('ascii')}",
                "orientation_image_source": image.get("sheet_source", image["source"]),
                "floor_plan_image_source": image["source"],
                "floor_plan_image_name": image["name"],
                "floor_plan_image_page": image["page"],
                "floor_plan_image_size_bytes": len(image["data"]),
                "floor_plan_analysis": analyze_floor_plan_geometry(image["data"]),
            }
        )
    else:
        result["floor_plan_image_error"] = "Nie udało się wyrenderować i przyciąć grafiki rzutu z PDF."

    return result


def extract_pdf_text(content: bytes) -> str:
    try:
        reader = PdfReader(BytesIO(content))
        pages = []
        for page in reader.pages[: settings.max_pdf_pages]:
            pages.append(page.extract_text() or "")
        return clean_text("\n".join(pages))[:8000]
    except Exception:
        return ""


def extract_likely_floor_plan_image(reader: PdfReader) -> dict[str, Any] | None:
    best: dict[str, Any] | None = None
    for page_index, page in enumerate(reader.pages[: settings.max_pdf_pages], start=1):
        try:
            page_images = list(getattr(page, "images", []) or [])
        except Exception:
            continue

        for image_index, image in enumerate(page_images, start=1):
            data = getattr(image, "data", b"") or b""
            if len(data) < 8000:
                continue

            name = getattr(image, "name", "") or f"page-{page_index}-image-{image_index}"
            mime_type = image_mime_type(name)
            score = len(data) + floor_plan_score(name, "") * 100000
            candidate = {
                "data": data,
                "mime_type": mime_type,
                "name": name,
                "page": page_index,
                "score": score,
                "source": "pdf_embedded_image",
            }
            if not best or candidate["score"] > best["score"]:
                best = candidate

    return best


def render_likely_floor_plan_page(content: bytes) -> dict[str, Any] | None:
    try:
        document = fitz.open(stream=content, filetype="pdf")
    except Exception:
        return None

    best: dict[str, Any] | None = None
    for page_index in range(min(document.page_count, settings.max_pdf_pages)):
        page = document.load_page(page_index)
        text = clean_text(page.get_text("text") or "")
        pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
        rendered = Image.open(BytesIO(pixmap.tobytes("png"))).convert("RGB")
        cropped = crop_architectural_drawing(rendered)
        normalized = normalize_plan_image(cropped)
        data = image_to_png_bytes(normalized)
        sheet = normalize_plan_image(rendered.copy())
        sheet_data = image_to_png_bytes(sheet)

        crop_area = cropped.width * cropped.height
        page_area = rendered.width * rendered.height
        score = (
            floor_plan_score("", text) * 1_000_000
            + len(page.get_images(full=True)) * 100_000
            + int((crop_area / max(page_area, 1)) * 100_000)
            + crop_area
        )
        candidate = {
            "data": data,
            "sheet_data": sheet_data,
            "mime_type": "image/png",
            "sheet_mime_type": "image/png",
            "name": f"architectural-drawing-page-{page_index + 1}.png",
            "page": page_index + 1,
            "score": score,
            "source": "pdf_rendered_cropped_page",
            "sheet_source": "pdf_rendered_full_page",
        }
        if not best or candidate["score"] > best["score"]:
            best = candidate

    document.close()
    return best


def crop_architectural_drawing(image: Image.Image) -> Image.Image:
    image = image.convert("RGB")
    detection = image.copy()
    detection.thumbnail((900, 900), Image.Resampling.LANCZOS)
    grayscale = ImageOps.grayscale(detection)
    threshold = estimate_plan_threshold(grayscale)
    mask = grayscale.point(lambda pixel: 255 if pixel < threshold else 0, mode="L")
    mask = mask.filter(ImageFilter.MaxFilter(11))
    component_bbox = largest_mask_component_bbox(mask)
    bbox = component_bbox or mask.getbbox()
    if not bbox:
        return normalize_plan_image(image)

    scale_x = image.width / detection.width
    scale_y = image.height / detection.height
    left, top, right, bottom = bbox
    mapped = (
        int(left * scale_x),
        int(top * scale_y),
        int(right * scale_x),
        int(bottom * scale_y),
    )
    return image.crop(expand_bbox(mapped, image.size, 0.04))


def estimate_plan_threshold(grayscale: Image.Image) -> int:
    histogram = grayscale.histogram()
    total = max(sum(histogram), 1)
    cumulative = 0
    percentile_88 = 245
    for value, count in enumerate(histogram):
        cumulative += count
        if cumulative / total >= 0.88:
            percentile_88 = value
            break
    return max(170, min(245, percentile_88 - 8))


def largest_mask_component_bbox(mask: Image.Image) -> tuple[int, int, int, int] | None:
    width, height = mask.size
    data = mask.load()
    visited: set[tuple[int, int]] = set()
    best_bbox: tuple[int, int, int, int] | None = None
    best_area = 0

    for y in range(height):
        for x in range(width):
            if data[x, y] == 0 or (x, y) in visited:
                continue

            stack = [(x, y)]
            visited.add((x, y))
            min_x = max_x = x
            min_y = max_y = y
            count = 0

            while stack:
                current_x, current_y = stack.pop()
                count += 1
                min_x = min(min_x, current_x)
                max_x = max(max_x, current_x)
                min_y = min(min_y, current_y)
                max_y = max(max_y, current_y)

                for next_x, next_y in (
                    (current_x + 1, current_y),
                    (current_x - 1, current_y),
                    (current_x, current_y + 1),
                    (current_x, current_y - 1),
                ):
                    if (
                        next_x < 0
                        or next_y < 0
                        or next_x >= width
                        or next_y >= height
                        or (next_x, next_y) in visited
                        or data[next_x, next_y] == 0
                    ):
                        continue
                    visited.add((next_x, next_y))
                    stack.append((next_x, next_y))

            bbox_area = (max_x - min_x + 1) * (max_y - min_y + 1)
            if count > 120 and bbox_area > best_area:
                best_area = bbox_area
                best_bbox = (min_x, min_y, max_x + 1, max_y + 1)

    if not best_bbox:
        return None
    if best_area < width * height * 0.04:
        return None
    return best_bbox


def expand_bbox(
    bbox: tuple[int, int, int, int],
    image_size: tuple[int, int],
    margin_ratio: float,
) -> tuple[int, int, int, int]:
    left, top, right, bottom = bbox
    width, height = image_size
    margin = int(max(right - left, bottom - top) * margin_ratio)
    return (
        max(0, left - margin),
        max(0, top - margin),
        min(width, right + margin),
        min(height, bottom + margin),
    )


def normalize_plan_image(image: Image.Image) -> Image.Image:
    image = image.convert("RGB")
    max_side = 1800
    if max(image.size) > max_side:
        image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    return image


def image_to_png_bytes(image: Image.Image) -> bytes:
    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


def analyze_floor_plan_geometry(content: bytes) -> dict[str, Any]:
    try:
        image = Image.open(BytesIO(content)).convert("RGB")
    except Exception:
        return {
            "status": "error",
            "method": "local_floor_plan_geometry_v1",
            "summary": "Could not decode the normalized floor-plan image.",
        }

    original_width, original_height = image.size
    analysis_image = image.copy()
    analysis_image.thumbnail((900, 900), Image.Resampling.LANCZOS)
    grayscale = ImageOps.grayscale(analysis_image)
    visual_profile = build_floor_plan_visual_profile(analysis_image, grayscale)
    threshold = estimate_plan_threshold(grayscale)
    ink_mask = grayscale.point(lambda pixel: 255 if pixel < threshold else 0, mode="L")
    ink_mask = ink_mask.filter(ImageFilter.MaxFilter(5))
    bbox = ink_mask.getbbox() or (0, 0, grayscale.width, grayscale.height)
    bbox_area = max((bbox[2] - bbox[0]) * (bbox[3] - bbox[1]), 1)
    ink_pixels = count_mask_pixels(ink_mask, bbox)
    room_candidates = find_room_like_components(ink_mask, bbox)
    room_count = len(room_candidates)
    elongated_count = sum(1 for component in room_candidates if component["classification"] == "elongated_space")
    line_summary = summarize_wall_runs(ink_mask, bbox)
    cubicasa = analyze_floor_plan_with_cubicasa5k(content)

    return {
        "status": "ready",
        "method": "local_floor_plan_geometry_v1",
        "source": "heuristic_preprocessor_with_optional_cubicasa5k_adapter",
        "image": {
            "width": original_width,
            "height": original_height,
            "analysis_width": grayscale.width,
            "analysis_height": grayscale.height,
        },
        "visual_profile": visual_profile,
        "drawing_bbox": normalize_bbox(bbox, grayscale.size),
        "ink_ratio": round(ink_pixels / bbox_area, 4),
        "estimated_room_like_spaces": room_count,
        "elongated_space_count": elongated_count,
        "wall_run_summary": line_summary,
        "room_candidates": room_candidates[:12],
        "cubicasa5k": cubicasa,
        "interpretation_notes": [
            "Use as weak structured evidence, not as measured architectural truth.",
            "Room candidates are enclosed or mostly enclosed light regions after thickening dark plan lines.",
            "Door gaps, furniture, labels, low-contrast scans, and decorative plan graphics can merge or split rooms.",
            "Wall types and legal usable area still require the developer plan or manual verification.",
            "CubiCasa5K detects visible floor-plan semantics; structural/load-bearing status and wall materials remain inferred or unknown.",
        ],
    }


def build_floor_plan_visual_profile(image: Image.Image, grayscale: Image.Image) -> dict[str, Any]:
    total_pixels = max(grayscale.width * grayscale.height, 1)
    luminance_values = list(grayscale.getdata())
    white_background_ratio = sum(1 for value in luminance_values if value >= 245) / total_pixels
    light_background_ratio = sum(1 for value in luminance_values if value >= 230) / total_pixels
    midtone_ratio = sum(1 for value in luminance_values if 90 < value < 220) / total_pixels
    dark_ratio = sum(1 for value in luminance_values if value <= 80) / total_pixels
    saturation_mean = ImageStat.Stat(image.convert("HSV").getchannel("S")).mean[0] / 255
    luminance_stat = ImageStat.Stat(grayscale)
    luminance_std = (luminance_stat.var[0] ** 0.5) / 255
    small = image.copy()
    small.thumbnail((120, 120), Image.Resampling.LANCZOS)
    quantized = small.quantize(colors=64)
    color_bucket_count = len(quantized.getcolors(maxcolors=100_000) or [])
    photo_like = (
        light_background_ratio < 0.12
        and white_background_ratio < 0.04
        and midtone_ratio > 0.45
        and color_bucket_count >= 48
    )
    return {
        "white_background_ratio": round(white_background_ratio, 4),
        "light_background_ratio": round(light_background_ratio, 4),
        "midtone_ratio": round(midtone_ratio, 4),
        "dark_ratio": round(dark_ratio, 4),
        "saturation_mean": round(saturation_mean, 4),
        "luminance_std": round(luminance_std, 4),
        "color_bucket_count": color_bucket_count,
        "photo_like": photo_like,
    }


def analyze_floor_plan_with_cubicasa5k(content: bytes) -> dict[str, Any]:
    command = (settings.cubicasa5k_command or "").strip()
    if not command:
        return {
            "status": "not_configured",
            "method": "cubicasa5k_adapter_v1",
            "schema": CUBICASA5K_SCHEMA,
            "summary": (
                "Set CUBICASA5K_COMMAND to a local parser command that accepts an image path "
                "and prints CubiCasa-style JSON."
            ),
        }

    try:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp.write(content)
            tmp_path = Path(tmp.name)
        try:
            completed = subprocess.run(
                [*shlex.split(command), str(tmp_path)],
                capture_output=True,
                text=True,
                timeout=90,
                check=False,
            )
        finally:
            tmp_path.unlink(missing_ok=True)
    except Exception as exc:
        return {
            "status": "error",
            "method": "cubicasa5k_adapter_v1",
            "schema": CUBICASA5K_SCHEMA,
            "summary": f"CubiCasa5K adapter failed before parsing output: {exc}",
        }

    if completed.returncode != 0:
        return {
            "status": "error",
            "method": "cubicasa5k_adapter_v1",
            "schema": CUBICASA5K_SCHEMA,
            "summary": f"CubiCasa5K command exited with {completed.returncode}.",
            "stderr": clean_text(completed.stderr)[:800],
        }

    try:
        parsed = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return {
            "status": "error",
            "method": "cubicasa5k_adapter_v1",
            "schema": CUBICASA5K_SCHEMA,
            "summary": "CubiCasa5K command did not return JSON.",
            "stdout_preview": clean_text(completed.stdout)[:800],
        }

    if not isinstance(parsed, dict):
        return {
            "status": "error",
            "method": "cubicasa5k_adapter_v1",
            "schema": CUBICASA5K_SCHEMA,
            "summary": "CubiCasa5K command returned JSON, but not an object.",
        }

    return normalize_cubicasa5k_output(parsed)


def normalize_cubicasa5k_output(parsed: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": parsed.get("status", "ready"),
        "method": "cubicasa5k_adapter_v1",
        "schema": CUBICASA5K_SCHEMA,
        "walls": ensure_list(parsed.get("walls")),
        "doors": ensure_list(parsed.get("doors")),
        "windows": ensure_list(parsed.get("windows")),
        "rooms": ensure_list(parsed.get("rooms")),
        "fixtures": ensure_list(parsed.get("fixtures") or parsed.get("icons")),
        "raw_counts": {
            "walls": len(ensure_list(parsed.get("walls"))),
            "doors": len(ensure_list(parsed.get("doors"))),
            "windows": len(ensure_list(parsed.get("windows"))),
            "rooms": len(ensure_list(parsed.get("rooms"))),
            "fixtures": len(ensure_list(parsed.get("fixtures") or parsed.get("icons"))),
        },
        "inference_limits": [
            "Load-bearing walls are not directly known from CubiCasa5K labels.",
            "Plumbing stacks and shafts are inferred only when fixtures/service cores make that plausible.",
        ],
    }


def ensure_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if value is None:
        return []
    return [value]


def count_mask_pixels(mask: Image.Image, bbox: tuple[int, int, int, int]) -> int:
    pixels = mask.load()
    left, top, right, bottom = bbox
    count = 0
    for y in range(top, bottom):
        for x in range(left, right):
            if pixels[x, y]:
                count += 1
    return count


def normalize_bbox(
    bbox: tuple[int, int, int, int],
    image_size: tuple[int, int],
) -> dict[str, float]:
    width, height = image_size
    left, top, right, bottom = bbox
    return {
        "x": round(left / max(width, 1), 4),
        "y": round(top / max(height, 1), 4),
        "width": round((right - left) / max(width, 1), 4),
        "height": round((bottom - top) / max(height, 1), 4),
    }


def find_room_like_components(
    ink_mask: Image.Image,
    bbox: tuple[int, int, int, int],
) -> list[dict[str, Any]]:
    width, height = ink_mask.size
    left, top, right, bottom = bbox
    bbox_width = max(right - left, 1)
    bbox_height = max(bottom - top, 1)
    bbox_area = bbox_width * bbox_height
    min_area = max(80, int(bbox_area * 0.006))
    max_area = int(bbox_area * 0.72)
    data = ink_mask.tobytes()
    visited = bytearray(width * height)
    components: list[dict[str, Any]] = []

    for y in range(top, bottom):
        row_offset = y * width
        for x in range(left, right):
            index = row_offset + x
            if visited[index] or data[index] != 0:
                continue

            component = flood_light_component(data, visited, width, height, index, bbox)
            if not component:
                continue
            if component["touches_bbox"]:
                continue
            if component["area"] < min_area or component["area"] > max_area:
                continue

            component_width = max(component["right"] - component["left"] + 1, 1)
            component_height = max(component["bottom"] - component["top"] + 1, 1)
            if component_width < 12 or component_height < 12:
                continue

            aspect_ratio = component_width / component_height
            classification = "elongated_space" if aspect_ratio >= 3.0 or aspect_ratio <= 0.33 else "room_like_space"
            components.append(
                {
                    "bbox": normalize_bbox(
                        (
                            component["left"],
                            component["top"],
                            component["right"] + 1,
                            component["bottom"] + 1,
                        ),
                        ink_mask.size,
                    ),
                    "area_ratio_of_drawing": round(component["area"] / bbox_area, 4),
                    "aspect_ratio": round(aspect_ratio, 2),
                    "classification": classification,
                }
            )

    components.sort(key=lambda item: item["area_ratio_of_drawing"], reverse=True)
    return components


def flood_light_component(
    data: bytes,
    visited: bytearray,
    width: int,
    height: int,
    start_index: int,
    bbox: tuple[int, int, int, int],
) -> dict[str, Any] | None:
    left, top, right, bottom = bbox
    stack = [start_index]
    visited[start_index] = 1
    min_x = max_x = start_index % width
    min_y = max_y = start_index // width
    area = 0
    touches_bbox = False

    while stack:
        index = stack.pop()
        x = index % width
        y = index // width
        area += 1
        min_x = min(min_x, x)
        max_x = max(max_x, x)
        min_y = min(min_y, y)
        max_y = max(max_y, y)
        if x <= left + 1 or x >= right - 2 or y <= top + 1 or y >= bottom - 2:
            touches_bbox = True

        neighbors = []
        if x > left:
            neighbors.append(index - 1)
        if x < right - 1:
            neighbors.append(index + 1)
        if y > top:
            neighbors.append(index - width)
        if y < bottom - 1:
            neighbors.append(index + width)

        for next_index in neighbors:
            if visited[next_index] or data[next_index] != 0:
                continue
            visited[next_index] = 1
            stack.append(next_index)

    return {
        "area": area,
        "left": min_x,
        "top": min_y,
        "right": max_x,
        "bottom": max_y,
        "touches_bbox": touches_bbox,
    }


def summarize_wall_runs(mask: Image.Image, bbox: tuple[int, int, int, int]) -> dict[str, Any]:
    left, top, right, bottom = bbox
    pixels = mask.load()
    horizontal = collect_dark_runs(pixels, left, top, right, bottom, axis="horizontal")
    vertical = collect_dark_runs(pixels, left, top, right, bottom, axis="vertical")
    return {
        "horizontal_long_runs": len(horizontal),
        "vertical_long_runs": len(vertical),
        "longest_horizontal_ratio": round(max(horizontal, default=0) / max(right - left, 1), 3),
        "longest_vertical_ratio": round(max(vertical, default=0) / max(bottom - top, 1), 3),
    }


def collect_dark_runs(
    pixels: Any,
    left: int,
    top: int,
    right: int,
    bottom: int,
    axis: str,
) -> list[int]:
    lengths: list[int] = []
    if axis == "horizontal":
        minimum = max(20, int((right - left) * 0.12))
        for y in range(top, bottom):
            run = 0
            for x in range(left, right):
                if pixels[x, y]:
                    run += 1
                elif run:
                    if run >= minimum:
                        lengths.append(run)
                    run = 0
            if run >= minimum:
                lengths.append(run)
        return lengths

    minimum = max(20, int((bottom - top) * 0.12))
    for x in range(left, right):
        run = 0
        for y in range(top, bottom):
            if pixels[x, y]:
                run += 1
            elif run:
                if run >= minimum:
                    lengths.append(run)
                run = 0
        if run >= minimum:
            lengths.append(run)
    return lengths


def image_mime_type(filename: str) -> str:
    guessed, _ = mimetypes.guess_type(filename)
    if guessed and guessed.startswith("image/"):
        return guessed
    return "image/png"
