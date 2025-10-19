# Zawartość nowego pliku: app/security.py

from passlib.context import CryptContext
import jwt
from jwt import PyJWTError
from datetime import datetime, timedelta
from typing import Optional

from .config import settings  # Używamy scentralizowanej konfiguracji

# JEDYNY I OFICJALNY PWD_CONTEXT W CAŁEJ APLIKACJI
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# --- FUNKCJE HASHOWANIA ---
def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)

def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)

# --- FUNKCJE JWT ---
def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        # Używamy wartości z pliku konfiguracyjnego
        expire = datetime.utcnow() + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)

def decode_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
    except PyJWTError:
        return None