# Plik: backend/app/services/user_management_service.py

import logging
from datetime import datetime, date
from bson import ObjectId
from fastapi import HTTPException, status
import motor.motor_asyncio

from .. import models

logger = logging.getLogger(__name__)

async def add_l4_leave(
    db: motor.motor_asyncio.AsyncIOMotorDatabase, 
    user_id: str, 
    start_date: date, 
    end_date: date,
    current_user: dict
):
    """Dodaje zwolnienie L4 dla pracownika i aktualizuje grafiki."""
    try:
        employee_id = ObjectId(user_id)
    except Exception:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Nieprawidłowy format ID pracownika.")

    # Sprawdzenie, czy pracownik istnieje i należy do tego samego sklepu
    employee = await db.users.find_one({"_id": employee_id, "franchise_code": current_user.get("franchise_code")})
    if not employee:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pracownik nie został znaleziony w Twoim sklepie.")

    # 1. Zapisz zwolnienie w kolekcji 'vacations' z nowym typem
    l4_leave_document = {
        "user_id": employee_id,
        "start_date": datetime.combine(start_date, datetime.min.time()),
        "end_date": datetime.combine(end_date, datetime.max.time()),
        "status": models.VacationStatus.APPROVED.value, # L4 jest domyślnie zatwierdzone
        "leave_type": "L4",
        "submitted_at": datetime.utcnow(),
        "approved_by_id": ObjectId(current_user["_id"])
    }
    await db.vacations.insert_one(l4_leave_document)
    logger.info(f"Zapisano zwolnienie L4 dla pracownika {user_id} od {start_date} do {end_date}.")

    # 2. Obsłuż opublikowane grafiki (Scenariusz 1 i 3)
    overlapping_schedules_cursor = db.schedules.find({
        "franchise_code": current_user.get("franchise_code"),
        "is_published": True,
        "start_date": {"$lte": datetime.combine(end_date, datetime.max.time())},
        "end_date": {"$gte": datetime.combine(start_date, datetime.min.time())}
    })

    async for schedule in overlapping_schedules_cursor:
        schedule_changed = False
        for day_str, shifts in schedule.get("schedule", {}).items():
            day_date = datetime.fromisoformat(day_str).date()
            if start_date <= day_date <= end_date:
                for shift_name, employee_ids in shifts.items():
                    # Używamy stringa user_id do porównania, bo tak jest w grafiku
                    if user_id in employee_ids:
                        schedule["schedule"][day_str][shift_name].remove(user_id)
                        schedule_changed = True
                        logger.info(f"Usunięto pracownika {user_id} ze zmiany '{shift_name}' w dniu {day_str} z powodu L4.")
                        # TODO: Dodać logikę uzupełniania luki i logowania konfliktu
        
        if schedule_changed:
            await db.schedules.update_one(
                {"_id": schedule["_id"]},
                {"$set": {"schedule": schedule["schedule"]}}
            )
            logger.info(f"Zaktualizowano opublikowany grafik o ID {schedule['_id']} po zgłoszeniu L4.")

    # Scenariusz 2 (przyszłe grafiki) jest obsługiwany automatycznie przez generator,
    # ponieważ odczytuje on dane z kolekcji 'vacations'.

    return {"detail": "Zwolnienie L4 zostało pomyślnie zgłoszone i przetworzone."}
