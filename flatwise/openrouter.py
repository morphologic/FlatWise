import json
from typing import Any

import httpx
from fastapi import HTTPException

from config import settings
from flatwise.constants import HTTPX_VERIFY


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

    async with httpx.AsyncClient(timeout=60, verify=HTTPX_VERIFY) as client:
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
