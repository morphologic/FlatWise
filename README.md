# grasshopper_hackathon

FlatWise is a local web app that helps Polish buyers evaluate developer flat listings. Paste a listing URL, optionally upload a floor plan, and receive a structured skeptical buyer report powered by OpenRouter, with geospatial context from Polish Geoportal services.

## Features

- Chat onboarding to collect listing URL, floor plan, and buyer priorities
- Scrapes developer listing pages for text, images, and JSON-LD addresses
- Accepts floor plan uploads (PNG, JPG, WebP, PDF) with crop and normalization
- OpenRouter analysis: layout, location, price metrics, geoportal assessment, due diligence steps
- Vision support when the configured OpenRouter model accepts images
- Geoportal integration: geocoding (UUG), parcel lookup (ULDK), WMS layers (MPZP, BDOT, GESUT, EGiB), orthophoto map preview
- Optional Google Geocoding for address validation before Geoportal queries
- 3D floor-plan visualization via OpenRouter image models (Nano Banana / Gemini image)
- Buyer onboarding spec (`kart.json`) for preference cards

## Getting started

```powershell
cd e:\Vibecoding\FlatWise
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
```

Add your `OPENROUTER_API_KEY` to `.env`, then run:

```powershell
uvicorn app:app --host 127.0.0.1 --port 8001 --reload
```

Open http://127.0.0.1:8001 in your browser.

Requires Python 3.10 or newer.

## Controls

| Variable | Purpose |
|----------|---------|
| `OPENROUTER_API_KEY` | API key for OpenRouter (required) |
| `OPENROUTER_MODEL` | Chat model for buyer report (default: `openai/gpt-4o-mini`) |
| `OPENROUTER_BASE_URL` | OpenRouter API base URL |
| `NANOBANANA_MODEL` | Image model for 3D floor-plan visualization |
| `GOOGLE_MAPS_API_KEY` | Optional geocoding before Geoportal |
| `APP_URL` | Referer sent to OpenRouter |
| `APP_NAME` | App title sent to OpenRouter |
| `REQUEST_TIMEOUT_SECONDS` | Listing fetch timeout |
| `MAX_UPLOAD_BYTES` | Max floor-plan upload size |

## Known limits

- Some developer sites block scraping or render content client-side only.
- PDF floor plans: text extraction plus rendered page crop; image-heavy PDFs work best as image uploads.
- Location quality depends on address visibility in the listing or notes.
- RCN transactional price data is not integrated in this MVP.
