import base64
import json
import mimetypes
import re
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
import fitz
from bs4 import BeautifulSoup
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageDraw, ImageFilter, ImageOps
from pyproj import Transformer
from pypdf import PdfReader

from config import settings


BASE_DIR = Path(__file__).resolve().parent
ONBOARDING_SPEC_PATH = BASE_DIR / "kart.json"
NANOBANANA_PROMPT = (
    "Create a 3d interior visualization from this architectural floor plan. "
    "Show the apartment interior as an isometric cutaway or top-down 3d view, "
    "with rooms, walls, furniture, finishes, and natural light visible. "
    "Do not create an exterior building facade or outside view."
)
PRICE_METRICS_GUIDE = """
Uwzględnij metryki podobne do hedonicznych modeli wyceny i analiz FPRE:
- Cechy lokalu: powierzchnia użytkowa, cena za m², liczba pokoi, układ, piętro, ekspozycja, balkon/taras/ogród, łazienki, komórka, parking, winda, standard wykończenia.
- Cechy budynku: rok budowy, stan/standard budynku, liczba kondygnacji, garaż, efektywność energetyczna, koszty administracyjne, etap inwestycji i ryzyko deweloperskie.
- Mikrolokalizacja: hałas, zieleń, widok, nasłonecznienie, sąsiedztwo, bezpieczeństwo, usługi pieszo, szkoły/przedszkola, transport publiczny, jakość najbliższego otoczenia.
- Makrolokalizacja: dzielnica/miasto, dostępność do centrum i miejsc pracy, infrastruktura, płynność rynku, potencjał wzrostu wartości i odsprzedaży.
- Dane brakujące: każde pole wpływające na cenę lub ryzyko, którego nie da się zweryfikować z oferty/rzutu, oznacz jako brak danych zamiast zgadywać.
""".strip()
GEOPORTAL_SERVICES = {
    "uug": "https://services.gugik.gov.pl/uug/",
    "uldk": "https://uldk.gugik.gov.pl/",
    "kimp": "https://mapy.geoportal.gov.pl/wss/ext/KrajowaIntegracjaMiejscowychPlanowZagospodarowaniaPrzestrzennego",
    "kiskzp": "https://mapy.geoportal.gov.pl/wss/ext/KrajowaIntegracjaStudiumKierunkowZagospodarowaniaPrzestrzennego",
    "egib": "https://integracja.gugik.gov.pl/cgi-bin/KrajowaIntegracjaEwidencjiGruntow",
    "gesut": "https://integracja.gugik.gov.pl/cgi-bin/KrajowaIntegracjaUzbrojeniaTerenu",
    "bdot": "https://integracja.gugik.gov.pl/cgi-bin/KrajowaIntegracjaBazDanychObiektowTopograficznych",
    "orto": "https://mapy.geoportal.gov.pl/wss/service/PZGIK/ORTO/WMS/StandardResolution",
}
EPSG2180_TO_WGS84 = Transformer.from_crs("EPSG:2180", "EPSG:4326", always_xy=True)
WGS84_TO_EPSG2180 = Transformer.from_crs("EPSG:4326", "EPSG:2180", always_xy=True)

app = FastAPI(title="FlatWise MVP")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def home() -> FileResponse:
    return FileResponse("static/index.html")


@app.get("/api/onboarding-spec")
def onboarding_spec() -> dict[str, Any]:
    try:
        return json.loads(ONBOARDING_SPEC_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=500, detail="Nie udało się wczytać konfiguracji onboardingu.") from exc


@app.post("/api/analyze")
async def analyze_flat(
    url: str = Form(...),
    notes: str = Form(""),
    floor_plan: UploadFile | None = File(None),
) -> dict[str, Any]:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(status_code=400, detail="Podaj poprawny adres URL zaczynający się od http(s).")

    openrouter_api_key = sanitize_openrouter_api_key(settings.openrouter_api_key)
    if not openrouter_api_key:
        raise HTTPException(
            status_code=400,
            detail="Brakuje OPENROUTER_API_KEY. Dodaj klucz do .env i uruchom serwer ponownie.",
        )
    if openrouter_api_key in {"sk-your-key", "sk-or-your-key"} or len(openrouter_api_key) < 32:
        raise HTTPException(
            status_code=400,
            detail="OPENROUTER_API_KEY wygląda jak placeholder albo jest za krótki. Wklej prawdziwy klucz do .env i uruchom serwer ponownie.",
        )

    listing = await extract_listing(url)
    uploaded_file = await read_uploaded_file(floor_plan)
    visualization = await create_floor_plan_visualization(uploaded_file)
    geo_context = await create_geo_context(listing, notes, openrouter_api_key)

    report = await create_openrouter_report(url, listing, notes, uploaded_file, geo_context, openrouter_api_key)
    return {"listing": listing, "report": report, "visualization": visualization, "geo_context": geo_context}


async def extract_listing(url: str) -> dict[str, Any]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ),
        "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.8",
    }

    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=settings.request_timeout_seconds,
            headers=headers,
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=422, detail=f"Nie udało się pobrać oferty: {exc}") from exc

    soup = BeautifulSoup(response.text, "html.parser")
    structured_addresses = extract_structured_addresses(soup)
    for tag in soup(["script", "style", "noscript", "svg", "iframe", "header", "footer", "nav"]):
        tag.decompose()

    title = clean_text(soup.title.get_text(" ", strip=True) if soup.title else "")
    description = ""
    meta_description = soup.find("meta", attrs={"name": "description"})
    if meta_description and meta_description.get("content"):
        description = clean_text(meta_description["content"])

    text_parts: list[str] = []
    for selector in ["h1", "h2", "h3", "p", "li", "td", "th"]:
        for node in soup.select(selector):
            value = clean_text(node.get_text(" ", strip=True))
            if value and len(value) > 2:
                text_parts.append(value)

    deduped_text = dedupe_preserve_order(text_parts)
    page_text = "\n".join(deduped_text)
    page_text = page_text[:18000]

    images = []
    for image in soup.find_all("img"):
        src = image.get("src") or image.get("data-src") or image.get("data-lazy-src")
        if not src:
            continue
        image_url = urljoin(url, src)
        alt = clean_text(image.get("alt") or "")
        images.append({"url": image_url, "alt": alt, "score": floor_plan_score(image_url, alt)})

    images.sort(key=lambda item: item["score"], reverse=True)

    return {
        "url": url,
        "title": title,
        "description": description,
        "text": page_text,
        "images": images[:18],
        "structured_addresses": structured_addresses,
        "extraction_quality": assess_extraction_quality(page_text, images),
    }


def extract_structured_addresses(soup: BeautifulSoup) -> list[str]:
    addresses: list[str] = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(script.string or "")
        except json.JSONDecodeError:
            continue
        addresses.extend(extract_addresses_from_jsonld(data))
    return dedupe_preserve_order([address for address in addresses if looks_like_address(address)])[:8]


def extract_addresses_from_jsonld(value: Any) -> list[str]:
    addresses: list[str] = []
    if isinstance(value, list):
        for item in value:
            addresses.extend(extract_addresses_from_jsonld(item))
        return addresses
    if not isinstance(value, dict):
        return addresses

    address = value.get("address")
    if isinstance(address, dict):
        parts = [
            address.get("addressLocality"),
            address.get("streetAddress"),
            address.get("postalCode"),
            address.get("addressRegion"),
        ]
        candidate = clean_text(", ".join(str(part) for part in parts if part))
        if candidate:
            addresses.append(candidate)
    elif isinstance(address, str):
        addresses.append(clean_text(address))

    for child in value.values():
        if isinstance(child, (dict, list)):
            addresses.extend(extract_addresses_from_jsonld(child))
    return addresses


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
        result["floor_plan_image_data_url"] = f"data:{image['mime_type']};base64,{encoded}"
        result["floor_plan_image_source"] = image["source"]
        result["floor_plan_image_name"] = image["name"]
        result["floor_plan_image_size_bytes"] = len(image["data"])
    elif content_type == "application/pdf" or (upload.filename or "").lower().endswith(".pdf"):
        pdf_result = extract_pdf_content(content)
        result.update(pdf_result)
    else:
        raise HTTPException(
            status_code=400,
            detail="Dodaj rzut jako PNG, JPG, WebP albo PDF.",
        )

    return result


def prepare_uploaded_plan_image(content: bytes) -> dict[str, Any]:
    try:
        image = Image.open(BytesIO(content))
        image = ImageOps.exif_transpose(image).convert("RGB")
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
    for page in reader.pages[:6]:
        pages.append(page.extract_text() or "")

    result: dict[str, Any] = {"pdf_text": clean_text("\n".join(pages))[:8000]}
    image = render_likely_floor_plan_page(content)

    if image:
        encoded = base64.b64encode(image["data"]).decode("ascii")
        result.update(
            {
                "floor_plan_image_data_url": f"data:{image['mime_type']};base64,{encoded}",
                "floor_plan_image_source": image["source"],
                "floor_plan_image_name": image["name"],
                "floor_plan_image_page": image["page"],
                "floor_plan_image_size_bytes": len(image["data"]),
            }
        )
    else:
        result["floor_plan_image_error"] = "Nie udało się wyrenderować i przyciąć grafiki rzutu z PDF."

    return result


def extract_pdf_text(content: bytes) -> str:
    try:
        reader = PdfReader(BytesIO(content))
        pages = []
        for page in reader.pages[:6]:
            pages.append(page.extract_text() or "")
        return clean_text("\n".join(pages))[:8000]
    except Exception:
        return ""


def extract_likely_floor_plan_image(reader: PdfReader) -> dict[str, Any] | None:
    best: dict[str, Any] | None = None
    for page_index, page in enumerate(reader.pages[:6], start=1):
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
    for page_index in range(min(document.page_count, 6)):
        page = document.load_page(page_index)
        text = clean_text(page.get_text("text") or "")
        pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
        rendered = Image.open(BytesIO(pixmap.tobytes("png"))).convert("RGB")
        cropped = crop_architectural_drawing(rendered)
        normalized = normalize_plan_image(cropped)
        data = image_to_png_bytes(normalized)

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
            "mime_type": "image/png",
            "name": f"architectural-drawing-page-{page_index + 1}.png",
            "page": page_index + 1,
            "score": score,
            "source": "pdf_rendered_cropped_page",
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


def image_mime_type(filename: str) -> str:
    guessed, _ = mimetypes.guess_type(filename)
    if guessed and guessed.startswith("image/"):
        return guessed
    return "image/png"


async def create_geo_context(
    listing: dict[str, Any],
    notes: str,
    api_key: str,
) -> dict[str, Any]:
    context: dict[str, Any] = {
        "status": "not_started",
        "address": None,
        "address_candidates": [],
        "coordinates": None,
        "parcel": None,
        "planning_context": [],
        "physical_context": [],
        "infrastructure_context": [],
        "market_context": {
            "status": "limited_unavailable",
            "summary": (
                "RCN i szczegółowe mapy cen transakcyjnych zwykle wymagają uprawnień albo osobnego źródła. "
                "W tej wersji aplikacja nie pobiera cen transakcyjnych z RCN."
            ),
        },
        "buffers": build_buffer_summary(),
        "intersections": [],
        "service_status": [],
        "map_image_data_url": None,
        "warnings": [],
    }

    candidates = build_address_candidates(listing, notes)
    context["address_candidates"] = candidates[:8]

    geocode_result = await geocode_address_candidates(candidates)
    if not geocode_result and api_key:
        inferred = await infer_address_with_openrouter(listing, notes, api_key)
        if inferred.get("address"):
            context["address_candidates"] = dedupe_preserve_order(
                [inferred["address"], *context["address_candidates"]]
            )[:8]
            geocode_result = await geocode_address_candidates([inferred["address"]])

    if not geocode_result:
        context["status"] = "missing_address"
        context["warnings"].append(
            "Nie udało się ustalić i zwalidować adresu inwestycji ze strony oferty ani notatek. "
            "Geoportal nie został odpytany, żeby nie pokazać błędnej działki."
        )
        context["service_status"].append(
            {"service": "address geocoding", "status": "no_verified_address", "source": GEOPORTAL_SERVICES["uug"]}
        )
        return context

    x = geocode_result["x"]
    y = geocode_result["y"]
    lon, lat = EPSG2180_TO_WGS84.transform(x, y)
    context["status"] = "partial"
    context["address"] = geocode_result.get("address") or geocode_result.get("query")
    context["coordinates"] = {
        "epsg": "EPSG:2180",
        "x": round(x, 3),
        "y": round(y, 3),
        "wgs84": {"lat": round(lat, 7), "lon": round(lon, 7)},
    }
    context["service_status"].append(
        {
            "service": "UUG geocoding",
            "status": "ok",
            "source": geocode_result.get("source_url") or GEOPORTAL_SERVICES["uug"],
            "query": geocode_result.get("query"),
            "provider": geocode_result.get("provider", "UUG"),
        }
    )

    parcel = await lookup_parcel_by_xy(x, y)
    context["parcel"] = parcel
    context["service_status"].append(
        {
            "service": "ULDK parcel",
            "status": "ok" if parcel else "no_match",
            "source": GEOPORTAL_SERVICES["uldk"],
        }
    )

    planning = [
        await query_wms_feature_info("KIMP MPZP", GEOPORTAL_SERVICES["kimp"], ["plany", "granice"], x, y),
        await query_wms_feature_info("KISKZP studium", GEOPORTAL_SERVICES["kiskzp"], ["studium", "granice"], x, y),
    ]
    context["planning_context"] = planning

    physical = [
        await query_wms_feature_info("BDOT", GEOPORTAL_SERVICES["bdot"], ["bdot10k", "bdot500"], x, y),
        {
            "service": "ORTO",
            "status": "queried_for_map",
            "summary": "Ortofotomapa została użyta jako tło mapy poglądowej.",
            "source": GEOPORTAL_SERVICES["orto"],
        },
        {
            "service": "NMT/NMPT/LiDAR",
            "status": "not_configured",
            "summary": "Model terenu i chmura punktów nie są jeszcze pobierane w tej wersji aplikacji.",
        },
    ]
    context["physical_context"] = physical

    infrastructure = [
        await query_wms_feature_info("GESUT", GEOPORTAL_SERVICES["gesut"], ["gesut"], x, y),
        await query_wms_feature_info("EGiB", GEOPORTAL_SERVICES["egib"], ["dzialki", "budynki"], x, y),
        {
            "service": "Drogi i transport publiczny",
            "status": "not_configured",
            "summary": "Warstwy transportu publicznego wymagają osobnego źródła miejskiego lub GTFS.",
        },
    ]
    context["infrastructure_context"] = infrastructure
    context["intersections"] = build_intersection_summary(planning, physical, infrastructure)
    context["service_status"].extend(
        service_status_from_sections(planning + physical + infrastructure)
    )

    map_image = await create_geoportal_map_image(x, y, parcel.get("geom_wkt") if parcel else None)
    if map_image:
        context["map_image_data_url"] = map_image
        context["service_status"].append(
            {
                "service": "ORTO map screenshot",
                "status": "ok",
                "source": GEOPORTAL_SERVICES["orto"],
            }
        )
    else:
        context["warnings"].append("Nie udało się pobrać mapy poglądowej z ortofotomapy Geoportalu.")
        context["service_status"].append(
            {
                "service": "ORTO map screenshot",
                "status": "error",
                "source": GEOPORTAL_SERVICES["orto"],
            }
        )

    if parcel or any(item.get("status") == "ok" for item in planning + infrastructure):
        context["status"] = "ready"
    return context


def build_address_candidates(listing: dict[str, Any], notes: str) -> list[str]:
    candidates: list[str] = []

    for address in listing.get("structured_addresses") or []:
        if looks_like_address(address):
            candidates.append(address)

    candidates.extend(extract_address_candidates_from_text(notes or ""))
    candidates.extend(extract_address_candidates_from_text(listing.get("text") or ""))
    candidates.extend(extract_address_candidates_from_text(listing.get("description") or ""))
    candidates.extend(extract_address_candidates_from_text(listing.get("title") or ""))

    return dedupe_preserve_order(candidates)[:12]


def extract_address_candidates_from_text(text: str) -> list[str]:
    candidates: list[str] = []
    if not text:
        return candidates

    normalized = re.sub(r"\s+", " ", text)
    city = infer_city(normalized)

    label_patterns = [
        r"(?:adres inwestycji|adres|lokalizacja|położenie|miejsce inwestycji)[:\s-]{1,12}([^.;\n]{8,180})",
        r"(?:inwestycja znajduje się|położona jest|zlokalizowana jest)[:\s-]{0,12}([^.;\n]{8,180})",
    ]
    for pattern in label_patterns:
        for match in re.findall(pattern, text, flags=re.IGNORECASE):
            candidate = normalize_address_candidate(match, city)
            if looks_like_address(candidate):
                candidates.append(candidate)

    for line in re.split(r"[\n\r]+", text):
        candidate = normalize_address_candidate(line, city)
        if 8 <= len(candidate) <= 180 and looks_like_address(candidate):
            candidates.append(candidate)

    street_matches = re.findall(
        r"\b(?:ul\.?|al\.?|aleja|plac|pl\.?|os\.?)\s+[A-ZŁŚŻŹĆŃÓ][A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż .'-]{2,70}\s+\d+[A-Za-z]?(?:/\d+)?",
        text,
        flags=re.IGNORECASE,
    )
    for street in street_matches:
        candidate = normalize_address_candidate(street, city)
        if looks_like_address(candidate):
            candidates.append(candidate)

    return dedupe_preserve_order(candidates)


def infer_city(text: str) -> str:
    match = re.search(
        r"\b(Warszawa|Kraków|Krakow|Wrocław|Wroclaw|Poznań|Poznan|Gdańsk|Gdansk|Gdynia|Łódź|Lodz|Katowice|Lublin|Rzeszów|Szczecin|Bydgoszcz|Białystok|Bialystok|Toruń|Torun)\b",
        text,
        flags=re.IGNORECASE,
    )
    return match.group(1) if match else ""


def normalize_address_candidate(value: str, fallback_city: str = "") -> str:
    candidate = clean_text(value)
    candidate = re.sub(
        r"^(?:adres inwestycji|adres|lokalizacja|położenie|miejsce inwestycji)[:\s-]+",
        "",
        candidate,
        flags=re.IGNORECASE,
    )
    candidate = clean_text(re.sub(r"\b(mieszkania|lokale|apartamenty|etap|budynek)\b.*$", "", candidate, flags=re.IGNORECASE))
    address_match = re.search(
        r"((?:Warszawa|Kraków|Krakow|Wrocław|Wroclaw|Poznań|Poznan|Gdańsk|Gdansk|Gdynia|Łódź|Lodz|Katowice|Lublin|Rzeszów|Szczecin|Bydgoszcz|Białystok|Bialystok|Toruń|Torun)\s*,?\s*(?:ul\.?|al\.?|aleja|plac|pl\.?|os\.?)\s+[A-ZŁŚŻŹĆŃÓ][A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż .'-]{2,70}\s+\d+[A-Za-z]?(?:/\d+)?)",
        candidate,
        flags=re.IGNORECASE,
    )
    if address_match:
        candidate = clean_text(address_match.group(1))
    candidate = candidate.strip(" ,:-")
    if fallback_city and fallback_city.lower() not in candidate.lower():
        candidate = f"{fallback_city}, {candidate}"
    return candidate


def looks_like_address(value: str) -> bool:
    has_street = bool(re.search(r"\b(ul\.?|al\.?|aleja|plac|pl\.?|os\.?)\b", value, flags=re.IGNORECASE))
    has_city = bool(
        re.search(
            r"\b(Warszawa|Kraków|Krakow|Wrocław|Wroclaw|Poznań|Poznan|Gdańsk|Gdansk|Gdynia|Łódź|Lodz|Katowice)\b",
            value,
            flags=re.IGNORECASE,
        )
    )
    has_number = bool(re.search(r"\d+[A-Za-z]?(?:/\d+)?", value))
    has_postcode = bool(re.search(r"\b\d{2}-\d{3}\b", value))
    return has_street and has_number and (has_city or has_postcode)


async def infer_address_with_openrouter(
    listing: dict[str, Any],
    notes: str,
    api_key: str,
) -> dict[str, Any]:
    prompt = f"""
Wyciągnij adres inwestycji deweloperskiej z danych oferty. Szukaj konkretnego adresu lokalizacji inwestycji, nie adresu biura sprzedaży.
Jeśli nie ma miasta, ulicy i numeru albo adres jest niepewny, zwróć null.
Zwróć wyłącznie JSON: {{"address": "miasto, ulica numer", "confidence": 0-1, "evidence": "krótki cytat lub opis miejsca w treści"}}

Notatki użytkownika:
{notes or "Brak"}

Dane oferty:
{json.dumps({
        "title": listing.get("title"),
        "description": listing.get("description"),
        "text": (listing.get("text") or "")[:5000],
    }, ensure_ascii=False, indent=2)}
""".strip()
    payload = {
        "model": settings.openrouter_model,
        "messages": [
            {"role": "system", "content": "Wyciągasz adresy z polskich ofert nieruchomości. Nie zgadujesz."},
            {"role": "user", "content": prompt},
        ],
        "response_format": {"type": "json_object"},
    }
    try:
        data = await post_openrouter_chat_completion(payload, api_key)
        text = data.get("choices", [{}])[0].get("message", {}).get("content") or "{}"
        parsed = parse_report(text)
        if parsed.get("address") and parsed.get("confidence", 0) >= 0.7:
            return parsed
    except Exception:
        return {}
    return {}


async def geocode_address_candidates(candidates: list[str]) -> dict[str, Any] | None:
    async with httpx.AsyncClient(timeout=12) as client:
        for candidate in candidates:
            google_result = await geocode_with_google_maps(client, candidate)
            if google_result:
                return google_result

            try:
                response = await client.get(
                    GEOPORTAL_SERVICES["uug"],
                    params={"request": "GetAddress", "address": candidate},
                )
                response.raise_for_status()
                data = response.json()
            except Exception:
                continue

            record = first_xy_record(data)
            if not record:
                continue
            record["query"] = candidate
            record["address"] = record.get("address") or record.get("label") or candidate
            record["provider"] = "UUG"
            record["source_url"] = GEOPORTAL_SERVICES["uug"]
            return record
    return None


async def geocode_with_google_maps(client: httpx.AsyncClient, address: str) -> dict[str, Any] | None:
    api_key = settings.google_maps_api_key
    if not api_key:
        return None

    try:
        response = await client.get(
            "https://maps.googleapis.com/maps/api/geocode/json",
            params={"address": address, "region": "pl", "key": api_key},
        )
        response.raise_for_status()
        data = response.json()
    except Exception:
        return None

    if data.get("status") != "OK" or not data.get("results"):
        return None

    result = data["results"][0]
    location = result.get("geometry", {}).get("location", {})
    lat = parse_float(location.get("lat"))
    lon = parse_float(location.get("lng"))
    if lat is None or lon is None:
        return None

    x, y = WGS84_TO_EPSG2180.transform(lon, lat)
    return {
        "x": x,
        "y": y,
        "lat": lat,
        "lon": lon,
        "query": address,
        "address": result.get("formatted_address") or address,
        "provider": "Google Geocoding API",
        "source_url": "https://maps.googleapis.com/maps/api/geocode/json",
        "raw": {
            "place_id": result.get("place_id"),
            "types": result.get("types", [])[:6],
            "location_type": result.get("geometry", {}).get("location_type"),
        },
    }


def first_xy_record(data: Any) -> dict[str, Any] | None:
    for record in walk_dicts(data):
        x = parse_float(record.get("x") or record.get("X"))
        y = parse_float(record.get("y") or record.get("Y"))
        if x is None or y is None:
            point = parse_point_wkt(str(record.get("wkt") or record.get("geom_wkt") or ""))
            if point:
                x, y = point
        if x is None or y is None:
            continue
        if 100000 <= x <= 1000000 and 100000 <= y <= 1000000:
            label = (
                record.get("label")
                or record.get("address")
                or record.get("name")
                or record.get("miejscowosc")
                or record.get("ulica")
            )
            return {"x": x, "y": y, "label": clean_text(str(label or "")), "raw": trim_geo_record(record)}
    return None


def walk_dicts(value: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(value, dict):
        found.append(value)
        for child in value.values():
            found.extend(walk_dicts(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(walk_dicts(child))
    return found


def trim_geo_record(record: dict[str, Any]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in record.items():
        if isinstance(value, (dict, list)):
            continue
        string_value = clean_text(str(value))
        if string_value:
            output[str(key)] = string_value[:180]
    return output


async def lookup_parcel_by_xy(x: float, y: float) -> dict[str, Any] | None:
    params = {
        "request": "GetParcelByXY",
        "xy": f"{x},{y},2180",
        "result": "id,numer,wojewodztwo,powiat,gmina,obreb,geom_wkt",
    }
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            response = await client.get(GEOPORTAL_SERVICES["uldk"], params=params)
            response.raise_for_status()
    except Exception:
        return None
    return parse_uldk_parcel(response.text)


def parse_uldk_parcel(text: str) -> dict[str, Any] | None:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in lines:
        if "|" not in line or line.startswith("-"):
            continue
        parts = line.split("|")
        if len(parts) < 7:
            continue
        return {
            "id": parts[0],
            "number": parts[1],
            "voivodeship": parts[2],
            "county": parts[3],
            "commune": parts[4],
            "district": parts[5],
            "geom_wkt": parts[6],
        }
    return None


async def query_wms_feature_info(
    service_name: str,
    service_url: str,
    layers: list[str],
    x: float,
    y: float,
) -> dict[str, Any]:
    bbox = make_bbox(x, y, 350)
    params = {
        "service": "WMS",
        "version": "1.3.0",
        "request": "GetFeatureInfo",
        "layers": ",".join(layers),
        "query_layers": ",".join(layers),
        "styles": "",
        "crs": "EPSG:2180",
        "bbox": ",".join(str(round(value, 2)) for value in bbox),
        "width": "512",
        "height": "512",
        "i": "256",
        "j": "256",
        "feature_count": "10",
        "info_format": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=14) as client:
            response = await client.get(service_url, params=params)
        if response.status_code >= 400:
            return {
                "service": service_name,
                "status": "error",
                "summary": f"Usługa zwróciła HTTP {response.status_code}.",
                "source": service_url,
            }
        return summarize_wms_response(service_name, service_url, response)
    except Exception as exc:
        return {
            "service": service_name,
            "status": "error",
            "summary": f"Nie udało się odpytać usługi: {exc}",
            "source": service_url,
        }


def summarize_wms_response(service_name: str, service_url: str, response: httpx.Response) -> dict[str, Any]:
    content_type = response.headers.get("content-type", "")
    text = response.text.strip()
    if "application/json" in content_type or text.startswith("{"):
        try:
            data = response.json()
        except json.JSONDecodeError:
            data = {}
        features = data.get("features") if isinstance(data, dict) else None
        if not features:
            return {
                "service": service_name,
                "status": "empty",
                "summary": "Brak obiektów zwróconych w punkcie zapytania.",
                "source": service_url,
            }
        properties = []
        for feature in features[:5]:
            props = feature.get("properties") if isinstance(feature, dict) else {}
            if props:
                properties.append(trim_geo_record(props))
        return {
            "service": service_name,
            "status": "ok",
            "summary": f"Usługa zwróciła {len(features)} obiektów w pobliżu punktu.",
            "source": service_url,
            "features": properties,
        }

    if not text or "ServiceException" in text or "ExceptionReport" in text:
        return {
            "service": service_name,
            "status": "empty" if not text else "error",
            "summary": "Brak czytelnych obiektów albo usługa nie zwróciła danych w formacie JSON.",
            "source": service_url,
        }

    return {
        "service": service_name,
        "status": "ok",
        "summary": clean_text(re.sub(r"<[^>]+>", " ", text))[:500],
        "source": service_url,
    }


async def create_geoportal_map_image(x: float, y: float, parcel_wkt: str | None) -> str | None:
    bbox = make_bbox(x, y, 550)
    params = {
        "service": "WMS",
        "version": "1.3.0",
        "request": "GetMap",
        "layers": "Raster",
        "styles": "",
        "crs": "EPSG:2180",
        "bbox": ",".join(str(round(value, 2)) for value in bbox),
        "width": "900",
        "height": "900",
        "format": "image/png",
        "transparent": "false",
    }
    try:
        image = None
        for _ in range(4):
            async with httpx.AsyncClient(timeout=18) as client:
                response = await client.get(GEOPORTAL_SERVICES["orto"], params=params)
            if response.status_code >= 400 or not response.content.startswith(b"\x89PNG"):
                continue
            image = Image.open(BytesIO(response.content)).convert("RGBA")
            break
        if image is None:
            return None
    except Exception:
        return None

    draw_geo_overlay(image, x, y, bbox, parcel_wkt)
    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    encoded = base64.b64encode(output.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def draw_geo_overlay(
    image: Image.Image,
    x: float,
    y: float,
    bbox: tuple[float, float, float, float],
    parcel_wkt: str | None,
) -> None:
    draw = ImageDraw.Draw(image, "RGBA")
    width, height = image.size
    center = point_to_pixel(x, y, bbox, width, height)

    for radius, color in [(500, (33, 107, 85, 70)), (300, (49, 95, 143, 70)), (100, (153, 100, 20, 95))]:
        top_left = point_to_pixel(x - radius, y + radius, bbox, width, height)
        bottom_right = point_to_pixel(x + radius, y - radius, bbox, width, height)
        draw.ellipse([top_left, bottom_right], outline=color, width=3)

    polygon = parse_polygon_wkt(parcel_wkt or "")
    if polygon:
        pixels = [point_to_pixel(px, py, bbox, width, height) for px, py in polygon]
        draw.polygon(pixels, outline=(255, 255, 255, 220), fill=(33, 107, 85, 70))
        draw.line(pixels + [pixels[0]], fill=(33, 107, 85, 240), width=4)

    cx, cy = center
    draw.ellipse((cx - 10, cy - 10, cx + 10, cy + 10), fill=(255, 255, 255, 240), outline=(23, 33, 29, 255), width=3)
    draw.ellipse((cx - 5, cy - 5, cx + 5, cy + 5), fill=(162, 56, 56, 255))


def make_bbox(x: float, y: float, radius: float) -> tuple[float, float, float, float]:
    return (x - radius, y - radius, x + radius, y + radius)


def point_to_pixel(
    x: float,
    y: float,
    bbox: tuple[float, float, float, float],
    width: int,
    height: int,
) -> tuple[int, int]:
    min_x, min_y, max_x, max_y = bbox
    pixel_x = int((x - min_x) / max(max_x - min_x, 1) * width)
    pixel_y = int(height - ((y - min_y) / max(max_y - min_y, 1) * height))
    return (pixel_x, pixel_y)


def parse_point_wkt(value: str) -> tuple[float, float] | None:
    match = re.search(r"POINT\s*\(\s*([0-9.]+)\s+([0-9.]+)\s*\)", value, flags=re.IGNORECASE)
    if not match:
        return None
    return (float(match.group(1)), float(match.group(2)))


def parse_polygon_wkt(value: str) -> list[tuple[float, float]]:
    match = re.search(r"POLYGON\s*\(\((.*?)\)\)", value, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return []
    points: list[tuple[float, float]] = []
    for pair in match.group(1).split(","):
        numbers = pair.strip().split()
        if len(numbers) < 2:
            continue
        x = parse_float(numbers[0])
        y = parse_float(numbers[1])
        if x is not None and y is not None:
            points.append((x, y))
    return points


def build_buffer_summary() -> list[dict[str, Any]]:
    descriptions = {
        50: "bezpośrednie sąsiedztwo budynku: wejścia, dojazdy, potencjalne źródła hałasu",
        100: "najbliższy kwartał: zabudowa, zieleń, uciążliwe funkcje",
        300: "codzienne dojścia piesze: usługi, przystanki, szkoły, parki",
        500: "lokalny kontekst osiedla i główne ulice",
        1000: "szersza dostępność transportowa i miejska",
    }
    return [
        {"radius_m": radius, "use": description, "status": "prepared"}
        for radius, description in descriptions.items()
    ]


def build_intersection_summary(
    planning: list[dict[str, Any]],
    physical: list[dict[str, Any]],
    infrastructure: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return [
        {
            "theme": "zoning",
            "label": "MPZP / studium",
            "buffers_m": [50, 100, 300, 500, 1000],
            "status": combined_status(planning),
            "summary": "Kontekst planistyczny odczytywany z KIMP/KISKZP jako zapytanie punktowe WMS.",
        },
        {
            "theme": "roads",
            "label": "Drogi",
            "buffers_m": [50, 100, 300, 500, 1000],
            "status": combined_status([item for item in physical if item.get("service") == "BDOT"]),
            "summary": "Drogi i obiekty topograficzne są sprawdzane przez warstwy BDOT, jeśli usługa zwraca obiekty.",
        },
        {
            "theme": "greenery",
            "label": "Zieleń",
            "buffers_m": [100, 300, 500, 1000],
            "status": "partial" if any(item.get("service") == "BDOT" for item in physical) else "not_configured",
            "summary": "Zieleń może wynikać z BDOT i ortofotomapy, ale pełne przecięcia poligonowe wymagają warstw wektorowych.",
        },
        {
            "theme": "noise",
            "label": "Hałas",
            "buffers_m": [50, 100, 300, 500],
            "status": "not_configured",
            "summary": "Mapy akustyczne nie są dostępne w obecnej konfiguracji ogólnych usług Geoportalu.",
        },
        {
            "theme": "infrastructure",
            "label": "Uzbrojenie i ewidencja",
            "buffers_m": [50, 100, 300],
            "status": combined_status(infrastructure),
            "summary": "Infrastruktura jest sprawdzana przez GESUT/EGiB, o ile dana usługa zwraca czytelne obiekty.",
        },
    ]


def combined_status(items: list[dict[str, Any]]) -> str:
    statuses = {item.get("status") for item in items}
    if "ok" in statuses:
        return "ok"
    if "empty" in statuses:
        return "empty"
    if "error" in statuses:
        return "error"
    if "not_configured" in statuses:
        return "not_configured"
    return "unknown"


def service_status_from_sections(sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    statuses = []
    for item in sections:
        statuses.append(
            {
                "service": item.get("service", "Geoportal"),
                "status": item.get("status", "unknown"),
                "source": item.get("source"),
            }
        )
    return statuses


def compact_geo_context_for_prompt(context: dict[str, Any]) -> dict[str, Any]:
    compact = dict(context)
    compact.pop("map_image_data_url", None)
    return compact


def parse_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value).replace(",", "."))
    except ValueError:
        return None


async def create_openrouter_report(
    url: str,
    listing: dict[str, Any],
    notes: str,
    uploaded_file: dict[str, Any] | None,
    geo_context: dict[str, Any],
    api_key: str,
) -> dict[str, Any]:
    prompt = build_prompt(url, listing, notes, uploaded_file, geo_context)

    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    floor_plan_image_data_url = uploaded_file.get("floor_plan_image_data_url") if uploaded_file else None
    if floor_plan_image_data_url:
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": floor_plan_image_data_url},
            }
        )

    messages = [
        {
            "role": "system",
            "content": (
                "Jesteś rzetelnym analitykiem polskiego rynku mieszkaniowego. "
                "Oddzielasz fakty od oceny, nie zmyślasz danych i jasno mówisz, gdy dowody są słabe."
            ),
        },
        {"role": "user", "content": content},
    ]

    payload: dict[str, Any] = {
        "model": settings.openrouter_model,
        "messages": messages,
        "response_format": {"type": "json_object"},
    }

    data = await post_openrouter_chat_completion(payload, api_key)
    output_text = data.get("choices", [{}])[0].get("message", {}).get("content") or ""
    return parse_report(output_text)


async def create_floor_plan_visualization(uploaded_file: dict[str, Any] | None) -> dict[str, Any]:
    if not uploaded_file:
        return {
            "status": "skipped",
            "message": "Nie dodano rzutu, więc nie można przygotować wizualizacji 3D.",
        }

    floor_plan_image_data_url = uploaded_file.get("floor_plan_image_data_url")
    if not floor_plan_image_data_url:
        return {
            "status": "skipped",
            "message": uploaded_file.get("floor_plan_image_error")
            or "Nie udało się zidentyfikować grafiki rzutu w przesłanym pliku.",
        }

    api_key = sanitize_openrouter_api_key(settings.openrouter_api_key)
    if not api_key:
        return {
            "status": "not_configured",
            "message": "Brakuje OPENROUTER_API_KEY, więc wizualizacja Nano Banana nie została wygenerowana.",
            "source_image_data_url": floor_plan_image_data_url,
        }

    try:
        mime_type, image_base64 = split_data_url(floor_plan_image_data_url)
        generated = await call_nanobanana_via_openrouter(api_key, mime_type, image_base64)
        return {
            "status": "ready",
            "prompt": NANOBANANA_PROMPT,
            "image_data_url": generated["image_data_url"],
            "text": generated.get("text", ""),
            "source_image_data_url": floor_plan_image_data_url,
            "source": uploaded_file.get("floor_plan_image_source"),
        }
    except Exception as exc:
        return {
            "status": "error",
            "message": f"Nie udało się wygenerować wizualizacji 3D: {exc}",
            "source_image_data_url": floor_plan_image_data_url,
        }


async def call_nanobanana_via_openrouter(
    api_key: str,
    mime_type: str,
    image_base64: str,
) -> dict[str, str]:
    url = f"{settings.openrouter_base_url.rstrip('/')}/chat/completions"
    source_image_data_url = f"data:{mime_type};base64,{image_base64}"
    payload = {
        "model": settings.nanobanana_model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": NANOBANANA_PROMPT},
                    {
                        "type": "image_url",
                        "image_url": {"url": source_image_data_url},
                    },
                ],
            }
        ],
        "modalities": ["image", "text"],
        "image_config": {
            "aspect_ratio": "16:9",
            "image_size": "1K",
        },
        "stream": False,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "HTTP-Referer": settings.app_url,
        "X-Title": settings.app_name,
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=90) as client:
        response = await client.post(url, headers=headers, json=payload)

    if response.status_code >= 400:
        raise RuntimeError(f"OpenRouter Nano Banana zwrócił {response.status_code}: {response.text}")

    data = response.json()
    message = data.get("choices", [{}])[0].get("message", {})
    for image in message.get("images", []) or []:
        image_url = image.get("image_url", {}).get("url")
        if image_url:
            return {
                "image_data_url": image_url,
                "text": message.get("content") or "",
            }

    raise RuntimeError("OpenRouter Nano Banana nie zwrócił obrazu.")


async def post_openrouter_chat_completion(
    payload: dict[str, Any],
    api_key: str,
) -> dict[str, Any]:
    url = f"{settings.openrouter_base_url.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "HTTP-Referer": settings.app_url,
        "X-Title": settings.app_name,
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(url, headers=headers, json=payload)
        if response.status_code >= 400 and "response_format" in payload:
            fallback_payload = dict(payload)
            fallback_payload.pop("response_format", None)
            response = await client.post(url, headers=headers, json=fallback_payload)

    if response.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=f"Zapytanie do OpenRouter nie powiodło się: {response.status_code} - {response.text}",
        )

    try:
        return response.json()
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=502,
            detail="OpenRouter zwrócił odpowiedź, której nie da się odczytać jako JSON.",
        ) from exc


def build_prompt(
    url: str,
    listing: dict[str, Any],
    notes: str,
    uploaded_file: dict[str, Any] | None,
    geo_context: dict[str, Any],
) -> str:
    uploaded_summary = "Nie dodano rzutu mieszkania."
    if uploaded_file:
        if uploaded_file.get("image_data_url"):
            uploaded_summary = (
                f"Użytkownik dodał rzut mieszkania jako obraz: {uploaded_file.get('filename')}."
            )
        elif uploaded_file.get("pdf_text"):
            uploaded_summary = (
                f"Użytkownik dodał PDF: {uploaded_file.get('filename')}. "
                f"Tekst wyodrębniony z PDF:\n{uploaded_file.get('pdf_text')}"
            )
            if uploaded_file.get("floor_plan_image_data_url"):
                uploaded_summary += (
                    "\nZ PDF wyodrębniono także grafikę, która najprawdopodobniej przedstawia rzut mieszkania."
                )
        else:
            uploaded_summary = (
                f"Użytkownik dodał {uploaded_file.get('filename')}, ale nie udało się odczytać treści."
            )

    return f"""
Przeanalizuj tę polską ofertę mieszkania od dewelopera dla osoby kupującej.

Adres oferty:
{url}

Notatki i priorytety użytkownika:
{notes or "Brak"}

Dowody pobrane ze strony oferty:
{json.dumps(listing, ensure_ascii=False, indent=2)}

Dowody z dodanego rzutu:
{uploaded_summary}

Kontekst z usług Geoportalu i GUGiK:
{json.dumps(compact_geo_context_for_prompt(geo_context), ensure_ascii=False, indent=2)}

Zwróć WYŁĄCZNIE poprawny JSON w poniższym kształcie. Klucze zostaw po angielsku, ale wszystkie wartości tekstowe pisz po polsku:
{{
  "verdict": "Dobry wybór | Do rozważenia | Unikać | Wymaga dalszego sprawdzenia",
  "confidence": 0-100,
  "one_sentence": "krótka rekomendacja po polsku",
  "facts_found": ["weryfikowalny fakt z dowodów"],
  "layout_analysis": {{
    "score": 0-10,
    "positives": ["..."],
    "risks": ["..."],
    "missing_information": ["..."]
  }},
  "location_analysis": {{
    "score": 0-10,
    "positives": ["..."],
    "risks": ["..."],
    "missing_information": ["..."]
  }},
  "financial_resale_view": {{
    "score": 0-10,
    "positives": ["..."],
    "risks": ["..."],
    "missing_information": ["..."]
  }},
  "price_metrics_assessment": {{
    "score": 0-10,
    "summary": "krótka ocena według metryk ceny i wartości",
    "value_drivers": ["metryka, która wspiera cenę lub atrakcyjność"],
    "risk_drivers": ["metryka, która obniża atrakcyjność lub zwiększa ryzyko"],
    "missing_information": ["brakujące dane potrzebne do rzetelniejszej oceny"]
  }},
  "geoportal_assessment": {{
    "score": 0-10,
    "summary": "krótka ocena lokalizacji na podstawie danych Geoportalu",
    "value_drivers": ["co w danych przestrzennych wspiera atrakcyjność lokalu"],
    "risk_drivers": ["co w danych przestrzennych zwiększa ryzyko lub wymaga sprawdzenia"],
    "buffers": ["wniosek z buforów 50 m / 100 m / 300 m / 500 m / 1 km"],
    "service_status": ["które źródła były dostępne, puste albo ograniczone"],
    "missing_information": ["brakujące warstwy lub dane potrzebne do pełnej oceny"]
  }},
  "deal_breakers": ["..."],
  "questions_for_developer": ["..."],
  "due_diligence_next_steps": ["..."],
  "what_would_change_my_mind": ["..."],
  "source_notes": ["wskaż, które twierdzenia pochodzą ze strony oferty, rzutu albo samego adresu URL"]
}}

Zasady:
- Pisz po polsku, konkretnie i sceptycznie.
- Oddzielaj fakty od oceny. Nie dopowiadaj szczegółów, których nie ma w źródłach.
- Nie zakładaj, że mieszkanie jest dobre tylko dlatego, że opis sprzedażowy brzmi pozytywnie.
- Lokalizację oceniaj na podstawie tekstu oferty, adresu URL i kontekstu Geoportalu. Jeśli brakuje adresu albo geokodowanie jest niepewne, nazwij niepewność.
- Jeśli kontekst Geoportalu ma status ready albo partial, uwzględnij go w location_analysis, source_notes i geoportal_assessment. Jeśli usługa zwróciła pusty wynik albo błąd, nie traktuj tego jako dowodu braku ryzyka.
- Układ oceniaj na podstawie rzutu, jeśli został dodany. Jeśli go nie ma, jasno opisz ograniczenie analizy.
- Uwzględnij polskie checklisty kupującego: KW, umowa deweloperska, rachunek powierniczy, koszty parkingu/komórki, hałas, nasłonecznienie, czynsz/opłaty administracyjne, standard wykończenia i planowane inwestycje w okolicy.
- W podsumowaniu i sekcji price_metrics_assessment uwzględnij poniższe metryki:
{PRICE_METRICS_GUIDE}
""".strip()


def get_response_text(response: Any) -> str:
    if hasattr(response, "output_text") and response.output_text:
        return response.output_text

    chunks: list[str] = []
    for item in getattr(response, "output", []) or []:
        for content in getattr(item, "content", []) or []:
            text = getattr(content, "text", None)
            if text:
                chunks.append(text)
    return "\n".join(chunks)


def parse_report(text: str) -> dict[str, Any]:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
    return {
        "verdict": "Wymaga dalszego sprawdzenia",
        "confidence": 25,
        "one_sentence": "Nie udało się odczytać odpowiedzi modelu jako uporządkowanego JSON.",
        "raw_response": text,
    }


def split_data_url(data_url: str) -> tuple[str, str]:
    match = re.match(r"^data:([^;]+);base64,(.+)$", data_url, flags=re.DOTALL)
    if not match:
        raise ValueError("Niepoprawny format obrazu źródłowego.")
    return match.group(1), match.group(2)


def sanitize_openrouter_api_key(value: str | None) -> str:
    if not value:
        return ""
    value = value.strip().strip('"').strip("'")
    if value.lower().startswith("bearer "):
        value = value[7:].strip()
    return value


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def dedupe_preserve_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        normalized = value.lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        output.append(value)
    return output


def floor_plan_score(url: str, alt: str) -> int:
    haystack = f"{url} {alt}".lower()
    keywords = ["rzut", "plan", "floor", "mieszkan", "lokal", "rzuty", "karta"]
    return sum(1 for keyword in keywords if keyword in haystack)


def assess_extraction_quality(text: str, images: list[dict[str, Any]]) -> str:
    if len(text) > 2500 and images:
        return "good"
    if len(text) > 800:
        return "partial"
    return "weak"
