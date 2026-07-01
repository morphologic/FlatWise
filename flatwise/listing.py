import ipaddress
import json
import re
import socket
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from fastapi import HTTPException

from config import settings
from flatwise.constants import HTTPX_VERIFY
from flatwise.geo import looks_like_address, looks_like_partial_address
from flatwise.text import (
    assess_extraction_quality,
    clean_text,
    dedupe_preserve_order,
    floor_plan_score,
    parse_float,
)


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
            verify=HTTPX_VERIFY,
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=422, detail=f"Nie udało się pobrać oferty: {exc}") from exc

    soup = BeautifulSoup(response.text, "html.parser")
    structured_locations = extract_structured_locations(soup)
    structured_addresses = [
        location["address"]
        for location in structured_locations
        if location.get("address") and location.get("exact_address")
    ]
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
    browser_images: list[dict[str, Any]] = []
    browser_status = {"status": "not_started"}
    if should_use_browser_gallery_scan(page_text, images):
        browser_result = await extract_listing_browser_gallery_images(url)
        browser_images = browser_result.get("images", [])
        browser_status = browser_result.get("status", {"status": "unknown"})
        images = merge_image_candidates(images, browser_images)
        images.sort(key=lambda item: item["score"], reverse=True)

    return {
        "url": url,
        "title": title,
        "description": description,
        "text": page_text,
        "images": images[:18],
        "browser_gallery_images": browser_images[:18],
        "browser_gallery_status": browser_status,
        "structured_locations": structured_locations,
        "structured_addresses": structured_addresses,
        "extraction_quality": assess_extraction_quality(page_text, images),
    }


def extract_structured_addresses(soup: BeautifulSoup) -> list[str]:
    return [
        location["address"]
        for location in extract_structured_locations(soup)
        if location.get("address") and location.get("exact_address")
    ][:8]


def should_use_browser_gallery_scan(page_text: str, images: list[dict[str, Any]]) -> bool:
    if not settings.enable_playwright_gallery_scan:
        return False
    if any(int(image.get("score") or 0) >= 2 for image in images[:12]):
        return False
    text = f"{page_text} {' '.join(image.get('alt') or '' for image in images[:12])}".lower()
    gallery_keywords = ["rzut", "plan", "galeria", "zdjecia", "zdjęcia", "wszystkie", "floor"]
    gallery_keywords.extend(["gallery", "obrazek", "pelny"])
    return any(keyword in text for keyword in gallery_keywords) or len(images) < 4


def merge_image_candidates(
    static_images: list[dict[str, Any]],
    browser_images: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for image in [*browser_images, *static_images]:
        url = image.get("url")
        if not url or url in seen:
            continue
        seen.add(url)
        merged.append(image)
    return merged


async def extract_listing_browser_gallery_images(url: str) -> dict[str, Any]:
    if not settings.enable_playwright_gallery_scan:
        return {"status": {"status": "disabled"}, "images": []}

    try:
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError
        from playwright.async_api import async_playwright
    except Exception as exc:
        return {
            "status": {
                "status": "not_installed",
                "summary": f"Playwright is not installed or browsers are missing: {exc}",
            },
            "images": [],
        }

    images: list[dict[str, Any]] = []
    clicked_labels: list[str] = []
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
                ),
                locale="pl-PL",
            )
            await page.goto(url, wait_until="domcontentloaded", timeout=int(settings.playwright_timeout_seconds * 1000))
            await page.wait_for_timeout(1400)
            images.extend(await collect_browser_image_candidates(page, url))
            await accept_browser_cookie_banner(page)
            clicked_labels.extend(await click_gallery_controls(page))
            await page.wait_for_timeout(1000)
            images.extend(await collect_browser_image_candidates(page, url))
            await browser.close()
    except PlaywrightTimeoutError as exc:
        return {"status": {"status": "timeout", "summary": str(exc)}, "images": dedupe_image_candidates(images)[:24]}
    except Exception as exc:
        return {"status": {"status": "error", "summary": str(exc)}, "images": dedupe_image_candidates(images)[:24]}

    deduped = dedupe_image_candidates(images)
    return {
        "status": {
            "status": "ready",
            "summary": f"Browser gallery scan collected {len(deduped)} image candidates.",
            "clicked_controls": clicked_labels[:8],
        },
        "images": deduped[:24],
    }


async def collect_browser_image_candidates(page: Any, base_url: str) -> list[dict[str, Any]]:
    candidates = await page.evaluate(
        """
        () => {
          const out = [];
          const push = (url, alt, context) => {
            if (!url || typeof url !== "string") return;
            out.push({ url, alt: alt || "", context: context || "" });
          };
          for (const img of document.querySelectorAll("img")) {
            push(img.currentSrc || img.src || img.getAttribute("data-src") || img.getAttribute("data-lazy-src"), img.alt, img.closest("button,a,figure,div")?.innerText || "");
          }
          for (const source of document.querySelectorAll("source[srcset]")) {
            const first = source.srcset.split(",")[0]?.trim()?.split(" ")[0];
            push(first, "", source.closest("picture,button,a,figure,div")?.innerText || "");
          }
          for (const node of document.querySelectorAll("[style*='background-image']")) {
            const style = getComputedStyle(node).backgroundImage || "";
            const match = style.match(/url\\([\"']?([^\"')]+)[\"']?\\)/);
            if (match) push(match[1], "", node.innerText || "");
          }
          for (const node of document.querySelectorAll("[data-src], [data-original], [data-image], [data-full], [href]")) {
            push(node.getAttribute("data-src") || node.getAttribute("data-original") || node.getAttribute("data-image") || node.getAttribute("data-full") || node.getAttribute("href"), node.getAttribute("aria-label") || "", node.innerText || "");
          }
          return out;
        }
        """
    )
    output = []
    for item in candidates or []:
        image_url = normalize_browser_image_url(base_url, item.get("url") or "")
        if not image_url:
            continue
        alt = clean_text(" ".join([item.get("alt") or "", item.get("context") or ""]))[:240]
        output.append(
            {
                "url": image_url,
                "alt": alt,
                "score": floor_plan_score(image_url, alt),
                "source": "playwright_gallery_scan",
            }
        )
    output.sort(key=lambda item: item["score"], reverse=True)
    return output


async def accept_browser_cookie_banner(page: Any) -> str | None:
    labels = ["Akceptuj wszystkie", "Akceptuj niezbedne", "Accept all", "Accept"]
    for label in labels:
        try:
            locator = page.get_by_role("button", name=label, exact=True).first
            if await locator.count():
                await locator.click(timeout=1500)
                await page.wait_for_timeout(500)
                return label
        except Exception:
            continue
    return None


async def click_gallery_controls(page: Any) -> list[str]:
    controls = [
        ("Rzut", lambda: page.get_by_role("button", name="Rzut", exact=True).first),
        ("Floor plan", lambda: page.get_by_role("button", name="Floor plan", exact=True).first),
        ("Plan", lambda: page.get_by_role("button", name="Plan", exact=True).first),
        ("Otworz rzut", lambda: page.get_by_role("button", name=re.compile(r"Otw[oó]rz rzut", re.I)).first),
        ("Rzut button text", lambda: page.locator("button").filter(has_text=re.compile(r"\bRzut\b", re.I)).first),
    ]
    clicked: list[str] = []
    for label, build_locator in controls:
        try:
            locator = build_locator()
            if await locator.count():
                await locator.scroll_into_view_if_needed(timeout=1500)
                await locator.click(timeout=1800)
                clicked.append(label)
                await page.wait_for_timeout(1200)
                return clicked
        except Exception:
            continue
    return clicked


def normalize_browser_image_url(base_url: str, value: str) -> str | None:
    value = clean_text(value)
    if not value or value.startswith("data:") or value.startswith("blob:"):
        return None
    parsed = urlparse(urljoin(base_url, value))
    if parsed.scheme not in {"http", "https"}:
        return None
    lower = parsed.path.lower()
    if not any(ext in lower for ext in [".jpg", ".jpeg", ".png", ".webp", ".avif"]) and "image" not in lower:
        return None
    return parsed.geturl()


def dedupe_image_candidates(images: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output = []
    seen = set()
    for image in sorted(images, key=lambda item: item.get("score") or 0, reverse=True):
        url = image.get("url")
        if not url or url in seen:
            continue
        seen.add(url)
        output.append(image)
    return output


def extract_structured_locations(soup: BeautifulSoup) -> list[dict[str, Any]]:
    locations: list[dict[str, Any]] = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(script.string or "")
        except json.JSONDecodeError:
            continue
        locations.extend(extract_locations_from_jsonld(data))
    return dedupe_locations(locations)[:8]


def extract_locations_from_jsonld(value: Any) -> list[dict[str, Any]]:
    locations: list[dict[str, Any]] = []
    if isinstance(value, list):
        for item in value:
            locations.extend(extract_locations_from_jsonld(item))
        return locations
    if not isinstance(value, dict):
        return locations

    address = format_jsonld_address(value.get("address"))
    lat, lon = parse_jsonld_geo(value.get("geo"))
    if address or (lat is not None and lon is not None):
        locations.append(
            {
                "address": address,
                "exact_address": looks_like_address(address) if address else False,
                "partial_address": looks_like_partial_address(address) if address else False,
                "lat": lat,
                "lon": lon,
                "source": "json_ld",
            }
        )

    for child in value.values():
        if isinstance(child, (dict, list)):
            locations.extend(extract_locations_from_jsonld(child))
    return locations


def format_jsonld_address(address: Any) -> str:
    if isinstance(address, dict):
        parts = [
            address.get("addressLocality"),
            address.get("streetAddress"),
            address.get("postalCode"),
            address.get("addressRegion"),
        ]
        return clean_text(", ".join(str(part) for part in parts if part))
    if isinstance(address, str):
        return clean_text(address)
    return ""


def parse_jsonld_geo(geo: Any) -> tuple[float | None, float | None]:
    if not isinstance(geo, dict):
        return None, None
    lat = parse_float(geo.get("latitude") or geo.get("lat"))
    lon = parse_float(geo.get("longitude") or geo.get("lng") or geo.get("lon"))
    if lat is None or lon is None:
        return None, None
    if not (48.0 <= lat <= 56.0 and 13.0 <= lon <= 26.0):
        return None, None
    return lat, lon


def dedupe_locations(locations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, str]] = set()
    output: list[dict[str, Any]] = []
    for location in locations:
        address = clean_text(location.get("address") or "")
        lat = location.get("lat")
        lon = location.get("lon")
        key = (
            address.lower(),
            "" if lat is None else str(round(float(lat), 7)),
            "" if lon is None else str(round(float(lon), 7)),
        )
        if key in seen:
            continue
        if not address and (lat is None or lon is None):
            continue
        seen.add(key)
        output.append(location)
    return output


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

def validate_listing_url(parsed: Any) -> None:
    if settings.allow_private_listing_urls:
        return

    hostname = parsed.hostname
    if not hostname:
        raise HTTPException(status_code=400, detail="Adres oferty nie zawiera poprawnej domeny.")

    if hostname.lower() in {"localhost", "localhost.localdomain"}:
        raise HTTPException(status_code=400, detail="Adres oferty nie moze wskazywac na localhost.")

    try:
        ip_address = ipaddress.ip_address(hostname)
    except ValueError:
        ip_address = None

    if ip_address:
        if is_private_network_address(ip_address):
            raise HTTPException(status_code=400, detail="Adres oferty wskazuje na siec prywatna lub lokalna.")
        return

    try:
        addresses = socket.getaddrinfo(hostname, parsed.port or 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise HTTPException(status_code=400, detail="Nie udalo sie zweryfikowac domeny oferty.") from exc

    for address_info in addresses:
        ip_value = address_info[4][0]
        try:
            ip_address = ipaddress.ip_address(ip_value)
        except ValueError:
            continue
        if is_private_network_address(ip_address):
            raise HTTPException(status_code=400, detail="Domena oferty wskazuje na siec prywatna lub lokalna.")


def is_private_network_address(ip_address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        ip_address.is_private
        or ip_address.is_loopback
        or ip_address.is_link_local
        or ip_address.is_multicast
        or ip_address.is_reserved
        or ip_address.is_unspecified
    )
