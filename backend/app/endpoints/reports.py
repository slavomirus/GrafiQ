from fastapi import APIRouter, Depends, HTTPException
from datetime import date, datetime, timedelta
from typing import List, Optional
import logging
import motor.motor_asyncio
from bson import ObjectId

from ..database import get_db
# POPRAWKA: Zmiana ścieżki importu na nowy moduł zależności
from ..dependencies import get_current_admin_user
from .. import schemas, models

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/hours", response_model=List[schemas.HoursReportRequest])
async def get_hours_report(
        start_date: date,
        end_date: date,
        franchise_code: Optional[str] = None,
        user_id: Optional[str] = None,
        current_user: dict = Depends(get_current_admin_user),
        db: motor.motor_asyncio.AsyncIOMotorClient = Depends(get_db)
):
    """
    Generuje raport godzin pracy dla administratora.
    """
    try:
        query = {
            "date": {
                "$gte": datetime.combine(start_date, datetime.min.time()),
                "$lte": datetime.combine(end_date, datetime.max.time())
            }
        }

        # Filtrowanie po user_id
        if user_id:
            query["user_id"] = ObjectId(user_id)

        # Filtrowanie po franczyzie
        if franchise_code:
            franchise_users = await db.users.find({"franchise_code": franchise_code}).to_list(length=None)
            franchise_user_ids = [user["_id"] for user in franchise_users]
            query["user_id"] = {"$in": franchise_user_ids}

        # Pobranie grafików
        schedules = await db.schedule.find(query).to_list(length=None)

        # Pobranie danych o użytkownikach
        user_ids = list({s["user_id"] for s in schedules})
        users = await db.users.find({"_id": {"$in": user_ids}}).to_list(length=None)
        user_dict = {user["_id"]: user for user in users}

        report_data = []
        for s in schedules:
            user_info = user_dict.get(s["user_id"])
            if user_info:
                start = datetime.combine(s['date'], s['start_time'])
                end = datetime.combine(s['date'], s['end_time'])
                duration = (end - start).total_seconds() / 3600

                report_data.append({
                    "user_id": str(s['user_id']),
                    "first_name": user_info.get("first_name", ""),
                    "last_name": user_info.get("last_name", ""),
                    "date": s['date'],
                    "start_time": s['start_time'],
                    "end_time": s['end_time'],
                    "hours": duration,
                })

        return report_data
    except Exception as e:
        logger.error(f"Błąd podczas generowania raportu godzin: {e}")
        raise HTTPException(status_code=500, detail="Błąd podczas generowania raportu godzin.")


@router.get("/vacations", response_model=List[schemas.VacationReportRequest])
async def get_vacations_report(
        start_date: date,
        end_date: date,
        franchise_code: Optional[str] = None,
        user_id: Optional[str] = None,
        current_user: dict = Depends(get_current_admin_user),
        db: motor.motor_asyncio.AsyncIOMotorClient = Depends(get_db)
):
    """
    Generuje raport urlopów dla administratora.
    """
    try:
        query = {
            "status": models.VacationStatus.APPROVED.value,
            "start_date": {"$lte": end_date},
            "end_date": {"$gte": start_date}
        }

        # Filtrowanie po user_id
        if user_id:
            query["user_id"] = ObjectId(user_id)

        # Filtrowanie po franczyzie
        if franchise_code:
            franchise_users = await db.users.find({"franchise_code": franchise_code}).to_list(length=None)
            franchise_user_ids = [user["_id"] for user in franchise_users]
            query["user_id"] = {"$in": franchise_user_ids}

        vacations = await db.vacations.find(query).to_list(length=None)

        user_ids = list({v["user_id"] for v in vacations})
        users = await db.users.find({"_id": {"$in": user_ids}}).to_list(length=None)
        user_dict = {user["_id"]: user for user in users}

        report_data = []
        for v in vacations:
            user_info = user_dict.get(v["user_id"])
            if user_info:
                report_data.append({
                    "user_id": str(v['user_id']),
                    "first_name": user_info.get("first_name", ""),
                    "last_name": user_info.get("last_name", ""),
                    "start_date": v['start_date'],
                    "end_date": v['end_date'],
                    "days_requested": v['days_requested'],
                    "reason": v.get('reason', '')
                })

        return report_data
    except Exception as e:
        logger.error(f"Błąd podczas generowania raportu urlopów: {e}")
        raise HTTPException(status_code=500, detail="Błąd podczas generowania raportu urlopów.")


@router.get("/availabilities", response_model=List[schemas.AvailabilityReportRequest])
async def get_availabilities_report(
        start_date: date,
        end_date: date,
        franchise_code: Optional[str] = None,
        user_id: Optional[str] = None,
        current_user: dict = Depends(get_current_admin_user),
        db: motor.motor_asyncio.AsyncIOMotorClient = Depends(get_db)
):
    """
    Generuje raport dostępności pracowników dla administratora.
    """
    try:
        query = {
            "date": {
                "$gte": datetime.combine(start_date, datetime.min.time()),
                "$lte": datetime.combine(end_date, datetime.max.time())
            }
        }

        # Filtrowanie po user_id
        if user_id:
            query["user_id"] = ObjectId(user_id)

        # Filtrowanie po franczyzie
        if franchise_code:
            franchise_users = await db.users.find({"franchise_code": franchise_code}).to_list(length=None)
            franchise_user_ids = [user["_id"] for user in franchise_users]
            query["user_id"] = {"$in": franchise_user_ids}

        availabilities = await db.availabilities.find(query).to_list(length=None)

        user_ids = list({a["user_id"] for a in availabilities})
        users = await db.users.find({"_id": {"$in": user_ids}}).to_list(length=None)
        user_dict = {user["_id"]: user for user in users}

        report_data = []
        for a in availabilities:
            user_info = user_dict.get(a["user_id"])
            if user_info:
                report_data.append({
                    "user_id": str(a['user_id']),
                    "first_name": user_info.get("first_name", ""),
                    "last_name": user_info.get("last_name", ""),
                    "date": a['date'],
                    "start_time": a['start_time'],
                    "end_time": a['end_time'],
                    "period_type": a['period_type'],
                })

        return report_data
    except Exception as e:
        logger.error(f"Błąd podczas generowania raportu dostępności: {e}")
        raise HTTPException(status_code=500, detail="Błąd podczas generowania raportu dostępności.")
