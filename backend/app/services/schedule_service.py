# Plik: backend/app/services/schedule_service.py

import logging
from datetime import datetime, time, date
from bson import ObjectId
from fastapi import HTTPException, status
import motor.motor_asyncio
from typing import List, Dict

logger = logging.getLogger(__name__)

async def get_schedules_history(db: motor.motor_asyncio.AsyncIOMotorDatabase, current_user: dict) -> List[dict]:
    """Pobiera historię wszystkich grafików (opublikowanych i roboczych) dla danego sklepu."""
    franchise_code = current_user.get("franchise_code")
    if not franchise_code:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Użytkownik nie jest przypisany do żadnego sklepu.")

    all_schedules = []
    
    # Pobierz opublikowane grafiki
    published_cursor = db.schedules.find({"franchise_code": franchise_code})
    async for schedule in published_cursor:
        schedule["_id"] = str(schedule["_id"])
        all_schedules.append(schedule)
        
    # Pobierz wersje robocze
    drafts_cursor = db.schedule_drafts.find({"franchise_code": franchise_code})
    async for draft in drafts_cursor:
        draft["_id"] = str(draft["_id"])
        draft["is_published"] = False # Dodajmy dla spójności
        all_schedules.append(draft)
        
    # Sortuj po dacie początkowej malejąco
    all_schedules.sort(key=lambda x: x['start_date'], reverse=True)
    
    return all_schedules

async def get_all_drafts_for_store(db: motor.motor_asyncio.AsyncIOMotorDatabase, current_user: dict) -> List[dict]:
    """Pobiera listę wszystkich roboczych wersji grafików dla danego sklepu."""
    franchise_code = current_user.get("franchise_code")
    if not franchise_code:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Użytkownik nie jest przypisany do żadnego sklepu.")

    drafts_cursor = db.schedule_drafts.find({
        "franchise_code": franchise_code
    })
    
    drafts = await drafts_cursor.to_list(length=None)
    
    for draft in drafts:
        draft["_id"] = str(draft["_id"])
        
    return drafts

async def publish_schedule_draft(db: motor.motor_asyncio.AsyncIOMotorDatabase, draft_id: str, current_user: dict):
    """
    Publikuje wersję roboczą grafiku, przenosząc ją z kolekcji 'schedule_drafts' do 'schedules'.
    """
    try:
        draft_object_id = ObjectId(draft_id)
    except Exception:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Nieprawidłowy format ID wersji roboczej.")

    draft = await db.schedule_drafts.find_one({"_id": draft_object_id})
    if not draft:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Wersja robocza grafiku nie została znaleziona.")

    if current_user.get("franchise_code") != draft.get("franchise_code"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Brak uprawnień do zarządzania tym grafikiem.")

    published_schedule = {
        "franchise_code": draft["franchise_code"],
        "start_date": draft["start_date"],
        "end_date": draft["end_date"],
        "schedule": draft["schedule"],
        "is_published": True,
        "published_at": datetime.utcnow(),
        "published_by": current_user.get("email")
    }

    await db.schedules.update_one(
        {"franchise_code": draft["franchise_code"], "start_date": draft["start_date"]},
        {"$set": published_schedule},
        upsert=True
    )
    logger.info(f"Użytkownik {current_user.get('email')} opublikował grafik dla sklepu {draft['franchise_code']}.")

    await db.schedule_drafts.delete_one({"_id": draft_object_id})
    logger.info(f"Usunięto wersję roboczą o ID {draft_id}.")

    return {"detail": "Grafik został pomyślnie opublikowany."}


async def get_employee_schedule(db: motor.motor_asyncio.AsyncIOMotorDatabase, current_user: dict):
    """Pobiera NAJNOWSZY opublikowany grafik dla sklepu pracownika."""
    franchise_code = current_user.get("franchise_code")
    if not franchise_code:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Użytkownik nie jest przypisany do żadnego sklepu.")

    cursor = db.schedules.find({
        "franchise_code": franchise_code,
        "is_published": True
    }).sort("published_at", -1).limit(1)

    schedules = await cursor.to_list(length=1)

    if not schedules:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Obecnie nie ma żadnego opublikowanego grafiku dla Twojego sklepu.")
    
    schedule = schedules[0]
    schedule["_id"] = str(schedule["_id"])
    return schedule
