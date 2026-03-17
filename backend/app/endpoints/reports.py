from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from datetime import date, datetime, timedelta
from typing import List, Optional
import logging
import motor.motor_asyncio
from bson import ObjectId

from ..database import get_db
from ..dependencies import get_current_admin_user, get_current_active_user
from .. import schemas, models
from ..services.pdf_service import generate_hours_report_pdf, parse_time

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/hours", response_model=List[schemas.ReportItem]) # ZMIANA: użycie nowego modelu
async def get_hours_report(
        start_date: date,
        end_date: date,
        franchise_code: Optional[str] = None,
        user_id: Optional[str] = None,
        current_user: dict = Depends(get_current_active_user),
        db: motor.motor_asyncio.AsyncIOMotorClient = Depends(get_db)
):
    """
    Generuje raport godzin pracy (JSON).
    Dostępny dla admina/franczyzobiorcy (widzi wszystkich) oraz pracownika (widzi tylko siebie).
    """
    try:
        # --- ZMIANA: RBAC (Sprawdzanie uprawnień) ---
        if current_user["role"] == models.UserRole.EMPLOYEE.value:
            if user_id and user_id != str(current_user["_id"]):
                raise HTTPException(status_code=403, detail="Brak dostępu do danych innych pracowników.")
            # Pracownik może widzieć tylko swoje dane
            user_id = str(current_user["_id"])
            franchise_code = current_user.get("franchise_code")
        else:
            # Dla admina/franchisee, upewnijmy się że franchise_code jest ustawiony (zabezpieczenie)
            if not franchise_code and current_user["role"] == models.UserRole.FRANCHISEE.value:
                franchise_code = current_user.get("franchise_code")
        # --------------------------------------------

        query = {
            "date": {
                "$gte": datetime.combine(start_date, datetime.min.time()),
                "$lte": datetime.combine(end_date, datetime.max.time())
            }
        }

        # Filtrowanie po user_id
        if user_id:
            query["user_id"] = ObjectId(user_id)

        # Filtrowanie po franczyzie (jeśli nie podano konkretnego user_id lub jesteśmy adminem sklepu)
        if franchise_code and not user_id:
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
                # Use parse_time helper for robustness
                start_time = parse_time(s['start_time'])
                end_time = parse_time(s['end_time'])
                
                if start_time and end_time:
                    start = datetime.combine(s['date'], start_time)
                    end = datetime.combine(s['date'], end_time)
                    duration = (end - start).total_seconds() / 3600

                    # Konwersja czasu na string dla Pydantic model (zgodnie ze schematem)
                    start_str = start_time.strftime('%H:%M:%S') if hasattr(start_time, 'strftime') else str(start_time)
                    end_str = end_time.strftime('%H:%M:%S') if hasattr(end_time, 'strftime') else str(end_time)

                    report_data.append({
                        "user_id": str(s['user_id']),
                        "first_name": user_info.get("first_name", ""),
                        "last_name": user_info.get("last_name", ""),
                        "date": s['date'],
                        "start_time": start_str,
                        "end_time": end_str,
                        "hours": duration,
                    })

        return report_data
    except HTTPException as he:
        raise he
    except Exception as e:
        logger.error(f"Błąd podczas generowania raportu godzin: {e}")
        raise HTTPException(status_code=500, detail="Błąd podczas generowania raportu godzin.")

@router.get("/hours/pdf")
async def get_hours_report_pdf_endpoint(
        start_date: date,
        end_date: date,
        user_id: Optional[str] = None,
        current_user: dict = Depends(get_current_active_user), # Allow active users (employees can download their own)
        db: motor.motor_asyncio.AsyncIOMotorClient = Depends(get_db)
):
    """
    Generuje raport godzin pracy w formacie PDF.
    """
    try:
        # Security check: Employee can only download their own report
        if current_user["role"] == models.UserRole.EMPLOYEE.value:
            if user_id and user_id != "me" and user_id != str(current_user["_id"]):
                raise HTTPException(status_code=403, detail="Możesz pobrać tylko własny raport.")
            target_user_id = current_user["_id"]
        else:
            # Admin/Franchisee can download for specific user or all
            if user_id == "me":
                target_user_id = current_user["_id"]
            elif user_id:
                target_user_id = ObjectId(user_id)
            else:
                target_user_id = None # All users

        query = {
            "date": {
                "$gte": datetime.combine(start_date, datetime.min.time()),
                "$lte": datetime.combine(end_date, datetime.max.time())
            }
        }

        if target_user_id:
            query["user_id"] = target_user_id
        else:
            # If no specific user, filter by franchise (for admin/franchisee)
            franchise_code = current_user.get("franchise_code")
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
                # Use helper function to parse time securely (handles HH:MM and HH:MM:SS)
                s_start = parse_time(s['start_time'])
                s_end = parse_time(s['end_time'])
                
                if s_start and s_end:
                    start_dt = datetime.combine(s['date'], s_start)
                    end_dt = datetime.combine(s['date'], s_end)
                    duration = (end_dt - start_dt).total_seconds() / 3600

                    report_data.append({
                        "user_id": str(s['user_id']),
                        "first_name": user_info.get("first_name", ""),
                        "last_name": user_info.get("last_name", ""),
                        "date": s['date'],
                        "start_time": s_start,
                        "end_time": s_end,
                        "hours": duration,
                    })

        pdf_buffer = generate_hours_report_pdf(report_data, start_date, end_date)
        
        filename = f"Raport_Godzin_{start_date}_{end_date}.pdf"
        
        return StreamingResponse(
            pdf_buffer, 
            media_type="application/pdf",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )

    except Exception as e:
        logger.error(f"Błąd podczas generowania PDF raportu godzin: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Błąd podczas generowania raportu PDF.")


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
