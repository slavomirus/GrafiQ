from fastapi import APIRouter, Depends, HTTPException, status, Query
from fastapi.responses import StreamingResponse
from datetime import datetime, timedelta, time, date
from typing import List, Optional, Dict
from bson import ObjectId
from bson.errors import InvalidId
import logging
import motor.motor_asyncio
from io import BytesIO
import calendar
import os

from ..database import get_db
from ..dependencies import get_current_active_user, get_current_admin_user, get_current_admin_user_query
from .. import models, schemas
from ..services.vacation_service import check_vacation_conflict
from ..services.pdf_service import generate_schedule_pdf, FONT_NAME, FONT_NAME_BOLD

router = APIRouter()
logger = logging.getLogger(__name__)

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib.units import cm

def generate_vacation_request_pdf(user: dict, vacation: dict) -> BytesIO:
    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    
    c.setFont(FONT_NAME_BOLD, 16)
    c.drawString(2*cm, 27*cm, "WNIOSEK O URLOP WYPOCZYNKOWY")
    
    c.setFont(FONT_NAME, 12)
    c.drawString(14*cm, 28*cm, f"Data: {datetime.now().strftime('%Y-%m-%d')}")
    
    c.drawString(2*cm, 25*cm, f"Pracownik: {user.get('first_name')} {user.get('last_name')}")
    c.drawString(2*cm, 24.5*cm, f"Stanowisko: {user.get('role', 'Pracownik')}")
    
    c.drawString(2*cm, 22*cm, "Proszę o udzielenie urlopu wypoczynkowego w terminie:")
    
    start_str = vacation['start_date'].strftime('%Y-%m-%d')
    end_str = vacation['end_date'].strftime('%Y-%m-%d')
    days = vacation.get('days_requested', 0)
    
    c.setFont(FONT_NAME_BOLD, 12)
    c.drawString(2*cm, 21*cm, f"Od: {start_str}   Do: {end_str}")
    c.drawString(2*cm, 20.5*cm, f"Liczba dni roboczych: {days}")
    
    if vacation.get('reason'):
        c.setFont(FONT_NAME, 12)
        c.drawString(2*cm, 19.5*cm, f"Uzasadnienie: {vacation['reason']}")
    
    c.line(2*cm, 15*cm, 8*cm, 15*cm)
    c.drawString(2*cm, 14.5*cm, "Podpis pracownika")
    
    c.line(12*cm, 15*cm, 18*cm, 15*cm)
    c.drawString(12*cm, 14.5*cm, "Podpis pracodawcy")
    
    c.save()
    buffer.seek(0)
    return buffer

async def populate_user_details(db, vacations, franchise_code):
    users = await db.users.find({"franchise_code": franchise_code}).to_list(length=None)
    user_map = {u["_id"]: u for u in users}
    
    for v in vacations:
        user_id = v.get("user_id")
        if user_id in user_map:
            user = user_map[user_id]
            first_name = user.get("first_name", "")
            last_name = user.get("last_name", "")
            
            v["first_name"] = first_name
            v["last_name"] = last_name
            v["firstName"] = first_name
            v["lastName"] = last_name
            v["employee"] = {
                "id": str(user_id),
                "first_name": first_name,
                "last_name": last_name,
                "firstName": first_name,
                "lastName": last_name
            }
            v["user"] = v["employee"]
        
        if isinstance(v.get("start_date"), datetime):
            v["start_date"] = v["start_date"].date()
        if isinstance(v.get("end_date"), datetime):
            v["end_date"] = v["end_date"].date()
            
        if "submitted_at" not in v:
            try:
                v["submitted_at"] = v["_id"].generation_time
            except:
                v["submitted_at"] = datetime.utcnow()
        
        if "created_at" not in v:
            v["created_at"] = v.get("submitted_at")
            
    return vacations

@router.get("/store-vacations", response_model=Dict[str, List[schemas.Vacation]])
async def get_store_vacations(
    year: int = Query(..., description="Rok"),
    month: int = Query(..., description="Miesiąc"),
    current_user: dict = Depends(get_current_active_user),
    db: motor.motor_asyncio.AsyncIOMotorClient = Depends(get_db)
):
    try:
        franchise_code = current_user.get("franchise_code")
        if not franchise_code:
             raise HTTPException(status_code=400, detail="Użytkownik nie jest przypisany do sklepu")

        _, last_day = calendar.monthrange(year, month)
        start_date = datetime(year, month, 1)
        end_date = datetime(year, month, last_day, 23, 59, 59)

        users = await db.users.find({"franchise_code": franchise_code}).to_list(length=None)
        user_map = {u["_id"]: u for u in users}
        result = {str(u["_id"]): [] for u in users}

        query = {
            "franchise_code": franchise_code,
            "status": models.VacationStatus.APPROVED.value,
            "start_date": {"$lte": end_date},
            "end_date": {"$gte": start_date}
        }

        vacations = await db.vacations.find(query).sort("start_date", 1).to_list(length=None)

        for v in vacations:
            user_id_oid = v.get("user_id")
            if user_id_oid in user_map:
                user = user_map[user_id_oid]
                first_name = user.get("first_name", "")
                last_name = user.get("last_name", "")
                
                v["first_name"] = first_name
                v["last_name"] = last_name
                v["firstName"] = first_name
                v["lastName"] = last_name
                v["employee"] = {
                    "id": str(user_id_oid),
                    "first_name": first_name,
                    "last_name": last_name,
                    "firstName": first_name,
                    "lastName": last_name
                }
                v["user"] = v["employee"]

            if isinstance(v.get("start_date"), datetime):
                v["start_date"] = v["start_date"].date()
            if isinstance(v.get("end_date"), datetime):
                v["end_date"] = v["end_date"].date()
            
            if "submitted_at" not in v:
                try:
                    v["submitted_at"] = v["_id"].generation_time
                except:
                    v["submitted_at"] = datetime.utcnow()
            
            if "created_at" not in v:
                v["created_at"] = v.get("submitted_at")

            user_id = str(v["user_id"])
            if user_id in result:
                result[user_id].append(v)
            else:
                result[user_id] = [v]

        return result

    except Exception as e:
        logger.error(f"Błąd podczas pobierania urlopów sklepu: {e}")
        raise HTTPException(status_code=500, detail="Błąd serwera")

@router.get("/my-vacations", response_model=List[schemas.Vacation])
async def get_my_vacations(
        status: Optional[str] = None,
        current_user: dict = Depends(get_current_active_user),
        db: motor.motor_asyncio.AsyncIOMotorClient = Depends(get_db)
):
    try:
        query = {"user_id": ObjectId(current_user["_id"])}

        if status:
            query["status"] = status

        vacations = await db.vacations.find(query).sort("submitted_at", -1).to_list(length=None)
        
        for v in vacations:
            first_name = current_user.get("first_name", "")
            last_name = current_user.get("last_name", "")
            
            v["first_name"] = first_name
            v["last_name"] = last_name
            v["firstName"] = first_name
            v["lastName"] = last_name
            v["employee"] = {
                "id": str(current_user["_id"]),
                "first_name": first_name,
                "last_name": last_name,
                "firstName": first_name,
                "lastName": last_name
            }
            v["user"] = v["employee"]
            
            if isinstance(v.get("start_date"), datetime):
                v["start_date"] = v["start_date"].date()
            if isinstance(v.get("end_date"), datetime):
                v["end_date"] = v["end_date"].date()
                
            if "submitted_at" not in v:
                try:
                    v["submitted_at"] = v["_id"].generation_time
                except:
                    v["submitted_at"] = datetime.utcnow()
            
            if "created_at" not in v:
                v["created_at"] = v.get("submitted_at")
                
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
    try:
        if vacation.start_date > vacation.end_date:
            raise HTTPException(status_code=400, detail="Data rozpoczęcia nie może być późniejsza niż data zakończenia")

        if vacation.start_date < datetime.now().date():
            raise HTTPException(status_code=400, detail="Nie można składać wniosków urlopowych z datą wsteczną")

        user = await db.users.find_one({"_id": ObjectId(current_user["_id"])})
        if not user:
            raise HTTPException(status_code=404, detail="Użytkownik nie znaleziony")

        vacation_days = (vacation.end_date - vacation.start_date).days + 1
        is_franchisee = user.get("role") == models.UserRole.FRANCHISEE.value
        
        if not is_franchisee and user.get("vacation_days_left", 0) < vacation_days:
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
                    "status": {"$in": [models.VacationStatus.APPROVED.value, models.VacationStatus.PENDING.value]}
                }
            ]
        })

        if conflicting_vacation:
            raise HTTPException(
                status_code=400,
                detail="Masz już złożony wniosek lub zaakceptowany urlop w podanym okresie"
            )
            
        initial_status = models.VacationStatus.APPROVED.value if is_franchisee else models.VacationStatus.PENDING.value
        
        submitted_at = datetime.utcnow()
        vacation_data = {
            "user_id": ObjectId(current_user["_id"]),
            "franchise_code": user.get("franchise_code"),
            "start_date": start_datetime,
            "end_date": end_datetime,
            "reason": vacation.reason,
            "status": initial_status,
            "submitted_at": submitted_at,
            "days_requested": vacation_days
        }
        
        if is_franchisee:
            vacation_data["approved_by_id"] = ObjectId(current_user["_id"])
            vacation_data["reviewed_at"] = datetime.utcnow()

        result = await db.vacations.insert_one(vacation_data)
        created_vacation = await db.vacations.find_one({"_id": result.inserted_id})
        
        response_data = created_vacation.copy()
        response_data["start_date"] = created_vacation["start_date"].date()
        response_data["end_date"] = created_vacation["end_date"].date()
        response_data["created_at"] = submitted_at

        first_name = user.get("first_name", "")
        last_name = user.get("last_name", "")
        response_data["first_name"] = first_name
        response_data["last_name"] = last_name
        response_data["firstName"] = first_name
        response_data["lastName"] = last_name
        response_data["employee"] = {
            "id": str(user["_id"]),
            "first_name": first_name,
            "last_name": last_name,
            "firstName": first_name,
            "lastName": last_name
        }
        response_data["user"] = response_data["employee"]
        
        return response_data

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Błąd podczas składania wniosku urlopowego: {e}")
        raise HTTPException(status_code=500, detail="Błąd podczas składania wniosku urlopowego")

@router.get("/pending", response_model=List[schemas.Vacation])
async def get_pending_vacations(
    current_user: dict = Depends(get_current_admin_user),
    db: motor.motor_asyncio.AsyncIOMotorClient = Depends(get_db)
):
    franchise_code = current_user.get("franchise_code")
    vacations = await db.vacations.find({
        "franchise_code": franchise_code,
        "status": models.VacationStatus.PENDING.value
    }).to_list(length=None)
    
    return await populate_user_details(db, vacations, franchise_code)

@router.get("/all", response_model=List[schemas.Vacation])
async def get_all_vacations(
    current_user: dict = Depends(get_current_admin_user),
    db: motor.motor_asyncio.AsyncIOMotorClient = Depends(get_db)
):
    franchise_code = current_user.get("franchise_code")
    vacations = await db.vacations.find({
        "franchise_code": franchise_code
    }).sort("start_date", -1).to_list(length=None)
    
    return await populate_user_details(db, vacations, franchise_code)

@router.get("/user/{user_id}", response_model=List[schemas.Vacation])
async def get_user_vacations(
    user_id: str,
    current_user: dict = Depends(get_current_admin_user),
    db: motor.motor_asyncio.AsyncIOMotorClient = Depends(get_db)
):
    try:
        user_oid = ObjectId(user_id)
    except:
        raise HTTPException(status_code=400, detail="Invalid ID")
        
    user = await db.users.find_one({"_id": user_oid})
    if not user or user.get("franchise_code") != current_user.get("franchise_code"):
        raise HTTPException(status_code=404, detail="Pracownik nie znaleziony")

    vacations = await db.vacations.find({
        "user_id": user_oid
    }).sort("start_date", -1).to_list(length=None)
    
    for v in vacations:
        first_name = user.get("first_name", "")
        last_name = user.get("last_name", "")
        
        v["first_name"] = first_name
        v["last_name"] = last_name
        v["firstName"] = first_name
        v["lastName"] = last_name
        v["employee"] = {
            "id": str(user["_id"]),
            "first_name": first_name,
            "last_name": last_name,
            "firstName": first_name,
            "lastName": last_name
        }
        v["user"] = v["employee"]
        
        if isinstance(v.get("start_date"), datetime):
            v["start_date"] = v["start_date"].date()
        if isinstance(v.get("end_date"), datetime):
            v["end_date"] = v["end_date"].date()

        if "submitted_at" not in v:
            try:
                v["submitted_at"] = v["_id"].generation_time
            except:
                v["submitted_at"] = datetime.utcnow()
        
        if "created_at" not in v:
            v["created_at"] = v.get("submitted_at")
            
    return vacations

@router.post("/{vacation_id}/approve", response_model=schemas.MessageResponse)
async def approve_vacation(
    vacation_id: str,
    current_user: dict = Depends(get_current_admin_user),
    db: motor.motor_asyncio.AsyncIOMotorClient = Depends(get_db)
):
    try:
        vac_oid = ObjectId(vacation_id)
        vacation = await db.vacations.find_one({"_id": vac_oid})
        if not vacation:
            raise HTTPException(status_code=404, detail="Wniosek nie znaleziony")
            
        if vacation.get("franchise_code") != current_user.get("franchise_code"):
            raise HTTPException(status_code=403, detail="Brak uprawnień")
            
        if vacation["status"] != models.VacationStatus.PENDING.value:
            raise HTTPException(status_code=400, detail="Wniosek nie jest w statusie oczekującym")
            
        days = vacation.get("days_requested", 0)
        user_id = vacation["user_id"]
        
        user = await db.users.find_one({"_id": user_id})
        if user["vacation_days_left"] < days:
             raise HTTPException(status_code=400, detail="Pracownik nie ma już wystarczającej liczby dni (stan mógł się zmienić).")
             
        await db.users.update_one(
            {"_id": user_id},
            {"$inc": {"vacation_days_left": -days}}
        )
        
        await db.vacations.update_one(
            {"_id": vac_oid},
            {"$set": {
                "status": models.VacationStatus.APPROVED.value,
                "approved_by_id": current_user["_id"],
                "reviewed_at": datetime.utcnow()
            }}
        )
        
        return {"message": "Wniosek zaakceptowany, dni urlopowe odjęte."}
        
    except InvalidId:
        raise HTTPException(status_code=400, detail="Invalid ID")

@router.post("/{vacation_id}/reject", response_model=schemas.MessageResponse)
async def reject_vacation(
    vacation_id: str,
    current_user: dict = Depends(get_current_admin_user),
    db: motor.motor_asyncio.AsyncIOMotorClient = Depends(get_db)
):
    try:
        vac_oid = ObjectId(vacation_id)
        vacation = await db.vacations.find_one({"_id": vac_oid})
        if not vacation:
            raise HTTPException(status_code=404, detail="Wniosek nie znaleziony")
            
        if vacation.get("franchise_code") != current_user.get("franchise_code"):
            raise HTTPException(status_code=403, detail="Brak uprawnień")
            
        await db.vacations.update_one(
            {"_id": vac_oid},
            {"$set": {
                "status": models.VacationStatus.REJECTED.value,
                "approved_by_id": current_user["_id"],
                "reviewed_at": datetime.utcnow()
            }}
        )
        
        return {"message": "Wniosek odrzucony."}
        
    except InvalidId:
        raise HTTPException(status_code=400, detail="Invalid ID")

@router.get("/{vacation_id}/pdf")
async def get_vacation_pdf(
    vacation_id: str,
    current_user: dict = Depends(get_current_admin_user_query),
    db: motor.motor_asyncio.AsyncIOMotorClient = Depends(get_db)
):
    try:
        vac_oid = ObjectId(vacation_id)
        vacation = await db.vacations.find_one({"_id": vac_oid})
        if not vacation:
            raise HTTPException(status_code=404, detail="Wniosek nie znaleziony")
            
        if vacation.get("status") != models.VacationStatus.APPROVED.value:
            raise HTTPException(status_code=403, detail="Można generować PDF tylko dla zaakceptowanych wniosków.")

        user = await db.users.find_one({"_id": vacation["user_id"]})
        
        pdf_buffer = generate_vacation_request_pdf(user, vacation)
        
        safe_lastname = "".join(c for c in user.get('last_name', 'Employee') if c.isalnum())
        filename = f"wniosek_urlopowy_{safe_lastname}_{vacation['start_date'].date()}.pdf"
        
        return StreamingResponse(
            pdf_buffer, 
            media_type="application/pdf",
            headers={"Content-Disposition": f"inline; filename=\"{filename}\""}
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Błąd PDF: {e}")
        raise HTTPException(status_code=500, detail="Błąd generowania PDF")

@router.get("/stats/{user_id}")
async def get_vacation_stats(
    user_id: str,
    current_user: dict = Depends(get_current_active_user),
    db: motor.motor_asyncio.AsyncIOMotorClient = Depends(get_db)
):
    """
    Zwraca statystyki urlopowe użytkownika (użyte dni, pozostałe dni).
    Dostępne dla danego pracownika lub jego przełożonego.
    """
    try:
        if current_user["role"] == models.UserRole.EMPLOYEE.value:
            if user_id != "me" and user_id != str(current_user["_id"]):
                raise HTTPException(status_code=403, detail="Możesz sprawdzić tylko własne statystyki urlopowe.")
            target_user_id = ObjectId(current_user["_id"])
        else:
            if user_id == "me":
                target_user_id = ObjectId(current_user["_id"])
            else:
                target_user_id = ObjectId(user_id)

        user = await db.users.find_one({"_id": target_user_id})
        if not user:
            raise HTTPException(status_code=404, detail="Użytkownik nie znaleziony.")
            
        # Zabezpieczenie na poziomie franczyzy dla adminów
        if current_user["role"] in [models.UserRole.FRANCHISEE.value, models.UserRole.ADMIN.value]:
            if user.get("franchise_code") != current_user.get("franchise_code"):
                raise HTTPException(status_code=403, detail="Brak uprawnień.")

        current_year = datetime.utcnow().year
        start_of_year = datetime(current_year, 1, 1)
        end_of_year = datetime(current_year, 12, 31, 23, 59, 59)

        # Pobieramy zaakceptowane wnioski w danym roku
        vacations = await db.vacations.find({
            "user_id": target_user_id,
            "status": models.VacationStatus.APPROVED.value,
            "start_date": {"$gte": start_of_year, "$lte": end_of_year}
        }).to_list(length=None)

        used_days = sum(v.get("days_requested", 0) for v in vacations)
        remaining_days = user.get("vacation_days_left", 26)

        return {
            "used_days": used_days,
            "remaining_days": remaining_days
        }

    except InvalidId:
        raise HTTPException(status_code=400, detail="Nieprawidłowy ID użytkownika.")
    except HTTPException as he:
        raise he
    except Exception as e:
        logger.error(f"Błąd podczas pobierania statystyk urlopowych: {e}")
        raise HTTPException(status_code=500, detail="Błąd serwera.")
