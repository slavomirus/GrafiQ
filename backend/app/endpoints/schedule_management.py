# Plik: backend/app/endpoints/schedule_management.py

from fastapi import APIRouter, Depends, HTTPException, status
import motor.motor_asyncio
from bson import ObjectId
import logging
from typing import List
from datetime import date, time, datetime, timedelta

from ..database import get_db
from ..dependencies import get_current_admin_user
from .. import schemas, models

router = APIRouter()
logger = logging.getLogger(__name__)

async def _validate_shift_update(db: motor.motor_asyncio.AsyncIOMotorDatabase, draft: dict, update_data: schemas.ShiftUpdate):
    """Waliduje, czy pracownicy mogą zostać przypisani do nowej zmiany."""
    employee_ids = update_data.employee_ids
    day = update_data.date
    shift_name = update_data.shift_name

    for emp_id in employee_ids:
        employee = await db.users.find_one({"_id": emp_id})
        if not employee:
            raise HTTPException(status_code=404, detail=f"Pracownik o ID {emp_id} nie został znaleziony.")

        # 1. Sprawdzenie urlopu
        vacation = await db.vacations.find_one({
            "user_id": emp_id,
            "status": models.VacationStatus.APPROVED.value,
            "start_date": {"$lte": day},
            "end_date": {"$gte": day}
        })
        if vacation:
            raise HTTPException(status_code=409, detail=f"Pracownik {employee.get('first_name')} {employee.get('last_name')} ma w tym dniu urlop.")

        # 2. Sprawdzenie dyspozycji "wolne"
        availability = await db.availability.find_one({"user_id": emp_id, "date": datetime.combine(day, time.min)})
        if availability and availability.get("period_type") == "wolne":
            raise HTTPException(status_code=409, detail=f"Pracownik {employee.get('first_name')} {employee.get('last_name')} zgłosił na ten dzień dyspozycję \"wolne\".")

        # 3. Sprawdzenie "clopening"
        if shift_name == "morning":
            yesterday_str = (day - timedelta(days=1)).isoformat()
            schedule = draft.get("schedule", {})
            if yesterday_str in schedule and str(emp_id) in schedule[yesterday_str].get("closing", []):
                raise HTTPException(status_code=409, detail=f"Pracownik {employee.get('first_name')} {employee.get('last_name')} nie może pracować rano po zmianie zamykającej.")

@router.get("/draft/latest", response_model=schemas.ScheduleDraftResponse)
async def get_latest_schedule_draft(current_user: dict = Depends(get_current_admin_user), db: motor.motor_asyncio.AsyncIOMotorDatabase = Depends(get_db)):
    franchise_code = current_user.get("franchise_code")
    draft = await db.schedule_drafts.find_one(
        {"franchise_code": franchise_code, "status": "DRAFT"},
        sort=[("created_at", -1)]
    )
    if not draft:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Nie znaleziono żadnego szkicu grafiku.")
    return draft

@router.post("/draft/{draft_id}/accept", status_code=status.HTTP_200_OK)
async def accept_schedule_draft(draft_id: str, current_user: dict = Depends(get_current_admin_user), db: motor.motor_asyncio.AsyncIOMotorDatabase = Depends(get_db)):
    try:
        draft_oid = ObjectId(draft_id)
    except Exception:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Nieprawidłowy format ID szkicu.")

    draft = await db.schedule_drafts.find_one({"_id": draft_oid})
    if not draft or draft.get("franchise_code") != current_user.get("franchise_code"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Szkic grafiku nie został znaleziony lub brak do niego uprawnień.")

    if draft.get("status") != "DRAFT":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Ten grafik został już przetworzony.")

    schedule_entries = []
    for date_str, shifts in draft.get("schedule", {}).items():
        schedule_date = date.fromisoformat(date_str)
        for shift_name, employee_ids in shifts.items():
            start_time, end_time = (time(6, 0), time(14, 0)) if shift_name == "morning" else (time(14, 0), time(22, 0))
            for emp_id in employee_ids:
                schedule_entries.append({
                    "user_id": emp_id,
                    "franchise_code": draft.get("franchise_code"),
                    "date": datetime.combine(schedule_date, time.min),
                    "start_time": start_time,
                    "end_time": end_time,
                    "assigned_by_id": current_user["_id"],
                    "created_at": datetime.utcnow()
                })
    
    if schedule_entries:
        await db.schedule.delete_many({
            "franchise_code": draft.get("franchise_code"),
            "date": {"$gte": draft["start_date"], "$lte": draft["end_date"]}
        })
        await db.schedule.insert_many(schedule_entries)

    await db.schedule_drafts.update_one(
        {"_id": draft_oid},
        {"$set": {"status": "PUBLISHED"}}
    )

    logger.info(f"Grafik (szkic {draft_id}) został zaakceptowany i opublikowany przez {current_user.get('email')}.")
    return {"message": "Grafik został pomyślnie opublikowany."}

@router.post("/draft/{draft_id}/reject", status_code=status.HTTP_200_OK)
async def reject_schedule_draft(draft_id: str, current_user: dict = Depends(get_current_admin_user), db: motor.motor_asyncio.AsyncIOMotorDatabase = Depends(get_db)):
    try:
        draft_oid = ObjectId(draft_id)
    except Exception:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Nieprawidłowy format ID szkicu.")

    draft = await db.schedule_drafts.find_one({"_id": draft_oid})
    if not draft or draft.get("franchise_code") != current_user.get("franchise_code"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Szkic grafiku nie został znaleziony lub brak do niego uprawnień.")

    if draft.get("status") != "DRAFT":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Ten grafik został już przetworzony.")

    await db.schedule_drafts.update_one(
        {"_id": draft_oid},
        {"$set": {"status": "REJECTED"}}
    )

    logger.info(f"Grafik (szkic {draft_id}) został odrzucony przez {current_user.get('email')}.")
    return {"message": "Grafik został pomyślnie odrzucony."}

@router.put("/draft/{draft_id}/shift", response_model=schemas.ScheduleDraftResponse)
async def update_draft_shift(
    draft_id: str,
    update_data: schemas.ShiftUpdate,
    current_user: dict = Depends(get_current_admin_user),
    db: motor.motor_asyncio.AsyncIOMotorDatabase = Depends(get_db)
):
    """Pozwala na ręczną edycję pojedynczej zmiany w szkicu grafiku."""
    try:
        draft_oid = ObjectId(draft_id)
    except Exception:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Nieprawidłowy format ID szkicu.")

    draft = await db.schedule_drafts.find_one({"_id": draft_oid})
    if not draft or draft.get("franchise_code") != current_user.get("franchise_code"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Szkic grafiku nie został znaleziony.")

    if draft.get("status") != "DRAFT":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Nie można edytować już przetworzonego grafiku.")

    # Walidacja zmiany
    await _validate_shift_update(db, draft, update_data)

    # Aktualizacja dokumentu szkicu w bazie danych
    date_str = update_data.date.isoformat()
    shift_name = update_data.shift_name
    
    # Używamy $set do aktualizacji konkretnego pola w zagnieżdżonym dokumencie
    update_field = f"schedule.{date_str}.{shift_name}"
    await db.schedule_drafts.update_one(
        {"_id": draft_oid},
        {"$set": {update_field: [str(eid) for eid in update_data.employee_ids]}}
    )

    # Pobierz i zwróć zaktualizowany szkic
    updated_draft = await db.schedule_drafts.find_one({"_id": draft_oid})
    return updated_draft
