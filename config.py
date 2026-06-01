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
    app_name: str = os.getenv("APP_NAME", "FlatWise")
    request_timeout_seconds: float = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "18"))
    max_upload_bytes: int = int(os.getenv("MAX_UPLOAD_BYTES", str(12 * 1024 * 1024)))
    max_image_pixels: int = int(os.getenv("MAX_IMAGE_PIXELS", str(24_000_000)))
    max_pdf_pages: int = int(os.getenv("MAX_PDF_PAGES", "6"))
    allow_private_listing_urls: bool = os.getenv("ALLOW_PRIVATE_LISTING_URLS", "false").lower() in {
        "1",
        "true",
        "yes",
    }
    cors_origins: list[str] = [
        origin.strip()
        for origin in os.getenv("CORS_ORIGINS", app_url).split(",")
        if origin.strip()
    ]


settings = Settings()
