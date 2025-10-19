# Plik: backend/app/endpoints/stores.py

from fastapi import APIRouter, Depends, HTTPException, status
import motor.motor_asyncio
from bson import ObjectId
import logging

from ..database import get_db
from .. import models, schemas
# POPRAWKA: Zmiana ścieżki importu na nowy moduł zależności
from ..dependencies import get_current_admin_user

router = APIRouter()
logger = logging.getLogger(__name__)

@router.post("/", status_code=status.HTTP_201_CREATED, response_model=schemas.Store)
async def add_new_store(
    store_data: schemas.StoreCreate,
    current_user: dict = Depends(get_current_admin_user),
    db: motor.motor_asyncio.AsyncIOMotorDatabase = Depends(get_db)
):
    """
    Tworzy nowy sklep i przypisuje go do zalogowanego franczyzobiorcy.
    """
    # 1. Sprawdzenie, czy sklep o danym kodzie już istnieje w bazie
    existing_store = await db.stores.find_one({"franchise_code": store_data.franchise_code})
    if existing_store:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Sklep o podanym kodzie franczyzy już istnieje."
        )

    # 2. Jeśli nie istnieje, przygotuj nowy dokument sklepu
    store_document = store_data.dict()
    store_document["owner_id"] = current_user["_id"]

    # 3. Zapisz nowy sklep do bazy
    result = await db.stores.insert_one(store_document)
    
    # 4. Pobierz i zwróć nowo utworzony dokument sklepu
    created_store = await db.stores.find_one({"_id": result.inserted_id})
    if not created_store:
        raise HTTPException(status_code=500, detail="Nie udało się utworzyć sklepu po zapisie do bazy danych.")

    return created_store
