import hashlib
import json
import re
from typing import Any

import httpx

from config import settings
from flatwise.constants import HTTPX_VERIFY, NANOBANANA_PROMPT, PRICE_METRICS_GUIDE
from flatwise.floor_plan import ensure_list, summarize_linked_floor_plan
from flatwise.geo import compact_geo_context_for_prompt
from flatwise.openrouter import post_openrouter_chat_completion
from flatwise.text import (
    choice_message_text,
    clean_text,
    parse_report,
    response_text,
    sanitize_openrouter_api_key,
    split_data_url,
)


def build_current_analysis_schema(
    url: str,
    notes: str,
    listing: dict[str, Any],
    report: dict[str, Any],
    uploaded_file: dict[str, Any] | None,
    geo_context: dict[str, Any],
    linked_floor_plan: dict[str, Any] | None,
) -> dict[str, Any]:
    analysis_id = hashlib.sha256(
        json.dumps(
            {
                "url": url,
                "notes": notes,
                "title": listing.get("title"),
                "address": geo_context.get("address"),
            },
            ensure_ascii=True,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:16]
    floor_plan_analysis = uploaded_file.get("floor_plan_analysis") if uploaded_file else None
    orientation_evidence = build_orientation_evidence(listing, uploaded_file)
    return {
        "schema_version": "flatwise.current_analysis.v1",
        "analysis_id": analysis_id,
        "stage": "current_analysis",
        "input": {
            "url": url,
            "notes": notes,
            "floor_plan_source": floor_plan_source_label(uploaded_file, linked_floor_plan),
        },
        "listing_profile": {
            "title": listing.get("title"),
            "description": listing.get("description"),
            "structured_locations": listing.get("structured_locations", [])[:8],
            "address": geo_context.get("address"),
            "coordinates": geo_context.get("coordinates"),
            "orientation_evidence": orientation_evidence,
        },
        "floor_plan_profile": build_floor_plan_profile(floor_plan_analysis),
        "orientation_profile": {
            "evidence": orientation_evidence,
            "assessment": report.get("orientation_analysis"),
        },
        "location_profile": {
            "status": geo_context.get("status"),
            "geoportal": compact_geo_context_for_prompt(geo_context),
            "warsaw_context": geo_context.get("warsaw_context"),
        },
        "baseline_report": report,
        "scores": {
            "overall_confidence": report.get("confidence"),
            "layout": nested_get(report, ["layout_analysis", "score"]),
            "location": nested_get(report, ["location_analysis", "score"]),
            "financial_resale": nested_get(report, ["financial_resale_view", "score"]),
            "geoportal": nested_get(report, ["geoportal_assessment", "score"]),
        },
        "limits": build_analysis_limits(floor_plan_analysis, geo_context),
    }


def build_orientation_evidence(
    listing: dict[str, Any],
    uploaded_file: dict[str, Any] | None = None,
) -> dict[str, Any]:
    text_sources = [
        listing.get("title") or "",
        listing.get("description") or "",
        listing.get("text") or "",
    ]
    for image in listing.get("images", [])[:18]:
        text_sources.append(image.get("alt") or "")
        text_sources.append(image.get("url") or "")
    if uploaded_file:
        text_sources.extend(
            [
                uploaded_file.get("filename") or "",
                uploaded_file.get("floor_plan_image_name") or "",
                uploaded_file.get("linked_image_alt") or "",
                uploaded_file.get("linked_image_url") or "",
                uploaded_file.get("pdf_text") or "",
            ]
        )

    source_text = "\n".join(clean_text(str(value)) for value in text_sources if value)
    matches = extract_orientation_matches(source_text)
    directions = sorted({direction for match in matches for direction in match["directions"]})
    return {
        "status": "found" if matches else "not_found",
        "directions": directions,
        "matches": matches[:12],
        "source_types": ["listing_text", "image_alt_or_url"] + (["floor_plan_file"] if uploaded_file else []),
        "limits": [
            "Text hints may describe marketing exposure rather than verified apartment compass orientation.",
            "Floor-plan image orientation requires a visible north arrow, compass marker, street label, or other directional cue.",
        ],
    }


def extract_orientation_matches(text: str) -> list[dict[str, Any]]:
    if not text:
        return []

    normalized = text.lower()
    direction_patterns = {
        "N": [r"\bpółnoc(?:na|ne|ny|nym|nej)?\b", r"\bpolnoc(?:na|ne|ny|nym|nej)?\b", r"\bnorth\b", r"\bpn\b"],
        "NE": [r"\bpółnocno[- ]wschod", r"\bpolnocno[- ]wschod", r"\bnorth[- ]?east\b", r"\bne\b"],
        "E": [r"\bwschód\b", r"\bwschod(?:nia|nie|ni|nim|niej)?\b", r"\beast\b", r"\bwsch\b"],
        "SE": [r"\bpołudniowo[- ]wschod", r"\bpoludniowo[- ]wschod", r"\bsouth[- ]?east\b", r"\bse\b"],
        "S": [
            r"\bpołudnie\b",
            r"\bpoludnie\b",
            r"\bpołudniow(?:a|e|y|ym|ej)\b",
            r"\bpoludniow(?:a|e|y|ym|ej)\b",
            r"\bsouth\b",
            r"\bpd\b",
        ],
        "SW": [r"\bpołudniowo[- ]zachod", r"\bpoludniowo[- ]zachod", r"\bsouth[- ]?west\b", r"\bsw\b"],
        "W": [r"\bzachód\b", r"\bzachod(?:nia|nie|ni|nim|niej)?\b", r"\bwest\b", r"\bzach\b"],
        "NW": [r"\bpółnocno[- ]zachod", r"\bpolnocno[- ]zachod", r"\bnorth[- ]?west\b", r"\bnw\b"],
    }
    context_markers = [
        "ekspozyc",
        "orientac",
        "stron",
        "okn",
        "balkon",
        "taras",
        "ogród",
        "ogrod",
        "świat",
        "swiat",
        "nasłonecz",
        "naslonecz",
        "window",
        "exposure",
        "orientation",
    ]

    matches: list[dict[str, Any]] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for patterns in direction_patterns.values():
        for pattern in patterns:
            for match in re.finditer(pattern, normalized, flags=re.IGNORECASE):
                start = max(0, match.start() - 90)
                end = min(len(text), match.end() + 90)
                snippet = clean_text(text[start:end])
                snippet_lower = snippet.lower()
                if not any(marker in snippet_lower for marker in context_markers):
                    continue
                directions = tuple(sorted(extract_directions_from_snippet(snippet, direction_patterns)))
                key = (snippet_lower, directions)
                if key in seen:
                    continue
                seen.add(key)
                matches.append(
                    {
                        "directions": list(directions),
                        "snippet": snippet,
                    }
                )
    return matches


def extract_directions_from_snippet(
    snippet: str,
    direction_patterns: dict[str, list[str]],
) -> list[str]:
    snippet_lower = snippet.lower()
    directions = []
    for direction, patterns in direction_patterns.items():
        if any(re.search(pattern, snippet_lower, flags=re.IGNORECASE) for pattern in patterns):
            directions.append(direction)
    return directions


def floor_plan_source_label(
    uploaded_file: dict[str, Any] | None,
    linked_floor_plan: dict[str, Any] | None,
) -> str:
    if linked_floor_plan:
        return "listing_linked_floor_plan"
    if not uploaded_file:
        return "none"
    if uploaded_file.get("pdf_text"):
        return "uploaded_pdf"
    if uploaded_file.get("image_data_url") or uploaded_file.get("floor_plan_image_data_url"):
        return "uploaded_image"
    return "uploaded_unreadable"


def build_floor_plan_profile(floor_plan_analysis: dict[str, Any] | None) -> dict[str, Any]:
    if not floor_plan_analysis:
        return {
            "status": "missing",
            "parser": None,
            "detected": {},
            "inferred_constraints": {},
            "limits": ["No floor plan image was available."],
        }

    cubicasa = floor_plan_analysis.get("cubicasa5k") or {}
    detected = {
        "room_like_spaces": floor_plan_analysis.get("estimated_room_like_spaces"),
        "elongated_spaces": floor_plan_analysis.get("elongated_space_count"),
        "wall_run_summary": floor_plan_analysis.get("wall_run_summary"),
        "room_candidates": floor_plan_analysis.get("room_candidates", []),
        "cubicasa5k": {
            "status": cubicasa.get("status"),
            "walls_count": nested_get(cubicasa, ["raw_counts", "walls"]),
            "doors_count": nested_get(cubicasa, ["raw_counts", "doors"]),
            "windows_count": nested_get(cubicasa, ["raw_counts", "windows"]),
            "rooms_count": nested_get(cubicasa, ["raw_counts", "rooms"]),
            "fixtures_count": nested_get(cubicasa, ["raw_counts", "fixtures"]),
        },
    }
    inferred_constraints = infer_floor_plan_constraints(floor_plan_analysis)
    return {
        "status": floor_plan_analysis.get("status", "unknown"),
        "parser": floor_plan_analysis.get("method"),
        "source": floor_plan_analysis.get("source"),
        "detected": detected,
        "inferred_constraints": inferred_constraints,
        "limits": floor_plan_analysis.get("interpretation_notes", []),
    }


def infer_floor_plan_constraints(floor_plan_analysis: dict[str, Any]) -> dict[str, Any]:
    cubicasa = floor_plan_analysis.get("cubicasa5k") or {}
    fixtures = ensure_list(cubicasa.get("fixtures"))
    fixture_text = " ".join(json.dumps(item, ensure_ascii=False).lower() for item in fixtures[:50])
    wet_keywords = ["toilet", "wc", "sink", "bath", "shower", "kitchen", "stove", "bathtub", "zlew"]
    possible_wet_core = any(keyword in fixture_text for keyword in wet_keywords)
    return {
        "possible_plumbing_core": {
            "status": "inferred" if possible_wet_core else "unknown",
            "basis": "CubiCasa-style fixture labels" if possible_wet_core else "No fixture-level evidence.",
        },
        "possible_shafts": {
            "status": "unknown",
            "basis": "Shafts are not a reliable direct CubiCasa5K label in this adapter.",
        },
        "possible_structural_walls": {
            "status": "unknown",
            "basis": "Wall geometry is visible, but load-bearing status is not encoded in normal floor-plan labels.",
        },
    }


def build_analysis_limits(
    floor_plan_analysis: dict[str, Any] | None,
    geo_context: dict[str, Any],
) -> list[str]:
    limits = []
    if not floor_plan_analysis:
        limits.append("No floor plan was available, so layout alternatives are conceptual.")
    elif (floor_plan_analysis.get("cubicasa5k") or {}).get("status") != "ready":
        limits.append("CubiCasa5K parser is not configured or did not return semantic geometry.")
    if geo_context.get("status") not in {"ready", "partial"}:
        limits.append("Location data is limited because address/geocoding evidence is incomplete.")
    if geo_context.get("warsaw_context", {}).get("status") in {None, "not_warsaw"}:
        limits.append("Warsaw city layers are only queried for coordinates inside Warsaw.")
    return limits


def nested_get(value: dict[str, Any], path: list[str]) -> Any:
    current: Any = value
    for item in path:
        if not isinstance(current, dict):
            return None
        current = current.get(item)
    return current


async def create_openrouter_vision_json(
    prompt: str,
    image_data_url: str,
    api_key: str,
    model: str,
    system_message: str,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_message},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": image_data_url}},
                ],
            },
        ],
        "response_format": {"type": "json_object"},
    }
    data = await post_openrouter_chat_completion(payload, api_key)
    output_text = choice_message_text(data)
    return parse_report(output_text)


async def create_layout_alternatives(
    current_analysis_data: dict[str, Any],
    preferences: str,
    api_key: str,
) -> dict[str, Any]:
    prompt = f"""
Na podstawie aktualnej analizy mieszkania zaproponuj 3 warianty poprawy ukladu.

Aktualna analiza JSON:
{json.dumps(current_analysis_data, ensure_ascii=False, indent=2)}

Dodatkowe preferencje uzytkownika:
{preferences or "Brak"}

Zwracaj wylacznie JSON:
{{
  "alternatives": [
    {{
      "id": "minimal | functional | rental | resale_family",
      "name": "nazwa wariantu po polsku",
      "goal": "cel wariantu",
      "changes": ["konkretna zmiana"],
      "expected_benefits": ["..."],
      "tradeoffs": ["..."],
      "constraints": ["co trzeba potwierdzic: sciany nosne, piony, okna, prawo"],
      "cost_level": "low | medium | high",
      "renovation_risk": "low | medium | high",
      "confidence": 0-100,
      "data_needed": ["brakujace dane"]
    }}
  ]
}}

Nie twierdz, ze sciana jest wyburzalna, jezeli dane nie potwierdzaja konstrukcji. Traktuj piony i sciany nosne jako ograniczenia do weryfikacji.
""".strip()
    return await create_openrouter_json(
        prompt,
        api_key,
        settings.openrouter_reasoning_model,
        "Jestes architektem-analitykiem. Proponujesz ostrozne, konceptualne warianty ukladu mieszkania.",
    )


async def create_floor_plan_layout_review(
    uploaded_file: dict[str, Any] | None,
    notes: str,
    api_key: str,
) -> dict[str, Any]:
    if not uploaded_file:
        return {
            "status": "missing_floor_plan",
            "summary": "Nie znaleziono rzutu do analizy.",
            "confidence": 0,
            "strengths": [],
            "risks": ["Dodaj rzut albo sprawdz, czy strona udostepnia go w galerii."],
            "missing_information": ["Brak obrazu rzutu."],
        }

    floor_plan_analysis = uploaded_file.get("floor_plan_analysis") or {}
    image_data_url = uploaded_file.get("floor_plan_image_data_url")
    if not api_key or not image_data_url:
        return normalize_floor_plan_layout_review(
            {
                "status": "local_only",
                "summary": "Dostepna jest tylko lokalna analiza geometrii, bez interpretacji modelu vision.",
                "confidence": 45 if floor_plan_analysis else 15,
                "strengths": [],
                "risks": [],
                "missing_information": ["Brak klucza API albo obrazu rzutu dla modelu vision."],
            },
            floor_plan_analysis,
        )

    prompt = f"""
Analyze only this apartment floor plan. Ignore resale, location, developer due diligence, and map context.

User priorities:
{notes or "None"}

Local floor-plan preprocessing JSON:
{json.dumps(floor_plan_analysis, ensure_ascii=False, indent=2)}

Return only JSON:
{{
  "status": "ready | low_confidence",
  "summary": "short Polish summary of the layout",
  "confidence": "integer 1-100",
  "detected_layout": {{
    "rooms": ["visible or inferred room"],
    "functional_zones": ["sleeping | cooking | storage | bathroom | entry | balcony | other"],
    "circulation": "short Polish assessment",
    "daylight": "short Polish assessment if windows are visible"
  }},
  "strengths": ["layout strength"],
  "risks": ["layout risk"],
  "missing_information": ["what cannot be known from the plan"],
  "renovation_constraints": ["possible wall/window/plumbing/shaft constraints to verify"]
}}

Rules:
- Write values in Polish.
- Treat local preprocessing as weak evidence only.
- If labels are too small or unclear, say that explicitly.
- Do not claim a wall is load-bearing or removable unless it is visibly labeled.
""".strip()

    try:
        review = await create_openrouter_vision_json(
            prompt,
            image_data_url,
            api_key,
            settings.openrouter_vision_model,
            "You are a cautious architect reviewing apartment floor plans. Return valid JSON only.",
        )
    except Exception as exc:
        review = {
            "status": "vision_error",
            "summary": f"Model vision nie zwrocil poprawnej analizy rzutu: {exc}",
            "confidence": 25,
            "strengths": [],
            "risks": [],
            "missing_information": ["Interpretacja modelu vision nie powiodla sie."],
        }
    return normalize_floor_plan_layout_review(review, floor_plan_analysis)


def normalize_floor_plan_layout_review(
    review: dict[str, Any],
    floor_plan_analysis: dict[str, Any] | None,
) -> dict[str, Any]:
    if not isinstance(review, dict):
        review = {}
    if not review.get("status"):
        review["status"] = "low_confidence"
    if not review.get("summary"):
        room_count = (floor_plan_analysis or {}).get("estimated_room_like_spaces")
        review["summary"] = (
            f"Lokalny skan wykryl okolo {room_count} regionow podobnych do pomieszczen."
            if room_count is not None
            else "Dostepna jest tylko ograniczona analiza rzutu."
        )
    parsed_confidence = parse_float(review.get("confidence"))
    confidence = int(parsed_confidence) if parsed_confidence is not None else 25
    if review.get("status") == "ready" and confidence <= 0:
        confidence = 60
    review["confidence"] = confidence
    review["detected_layout"] = review.get("detected_layout") if isinstance(review.get("detected_layout"), dict) else {}
    for key in ["strengths", "risks", "missing_information", "renovation_constraints"]:
        review[key] = ensure_list(review.get(key))
    return review


def build_floor_plan_module_response(
    listing: dict[str, Any],
    uploaded_file: dict[str, Any] | None,
    linked_floor_plan: dict[str, Any] | None,
    layout_review: dict[str, Any],
) -> dict[str, Any]:
    floor_plan_analysis = uploaded_file.get("floor_plan_analysis") if uploaded_file else None
    found = bool(uploaded_file and uploaded_file.get("floor_plan_image_data_url"))
    return {
        "schema_version": "flatwise.floor_plan_module.v1",
        "stage": "floor_plan_analysis",
        "acquisition": {
            "status": "found" if found else "missing",
            "source": floor_plan_source_label(uploaded_file, linked_floor_plan) if uploaded_file else "none",
            "linked_floor_plan": summarize_linked_floor_plan(linked_floor_plan),
            "browser_gallery_status": listing.get("browser_gallery_status"),
            "image_count": len(listing.get("images") or []),
        },
        "source_image_data_url": uploaded_file.get("floor_plan_image_data_url") if uploaded_file else None,
        "source_image_name": uploaded_file.get("floor_plan_image_name") if uploaded_file else None,
        "floor_plan_analysis": floor_plan_analysis,
        "layout_review": layout_review,
    }


def normalize_alternatives(value: dict[str, Any]) -> list[dict[str, Any]]:
    alternatives = ensure_list(value.get("alternatives"))
    normalized = []
    for index, item in enumerate(alternatives[:4], start=1):
        if not isinstance(item, dict):
            continue
        normalized.append(
            {
                "id": clean_text(str(item.get("id") or f"alternative_{index}")),
                "name": clean_text(str(item.get("name") or f"Wariant {index}")),
                "goal": clean_text(str(item.get("goal") or "")),
                "changes": ensure_list(item.get("changes")),
                "expected_benefits": ensure_list(item.get("expected_benefits")),
                "tradeoffs": ensure_list(item.get("tradeoffs")),
                "constraints": ensure_list(item.get("constraints")),
                "cost_level": item.get("cost_level", "unknown"),
                "renovation_risk": item.get("renovation_risk", "unknown"),
                "confidence": item.get("confidence"),
                "data_needed": ensure_list(item.get("data_needed")),
            }
        )
    return normalized


async def create_layout_comparison(
    current_analysis_data: dict[str, Any],
    selected_alternative: dict[str, Any],
    api_key: str,
) -> dict[str, Any]:
    prompt = f"""
Porownaj obecny uklad mieszkania z wybranym wariantem.

Aktualna analiza:
{json.dumps(current_analysis_data, ensure_ascii=False, indent=2)}

Wybrany wariant:
{json.dumps(selected_alternative, ensure_ascii=False, indent=2)}

Zwracaj wylacznie JSON:
{{
  "recommendation": "wdrozyc | odrzucic | dopracowac | wymaga weryfikacji",
  "summary": "krotkie porownanie po polsku",
  "comparison": [
    {{"criterion": "funkcjonalnosc", "original": "...", "alternative": "...", "winner": "original | alternative | tie | unknown"}},
    {{"criterion": "koszt/ryzyko", "original": "...", "alternative": "...", "winner": "original | alternative | tie | unknown"}}
  ],
  "score_delta": {{
    "functionality": -10,
    "resale": -10,
    "rental": -10,
    "renovation_risk": -10
  }},
  "must_verify_before_action": ["..."],
  "final_next_steps": ["..."]
}}
""".strip()
    return await create_openrouter_json(
        prompt,
        api_key,
        settings.openrouter_reasoning_model,
        "Jestes sceptycznym doradca kupujacego i porownujesz warianty bez nadinterpretacji danych.",
    )


def normalize_layout_comparison(
    current_analysis_data: dict[str, Any],
    selected_alternative: dict[str, Any],
    comparison: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": "flatwise.layout_comparison.v1",
        "stage": "layout_comparison",
        "current_analysis_id": current_analysis_data.get("analysis_id"),
        "selected_alternative": selected_alternative,
        "recommendation": comparison.get("recommendation", "wymaga weryfikacji"),
        "summary": comparison.get("summary", ""),
        "comparison": ensure_list(comparison.get("comparison")),
        "score_delta": comparison.get("score_delta", {}),
        "must_verify_before_action": ensure_list(comparison.get("must_verify_before_action")),
        "final_next_steps": ensure_list(comparison.get("final_next_steps")),
    }


async def create_openrouter_json(
    prompt: str,
    api_key: str,
    model: str,
    system_message: str,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_message},
            {"role": "user", "content": prompt},
        ],
        "response_format": {"type": "json_object"},
    }
    data = await post_openrouter_chat_completion(payload, api_key)
    output_text = choice_message_text(data)
    return parse_report(output_text)


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
    orientation_review = await create_floor_plan_orientation_review(uploaded_file, api_key)
    prompt = build_prompt(url, listing, notes, uploaded_file, geo_context, orientation_review)

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
        "model": settings.openrouter_vision_model if floor_plan_image_data_url else settings.openrouter_reasoning_model,
        "messages": messages,
        "response_format": {"type": "json_object"},
    }

    data = await post_openrouter_chat_completion(payload, api_key)
    output_text = choice_message_text(data)
    report = parse_report(output_text)
    if report.get("raw_response"):
        report = await repair_report_json(output_text, api_key)
    if report.get("raw_response"):
        report = build_structured_fallback_report(
            url,
            listing,
            notes,
            uploaded_file,
            geo_context,
            orientation_review,
            report,
        )
    report = apply_orientation_review_to_report(report, orientation_review)
    return apply_polish_floor_numbering_rules(report)


async def repair_report_json(raw_response: Any, api_key: str) -> dict[str, Any]:
    raw_response = response_text(raw_response)
    if not raw_response.strip():
        return parse_report(raw_response)

    prompt = f"""
Convert the following model response into one valid JSON object matching the FlatWise report schema.
Do not add markdown fences. Do not explain. Preserve useful Polish content where possible.
If a field is missing, fill it with empty arrays or cautious defaults.

Required top-level keys:
verdict, confidence, one_sentence, facts_found, layout_analysis, location_analysis,
orientation_analysis, financial_resale_view, price_metrics_assessment, geoportal_assessment,
deal_breakers, questions_for_developer, due_diligence_next_steps, what_would_change_my_mind, source_notes.

Raw response:
{raw_response[:12000]}
""".strip()

    try:
        repaired = await create_openrouter_json(
            prompt,
            api_key,
            settings.openrouter_reasoning_model,
            "You repair malformed model output into strict JSON. Return JSON only.",
        )
    except Exception:
        return parse_report(raw_response)

    if repaired.get("raw_response"):
        return parse_report(raw_response)
    repaired.setdefault("source_notes", [])
    repaired["source_notes"] = merge_lists(
        repaired.get("source_notes"),
        ["Odpowiedź modelu została automatycznie naprawiona do poprawnego JSON po pierwszej nieustrukturyzowanej odpowiedzi."],
    )
    return repaired


def build_structured_fallback_report(
    url: str,
    listing: dict[str, Any],
    notes: str,
    uploaded_file: dict[str, Any] | None,
    geo_context: dict[str, Any],
    orientation_review: dict[str, Any] | None,
    parse_failure: dict[str, Any],
) -> dict[str, Any]:
    floor_plan_analysis = uploaded_file.get("floor_plan_analysis") if uploaded_file else None
    room_count = floor_plan_analysis.get("estimated_room_like_spaces") if floor_plan_analysis else None
    geometry_status = floor_plan_analysis.get("status") if floor_plan_analysis else "missing"
    geo_status = geo_context.get("status") or "unknown"
    orientation = normalize_floor_plan_orientation_review(orientation_review or {})
    floor_plan_available = bool(uploaded_file and uploaded_file.get("floor_plan_image_data_url"))

    layout_missing = []
    if not floor_plan_available:
        layout_missing.append("Brak rzutu mieszkania do analizy układu.")
    if floor_plan_analysis and (floor_plan_analysis.get("cubicasa5k") or {}).get("status") != "ready":
        layout_missing.append("Brak semantycznego parsera CubiCasa5K; dostępna jest tylko heurystyka geometrii.")

    location_missing = []
    if geo_status not in {"ready", "partial"}:
        location_missing.append("Adres lub geokodowanie nie są wystarczająco pewne dla pełnej oceny lokalizacji.")

    orientation_missing = orientation.get("missing_information") or []
    if orientation.get("status") in {"not_found", "error"} and floor_plan_available:
        orientation_missing = merge_lists(
            orientation_missing,
            ["Nie udało się automatycznie potwierdzić kompasu/ekspozycji; sprawdź oznaczenia na pełnym arkuszu rzutu."],
        )

    return {
        "verdict": "Wymaga dalszego sprawdzenia",
        "confidence": 45 if floor_plan_available else 30,
        "one_sentence": "Model zwrócił niepoprawny JSON, więc pokazuję lokalny raport awaryjny oparty na danych z oferty, rzutu i Geoportalu.",
        "facts_found": [
            f"Analizowana oferta: {listing.get('title') or 'Oferta'}.",
            f"URL: {url}.",
            f"Lokalny skan rzutu: {geometry_status}.",
            *([f"Lokalny skan wykrył około {room_count} regionów podobnych do pomieszczeń."] if room_count is not None else []),
            f"Status kontekstu lokalizacji: {geo_status}.",
        ],
        "layout_analysis": {
            "score": 5 if floor_plan_available else 2,
            "positives": [
                "Rzut został znaleziony i przetworzony lokalnie." if floor_plan_available else "Brak pozytywnych wniosków bez rzutu.",
                *([f"Heurystyka wykryła około {room_count} przestrzeni/pomieszczeń."] if room_count is not None else []),
            ],
            "risks": ["To raport awaryjny po błędzie formatowania odpowiedzi modelu; wnioski układowe są ograniczone."],
            "missing_information": layout_missing,
        },
        "location_analysis": {
            "score": 6 if geo_status in {"ready", "partial"} else 3,
            "positives": [f"Adres/kontekst lokalizacji ma status: {geo_status}."],
            "risks": ensure_list(geo_context.get("warnings"))[:4],
            "missing_information": location_missing,
        },
        "orientation_analysis": {
            "status": orientation.get("status", "not_found"),
            "directions": orientation.get("directions", []),
            "summary": orientation.get("summary") or "Orientacja została oceniona tylko w zakresie dostępnej automatycznej analizy rzutu.",
            "confidence": orientation.get("confidence", 0),
            "north_on_page": orientation.get("north_on_page", "unknown"),
            "evidence": orientation.get("evidence", []),
            "risks": orientation.get("risks", []),
            "missing_information": orientation_missing,
        },
        "financial_resale_view": {
            "score": 4,
            "positives": [],
            "risks": ["Brak pełnej odpowiedzi modelu ogranicza ocenę finansową i odsprzedażową."],
            "missing_information": ["Cena transakcyjna, czynsz, standard, ekspozycja, hałas i porównywalne oferty wymagają osobnej weryfikacji."],
        },
        "price_metrics_assessment": {
            "score": 4,
            "summary": "Raport awaryjny nie wykonuje pełnej oceny hedonicznej ceny.",
            "value_drivers": [],
            "risk_drivers": ["Brak pełnej ustrukturyzowanej odpowiedzi modelu dla metryk ceny."],
            "missing_information": ["Cena, cena za m², czynsz, piętro, ekspozycja, standard budynku i porównywalne transakcje."],
        },
        "geoportal_assessment": {
            "score": 6 if geo_status in {"ready", "partial"} else 3,
            "summary": f"Kontekst przestrzenny ma status: {geo_status}.",
            "value_drivers": [],
            "risk_drivers": ensure_list(geo_context.get("warnings"))[:4],
            "buffers": [str(item) for item in ensure_list(geo_context.get("buffers"))[:4]],
            "service_status": [
                f"{item.get('service')}: {item.get('status')}"
                for item in ensure_list(geo_context.get("service_status"))[:8]
                if isinstance(item, dict)
            ],
            "missing_information": location_missing,
        },
        "deal_breakers": [],
        "questions_for_developer": [
            "Poproś o pełny rzut z czytelnym kompasem/strzałką północy i potwierdzeniem ekspozycji okien.",
            "Potwierdź piętro/kondygnację, czynsz, status parkingu/komórki i standard wykończenia.",
        ],
        "due_diligence_next_steps": [
            "Powtórz analizę po ustabilizowaniu odpowiedzi modelu albo zmień model OpenRouter na stabilniejszy dla JSON.",
            "Ręcznie zweryfikuj kluczowe dane z prospektem informacyjnym i dokumentacją dewelopera.",
        ],
        "what_would_change_my_mind": [
            "Poprawna ustrukturyzowana odpowiedź modelu oraz pełny zestaw danych ceny, opłat, ekspozycji i otoczenia.",
        ],
        "source_notes": [
            "Raport awaryjny został wygenerowany lokalnie, ponieważ odpowiedź modelu nie była poprawnym JSON.",
            f"Błąd parsowania: {parse_failure.get('parse_error', 'unknown')}",
        ],
        "parse_error": parse_failure.get("parse_error"),
        "raw_response": parse_failure.get("raw_response"),
    }


async def create_floor_plan_orientation_review(
    uploaded_file: dict[str, Any] | None,
    api_key: str,
) -> dict[str, Any]:
    image_data_url = orientation_image_data_url(uploaded_file)
    if not image_data_url:
        return {
            "status": "not_found",
            "summary": "No floor-plan image was available for compass/orientation inspection.",
            "confidence": 0,
            "directions": [],
            "evidence": [],
            "missing_information": ["Floor-plan image with compass, north arrow, or exposure labels."],
        }

    prompt = """
Inspect the entire floor-plan image for apartment orientation evidence.

Important:
- Look at the full sheet, including title blocks, margins, corners, legends, and small symbols outside the apartment outline.
- A compass rose, north arrow, or directional symbol on the sheet IS valid orientation evidence even if windows are not separately labeled.
- If a compass/north arrow is visible, determine where north points on the page, then map exterior walls/windows/balcony/terrace edges to compass directions.
- Do not say that window exposure is unmarked when a compass/north arrow is visible and windows/exterior walls can be located.
- If the symbol is too small or ambiguous, say it is inferred/low confidence instead of absent.
- If no compass, north arrow, street alignment, exposure text, or directional label is visible, use status "not_found".

Return only JSON:
{
  "status": "confirmed | inferred | conflicting | not_found",
  "north_on_page": "up | up-right | right | down-right | down | down-left | left | up-left | unknown",
  "directions": ["N | NE | E | SE | S | SW | W | NW"],
  "summary": "short Polish summary of apartment orientation/exposure",
  "confidence": 0-100,
  "evidence": ["what you saw, e.g. compass in top-right corner, north arrow direction, windows on left wall"],
  "risks": ["sunlight/overheating/noise/privacy risks if inferable"],
  "missing_information": ["what would confirm orientation"]
}
""".strip()

    try:
        review = await create_openrouter_vision_json(
            prompt,
            image_data_url,
            api_key,
            settings.openrouter_vision_model,
            "You inspect architectural floor plans. Be precise about visible compass/north-arrow evidence and avoid guessing beyond the image.",
        )
    except Exception as exc:
        return {
            "status": "error",
            "summary": f"Orientation vision pre-check failed: {exc}",
            "confidence": 0,
            "directions": [],
            "evidence": [],
            "missing_information": ["Retry orientation inspection or verify manually from the plan compass."],
        }
    return normalize_floor_plan_orientation_review(review)


def orientation_image_data_url(uploaded_file: dict[str, Any] | None) -> str | None:
    if not uploaded_file:
        return None
    return (
        uploaded_file.get("orientation_image_data_url")
        or uploaded_file.get("image_data_url")
        or uploaded_file.get("floor_plan_image_data_url")
    )


def normalize_floor_plan_orientation_review(review: dict[str, Any]) -> dict[str, Any]:
    status = review.get("status")
    if status not in {"confirmed", "inferred", "conflicting", "not_found", "error"}:
        status = "inferred" if review.get("directions") or review.get("evidence") else "not_found"

    confidence = review.get("confidence")
    try:
        confidence = int(confidence)
    except (TypeError, ValueError):
        confidence = 0 if status in {"not_found", "error"} else 45
    confidence = max(0, min(100, confidence))

    return {
        "status": status,
        "north_on_page": review.get("north_on_page") or "unknown",
        "directions": ensure_list(review.get("directions")),
        "summary": review.get("summary") or "",
        "confidence": confidence,
        "evidence": ensure_list(review.get("evidence")),
        "risks": ensure_list(review.get("risks")),
        "missing_information": ensure_list(review.get("missing_information")),
    }


def apply_orientation_review_to_report(
    report: dict[str, Any],
    orientation_review: dict[str, Any] | None,
) -> dict[str, Any]:
    if not has_compass_orientation_evidence(orientation_review):
        return report

    review = normalize_floor_plan_orientation_review(orientation_review or {})
    orientation = report.get("orientation_analysis")
    if not isinstance(orientation, dict):
        orientation = {}
    if orientation.get("status") in {None, "", "not_found"}:
        orientation["status"] = review["status"]
    if not orientation.get("north_on_page") or orientation.get("north_on_page") == "unknown":
        orientation["north_on_page"] = review["north_on_page"]
    if not orientation.get("directions"):
        orientation["directions"] = review["directions"]
    if not orientation.get("summary"):
        orientation["summary"] = review["summary"] or "Na rzucie wykryto kompas/strzałkę północy; ekspozycję należy oceniać względem tego oznaczenia."
    orientation["confidence"] = max(int(orientation.get("confidence") or 0), review["confidence"])
    orientation["evidence"] = merge_lists(orientation.get("evidence"), review["evidence"])
    orientation["risks"] = scrub_false_missing_orientation_claims(ensure_list(orientation.get("risks")))
    orientation["missing_information"] = scrub_false_missing_orientation_claims(
        ensure_list(orientation.get("missing_information"))
    )
    if not orientation["missing_information"]:
        orientation["missing_information"] = [
            "Do ręcznej weryfikacji pozostaje dokładne przypisanie poszczególnych okien/ścian do kierunków z kompasu."
        ]
    report["orientation_analysis"] = orientation

    for section_name in [
        "layout_analysis",
        "location_analysis",
        "financial_resale_view",
        "price_metrics_assessment",
        "geoportal_assessment",
    ]:
        section = report.get(section_name)
        if not isinstance(section, dict):
            continue
        for key in ["risks", "missing_information", "risk_drivers"]:
            if key in section:
                section[key] = scrub_false_missing_orientation_claims(ensure_list(section.get(key)))

    for key in ["deal_breakers", "questions_for_developer", "due_diligence_next_steps", "what_would_change_my_mind"]:
        if key in report:
            report[key] = scrub_false_missing_orientation_claims(ensure_list(report.get(key)))

    report["facts_found"] = merge_lists(
        report.get("facts_found"),
        ["Na rzucie wykryto kompas/strzałkę północy jako dowód orientacji mieszkania."],
    )
    report["source_notes"] = merge_lists(
        report.get("source_notes"),
        ["Ekspozycja okien nie powinna być oznaczana jako całkowicie brakująca, jeśli kompas jest widoczny na rzucie."],
    )
    return report


def has_compass_orientation_evidence(orientation_review: dict[str, Any] | None) -> bool:
    if not isinstance(orientation_review, dict):
        return False
    status = orientation_review.get("status")
    if status not in {"confirmed", "inferred", "conflicting"}:
        return False
    evidence_text = " ".join(str(item).lower() for item in ensure_list(orientation_review.get("evidence")))
    summary_text = str(orientation_review.get("summary") or "").lower()
    return bool(orientation_review.get("directions")) or any(
        keyword in f"{evidence_text} {summary_text}"
        for keyword in ["kompas", "compass", "północ", "polnoc", "north", "strzał"]
    )


def scrub_false_missing_orientation_claims(items: list[Any]) -> list[Any]:
    output = []
    for item in items:
        text = str(item)
        lowered = text.lower()
        mentions_exposure = any(token in lowered for token in ["ekspozycj", "nasłonecz", "naslonecz", "stron świata", "stron swiata"])
        claims_missing = any(
            token in lowered
            for token in [
                "brak informacji",
                "nie zaznaczono",
                "nie oznaczono",
                "uniemożliwia",
                "uniemozliwia",
                "brak danych",
            ]
        )
        if mentions_exposure and claims_missing:
            continue
        output.append(item)
    return output


def merge_lists(first: Any, second: Any) -> list[Any]:
    output = []
    seen = set()
    for item in [*ensure_list(first), *ensure_list(second)]:
        key = str(item).lower()
        if key in seen:
            continue
        seen.add(key)
        output.append(item)
    return output


def apply_polish_floor_numbering_rules(report: dict[str, Any]) -> dict[str, Any]:
    for section_name in [
        "layout_analysis",
        "location_analysis",
        "financial_resale_view",
        "price_metrics_assessment",
        "geoportal_assessment",
    ]:
        section = report.get(section_name)
        if not isinstance(section, dict):
            continue
        for key in ["risks", "missing_information", "risk_drivers"]:
            if key in section:
                section[key] = scrub_false_floor_conflict_claims(ensure_list(section.get(key)))

    for key in [
        "deal_breakers",
        "questions_for_developer",
        "due_diligence_next_steps",
        "what_would_change_my_mind",
        "source_notes",
    ]:
        if key in report:
            report[key] = scrub_false_floor_conflict_claims(ensure_list(report.get(key)))

    facts = report.get("facts_found")
    report["facts_found"] = merge_lists(
        facts,
        [
            "W polskiej numeracji budynków kondygnacja 5 zwykle odpowiada piętru 4, gdy parter jest liczony jako pierwsza kondygnacja."
        ],
    )
    return report


def scrub_false_floor_conflict_claims(items: list[Any]) -> list[Any]:
    output = []
    for item in items:
        text = str(item)
        lowered = text.lower()
        mentions_floor = "kondygnac" in lowered or "piętr" in lowered or "pietr" in lowered
        mentions_5_and_4 = bool(re.search(r"\b5\b", lowered) and re.search(r"\b4\b", lowered))
        claims_conflict = any(
            token in lowered
            for token in [
                "sprzecz",
                "rozbież",
                "rozbiez",
                "błąd",
                "blad",
                "niezgod",
                "natomiast",
                "różni",
                "rozni",
            ]
        )
        if mentions_floor and mentions_5_and_4 and claims_conflict:
            continue
        output.append(item)
    return output


async def create_floor_plan_visualization(uploaded_file: dict[str, Any] | None) -> dict[str, Any]:
    if not uploaded_file:
        return {
            "status": "skipped",
            "message": "Nie dodano rzutu, więc nie można przygotować wizualizacji 3D.",
        }

    floor_plan_image_data_url = uploaded_file.get("floor_plan_image_data_url")
    if not settings.enable_3d_visualization:
        return {
            "status": "disabled",
            "message": "Wizualizacja 3D jest tymczasowo wylaczona.",
            "source_image_data_url": floor_plan_image_data_url,
            "source": uploaded_file.get("floor_plan_image_source"),
        }
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

    async with httpx.AsyncClient(timeout=90, verify=HTTPX_VERIFY) as client:
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


def build_prompt(
    url: str,
    listing: dict[str, Any],
    notes: str,
    uploaded_file: dict[str, Any] | None,
    geo_context: dict[str, Any],
    orientation_review: dict[str, Any] | None = None,
) -> str:
    orientation_evidence = build_orientation_evidence(listing, uploaded_file)
    uploaded_summary = "Nie dodano rzutu mieszkania."
    if uploaded_file:
        floor_plan_analysis = uploaded_file.get("floor_plan_analysis")
        if uploaded_file.get("linked_image_url"):
            uploaded_summary = (
                "Aplikacja znalazła prawdopodobny rzut mieszkania w obrazach oferty: "
                f"{uploaded_file.get('linked_image_url')}."
            )
        elif uploaded_file.get("image_data_url"):
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

    if uploaded_file and floor_plan_analysis:
        uploaded_summary += (
            "\n\nStructured floor-plan preprocessing JSON "
            "(local heuristic parser, not certified measurements and not CubiCasa weights):\n"
            f"{json.dumps(floor_plan_analysis, ensure_ascii=False, indent=2)}"
        )

    return f"""
Przeanalizuj tę polską ofertę mieszkania od dewelopera dla osoby kupującej.

Adres oferty:
{url}

Notatki i priorytety użytkownika:
{notes or "Brak"}

Dowody pobrane ze strony oferty:
{json.dumps(listing, ensure_ascii=False, indent=2)}

Wstępne dowody o orientacji mieszkania:
{json.dumps(orientation_evidence, ensure_ascii=False, indent=2)}

Wstępna analiza obrazu rzutu pod kątem kompasu i ekspozycji:
{json.dumps(orientation_review or {}, ensure_ascii=False, indent=2)}

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
  "orientation_analysis": {{
    "status": "confirmed | inferred | conflicting | not_found",
    "directions": ["N | NE | E | SE | S | SW | W | NW"],
    "summary": "krótka ocena ekspozycji/orientacji mieszkania",
    "confidence": 0-100,
    "north_on_page": "up | up-right | right | down-right | down | down-left | left | up-left | unknown",
    "evidence": ["cytat lub opis dowodu: tekst oferty, alt obrazu, oznaczenie na rzucie, kompas/strzałka północy"],
    "risks": ["ryzyka związane z nasłonecznieniem, przegrzewaniem, hałasem od konkretnej strony albo niepewnością"],
    "missing_information": ["czego brakuje do potwierdzenia orientacji"]
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
- Orientację mieszkania oceniaj tylko z dowodów: tekstu oferty, alt/URL obrazów, widocznej strzałki północy/kompasu na rzucie, oznaczeń stron świata, nazw ulic lub jednoznacznego opisu ekspozycji. Jeśli nie ma dowodu, ustaw orientation_analysis.status na "not_found".
- Widoczny kompas albo strzałka północy na rzucie JEST dowodem orientacji. Sprawdź także małe symbole w narożnikach, legendzie i marginesach arkusza. Nie pisz, że ekspozycja okien nie jest zaznaczona, jeśli na rzucie widać kompas i da się wskazać ściany z oknami lub balkonem.
- Jeśli kompas wskazuje północ na stronie, przemapuj ściany zewnętrzne, okna, balkon lub taras na kierunki świata. W evidence opisz: gdzie jest kompas, gdzie wskazuje północ na stronie, które ściany/okna uznajesz za zewnętrzne.
- Jeśli dane tekstowe i rzut są sprzeczne albo niejasne, ustaw orientation_analysis.status na "conflicting" lub "inferred" i obniż confidence.
- Nie traktuj jako błędu sytuacji, w której rzut pokazuje "kondygnacja 5" albo podobny zapis, a oferta mówi "piętro 4". W polskiej praktyce parter jest pierwszą kondygnacją, więc kondygnacja 5 zwykle odpowiada 4. piętru. Oznacz to jako normalizację numeracji, nie sprzeczność.
- Uwzględnij polskie checklisty kupującego: KW, umowa deweloperska, rachunek powierniczy, koszty parkingu/komórki, hałas, nasłonecznienie, czynsz/opłaty administracyjne, standard wykończenia i planowane inwestycje w okolicy.
- If structured floor-plan preprocessing JSON is available, use it as weak geometric evidence about possible room-like spaces, elongated/corridor-like spaces, line density, and detection limits. Do not treat it as certified geometry.
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
