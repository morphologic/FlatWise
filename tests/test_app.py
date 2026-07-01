import asyncio
import socket
from io import BytesIO
from urllib.parse import urlparse

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

import app

client = TestClient(app.app)


def make_png(width: int = 20, height: int = 20) -> bytes:
    output = BytesIO()
    Image.new("RGB", (width, height), "white").save(output, format="PNG")
    return output.getvalue()


def make_two_room_plan() -> bytes:
    image = Image.new("RGB", (320, 220), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((20, 20, 300, 200), outline="black", width=8)
    draw.line((160, 24, 160, 196), fill="black", width=8)
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def test_onboarding_spec_route_returns_flow():
    response = client.get("/api/onboarding-spec")

    assert response.status_code == 200
    assert response.json()["flow_id"] == "apartment-buyer-onboarding-v1"


def test_listing_preview_route_rejects_invalid_url():
    response = client.post("/api/listing-preview", data={"url": "not-a-url"})

    assert response.status_code == 400


def test_listing_preview_route_uses_scraper_and_floor_plan_lookup(monkeypatch):
    listing = {
        "url": "https://example.com/listing",
        "title": "Test listing",
        "images": [],
        "structured_locations": [],
    }
    linked_floor_plan = {
        "linked_image_url": "https://example.com/plan.png",
        "linked_image_alt": "Rzut",
        "floor_plan_analysis": {"status": "ready"},
    }

    async def fake_extract_listing(url):
        assert url == "https://example.com/listing"
        return listing

    async def fake_find_linked_floor_plan(value):
        assert value is listing
        return linked_floor_plan

    monkeypatch.setattr(app, "validate_listing_url", lambda parsed: None)
    monkeypatch.setattr(app, "extract_listing", fake_extract_listing)
    monkeypatch.setattr(app, "find_linked_floor_plan", fake_find_linked_floor_plan)

    response = client.post("/api/listing-preview", data={"url": "https://example.com/listing"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["listing"]["title"] == "Test listing"
    assert payload["linked_floor_plan"]["linked_image_url"] == "https://example.com/plan.png"


def test_current_analysis_route_builds_response_with_mocked_dependencies(monkeypatch):
    monkeypatch.setattr(app.settings, "openrouter_api_key", "sk-or-" + "x" * 40)
    monkeypatch.setattr(app, "validate_listing_url", lambda parsed: None)

    async def fake_extract_listing(url):
        return {"url": url, "title": "Test", "structured_locations": [], "images": []}

    async def fake_read_uploaded_file(upload):
        return None

    async def fake_find_linked_floor_plan(listing):
        return None

    async def fake_visualization(uploaded_file):
        return {"status": "skipped"}

    async def fake_geo_context(listing, notes, api_key):
        return {"status": "skipped", "address": "", "coordinates": {}}

    async def fake_report(url, listing, notes, uploaded_file, geo_context, api_key):
        return {
            "verdict": "Wymaga dalszego sprawdzenia",
            "confidence": 70,
            "layout_analysis": {"score": 5},
        }

    monkeypatch.setattr(app, "extract_listing", fake_extract_listing)
    monkeypatch.setattr(app, "read_uploaded_file", fake_read_uploaded_file)
    monkeypatch.setattr(app, "find_linked_floor_plan", fake_find_linked_floor_plan)
    monkeypatch.setattr(app, "create_floor_plan_visualization", fake_visualization)
    monkeypatch.setattr(app, "create_geo_context", fake_geo_context)
    monkeypatch.setattr(app, "create_openrouter_report", fake_report)

    response = client.post(
        "/api/current-analysis",
        data={"url": "https://example.com/listing", "notes": "quiet"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["schema_version"] == "flatwise.current_analysis_response.v1"
    assert payload["current_analysis"]["stage"] == "current_analysis"
    assert payload["current_analysis"]["scores"]["layout"] == 5


def test_validate_listing_url_blocks_localhost(monkeypatch):
    monkeypatch.setattr(app.settings, "allow_private_listing_urls", False)

    with pytest.raises(HTTPException) as exc:
        app.validate_listing_url(urlparse("http://localhost/listing"))

    assert exc.value.status_code == 400


def test_validate_listing_url_blocks_private_ip(monkeypatch):
    monkeypatch.setattr(app.settings, "allow_private_listing_urls", False)

    with pytest.raises(HTTPException) as exc:
        app.validate_listing_url(urlparse("http://192.168.1.10/listing"))

    assert exc.value.status_code == 400


def test_validate_listing_url_blocks_hostname_resolving_to_private_ip(monkeypatch):
    monkeypatch.setattr(app.settings, "allow_private_listing_urls", False)

    def fake_getaddrinfo(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]

    monkeypatch.setattr(app.socket, "getaddrinfo", fake_getaddrinfo)

    with pytest.raises(HTTPException) as exc:
        app.validate_listing_url(urlparse("https://example.test/listing"))

    assert exc.value.status_code == 400


def test_validate_listing_url_allows_public_hostname(monkeypatch):
    monkeypatch.setattr(app.settings, "allow_private_listing_urls", False)

    def fake_getaddrinfo(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

    monkeypatch.setattr(app.socket, "getaddrinfo", fake_getaddrinfo)

    app.validate_listing_url(urlparse("https://example.com/listing"))


def test_prepare_uploaded_plan_image_rejects_too_many_pixels(monkeypatch):
    monkeypatch.setattr(app.settings, "max_image_pixels", 100)

    with pytest.raises(HTTPException) as exc:
        app.prepare_uploaded_plan_image(make_png())

    assert exc.value.status_code == 413


def test_prepare_uploaded_plan_image_accepts_small_image(monkeypatch):
    monkeypatch.setattr(app.settings, "max_image_pixels", 10_000)

    result = app.prepare_uploaded_plan_image(make_png())

    assert result["mime_type"] == "image/png"
    assert result["data"]


def test_analyze_floor_plan_geometry_detects_room_like_spaces():
    result = app.analyze_floor_plan_geometry(make_two_room_plan())

    assert result["status"] == "ready"
    assert result["method"] == "local_floor_plan_geometry_v1"
    assert result["estimated_room_like_spaces"] >= 2
    assert result["room_candidates"]
    assert result["wall_run_summary"]["vertical_long_runs"] > 0


def test_analyze_floor_plan_geometry_handles_blank_image():
    result = app.analyze_floor_plan_geometry(make_png(120, 120))

    assert result["status"] == "ready"
    assert result["estimated_room_like_spaces"] == 0


def test_is_likely_linked_floor_plan_uses_geometry_signal():
    analysis = app.analyze_floor_plan_geometry(make_two_room_plan())
    linked = {"floor_plan_analysis": analysis}

    assert app.is_likely_linked_floor_plan(linked)


def test_is_likely_linked_floor_plan_rejects_photo_like_dense_signal():
    linked = {
        "floor_plan_analysis": {
            "status": "ready",
            "ink_ratio": 0.92,
            "estimated_room_like_spaces": 3,
            "wall_run_summary": {
                "horizontal_long_runs": 100,
                "vertical_long_runs": 100,
            },
        }
    }

    assert not app.is_likely_linked_floor_plan(linked)


def test_is_likely_linked_floor_plan_rejects_photo_like_visual_profile():
    linked = {
        "floor_plan_analysis": {
            "status": "ready",
            "ink_ratio": 0.32,
            "estimated_room_like_spaces": 4,
            "visual_profile": {
                "photo_like": True,
                "light_background_ratio": 0.03,
                "white_background_ratio": 0.01,
            },
            "wall_run_summary": {
                "horizontal_long_runs": 120,
                "vertical_long_runs": 120,
            },
        }
    }

    assert not app.is_likely_linked_floor_plan(linked, text_score=3)


def test_floor_plan_score_ignores_generic_apartment_words():
    assert app.floor_plan_score("https://example.com/photo.jpg", "Mieszkanie lokal apartament") == 0
    assert app.floor_plan_score("https://example.com/rzut.jpg", "Karta lokalu") >= 2


def test_build_orientation_evidence_extracts_listing_exposure():
    listing = {
        "title": "Mieszkanie z balkonem",
        "description": "Ekspozycja południowo-zachodnia, duże okna i balkon od cichej strony.",
        "text": "",
        "images": [{"url": "https://example.com/rzut-poludnie.png", "alt": "Rzut mieszkania, okna na zachód"}],
    }

    evidence = app.build_orientation_evidence(listing)

    assert evidence["status"] == "found"
    assert "S" in evidence["directions"]
    assert "W" in evidence["directions"]
    assert evidence["matches"]


def test_build_orientation_evidence_ignores_unrelated_direction_words():
    listing = {
        "title": "Mieszkanie przy ulicy Zachodniej",
        "description": "Blisko przystanku i sklepów.",
        "text": "",
        "images": [],
    }

    evidence = app.build_orientation_evidence(listing)

    assert evidence["status"] == "not_found"
    assert evidence["directions"] == []


def test_normalize_floor_plan_orientation_review_keeps_compass_fields():
    review = app.normalize_floor_plan_orientation_review(
        {
            "status": "confirmed",
            "north_on_page": "up-right",
            "directions": ["SW"],
            "summary": "Kompas jest widoczny, okna są na lewej ścianie.",
            "confidence": 82,
            "evidence": ["kompas w prawym górnym rogu", "okna na lewej elewacji"],
        }
    )

    assert review["status"] == "confirmed"
    assert review["north_on_page"] == "up-right"
    assert review["directions"] == ["SW"]
    assert review["confidence"] == 82


def test_apply_orientation_review_removes_false_missing_exposure_claim():
    report = {
        "layout_analysis": {
            "missing_information": [
                "Brak informacji o ekspozycji okien, co ma znaczenie dla nasłonecznienia.",
                "Brak wymiarów ścian.",
            ]
        },
        "orientation_analysis": {
            "status": "not_found",
            "missing_information": ["W rzucie nie zaznaczono ekspozycji okien."],
        },
        "facts_found": [],
        "source_notes": [],
    }
    review = {
        "status": "confirmed",
        "north_on_page": "up-right",
        "directions": ["SW"],
        "confidence": 82,
        "evidence": ["kompas w prawym górnym rogu"],
    }

    result = app.apply_orientation_review_to_report(report, review)

    assert result["orientation_analysis"]["status"] == "confirmed"
    assert result["orientation_analysis"]["directions"] == ["SW"]
    assert result["layout_analysis"]["missing_information"] == ["Brak wymiarów ścian."]
    assert "nie zaznaczono ekspozycji" not in " ".join(result["orientation_analysis"]["missing_information"]).lower()


def test_polish_floor_numbering_treats_kondygnacja_5_as_pietro_4():
    report = {
        "layout_analysis": {
            "risks": [
                "W rzucie jest zapisane 'piętro 4', natomiast w ofercie 'piętro 5'.",
                "Mała łazienka.",
            ],
            "missing_information": [
                "W rzucie jest zapisane 'kondygnacja 5', natomiast w ofercie 'piętro 4'."
            ],
        },
        "facts_found": [],
    }

    result = app.apply_polish_floor_numbering_rules(report)

    assert result["layout_analysis"]["risks"] == ["Mała łazienka."]
    assert result["layout_analysis"]["missing_information"] == []
    assert any("kondygnacja 5" in fact for fact in result["facts_found"])


def test_build_prompt_treats_compass_as_orientation_evidence():
    prompt = app.build_prompt(
        "https://example.com/listing",
        {"title": "Test", "description": "", "text": "", "images": []},
        "",
        {"floor_plan_analysis": app.analyze_floor_plan_geometry(make_two_room_plan())},
        {"status": "skipped"},
        {
            "status": "confirmed",
            "north_on_page": "up-right",
            "directions": ["SW"],
            "evidence": ["kompas w prawym górnym rogu"],
        },
    )

    assert "Widoczny kompas albo strzałka północy na rzucie JEST dowodem orientacji" in prompt
    assert "nie jest zaznaczona" in prompt
    assert "north_on_page" in prompt


def test_extract_locations_from_jsonld_keeps_partial_address_with_coordinates():
    data = {
        "@type": "Offer",
        "address": {
            "@type": "PostalAddress",
            "addressCountry": "Polska",
            "addressLocality": "Poznan",
            "addressRegion": "wielkopolskie",
            "streetAddress": "ul. Niedzialkowskiego",
        },
        "geo": {
            "@type": "GeoCoordinates",
            "latitude": 52.39884521075,
            "longitude": 16.919566401094,
        },
    }

    locations = app.extract_locations_from_jsonld(data)

    assert locations[0]["address"] == "Poznan, ul. Niedzialkowskiego, wielkopolskie"
    assert locations[0]["partial_address"]
    assert not locations[0]["exact_address"]
    assert locations[0]["lat"] == 52.39884521075
    assert locations[0]["lon"] == 16.919566401094


def test_location_from_listing_coordinates_uses_jsonld_geo():
    listing = {
        "url": "https://example.com/listing",
        "structured_locations": [
            {
                "address": "Poznan, ul. Niedzialkowskiego, wielkopolskie",
                "exact_address": False,
                "partial_address": True,
                "lat": 52.39884521075,
                "lon": 16.919566401094,
                "source": "json_ld",
            }
        ],
    }

    result = app.location_from_listing_coordinates(listing)

    assert result
    assert result["provider"] == "Listing JSON-LD coordinates"
    assert result["address"] == "Poznan, ul. Niedzialkowskiego, wielkopolskie"
    assert not result["exact_address"]
    assert result["x"] > 0
    assert result["y"] > 0


def test_partial_address_candidates_include_structured_location():
    listing = {
        "structured_locations": [
            {
                "address": "Warszawa, ul. Jagiellonska, mazowieckie",
                "partial_address": True,
                "exact_address": False,
            }
        ],
    }

    candidates = app.build_partial_address_candidates(listing, "")

    assert candidates == ["Warszawa, ul. Jagiellonska, mazowieckie"]


def test_select_location_result_prefers_google_for_partial_listing_coordinates():
    listing_result = {
        "provider": "Listing JSON-LD coordinates",
        "address": "Warszawa, ul. Jagiellonska, mazowieckie",
        "exact_address": False,
        "lat": 52.2763989,
        "lon": 21.0118017,
        "x": 1,
        "y": 1,
    }
    google_result = {
        "provider": "Google Geocoding API",
        "address": "Jagiellonska, Warszawa",
        "lat": 52.256,
        "lon": 21.02,
        "x": 2,
        "y": 2,
    }

    selected = app.select_location_result(listing_result, google_result)

    assert selected["provider"] == "Google Geocoding API"
    assert selected["validated_listing_coordinates"]
    assert selected["distance_from_listing_coordinates_m"] > 0


def test_select_location_result_rejects_partial_listing_coordinates_without_validation():
    listing_result = {
        "provider": "Listing JSON-LD coordinates",
        "address": "Warszawa, ul. Jagiellonska, mazowieckie",
        "exact_address": False,
        "lat": 52.2763989,
        "lon": 21.0118017,
        "x": 1,
        "y": 1,
    }

    selected = app.select_location_result(listing_result, None)

    assert selected is None


def test_location_validation_warns_on_conflict():
    listing_result = {
        "provider": "Listing JSON-LD coordinates",
        "address": "A",
        "exact_address": False,
        "lat": 52.2763989,
        "lon": 21.0118017,
    }
    google_result = {
        "provider": "Google Geocoding API",
        "address": "B",
        "lat": 52.2297,
        "lon": 21.0122,
        "raw": {"location_type": "GEOMETRIC_CENTER"},
    }

    validation = app.build_location_validation(listing_result, google_result, None, google_result)

    assert validation["status"] == "conflict"
    assert validation["distance_m"] > 250
    assert validation["warnings"]


def test_location_validation_supports_osm_only():
    osm_result = {
        "provider": "OpenStreetMap Nominatim",
        "address": "ul. Jagiellonska, Pelcowizna, Praga-Polnoc, Warszawa, mazowieckie",
        "display_address": "ul. Jagiellonska, Pelcowizna, Praga-Polnoc, Warszawa, mazowieckie",
        "geocoder_address": "Jagiellonska, Pelcowizna, Warszawa",
        "approximate": True,
        "lat": 52.256,
        "lon": 21.02,
        "osm_url": "https://www.openstreetmap.org/",
        "raw": {"category": "highway"},
    }

    validation = app.build_location_validation(None, None, osm_result, osm_result)

    assert validation["status"] == "osm_only"
    assert validation["osm_geocoding"]["provider"] == "OpenStreetMap Nominatim"
    assert validation["osm_geocoding"]["display_address"].startswith("ul. Jagiellonska")


def test_osm_query_variants_reorder_polish_street_address():
    variants = app.osm_query_variants_v2("Warszawa, ul. Jagiellonska, mazowieckie")

    assert "ulica Jagiellonska, Warszawa" in variants


def test_choose_osm_result_prefers_street_over_named_stop():
    results = [
        {
            "display_name": "Ulica Goledzinowska, 70, Jagiellonska, Warszawa",
            "category": "highway",
            "type": "bus_stop",
            "osm_type": "node",
            "importance": 0.1,
        },
        {
            "display_name": "Jagiellonska, Pelcowizna, Praga-Polnoc, Warszawa",
            "category": "highway",
            "type": "residential",
            "osm_type": "way",
            "importance": 0.1,
        },
    ]

    selected = app.choose_osm_result(results, "ulica Jagiellonska, Warszawa")

    assert selected["type"] == "residential"


def test_osm_result_rejects_point_information_for_street_only_query():
    result = {
        "display_name": "Ulica Goledzinowska, 70, Jagiellonska, Warszawa",
        "category": "tourism",
        "type": "information",
        "osm_type": "node",
    }

    assert not app.osm_result_acceptable_for_query(result, "ulica Jagiellonska, Warszawa")


def test_lat_lon_to_tile_fraction_returns_positive_values():
    x, y = app.lat_lon_to_tile_fraction(52.2297, 21.0122, 14)

    assert x > 0
    assert y > 0


def test_parse_report_extracts_json_object_from_text():
    result = app.parse_report('prefix {"verdict": "Dobry wybor", "confidence": 80} suffix')

    assert result["verdict"] == "Dobry wybor"
    assert result["confidence"] == 80


def test_parse_report_handles_json_code_fence():
    result = app.parse_report('```json\n{"verdict": "OK", "confidence": 90}\n```')

    assert result["verdict"] == "OK"
    assert result["confidence"] == 90


def test_parse_report_handles_multimodal_content_blocks():
    result = app.parse_report(
        [
            {"type": "text", "text": '```json\n{"verdict": "OK", "confidence": 88}\n```'},
        ]
    )

    assert result["verdict"] == "OK"
    assert result["confidence"] == 88


def test_choice_message_text_extracts_list_content():
    data = {
        "choices": [
            {
                "message": {
                    "content": [
                        {"type": "text", "text": '{"verdict": "OK"}'},
                    ]
                }
            }
        ]
    }

    assert app.choice_message_text(data) == '{"verdict": "OK"}'


def test_parse_report_reports_empty_model_response():
    result = app.parse_report("")

    assert result["confidence"] == 25
    assert result["parse_error"] == "Model returned an empty response."


def test_repair_report_json_returns_fallback_on_empty_response():
    result = asyncio.run(app.repair_report_json("", "sk-or-" + "x" * 40))

    assert result["confidence"] == 25
    assert result["parse_error"] == "Model returned an empty response."


def test_structured_fallback_report_keeps_scores_after_parse_failure():
    floor_plan_analysis = app.analyze_floor_plan_geometry(make_two_room_plan())
    result = app.build_structured_fallback_report(
        "https://example.com/listing",
        {"title": "Test listing"},
        "",
        {"floor_plan_image_data_url": "data:image/png;base64,abc", "floor_plan_analysis": floor_plan_analysis},
        {"status": "partial", "warnings": [], "service_status": [{"service": "Location", "status": "ok"}]},
        {"status": "confirmed", "directions": ["SW"], "confidence": 80, "evidence": ["kompas"]},
        {"parse_error": "Could not parse model response as a JSON object.", "raw_response": "not json"},
    )

    assert result["confidence"] == 45
    assert result["layout_analysis"]["score"] == 5
    assert result["location_analysis"]["score"] == 6
    assert result["orientation_analysis"]["directions"] == ["SW"]
    assert result["parse_error"] == "Could not parse model response as a JSON object."


def test_build_floor_plan_profile_marks_cubicasa_limits():
    analysis = app.analyze_floor_plan_geometry(make_two_room_plan())

    profile = app.build_floor_plan_profile(analysis)

    assert profile["status"] == "ready"
    assert profile["detected"]["room_like_spaces"] >= 2
    assert profile["detected"]["cubicasa5k"]["status"] == "not_configured"
    assert profile["inferred_constraints"]["possible_structural_walls"]["status"] == "unknown"


def test_is_warsaw_location_uses_city_bounds():
    assert app.is_warsaw_location(52.2297, 21.0122)
    assert not app.is_warsaw_location(52.3988, 16.9196)


def test_parse_warsaw_wfs_features_extracts_distance():
    xml = """
    <wfs:FeatureCollection xmlns:wfs="http://www.opengis.net/wfs" xmlns:gml="http://www.opengis.net/gml">
      <gml:featureMember>
        <wfs:METRO_WEJSCIA>
          <wfs:OBJECTID>1</wfs:OBJECTID>
          <wfs:NAZWA>Metro Centrum</wfs:NAZWA>
          <wfs:SHAPE><gml:Point><gml:pos>21.0122 52.2297</gml:pos></gml:Point></wfs:SHAPE>
        </wfs:METRO_WEJSCIA>
      </gml:featureMember>
    </wfs:FeatureCollection>
    """

    features = app.parse_warsaw_wfs_features(xml, 52.2297, 21.0122)

    assert len(features) == 1
    assert features[0]["properties"]["NAZWA"] == "Metro Centrum"
    assert features[0]["distance_m"] == 0


def test_build_current_analysis_schema_is_stable():
    schema = app.build_current_analysis_schema(
        "https://example.com/listing",
        "quiet, resale",
        {"title": "Test", "structured_locations": []},
        {"confidence": 70, "layout_analysis": {"score": 6}},
        {"floor_plan_analysis": app.analyze_floor_plan_geometry(make_two_room_plan())},
        {"status": "ready", "address": "Warszawa", "coordinates": {"wgs84": {"lat": 52.2, "lon": 21.0}}},
        None,
    )

    assert schema["schema_version"] == "flatwise.current_analysis.v1"
    assert schema["stage"] == "current_analysis"
    assert schema["scores"]["layout"] == 6
    assert schema["orientation_profile"]["evidence"]["status"] == "not_found"


def test_should_use_browser_gallery_scan_when_gallery_hint_and_no_static_plan(monkeypatch):
    monkeypatch.setattr(app.settings, "enable_playwright_gallery_scan", True)

    assert app.should_use_browser_gallery_scan("Galeria Wszystkie zdjecia", [])


def test_should_use_browser_gallery_scan_for_otodom_full_image_labels(monkeypatch):
    monkeypatch.setattr(app.settings, "enable_playwright_gallery_scan", True)
    images = [{"url": "https://example.com/photo.jpg", "alt": "Pelny obrazek: 1/14", "score": 0}]

    assert app.should_use_browser_gallery_scan("", images)


def test_should_not_use_browser_gallery_scan_when_static_floor_plan_found(monkeypatch):
    monkeypatch.setattr(app.settings, "enable_playwright_gallery_scan", True)
    images = [{"url": "https://example.com/rzut.jpg", "alt": "Rzut mieszkania", "score": 3}]

    assert not app.should_use_browser_gallery_scan("Galeria", images)


def test_merge_image_candidates_prefers_browser_scan_for_duplicate_url():
    static_images = [{"url": "https://example.com/plan.jpg", "alt": "", "score": 1}]
    browser_images = [
        {"url": "https://example.com/plan.jpg", "alt": "Rzut", "score": 4},
        {"url": "https://example.com/photo.jpg", "alt": "", "score": 1},
    ]

    merged = app.merge_image_candidates(static_images, browser_images)

    assert merged[0]["score"] == 4
    assert [image["url"] for image in merged] == [
        "https://example.com/plan.jpg",
        "https://example.com/photo.jpg",
    ]


def test_normalize_browser_image_url_accepts_relative_and_rejects_embedded_data():
    assert (
        app.normalize_browser_image_url("https://example.com/listing", "/assets/plan.webp")
        == "https://example.com/assets/plan.webp"
    )
    assert app.normalize_browser_image_url("https://example.com/listing", "data:image/png;base64,abc") is None


def test_normalize_floor_plan_layout_review_fills_missing_fields():
    review = app.normalize_floor_plan_layout_review({}, {"estimated_room_like_spaces": 2})

    assert review["status"] == "low_confidence"
    assert review["confidence"] == 25
    assert "2" in review["summary"]
    assert review["risks"] == []


def test_normalize_floor_plan_layout_review_repairs_zero_ready_confidence():
    review = app.normalize_floor_plan_layout_review(
        {"status": "ready", "summary": "OK", "confidence": 0},
        {},
    )

    assert review["confidence"] == 60


def test_build_floor_plan_module_response_marks_found_source():
    analysis = app.analyze_floor_plan_geometry(make_two_room_plan())
    linked = {
        "linked_image_url": "https://example.com/plan.jpg",
        "linked_image_alt": "Rzut",
        "floor_plan_image_data_url": "data:image/png;base64,abc",
        "floor_plan_image_name": "plan.png",
        "floor_plan_analysis": analysis,
    }

    response = app.build_floor_plan_module_response(
        {"images": [], "browser_gallery_status": {"status": "ready"}},
        linked,
        linked,
        {"status": "ready", "summary": "OK", "confidence": 80},
    )

    assert response["schema_version"] == "flatwise.floor_plan_module.v1"
    assert response["acquisition"]["status"] == "found"
    assert response["acquisition"]["source"] == "listing_linked_floor_plan"
    assert response["floor_plan_analysis"]["status"] == "ready"
