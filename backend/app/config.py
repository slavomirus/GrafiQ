from pydantic_settings import BaseSettings
from functools import lru_cache
import logging

logger = logging.getLogger(__name__)

class Settings(BaseSettings):
    """
    Ustawienia aplikacji ładowane z zmiennych środowiskowych lub pliku .env
    """
    MONGODB_URI: str
    MONGODB_DB_NAME: str = "schedule_db"
    SECRET_KEY: str
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15  # <-- DODAJ TĘ LINIĘ

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
        env_file = ".env"
        env_file_encoding = 'utf-8'

@lru_cache()
def get_settings():
    """
    Funkcja pomocnicza do pobierania instancji ustawień z cache'u
    """
    return Settings()

settings = get_settings()
logger.info("Załadowano ustawienia aplikacji.")
