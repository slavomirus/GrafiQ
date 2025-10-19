from fastapi import APIRouter, Depends, HTTPException
from datetime import datetime, timedelta, time
from typing import List, Optional
from bson import ObjectId
import logging
import motor.motor_asyncio

from ..database import get_db
from ..dependencies import get_current_active_user, get_current_admin_user
from .. import models, schemas

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/my-vacations", response_model=List[schemas.Vacation])
async def get_my_vacations(
        status: Optional[str] = None,
        current_user: dict = Depends(get_current_active_user),
        db: motor.motor_asyncio.AsyncIOMotorClient = Depends(get_db)
):
    """
    Pobierz własne wnioski urlopowe
    """
    try:
        query = {"user_id": ObjectId(current_user["_id"])}

        if status:
            query["status"] = status

        vacations = await db.vacations.find(query).sort("submitted_at", -1).to_list(length=None)
        return vacations

    except Exception as e:
        logger.error(f"Błąd podczas pobierania własnych urlopów: {e}")
        raise HTTPException(status_code=500, detail="Błąd podczas pobierania własnych urlopów")


@router.post("/request", response_model=schemas.Vacation)
async def request_vacation(
        vacation: schemas.VacationCreate,
        current_user: dict = Depends(get_current_active_user),
        db: motor.motor_asyncio.AsyncIOMotorClient = Depends(get_db)
):
    """
    Złóż wniosek urlopowy
    """
    try:
        if vacation.start_date > vacation.end_date:
            raise HTTPException(status_code=400, detail="Data rozpoczęcia nie może być późniejsza niż data zakończenia")

        if vacation.start_date < datetime.now().date():
            raise HTTPException(status_code=400, detail="Nie można składać wniosków urlopowych z datą wsteczną")

        user = await db.users.find_one({"_id": ObjectId(current_user["_id"])})
        if not user:
            raise HTTPException(status_code=404, detail="Użytkownik nie znaleziony")

        vacation_days = (vacation.end_date - vacation.start_date).days + 1

        if user.get("vacation_days_left", 0) < vacation_days:
            raise HTTPException(
                status_code=400,
                detail=f"Nie masz wystarczającej liczby dni urlopowych. Dostępne: {user.get('vacation_days_left', 0)}, Wymagane: {vacation_days}"
            )

        start_datetime = datetime.combine(vacation.start_date, time.min)
        end_datetime = datetime.combine(vacation.end_date, time.max)

        conflicting_vacation = await db.vacations.find_one({
            "user_id": ObjectId(current_user["_id"]),
            "$or": [
                {
                    "start_date": {"$lte": end_datetime},
                    "end_date": {"$gte": start_datetime},
                    "status": models.VacationStatus.APPROVED.value
                },
                {
                    "start_date": {"$lte": end_datetime},
                    "end_date": {"$gte": start_datetime},
                    "status": models.VacationStatus.PENDING.value
                }
            ]
        })

        if conflicting_vacation:
            raise HTTPException(
                status_code=400,
                detail="Masz już złożony wniosek lub zaakceptowany urlop w podanym okresie"
            )

        vacation_data = {
            "user_id": ObjectId(current_user["_id"]),
            "start_date": start_datetime,
            "end_date": end_datetime,
            "reason": vacation.reason,
            "status": models.VacationStatus.PENDING.value,
            "submitted_at": datetime.utcnow(),
            "days_requested": vacation_days
        }

        result = await db.vacations.insert_one(vacation_data)
        created_vacation = await db.vacations.find_one({"_id": result.inserted_id})
        return created_vacation

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Błąd podczas składania wniosku urlopowego: {e}")
        raise HTTPException(status_code=500, detail="Błąd podczas składania wniosku urlopowego")

# Pozostałe endpointy bez zmian...
