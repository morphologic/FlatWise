# FlatWise

FlatWise is a local web app that helps Polish buyers evaluate developer flat listings. Paste a listing URL, optionally upload a floor plan, and receive a structured skeptical buyer report powered by OpenRouter, with geospatial context from Polish Geoportal services.

## Features

- Chat onboarding to collect listing URL, floor plan, and buyer priorities
- Scrapes developer listing pages for text, images, and JSON-LD addresses
- Checks listing images for a likely floor plan before asking the buyer to upload one
- Optional Playwright gallery scan for floor plans hidden behind buttons, modals, or client-rendered galleries
- Accepts floor plan uploads (PNG, JPG, WebP, PDF) with crop and normalization
- Local floor-plan geometry preprocessing: estimates room-like regions, elongated spaces, wall-line density, and detection limits before LLM analysis
- Optional CubiCasa5K-compatible parser adapter for semantic walls, doors, windows, rooms, and fixtures when a local parser command is configured
- OpenRouter analysis: layout, location, price metrics, geoportal assessment, due diligence steps
- Staged workflow: current analysis -> generate layout alternatives -> select one -> compare original vs alternative
- Vision support when the configured OpenRouter model accepts images
- Geoportal integration: geocoding (UUG), parcel lookup (ULDK), WMS layers (MPZP, BDOT, GESUT, EGiB), orthophoto map preview
- Warsaw city map integration for Warsaw coordinates: WFS context for metro, P+R, bike, greenery, education/planning/service layers and WMS checks for 2022 noise/planning layers
- Optional Google Geocoding and OpenStreetMap/Nominatim geocoding for address validation before Geoportal queries, with conflict warnings when listing coordinates and web maps disagree
- 3D floor-plan visualization via OpenRouter image models (Nano Banana / Gemini image)
- API-readable buyer onboarding spec (`kart.json`) for future preference-card flows

## Getting started

```powershell
cd e:\Vibecoding\FlatWise
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
```

For development checks, install the dev requirements and run the quality commands:

```powershell
pip install -r requirements-dev.txt
python -m pytest
python -m ruff check .
```

Install the Playwright browser runtime when you want hidden-gallery extraction:

```powershell
.\.venv\Scripts\python.exe -m playwright install chromium
```

Add your `OPENROUTER_API_KEY` to `.env`, then run:

```powershell
uvicorn app:app --host 127.0.0.1 --port 8001 --reload
```

Open http://127.0.0.1:8001 in your browser.

Requires Python 3.10 or newer.

## Architecture

Backend feature code is split by responsibility:

| File | Responsibility |
|------|----------------|
| `app.py` | FastAPI routes and compatibility exports |
| `flatwise/listing.py` | Listing scraping, JSON-LD extraction, gallery image discovery, URL safety |
| `flatwise/floor_plan.py` | Upload/PDF handling, floor-plan image normalization, geometry scan, CubiCasa adapter |
| `flatwise/geo.py` | Address extraction, geocoding, Geoportal/Warsaw/OSM context, map previews |
| `flatwise/analysis.py` | Current-analysis schema, orientation analysis, layout alternatives, comparison, prompts |
| `flatwise/openrouter.py` | Shared OpenRouter HTTP transport |
| `flatwise/constants.py` | Service URLs, CRS transforms, prompt constants |
| `flatwise/text.py` | Shared parsing and text helpers |

## Controls

| Variable | Purpose |
|----------|---------|
| `OPENROUTER_API_KEY` | API key for OpenRouter (required) |
| `OPENROUTER_MODEL` | Chat model for buyer report (default: `openai/gpt-4o-mini`) |
| `OPENROUTER_REASONING_MODEL` | Reasoning model for alternatives and comparisons |
| `OPENROUTER_VISION_MODEL` | Vision-capable model for floor-plan/listing analysis |
| `OPENROUTER_BASE_URL` | OpenRouter API base URL |
| `NANOBANANA_MODEL` | Image model for 3D floor-plan visualization |
| `GOOGLE_MAPS_API_KEY` | Optional geocoding before Geoportal |
| `OSM_NOMINATIM_BASE_URL` | OpenStreetMap Nominatim base URL used when Google Maps is not configured |
| `CUBICASA5K_COMMAND` | Optional local CubiCasa5K-compatible command; receives an image path and returns JSON |
| `ENABLE_3D_VISUALIZATION` | Enable OpenRouter image-model 3D visualization; default `false` |
| `ENABLE_PLAYWRIGHT_GALLERY_SCAN` | Enable optional browser scan for hidden listing gallery images; default `true` |
| `PLAYWRIGHT_TIMEOUT_SECONDS` | Timeout for the optional browser gallery scan |
| `APP_URL` | Referer sent to OpenRouter |
| `APP_NAME` | App title sent to OpenRouter |
| `REQUEST_TIMEOUT_SECONDS` | Listing fetch timeout |
| `MAX_UPLOAD_BYTES` | Max floor-plan upload size |
| `MAX_IMAGE_PIXELS` | Max decoded image resolution for floor-plan uploads |
| `MAX_PDF_PAGES` | Max PDF pages inspected/rendered for floor-plan extraction |
| `ALLOW_PRIVATE_LISTING_URLS` | Allow listing URLs that resolve to private/local networks; keep `false` unless developing locally against a private host |
| `CORS_ORIGINS` | Comma-separated browser origins allowed to call the API |

## Known limits

- Some developer sites block scraping or render content client-side only.
- Hidden gallery scanning uses Playwright as a fallback and may still fail when a site blocks automation, requires login, or hides original image URLs behind scripts.
- PDF floor plans: text extraction plus rendered page crop; image-heavy PDFs work best as image uploads.
- Floor-plan preprocessing is heuristic unless `CUBICASA5K_COMMAND` points to a working local CubiCasa5K-compatible parser. Even then, structural walls, wall materials, plumbing stacks, shafts, and legal feasibility remain inferred or unknown unless explicitly marked in the source plan.
- Warsaw WMS noise/planning layers are queried through GetFeatureInfo and can be scale-sensitive; an empty response is not proof that a condition is absent.
- Location quality depends on address visibility in the listing or notes. Street-only addresses can still geocode to an approximate point; add a building number or configure `GOOGLE_MAPS_API_KEY` for stronger validation. OpenStreetMap is used as a no-key fallback.
- RCN transactional price data is not integrated in this MVP.
