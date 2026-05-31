import os

from dotenv import load_dotenv


load_dotenv()


class Settings:
    openrouter_api_key: str | None = os.getenv("OPENROUTER_API_KEY")
    openrouter_model: str = os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini")
    openrouter_base_url: str = os.getenv(
        "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"
    )
    nanobanana_model: str = os.getenv("NANOBANANA_MODEL", "google/gemini-2.5-flash-image")
    google_maps_api_key: str | None = os.getenv("GOOGLE_MAPS_API_KEY")
    app_url: str = os.getenv("APP_URL", "http://127.0.0.1:8001")
    app_name: str = os.getenv("APP_NAME", "grasshopper_hackathon")
    request_timeout_seconds: float = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "18"))
    max_upload_bytes: int = int(os.getenv("MAX_UPLOAD_BYTES", str(12 * 1024 * 1024)))


settings = Settings()
