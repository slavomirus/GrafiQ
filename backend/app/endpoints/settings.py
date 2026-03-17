# Plik: backend/app/endpoints/settings.py

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.encoders import jsonable_encoder
import motor.motor_asyncio
from bson import ObjectId
import logging
from typing import List, Union
from datetime import datetime

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
        # Utwórz domyślny obiekt i przekonwertuj na dict, aby móc go modyfikować
        settings = jsonable_encoder(schemas.StoreSettingsCreate(franchise_code=franchise_code))
        # Dodaj _id, bo response_model tego wymaga (choć może być puste/nowe)
        settings["_id"] = ObjectId()

    # Pobierz godziny specjalne (święta)
    special_hours_cursor = db.specialopeninghours.find({"franchise_code": franchise_code})
    special_hours_list = await special_hours_cursor.to_list(length=None)
    
    # Mapuj godziny specjalne na format oczekiwany przez frontend w opening_hours.holiday
    holiday_map = {}
    for sh in special_hours_list:
        # Data w bazie może być datetime lub string (zależy jak zapisano), upewnijmy się
        d = sh.get("date")
        if isinstance(d, datetime):
            date_str = d.strftime("%Y-%m-%d")
        elif isinstance(d, str):
            date_str = d # Zakładamy format YYYY-MM-DD
        else:
            continue # Skip invalid
            
        holiday_map[date_str] = {
            "open": sh.get("open_time"),
            "close": sh.get("close_time")
        }

    # Upewnij się, że struktura opening_hours istnieje
    if "opening_hours" not in settings or settings["opening_hours"] is None:
        settings["opening_hours"] = {
            "weekday": {"from": "06:00", "to": "23:00"},
            "sunday": {"from": "10:00", "to": "20:00"},
            "holiday": {}
        }
    
    # Jeśli opening_hours jest obiektem (np. z Pydantic), zamień na dict
    if hasattr(settings["opening_hours"], "dict"):
        settings["opening_hours"] = settings["opening_hours"].dict(by_alias=True)

    # Upewnij się, że holiday istnieje
    if "holiday" not in settings["opening_hours"]:
        settings["opening_hours"]["holiday"] = {}
        
    # Wstrzyknij pobrane godziny specjalne
    settings["opening_hours"]["holiday"].update(holiday_map)
        
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

    # FIX: Konwersja time -> str dla MongoDB (bson nie obsługuje datetime.time)
    settings_dict = jsonable_encoder(settings_data)

    # Użycie operacji upsert (update or insert)
    await db.storesettings.update_one(
        {"franchise_code": franchise_code},
        {"$set": settings_dict},
        upsert=True
    )

    updated_settings = await db.storesettings.find_one({"franchise_code": franchise_code})
    if not updated_settings:
        raise HTTPException(status_code=500, detail="Zapis ustawień nie powiódł się.")

    # Tutaj też musimy wstrzyknąć holiday, żeby odpowiedź pasowała do modelu (jeśli frontend tego wymaga w odpowiedzi na POST)
    # Ale zazwyczaj POST zwraca to co zapisano. 
    # Jednak response_model=schemas.StoreSettings wymaga opening_hours.
    # Jeśli w bazie zapisaliśmy opening_hours (z payloadu), to jest ok.
    # Ale holiday z payloadu może być pusty, a w bazie specialopeninghours są dane.
    # Dla spójności, można by też tu wstrzyknąć, ale to dodatkowe zapytanie.
    # Zostawmy standardowy zwrot z bazy - frontend sobie poradzi (odświeży GETem jeśli trzeba).
    
    logger.info(f"Ustawienia dla sklepu {franchise_code} zostały zaktualizowane przez {current_user.get('email')}.")
    return updated_settings

@router.get("/special-hours", response_model=List[schemas.SpecialOpeningHours])
async def get_special_hours(
    current_user: dict = Depends(get_current_admin_user),
    db: motor.motor_asyncio.AsyncIOMotorDatabase = Depends(get_db)
):
    """Pobiera listę dni ze specjalnymi godzinami otwarcia."""
    franchise_code = current_user.get("franchise_code")
    hours = await db.specialopeninghours.find({"franchise_code": franchise_code}).to_list(length=None)
    
    # Fix dates/times for Pydantic
    for h in hours:
        if isinstance(h.get("date"), datetime):
            h["date"] = h["date"].date()
            
    return hours

@router.post("/special-hours", response_model=schemas.MessageResponse)
async def update_special_hours(
    hours_data: Union[schemas.SpecialOpeningHoursCreate, List[schemas.SpecialOpeningHoursCreate]],
    current_user: dict = Depends(get_current_admin_user),
    db: motor.motor_asyncio.AsyncIOMotorDatabase = Depends(get_db)
):
    """Dodaje lub aktualizuje godziny specjalne (pojedynczo lub masowo)."""
    franchise_code = current_user.get("franchise_code")
    
    data_list = hours_data if isinstance(hours_data, list) else [hours_data]
    
    for item in data_list:
        if item.franchise_code != franchise_code:
             raise HTTPException(status_code=403, detail="Brak uprawnień do tego sklepu.")
             
        # Konwersja date/time na format MongoDB (datetime)
        item_dict = jsonable_encoder(item)
        # Ale jsonable_encoder zamienia date na string 'YYYY-MM-DD', a my chcemy datetime w bazie dla zapytań
        # Wróćmy do datetime dla pola 'date'
        item_dict["date"] = datetime.combine(item.date, datetime.min.time())
        
        await db.specialopeninghours.update_one(
            {"franchise_code": franchise_code, "date": item_dict["date"]},
            {"$set": item_dict},
            upsert=True
        )
        
    return {"message": "Godziny specjalne zaktualizowane."}

@router.delete("/special-hours/{date_str}", response_model=schemas.MessageResponse)
async def delete_special_hours(
    date_str: str,
    current_user: dict = Depends(get_current_admin_user),
    db: motor.motor_asyncio.AsyncIOMotorDatabase = Depends(get_db)
):
    """Usuwa specjalne godziny dla danej daty (przywraca domyślne)."""
    franchise_code = current_user.get("franchise_code")
    try:
        target_date = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="Nieprawidłowy format daty (YYYY-MM-DD).")
        
    result = await db.specialopeninghours.delete_one({
        "franchise_code": franchise_code,
        "date": target_date
    })
    
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Nie znaleziono wpisu dla tej daty.")
        
    return {"message": "Usunięto godziny specjalne."}
