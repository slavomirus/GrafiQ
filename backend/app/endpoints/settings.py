# Plik: backend/app/endpoints/settings.py

from fastapi import APIRouter, Depends, HTTPException, status
import motor.motor_asyncio
from bson import ObjectId
import logging

from ..database import get_db
from ..dependencies import get_current_admin_user
from .. import schemas

router = APIRouter()
logger = logging.getLogger(__name__)

@router.get("/store", response_model=schemas.StoreSettings)
async def get_store_settings(
    current_user: dict = Depends(get_current_admin_user),
    db: motor.motor_asyncio.AsyncIOMotorDatabase = Depends(get_db)
):
    """Pobiera ustawienia dla sklepu franczyzobiorcy lub zwraca domyślne."""
    franchise_code = current_user.get("franchise_code")
    if not franchise_code:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Użytkownik nie jest przypisany do żadnego sklepu.")

    settings = await db.storesettings.find_one({"franchise_code": franchise_code})
    
    if not settings:
        logger.warning(f"Brak zdefiniowanych ustawień dla sklepu {franchise_code}. Zwracam domyślne.")
        # Zwróć domyślny model Pydantic, który ma już zdefiniowane wartości
        return schemas.StoreSettingsCreate(franchise_code=franchise_code)
        
    return settings

@router.post("/store", response_model=schemas.StoreSettings)
async def create_or_update_store_settings(
    settings_data: schemas.StoreSettingsCreate,
    current_user: dict = Depends(get_current_admin_user),
    db: motor.motor_asyncio.AsyncIOMotorDatabase = Depends(get_db)
):
    """Tworzy lub aktualizuje ustawienia dla sklepu."""
    franchise_code = current_user.get("franchise_code")
    if not franchise_code or franchise_code != settings_data.franchise_code:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Brak uprawnień do zmiany ustawień tego sklepu.")

    # Użycie operacji upsert (update or insert)
    await db.storesettings.update_one(
        {"franchise_code": franchise_code},
        {"$set": settings_data.dict()},
        upsert=True
    )

    updated_settings = await db.storesettings.find_one({"franchise_code": franchise_code})
    if not updated_settings:
        raise HTTPException(status_code=500, detail="Zapis ustawień nie powiódł się.")

    logger.info(f"Ustawienia dla sklepu {franchise_code} zostały zaktualizowane przez {current_user.get('email')}.")
    return updated_settings
