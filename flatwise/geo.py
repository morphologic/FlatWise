import asyncio
import base64
import json
import math
import re
import xml.etree.ElementTree as ET
from io import BytesIO
from typing import Any

import httpx
from PIL import Image, ImageDraw

from config import settings
from flatwise.constants import (
    EPSG2180_TO_WGS84,
    GEOPORTAL_SERVICES,
    HTTPX_VERIFY,
    WARSAW_NOISE_WMS_LAYERS,
    WARSAW_SERVICES,
    WARSAW_WFS_LAYERS,
    WARSAW_WGS84_BBOX,
    WGS84_TO_EPSG2180,
)
from flatwise.openrouter import post_openrouter_chat_completion
from flatwise.text import choice_message_text, clean_text, dedupe_preserve_order, nested_get, parse_float, parse_report


async def create_geo_context(
    listing: dict[str, Any],
    notes: str,
    api_key: str,
) -> dict[str, Any]:
    context: dict[str, Any] = {
        "status": "not_started",
        "address": None,
        "address_candidates": [],
        "listing_locations": listing.get("structured_locations", [])[:8],
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
        "warsaw_context": None,
        "buffers": build_buffer_summary(),
        "intersections": [],
        "service_status": [],
        "map_image_data_url": None,
        "web_map_image_data_url": None,
        "map_links": {},
        "warnings": [],
    }

    candidates = build_address_candidates(listing, notes)
    partial_candidates = build_partial_address_candidates(listing, notes)
    context["address_candidates"] = dedupe_preserve_order([*candidates, *partial_candidates])[:8]

    listing_geocode_result = location_from_listing_coordinates(listing)
    google_geocode_result = await geocode_google_address_candidates(
        dedupe_preserve_order([*candidates, *partial_candidates])
    )
    osm_geocode_result = None
    if not google_geocode_result:
        osm_geocode_result = await geocode_osm_address_candidates(
            dedupe_preserve_order([*candidates, *partial_candidates])
        )
    web_geocode_result = google_geocode_result or osm_geocode_result
    geocode_result = select_location_result(listing_geocode_result, web_geocode_result)
    context["location_validation"] = build_location_validation(
        listing_geocode_result,
        google_geocode_result,
        osm_geocode_result,
        geocode_result,
    )
    context["warnings"].extend(context["location_validation"].get("warnings", []))

    if not geocode_result:
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
    context["map_links"] = {
        "google_maps": geocode_result.get("google_maps_url") or google_maps_url(lat, lon),
        "openstreetmap": geocode_result.get("osm_url") or osm_url(lat, lon),
    }
    context["service_status"].append(
        {
            "service": "Location geocoding",
            "status": "ok",
            "source": geocode_result.get("source_url") or GEOPORTAL_SERVICES["uug"],
            "query": geocode_result.get("query"),
            "provider": geocode_result.get("provider", "UUG"),
        }
    )
    context["warsaw_context"] = await create_warsaw_context(x, y, lat, lon)
    context["service_status"].append(
        {
            "service": "Warsaw city map",
            "status": context["warsaw_context"].get("status", "unknown"),
            "source": WARSAW_SERVICES["docs"],
        }
    )

    (
        parcel,
        kimp_result,
        kiskzp_result,
        bdot_result,
        gesut_result,
        egib_result,
    ) = await asyncio.gather(
        lookup_parcel_by_xy(x, y),
        query_wms_feature_info("KIMP MPZP", GEOPORTAL_SERVICES["kimp"], ["plany", "granice"], x, y),
        query_wms_feature_info("KISKZP studium", GEOPORTAL_SERVICES["kiskzp"], ["studium", "granice"], x, y),
        query_wms_feature_info("BDOT", GEOPORTAL_SERVICES["bdot"], ["bdot10k", "bdot500"], x, y),
        query_wms_feature_info("GESUT", GEOPORTAL_SERVICES["gesut"], ["gesut"], x, y),
        query_wms_feature_info("EGiB", GEOPORTAL_SERVICES["egib"], ["dzialki", "budynki"], x, y),
    )
    context["parcel"] = parcel
    context["service_status"].append(
        {
            "service": "ULDK parcel",
            "status": "ok" if parcel else "no_match",
            "source": GEOPORTAL_SERVICES["uldk"],
        }
    )

    planning = [kimp_result, kiskzp_result]
    context["planning_context"] = planning

    physical = [
        bdot_result,
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
        gesut_result,
        egib_result,
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

    web_map_image = await create_osm_map_image(lat, lon)
    if web_map_image:
        context["web_map_image_data_url"] = web_map_image
        context["service_status"].append(
            {
                "service": "OpenStreetMap preview",
                "status": "ok",
                "source": "https://tile.openstreetmap.org",
            }
        )

    map_image = None
    should_draw_geoportal_map = geocode_result.get("exact_address") or context["location_validation"].get("status") in {
        "google_only",
        "osm_only",
        "matches",
    }
    if should_draw_geoportal_map:
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
                "status": "error" if should_draw_geoportal_map else "skipped_unverified_location",
                "source": GEOPORTAL_SERVICES["orto"],
            }
        )

    if parcel or any(item.get("status") == "ok" for item in planning + infrastructure):
        context["status"] = "ready"
    return context


async def create_warsaw_context(x: float, y: float, lat: float, lon: float) -> dict[str, Any]:
    if not is_warsaw_location(lat, lon):
        return {
            "status": "not_warsaw",
            "summary": "Coordinates are outside Warsaw city map service bounds.",
            "source": WARSAW_SERVICES["docs"],
        }

    layer_tasks = [
        query_warsaw_wfs_layer(layer["theme"], layer["layer"], layer["label"], lat, lon, layer["radius_m"])
        for layer in WARSAW_WFS_LAYERS
    ]
    noise_task = query_wms_feature_info(
        "Warsaw noise map 2022",
        WARSAW_SERVICES["wms"],
        WARSAW_NOISE_WMS_LAYERS,
        x,
        y,
    )
    planning_wms_task = query_wms_feature_info(
        "Warsaw MPZP and development decisions",
        WARSAW_SERVICES["wms"],
        [
            "MPZP_PRZEZNACZENIE_TERENU",
            "MPZP_ZAKRESY_OBOWIAZUJACE",
            "MPZP_ZAKRESY_SPORZADZANE",
            "DECYZJE_O_POZWOLENIU_NA_BUDOWE",
            "DECYZJE_O_WARUNKACH_ZABUDOWY",
        ],
        x,
        y,
    )
    results = await asyncio.gather(*layer_tasks, noise_task, planning_wms_task)
    wfs_results = results[: len(layer_tasks)]
    noise = results[-2]
    planning_wms = results[-1]
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in wfs_results:
        grouped.setdefault(item["theme"], []).append(item)

    status = "ready" if any(item.get("status") == "ok" for item in wfs_results + [noise, planning_wms]) else "empty"
    return {
        "status": status,
        "summary": "Warsaw city WFS/WMS layers queried for location context.",
        "source": WARSAW_SERVICES["docs"],
        "coordinates": {"lat": round(lat, 7), "lon": round(lon, 7)},
        "themes": grouped,
        "noise_context": noise,
        "planning_wms_context": planning_wms,
        "warnings": [
            "Point and line city layers are queried by a small bounding box; counts are contextual, not a walking-distance route calculation.",
            "Noise and MPZP WMS GetFeatureInfo can be scale-sensitive; empty responses do not prove absence of a layer.",
        ],
    }


def is_warsaw_location(lat: float, lon: float) -> bool:
    return (
        WARSAW_WGS84_BBOX["min_lat"] <= lat <= WARSAW_WGS84_BBOX["max_lat"]
        and WARSAW_WGS84_BBOX["min_lon"] <= lon <= WARSAW_WGS84_BBOX["max_lon"]
    )


async def query_warsaw_wfs_layer(
    theme: str,
    layer: str,
    label: str,
    lat: float,
    lon: float,
    radius_m: int,
) -> dict[str, Any]:
    min_lon, min_lat, max_lon, max_lat = make_wgs84_bbox(lat, lon, radius_m)
    params = {
        "SERVICE": "WFS",
        "VERSION": "1.1.0",
        "REQUEST": "GetFeature",
        "TYPENAME": layer,
        "SRSNAME": "EPSG:4326",
        "BBOX": f"{min_lon},{min_lat},{max_lon},{max_lat},EPSG:4326",
        "MAXFEATURES": "12",
    }
    try:
        async with httpx.AsyncClient(timeout=18, verify=HTTPX_VERIFY) as client:
            response = await client.get(WARSAW_SERVICES["wfs"], params=params)
        if response.status_code >= 400:
            return {
                "theme": theme,
                "service": label,
                "layer": layer,
                "status": "error",
                "summary": f"Warsaw WFS returned HTTP {response.status_code}.",
                "source": WARSAW_SERVICES["wfs"],
            }
        features = parse_warsaw_wfs_features(response.text, lat, lon)
        return {
            "theme": theme,
            "service": label,
            "layer": layer,
            "status": "ok" if features else "empty",
            "summary": f"Found {len(features)} feature(s) within about {radius_m} m." if features else f"No features in about {radius_m} m.",
            "radius_m": radius_m,
            "source": WARSAW_SERVICES["wfs"],
            "features": features[:8],
        }
    except Exception as exc:
        return {
            "theme": theme,
            "service": label,
            "layer": layer,
            "status": "error",
            "summary": f"Warsaw WFS query failed: {exc}",
            "source": WARSAW_SERVICES["wfs"],
        }


def make_wgs84_bbox(lat: float, lon: float, radius_m: int) -> tuple[float, float, float, float]:
    delta_lat = radius_m / 111_320
    delta_lon = radius_m / max(111_320 * math.cos(math.radians(lat)), 1)
    return (
        round(lon - delta_lon, 7),
        round(lat - delta_lat, 7),
        round(lon + delta_lon, 7),
        round(lat + delta_lat, 7),
    )


def parse_warsaw_wfs_features(text: str, center_lat: float, center_lon: float) -> list[dict[str, Any]]:
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    features = []
    for member in root.iter():
        if xml_local_name(member.tag) != "featureMember":
            continue
        children = list(member)
        feature_node = children[0] if children else None
        if feature_node is None:
            continue
        lon, lat = first_gml_position(feature_node)
        properties = extract_feature_properties(feature_node)
        item: dict[str, Any] = {
            "properties": trim_geo_record(properties),
        }
        if lat is not None and lon is not None:
            item["coordinates"] = {"lat": round(lat, 7), "lon": round(lon, 7)}
            item["distance_m"] = round(haversine_m(center_lat, center_lon, lat, lon))
        features.append(item)
    features.sort(key=lambda item: item.get("distance_m", 10**9))
    return features


def first_gml_position(node: ET.Element) -> tuple[float | None, float | None]:
    for child in node.iter():
        if xml_local_name(child.tag) == "pos" and child.text:
            values = [parse_float(part) for part in child.text.split()]
            if len(values) >= 2 and values[0] is not None and values[1] is not None:
                return values[0], values[1]
    return None, None


def extract_feature_properties(node: ET.Element) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    for child in node.iter():
        name = xml_local_name(child.tag)
        if name in {"featureMember", "SHAPE", "Point", "LineString", "Polygon", "pos", "posList"}:
            continue
        if list(child):
            continue
        text = clean_text(child.text or "")
        if text:
            properties[name] = text
        if len(properties) >= 16:
            break
    return properties


def xml_local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6_371_000
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    value = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * radius * math.atan2(math.sqrt(value), math.sqrt(1 - value))


def google_maps_url(lat: float, lon: float) -> str:
    return f"https://www.google.com/maps/search/?api=1&query={lat},{lon}"


def osm_url(lat: float, lon: float, zoom: int = 16) -> str:
    return f"https://www.openstreetmap.org/?mlat={lat}&mlon={lon}#map={zoom}/{lat}/{lon}"


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


def build_partial_address_candidates(listing: dict[str, Any], notes: str) -> list[str]:
    candidates: list[str] = []
    for location in listing.get("structured_locations") or []:
        address = clean_text(str(location.get("address") or ""))
        if address and location.get("partial_address"):
            candidates.append(address)

    candidates.extend(extract_partial_address_candidates_from_text(notes or ""))
    candidates.extend(extract_partial_address_candidates_from_text(listing.get("text") or ""))
    candidates.extend(extract_partial_address_candidates_from_text(listing.get("description") or ""))
    candidates.extend(extract_partial_address_candidates_from_text(listing.get("title") or ""))
    return dedupe_preserve_order(candidates)[:8]


def location_from_listing_coordinates(listing: dict[str, Any]) -> dict[str, Any] | None:
    for location in listing.get("structured_locations") or []:
        lat = parse_float(location.get("lat"))
        lon = parse_float(location.get("lon"))
        if lat is None or lon is None:
            continue
        x, y = WGS84_TO_EPSG2180.transform(lon, lat)
        address = location.get("address") or "Wspolrzedne z danych strukturalnych oferty"
        return {
            "x": x,
            "y": y,
            "lat": lat,
            "lon": lon,
            "query": address,
            "address": address,
            "provider": "Listing JSON-LD coordinates",
            "source_url": listing.get("url"),
            "exact_address": bool(location.get("exact_address")),
            "raw": {
                "source": location.get("source"),
                "partial_address": location.get("partial_address"),
            },
        }
    return None


def extract_partial_address_candidates_from_text(text: str) -> list[str]:
    candidates: list[str] = []
    if not text:
        return candidates
    normalized = re.sub(r"\s+", " ", text)
    city = infer_city(normalized)
    street_matches = re.findall(
        r"\b(?:ul\.?|al\.?|aleja|plac|pl\.?|os\.?)\s+[A-ZĹĹšĹ»ĹąÄ†ĹĂ“][A-Za-zÄ„Ä†ÄĹĹĂ“ĹšĹąĹ»Ä…Ä‡Ä™Ĺ‚Ĺ„ĂłĹ›ĹşĹĽ .'-]{2,70}",
        normalized,
        flags=re.IGNORECASE,
    )
    for street in street_matches[:8]:
        candidate = normalize_address_candidate(street, city)
        if looks_like_partial_address(candidate):
            candidates.append(candidate)
    return dedupe_preserve_order(candidates)


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
    return has_street and has_number and (has_city or has_postcode or "," in value)


def looks_like_partial_address(value: str) -> bool:
    has_street = bool(re.search(r"\b(ul\.?|al\.?|aleja|plac|pl\.?|os\.?)\b", value, flags=re.IGNORECASE))
    has_city = bool(
        re.search(
            r"\b(Warszawa|KrakĂłw|Krakow|WrocĹ‚aw|Wroclaw|PoznaĹ„|Poznan|GdaĹ„sk|Gdansk|Gdynia|ĹĂłdĹş|Lodz|Katowice|Lublin|RzeszĂłw|Szczecin|Bydgoszcz|BiaĹ‚ystok|Bialystok|ToruĹ„|Torun)\b",
            value,
            flags=re.IGNORECASE,
        )
    )
    has_postcode = bool(re.search(r"\b\d{2}-\d{3}\b", value))
    return has_street and (has_city or has_postcode or "," in value)


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
        text = choice_message_text(data) or "{}"
        parsed = parse_report(text)
        if parsed.get("address") and parsed.get("confidence", 0) >= 0.7:
            return parsed
    except Exception:
        return {}
    return {}


async def geocode_address_candidates(candidates: list[str]) -> dict[str, Any] | None:
    async with httpx.AsyncClient(timeout=12, verify=HTTPX_VERIFY) as client:
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


async def geocode_google_address_candidates(candidates: list[str]) -> dict[str, Any] | None:
    if not settings.google_maps_api_key:
        return None
    async with httpx.AsyncClient(timeout=12, verify=HTTPX_VERIFY) as client:
        for candidate in candidates:
            result = await geocode_with_google_maps(client, candidate)
            if result:
                return result
    return None


async def geocode_osm_address_candidates(candidates: list[str]) -> dict[str, Any] | None:
    headers = {
        "User-Agent": f"{settings.app_name}/1.0 ({settings.app_url})",
        "Accept": "application/json",
    }
    async with httpx.AsyncClient(timeout=14, verify=HTTPX_VERIFY, headers=headers) as client:
        for candidate in candidates:
            for query in osm_query_variants_v2(candidate):
                result = await geocode_with_openstreetmap(client, query)
                if result:
                    result["query"] = candidate
                    result["raw_query"] = query
                    result["address"] = candidate
                    result["display_address"] = candidate
                    result["geocoder_address"] = result.get("geocoder_address") or result.get("raw", {}).get("display_name")
                    result["exact_address"] = looks_like_address(candidate)
                    if not result["exact_address"]:
                        result["approximate"] = True
                    return result
    return None


def osm_query_variants(candidate: str) -> list[str]:
    value = clean_text(candidate)
    if not value:
        return []
    variants = [value]
    variants.append(re.sub(r"\bul\.?", "ulica", value, flags=re.IGNORECASE))
    parts = [part.strip() for part in value.split(",") if part.strip()]
    city = infer_city(value)
    street_match = re.search(
        r"\b(?:ul\.?|ulica|al\.?|aleja|plac|pl\.?|os\.?)\s+[A-ZĹĹšĹ»ĹąÄ†ĹĂ“][A-Za-zÄ„Ä†ÄĹĹĂ“ĹšĹąĹ»Ä…Ä‡Ä™Ĺ‚Ĺ„ĂłĹ›ĹşĹĽ .'-]{2,70}",
        value,
        flags=re.IGNORECASE,
    )
    if city and street_match:
        street = clean_text(street_match.group(0))
        osm_street = re.sub(r"\bul\.?", "ulica", street, flags=re.IGNORECASE)
        variants.append(f"{street}, {city}")
        variants.append(f"{osm_street}, {city}")
    if len(parts) >= 2 and city:
        for part in parts:
            if part != city and looks_like_partial_address(f"{city}, {part}"):
                variants.append(f"{part}, {city}")
    return dedupe_preserve_order([item for item in variants if item])


def osm_query_variants_v2(candidate: str) -> list[str]:
    value = clean_text(candidate)
    if not value:
        return []
    variants = [value, re.sub(r"\bul\.?", "ulica", value, flags=re.IGNORECASE)]
    parts = [part.strip() for part in value.split(",") if part.strip()]
    city = infer_city(value)
    street_match = re.search(
        r"\b(?:ul\.?|ulica|al\.?|aleja|plac|pl\.?|os\.?)\s+[^,;\d]{2,70}",
        value,
        flags=re.IGNORECASE,
    )
    if city and street_match:
        street = clean_text(street_match.group(0))
        osm_street = re.sub(r"\bul\.?", "ulica", street, flags=re.IGNORECASE)
        street_name = clean_text(
            re.sub(r"^(?:ul\.?|ulica|al\.?|aleja|plac|pl\.?|os\.?)\s+", "", street, flags=re.IGNORECASE)
        )
        variants.extend([f"{street}, {city}", f"{osm_street}, {city}", f"{street_name}, {city}"])
        if "Praga" in value:
            variants.extend([f"{street_name}, Praga-Polnoc, {city}", f"{street_name}, Praga-Północ, {city}"])
    if len(parts) >= 2 and city:
        for part in parts:
            has_street_prefix = re.search(r"\b(?:ul\.?|ulica|al\.?|aleja|plac|pl\.?|os\.?)\b", part, flags=re.IGNORECASE)
            if part != city and (looks_like_partial_address(f"{city}, {part}") or has_street_prefix):
                variants.append(f"{part}, {city}")
    return dedupe_preserve_order([item for item in variants if item])


async def geocode_with_openstreetmap(client: httpx.AsyncClient, address: str) -> dict[str, Any] | None:
    try:
        response = await client.get(
            f"{settings.osm_nominatim_base_url.rstrip('/')}/search",
            params={
                "q": address,
                "format": "jsonv2",
                "limit": "5",
                "countrycodes": "pl",
                "addressdetails": "1",
            },
        )
        response.raise_for_status()
        data = response.json()
    except Exception:
        return None

    if not isinstance(data, list) or not data:
        return None
    result = choose_osm_result(data, address)
    if not result:
        return None
    lat = parse_float(result.get("lat"))
    lon = parse_float(result.get("lon"))
    if lat is None or lon is None:
        return None
    x, y = WGS84_TO_EPSG2180.transform(lon, lat)
    return {
        "x": x,
        "y": y,
        "lat": lat,
        "lon": lon,
        "query": address,
        "address": address,
        "display_address": address,
        "geocoder_address": result.get("display_name") or address,
        "exact_address": looks_like_address(address),
        "approximate": not looks_like_address(address),
        "provider": "OpenStreetMap Nominatim",
        "source_url": f"{settings.osm_nominatim_base_url.rstrip('/')}/search",
        "osm_url": osm_url(lat, lon),
        "google_maps_url": google_maps_url(lat, lon),
        "raw": {
            "place_id": result.get("place_id"),
            "osm_type": result.get("osm_type"),
            "osm_id": result.get("osm_id"),
            "category": result.get("category"),
            "type": result.get("type"),
            "importance": result.get("importance"),
            "place_rank": result.get("place_rank"),
            "display_name": result.get("display_name"),
        },
    }


def choose_osm_result(results: list[Any], query: str) -> dict[str, Any] | None:
    candidates = [item for item in results if isinstance(item, dict)]
    if not candidates:
        return None
    candidates = [item for item in candidates if osm_result_acceptable_for_query(item, query)]
    if not candidates:
        return None
    scored = [(score_osm_result(item, query), item) for item in candidates]
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return scored[0][1]


def osm_result_acceptable_for_query(result: dict[str, Any], query: str) -> bool:
    if looks_like_address(query):
        return True
    category = str(result.get("category") or "").lower()
    result_type = str(result.get("type") or "").lower()
    osm_type = str(result.get("osm_type") or "").lower()
    if category in {"tourism", "amenity", "shop", "office"}:
        return False
    if result_type in {"bus_stop", "platform", "stop", "information", "board"}:
        return False
    if osm_type == "node" and category not in {"place"}:
        return False
    return category in {"highway", "place", "boundary"} or result_type in {
        "residential",
        "tertiary",
        "secondary",
        "primary",
        "road",
        "suburb",
        "neighbourhood",
    }


def score_osm_result(result: dict[str, Any], query: str) -> float:
    display = clean_text(str(result.get("display_name") or "")).lower()
    query_norm = clean_text(query).lower()
    score = 0.0
    for token in re.findall(r"[a-ząćęłńóśźżA-ZĄĆĘŁŃÓŚŹŻ]{4,}", query_norm):
        if token in display:
            score += 2
    category = str(result.get("category") or "")
    result_type = str(result.get("type") or "")
    if category in {"highway", "place"}:
        score += 3
    if result_type in {"residential", "tertiary", "secondary", "primary", "road", "suburb", "neighbourhood"}:
        score += 2
    if str(result.get("osm_type") or "").lower() == "way":
        score += 1
    if result.get("importance") is not None:
        try:
            score += float(result["importance"])
        except (TypeError, ValueError):
            pass
    return score


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
        "google_maps_url": google_maps_url(lat, lon),
        "osm_url": osm_url(lat, lon),
        "raw": {
            "place_id": result.get("place_id"),
            "types": result.get("types", [])[:6],
            "location_type": result.get("geometry", {}).get("location_type"),
        },
    }


def select_location_result(
    listing_result: dict[str, Any] | None,
    web_result: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if listing_result and listing_result.get("exact_address"):
        return listing_result
    if web_result:
        result = dict(web_result)
        result["validated_listing_coordinates"] = listing_result is not None
        if listing_result and web_result.get("lat") is not None and web_result.get("lon") is not None:
            result["distance_from_listing_coordinates_m"] = round(
                haversine_m(
                    float(listing_result["lat"]),
                    float(listing_result["lon"]),
                    float(web_result["lat"]),
                    float(web_result["lon"]),
                )
            )
        return result
    if listing_result:
        return None
    return None


def build_location_validation(
    listing_result: dict[str, Any] | None,
    google_result: dict[str, Any] | None,
    osm_result: dict[str, Any] | None,
    selected_result: dict[str, Any] | None,
) -> dict[str, Any]:
    web_result = google_result or osm_result
    web_label = "Google Maps" if google_result else "OpenStreetMap"
    validation: dict[str, Any] = {
        "status": "not_validated",
        "selected_provider": selected_result.get("provider") if selected_result else None,
        "listing_coordinates": summarize_location_result(listing_result),
        "google_geocoding": summarize_location_result(google_result),
        "osm_geocoding": summarize_location_result(osm_result),
        "web_geocoding": summarize_location_result(web_result),
        "distance_m": None,
        "warnings": [],
    }
    if listing_result and web_result:
        distance = round(
            haversine_m(
                float(listing_result["lat"]),
                float(listing_result["lon"]),
                float(web_result["lat"]),
                float(web_result["lon"]),
            )
        )
        validation["distance_m"] = distance
        validation["status"] = "matches" if distance <= 250 else "conflict"
        if distance > 250:
            validation["warnings"].append(
                f"{web_label} i wspolrzedne z oferty roznia sie o okolo {distance} m. "
                "Mapa uzywa wybranego geokodera, ale adres trzeba potwierdzic recznie."
            )
    elif google_result:
        validation["status"] = "google_only"
    elif osm_result:
        validation["status"] = "osm_only"
    elif listing_result:
        validation["status"] = "listing_only"
        if not listing_result.get("exact_address"):
            validation["warnings"].append(
                "Oferta zawiera adres bez numeru budynku i nie zostal zwalidowany przez Google Maps ani OpenStreetMap. "
                "Mapa moze pokazywac niedokladna lokalizacje."
            )
    else:
        validation["status"] = "missing"

    if google_result and not (listing_result and listing_result.get("exact_address")):
        location_type = nested_get(google_result, ["raw", "location_type"])
        if location_type and location_type != "ROOFTOP":
            validation["warnings"].append(
                f"Google Maps zwrocil lokalizacje typu {location_type}; bez numeru budynku to nadal moze byc punkt przyblizony."
            )
    if osm_result and not (listing_result and listing_result.get("exact_address")):
        validation["warnings"].append(
            "OpenStreetMap/Nominatim may return a street or neighbourhood centroid when no building number is available."
        )
    return validation


def summarize_location_result(result: dict[str, Any] | None) -> dict[str, Any] | None:
    if not result:
        return None
    return {
        "provider": result.get("provider"),
        "address": result.get("address"),
        "display_address": result.get("display_address"),
        "geocoder_address": result.get("geocoder_address"),
        "query": result.get("query"),
        "exact_address": result.get("exact_address"),
        "approximate": result.get("approximate"),
        "lat": round(float(result["lat"]), 7) if result.get("lat") is not None else None,
        "lon": round(float(result["lon"]), 7) if result.get("lon") is not None else None,
        "google_maps_url": result.get("google_maps_url"),
        "osm_url": result.get("osm_url"),
        "location_type": nested_get(result, ["raw", "location_type"]),
        "osm_type": nested_get(result, ["raw", "osm_type"]),
        "osm_class": nested_get(result, ["raw", "category"]),
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
        async with httpx.AsyncClient(timeout=12, verify=HTTPX_VERIFY) as client:
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
        async with httpx.AsyncClient(timeout=14, verify=HTTPX_VERIFY) as client:
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

    xml_rows = parse_wms_feature_info_xml(text)
    if xml_rows is not None:
        if not xml_rows:
            return {
                "service": service_name,
                "status": "empty",
                "summary": "Brak obiektow zwroconych w punkcie zapytania.",
                "source": service_url,
            }
        return {
            "service": service_name,
            "status": "ok",
            "summary": f"Usluga zwrocila {len(xml_rows)} obiektow w punkcie zapytania.",
            "source": service_url,
            "features": xml_rows[:5],
        }

    return {
        "service": service_name,
        "status": "ok",
        "summary": clean_text(re.sub(r"<[^>]+>", " ", text))[:500],
        "source": service_url,
    }


def parse_wms_feature_info_xml(text: str) -> list[dict[str, Any]] | None:
    if "<GetFeatureInfo_Result" not in text and "<ROWSET" not in text:
        return None
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None
    rows = []
    for row in root.iter():
        if xml_local_name(row.tag).upper() != "ROW":
            continue
        props: dict[str, Any] = {}
        for child in list(row):
            value = clean_text(child.text or "")
            if value:
                props[xml_local_name(child.tag)] = value
        if props:
            rows.append(trim_geo_record(props))
    return rows


async def create_osm_map_image(lat: float, lon: float, zoom: int = 14) -> str | None:
    tile_size = 256
    center_x, center_y = lat_lon_to_tile_fraction(lat, lon, zoom)
    base_x = math.floor(center_x) - 1
    base_y = math.floor(center_y) - 1
    headers = {"User-Agent": f"{settings.app_name}/1.0 ({settings.app_url})"}
    canvas = Image.new("RGB", (tile_size * 3, tile_size * 3), "#eef2f0")

    try:
        async with httpx.AsyncClient(timeout=18, verify=HTTPX_VERIFY, headers=headers) as client:
            for dx in range(3):
                for dy in range(3):
                    tile_x = base_x + dx
                    tile_y = base_y + dy
                    url = f"https://tile.openstreetmap.org/{zoom}/{tile_x}/{tile_y}.png"
                    response = await client.get(url)
                    if response.status_code >= 400 or not response.content.startswith(b"\x89PNG"):
                        continue
                    tile = Image.open(BytesIO(response.content)).convert("RGB")
                    canvas.paste(tile, (dx * tile_size, dy * tile_size))
    except Exception:
        return None

    pin_x = int((center_x - base_x) * tile_size)
    pin_y = int((center_y - base_y) * tile_size)
    draw_osm_pin(canvas, pin_x, pin_y)
    output = BytesIO()
    canvas.save(output, format="PNG", optimize=True)
    encoded = base64.b64encode(output.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def lat_lon_to_tile_fraction(lat: float, lon: float, zoom: int) -> tuple[float, float]:
    lat = max(min(lat, 85.05112878), -85.05112878)
    scale = 2**zoom
    x = (lon + 180.0) / 360.0 * scale
    lat_rad = math.radians(lat)
    y = (1.0 - math.log(math.tan(lat_rad) + 1 / math.cos(lat_rad)) / math.pi) / 2.0 * scale
    return x, y


def draw_osm_pin(image: Image.Image, x: int, y: int) -> None:
    draw = ImageDraw.Draw(image, "RGBA")
    shadow = (0, 0, 0, 70)
    fill = (20, 28, 44, 255)
    outline = (255, 255, 255, 245)
    draw.ellipse((x - 17, y - 38, x + 17, y - 4), fill=shadow)
    draw.polygon([(x, y + 24), (x - 15, y - 8), (x + 15, y - 8)], fill=shadow)
    draw.ellipse((x - 16, y - 42, x + 16, y - 10), fill=fill, outline=outline, width=3)
    draw.polygon([(x, y + 20), (x - 13, y - 13), (x + 13, y - 13)], fill=fill)
    draw.ellipse((x - 5, y - 31, x + 5, y - 21), fill=outline)
    draw.text((8, image.height - 22), "© OpenStreetMap contributors", fill=(20, 28, 44, 220))


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
            async with httpx.AsyncClient(timeout=18, verify=HTTPX_VERIFY) as client:
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
