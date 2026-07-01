import json
import socket as socket
from typing import Any
from urllib.parse import urlparse

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from config import settings
from flatwise.analysis import (
    apply_orientation_review_to_report,
    apply_polish_floor_numbering_rules,
    build_analysis_limits,
    build_current_analysis_schema,
    build_floor_plan_module_response,
    build_floor_plan_profile,
    build_orientation_evidence,
    build_prompt,
    build_structured_fallback_report,
    create_floor_plan_layout_review,
    create_floor_plan_orientation_review,
    create_floor_plan_visualization,
    create_layout_alternatives,
    create_layout_comparison,
    create_openrouter_json,
    create_openrouter_report,
    create_openrouter_vision_json,
    extract_directions_from_snippet,
    extract_orientation_matches,
    floor_plan_source_label,
    get_response_text,
    infer_floor_plan_constraints,
    nested_get,
    normalize_alternatives,
    normalize_floor_plan_layout_review,
    normalize_floor_plan_orientation_review,
    normalize_layout_comparison,
    repair_report_json,
)
from flatwise.constants import ONBOARDING_SPEC_PATH
from flatwise.floor_plan import (
    analyze_floor_plan_geometry,
    analyze_floor_plan_with_cubicasa5k,
    build_floor_plan_visual_profile,
    collect_dark_runs,
    count_mask_pixels,
    crop_architectural_drawing,
    ensure_list,
    estimate_plan_threshold,
    expand_bbox,
    extract_likely_floor_plan_image,
    extract_pdf_content,
    extract_pdf_text,
    fetch_floor_plan_from_url,
    find_linked_floor_plan,
    find_room_like_components,
    flood_light_component,
    image_mime_type,
    image_to_png_bytes,
    is_likely_linked_floor_plan,
    largest_mask_component_bbox,
    normalize_bbox,
    normalize_cubicasa5k_output,
    normalize_plan_image,
    prepare_uploaded_plan_image,
    read_uploaded_file,
    render_likely_floor_plan_page,
    summarize_linked_floor_plan,
    summarize_wall_runs,
)
from flatwise.geo import (
    build_address_candidates,
    build_buffer_summary,
    build_intersection_summary,
    build_location_validation,
    build_partial_address_candidates,
    choose_osm_result,
    combined_status,
    compact_geo_context_for_prompt,
    create_geo_context,
    create_geoportal_map_image,
    create_osm_map_image,
    create_warsaw_context,
    draw_geo_overlay,
    draw_osm_pin,
    extract_address_candidates_from_text,
    extract_feature_properties,
    extract_partial_address_candidates_from_text,
    first_gml_position,
    first_xy_record,
    geocode_address_candidates,
    geocode_google_address_candidates,
    geocode_osm_address_candidates,
    geocode_with_google_maps,
    geocode_with_openstreetmap,
    google_maps_url,
    haversine_m,
    infer_address_with_openrouter,
    infer_city,
    is_warsaw_location,
    lat_lon_to_tile_fraction,
    location_from_listing_coordinates,
    looks_like_address,
    looks_like_partial_address,
    lookup_parcel_by_xy,
    make_bbox,
    make_wgs84_bbox,
    normalize_address_candidate,
    osm_query_variants,
    osm_query_variants_v2,
    osm_result_acceptable_for_query,
    osm_url,
    parse_point_wkt,
    parse_polygon_wkt,
    parse_uldk_parcel,
    parse_warsaw_wfs_features,
    parse_wms_feature_info_xml,
    point_to_pixel,
    query_warsaw_wfs_layer,
    query_wms_feature_info,
    score_osm_result,
    select_location_result,
    service_status_from_sections,
    summarize_location_result,
    summarize_wms_response,
    trim_geo_record,
    walk_dicts,
    xml_local_name,
)
from flatwise.listing import (
    accept_browser_cookie_banner,
    click_gallery_controls,
    collect_browser_image_candidates,
    dedupe_image_candidates,
    dedupe_locations,
    extract_addresses_from_jsonld,
    extract_listing,
    extract_listing_browser_gallery_images,
    extract_locations_from_jsonld,
    extract_structured_addresses,
    extract_structured_locations,
    format_jsonld_address,
    is_private_network_address,
    merge_image_candidates,
    normalize_browser_image_url,
    parse_jsonld_geo,
    should_use_browser_gallery_scan,
    validate_listing_url,
)
from flatwise.openrouter import post_openrouter_chat_completion
from flatwise.text import (
    assess_extraction_quality,
    choice_message_text,
    clean_text,
    dedupe_preserve_order,
    floor_plan_score,
    parse_float,
    parse_report,
    sanitize_openrouter_api_key,
    split_data_url,
)

app = FastAPI(title="FlatWise MVP")
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
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
        raise HTTPException(status_code=500, detail="Nie uda?o si? wczyta? konfiguracji onboardingu.") from exc


@app.post("/api/listing-preview")
async def listing_preview(url: str = Form(...)) -> dict[str, Any]:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(status_code=400, detail="Podaj poprawny adres URL zaczynajacy sie od http(s).")
    validate_listing_url(parsed)

    listing = await extract_listing(url)
    linked_floor_plan = await find_linked_floor_plan(listing)
    return {
        "listing": listing,
        "linked_floor_plan": summarize_linked_floor_plan(linked_floor_plan),
    }


@app.post("/api/analyze")
async def analyze_flat(
    url: str = Form(...),
    notes: str = Form(""),
    floor_plan: UploadFile | None = File(None),
) -> dict[str, Any]:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(status_code=400, detail="Podaj poprawny adres URL zaczynaj?cy si? od http(s).")

    validate_listing_url(parsed)

    openrouter_api_key = sanitize_openrouter_api_key(settings.openrouter_api_key)
    if not openrouter_api_key:
        raise HTTPException(
            status_code=400,
            detail="Brakuje OPENROUTER_API_KEY. Dodaj klucz do .env i uruchom serwer ponownie.",
        )
    if openrouter_api_key in {"sk-your-key", "sk-or-your-key"} or len(openrouter_api_key) < 32:
        raise HTTPException(
            status_code=400,
            detail="OPENROUTER_API_KEY wygl?da jak placeholder albo jest za kr?tki. Wklej prawdziwy klucz do .env i uruchom serwer ponownie.",
        )

    listing = await extract_listing(url)
    uploaded_file = await read_uploaded_file(floor_plan)
    linked_floor_plan = None
    if not uploaded_file:
        linked_floor_plan = await find_linked_floor_plan(listing)
        uploaded_file = linked_floor_plan
    visualization = await create_floor_plan_visualization(uploaded_file)
    geo_context = await create_geo_context(listing, notes, openrouter_api_key)

    report = await create_openrouter_report(url, listing, notes, uploaded_file, geo_context, openrouter_api_key)
    return {
        "listing": listing,
        "report": report,
        "visualization": visualization,
        "geo_context": geo_context,
        "floor_plan_analysis": uploaded_file.get("floor_plan_analysis") if uploaded_file else None,
        "linked_floor_plan": summarize_linked_floor_plan(linked_floor_plan),
    }


@app.post("/api/current-analysis")
async def current_analysis(
    url: str = Form(...),
    notes: str = Form(""),
    floor_plan: UploadFile | None = File(None),
) -> dict[str, Any]:
    return await run_current_analysis(url, notes, floor_plan)


@app.post("/api/floor-plan-analysis")
async def floor_plan_analysis_only(
    url: str = Form(...),
    notes: str = Form(""),
    floor_plan: UploadFile | None = File(None),
) -> dict[str, Any]:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(status_code=400, detail="Podaj poprawny adres URL zaczynajacy sie od http(s).")
    validate_listing_url(parsed)

    listing = await extract_listing(url)
    uploaded_file = await read_uploaded_file(floor_plan)
    linked_floor_plan = None
    if not uploaded_file:
        linked_floor_plan = await find_linked_floor_plan(listing)
        uploaded_file = linked_floor_plan

    api_key = sanitize_openrouter_api_key(settings.openrouter_api_key)
    layout_review = await create_floor_plan_layout_review(uploaded_file, notes, api_key)
    return build_floor_plan_module_response(listing, uploaded_file, linked_floor_plan, layout_review)


@app.post("/api/layout-alternatives")
async def layout_alternatives(
    current_analysis_json: str = Form(...),
    preferences: str = Form(""),
) -> dict[str, Any]:
    api_key = require_openrouter_api_key()
    current_analysis_data = parse_json_form_value(current_analysis_json, "current_analysis_json")
    alternatives = await create_layout_alternatives(current_analysis_data, preferences, api_key)
    return {
        "schema_version": "flatwise.layout_alternatives.v1",
        "stage": "layout_alternatives",
        "current_analysis_id": current_analysis_data.get("analysis_id"),
        "alternatives": normalize_alternatives(alternatives),
    }


@app.post("/api/compare-layouts")
async def compare_layouts(
    current_analysis_json: str = Form(...),
    selected_alternative_json: str = Form(...),
) -> dict[str, Any]:
    api_key = require_openrouter_api_key()
    current_analysis_data = parse_json_form_value(current_analysis_json, "current_analysis_json")
    selected_alternative = parse_json_form_value(selected_alternative_json, "selected_alternative_json")
    comparison = await create_layout_comparison(current_analysis_data, selected_alternative, api_key)
    return normalize_layout_comparison(current_analysis_data, selected_alternative, comparison)


async def run_current_analysis(
    url: str,
    notes: str,
    floor_plan: UploadFile | None,
) -> dict[str, Any]:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(status_code=400, detail="Podaj poprawny adres URL zaczynajacy sie od http(s).")

    validate_listing_url(parsed)
    openrouter_api_key = require_openrouter_api_key()

    listing = await extract_listing(url)
    uploaded_file = await read_uploaded_file(floor_plan)
    linked_floor_plan = None
    if not uploaded_file:
        linked_floor_plan = await find_linked_floor_plan(listing)
        uploaded_file = linked_floor_plan

    visualization = await create_floor_plan_visualization(uploaded_file)
    geo_context = await create_geo_context(listing, notes, openrouter_api_key)
    report = await create_openrouter_report(url, listing, notes, uploaded_file, geo_context, openrouter_api_key)
    current_analysis_data = build_current_analysis_schema(
        url,
        notes,
        listing,
        report,
        uploaded_file,
        geo_context,
        linked_floor_plan,
    )
    return {
        "schema_version": "flatwise.current_analysis_response.v1",
        "listing": listing,
        "report": report,
        "current_analysis": current_analysis_data,
        "visualization": visualization,
        "geo_context": geo_context,
        "floor_plan_analysis": uploaded_file.get("floor_plan_analysis") if uploaded_file else None,
        "linked_floor_plan": summarize_linked_floor_plan(linked_floor_plan),
    }


def require_openrouter_api_key() -> str:
    openrouter_api_key = sanitize_openrouter_api_key(settings.openrouter_api_key)
    if not openrouter_api_key:
        raise HTTPException(
            status_code=400,
            detail="Brakuje OPENROUTER_API_KEY. Dodaj klucz do .env i uruchom serwer ponownie.",
        )
    if openrouter_api_key in {"sk-your-key", "sk-or-your-key"} or len(openrouter_api_key) < 32:
        raise HTTPException(
            status_code=400,
            detail="OPENROUTER_API_KEY wyglada jak placeholder albo jest za krotki. Wklej prawdziwy klucz do .env i uruchom serwer ponownie.",
        )
    return openrouter_api_key


def parse_json_form_value(value: str, field_name: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"Pole {field_name} nie jest poprawnym JSON.") from exc
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=400, detail=f"Pole {field_name} musi byc obiektem JSON.")
    return parsed
