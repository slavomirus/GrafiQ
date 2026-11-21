from pydantic_settings import BaseSettings
from functools import lru_cache
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

class Settings(BaseSettings):
    """
    Ustawienia aplikacji ładowane z zmiennych środowiskowych lub pliku .env
    """
    MONGODB_URI: str
    MONGODB_DB_NAME: str = "schedule_db"
    SECRET_KEY: str
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    REFRESH_TOKEN_EXPIRE_DAYS: int = 30
    FIELD_ENCRYPTION_KEY: Optional[str] = None
    SENTRY_DSN: Optional[str] = None

    # Wersje dokumentów
    EULA_VERSION: str = "1.0"
    RODO_VERSION: str = "1.0"
    DPA_VERSION: str = "1.0"

    # MAIL
    MAIL_USERNAME: str
    MAIL_PASSWORD: str
    MAIL_FROM: str
    MAIL_SERVER: str
    MAIL_STARTTLS: bool
    MAIL_SSL_TLS: bool
    MAIL_PORT: int
    MAIL_FROM_NAME: str

    class Config:
        # Ustalanie ścieżki do pliku .env względem lokalizacji tego pliku (app/config.py -> backend/.env)
        base_dir = Path(__file__).resolve().parent.parent
        env_file = base_dir / ".env"
        env_file_encoding = 'utf-8'

@lru_cache()
def get_settings():
    """
    Funkcja pomocnicza do pobierania instancji ustawień z cache'u
    """
    return Settings()

settings = get_settings()
logger.info(f"Załadowano ustawienia aplikacji z: {Settings.Config.env_file}")
