import socket
from io import BytesIO
from urllib.parse import urlparse

import pytest
from fastapi import HTTPException
from PIL import Image, ImageDraw

import app


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


def test_parse_report_extracts_json_object_from_text():
    result = app.parse_report('prefix {"verdict": "Dobry wybor", "confidence": 80} suffix')

    assert result["verdict"] == "Dobry wybor"
    assert result["confidence"] == 80
