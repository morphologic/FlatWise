import json
import re
from typing import Any


def parse_report(text: Any) -> dict[str, Any]:
    text = response_text(text)
    if not isinstance(text, str):
        return fallback_report("", f"Model response content was {type(text).__name__}, not text.")

    text = text.strip()
    if not text:
        return fallback_report("", "Model returned an empty response.")

    cleaned = strip_json_fence(text)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        parsed = parse_first_json_object(cleaned)
        if parsed is not None:
            return parsed
        parsed = parse_first_json_object(text)
        if parsed is not None:
            return parsed
    return fallback_report(text, "Could not parse model response as a JSON object.")


def response_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        chunks = []
        for item in value:
            item_text = response_text(item)
            if item_text:
                chunks.append(item_text)
        return "\n".join(chunks)
    if isinstance(value, dict):
        for key in ["text", "content", "output_text", "value"]:
            item_text = response_text(value.get(key))
            if item_text:
                return item_text
        if value.get("type") in {"text", "output_text"} and isinstance(value.get("data"), str):
            return value["data"]
        return ""
    return str(value)


def choice_message_text(data: dict[str, Any]) -> str:
    message = (data.get("choices") or [{}])[0].get("message") or {}
    for key in ["content", "reasoning", "refusal"]:
        text = response_text(message.get(key))
        if text:
            return text
    return ""


def strip_json_fence(text: str) -> str:
    stripped = text.strip()
    fence_match = re.match(r"^```(?:json)?\s*(.*?)\s*```$", stripped, flags=re.DOTALL | re.IGNORECASE)
    if fence_match:
        return fence_match.group(1).strip()
    return stripped


def parse_first_json_object(text: str) -> dict[str, Any] | None:
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            parsed, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def fallback_report(raw_response: str, parse_error: str) -> dict[str, Any]:
    return {
        "verdict": "Wymaga dalszego sprawdzenia",
        "confidence": 25,
        "one_sentence": "Nie udało się odczytać odpowiedzi modelu jako uporządkowanego JSON.",
        "parse_error": parse_error,
        "raw_response": raw_response,
    }


def split_data_url(data_url: str) -> tuple[str, str]:
    match = re.match(r"^data:([^;]+);base64,(.+)$", data_url, flags=re.DOTALL)
    if not match:
        raise ValueError("Niepoprawny format obrazu źródłowego.")
    return match.group(1), match.group(2)


def parse_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value).replace(",", "."))
    except ValueError:
        return None


def nested_get(value: dict[str, Any], path: list[str]) -> Any:
    current: Any = value
    for item in path:
        if not isinstance(current, dict):
            return None
        current = current.get(item)
    return current


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
    keywords = [
        "rzut",
        "rzuty",
        "floorplan",
        "floor-plan",
        "floor plan",
        "uklad",
        "układ",
        "karta-lokalu",
        "karta lokalu",
        "plan-mieszkania",
        "plan mieszkania",
    ]
    return sum(1 for keyword in keywords if keyword in haystack)


def assess_extraction_quality(text: str, images: list[dict[str, Any]]) -> str:
    if len(text) > 2500 and images:
        return "good"
    if len(text) > 800:
        return "partial"
    return "weak"
