# Plik: backend/app/dependencies.py

import logging
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jwt import PyJWTError
from bson import ObjectId
import motor.motor_asyncio

from .database import get_db
from . import models, security

logger = logging.getLogger(__name__)
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/token")

async def get_current_user(token: str = Depends(oauth2_scheme),
                           db: motor.motor_asyncio.AsyncIOMotorClient = Depends(get_db)):
    """
    Dekoduje token JWT, znajduje użytkownika w bazie po ID i go zwraca.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = security.decode_token(token)
        if payload is None:
            raise credentials_exception

        user_id: str = payload.get("sub")
        if user_id is None:
            raise credentials_exception

        user = await db.users.find_one({"_id": ObjectId(user_id)})
        if user is None:
            raise credentials_exception

    except (PyJWTError, ValueError):
        raise credentials_exception
    except Exception as e:
        logger.error(f"An unexpected error occurred during user authentication: {e}", exc_info=True)
        raise credentials_exception

    return user


async def get_current_active_user(current_user: dict = Depends(get_current_user)):
    """Sprawdza, czy użytkownik jest aktywny lub musi zmienić hasło."""
    # Użycie stringa jako fallback na wypadek problemów ze środowiskiem
    if current_user.get("status") not in [models.UserStatus.ACTIVE.value, "needs_password_change"]:
        raise HTTPException(status_code=403, detail="Inactive user")
    return current_user


async def get_current_admin_user(current_user: dict = Depends(get_current_active_user)):
    """Sprawdza, czy aktywny użytkownik ma uprawnienia administracyjne."""
    if current_user.get("role") not in [models.UserRole.ADMIN.value, models.UserRole.FRANCHISEE.value]:
        raise HTTPException(status_code=403, detail="Not enough permissions")
    return current_user
