# Plik: backend/app/endpoints/schedule.py

from fastapi import APIRouter, Depends, HTTPException, status, Query
from fastapi.responses import StreamingResponse
from fastapi.encoders import jsonable_encoder
import logging
from typing import List, Dict, Any, Optional, Union
from bson import ObjectId
from datetime import date, time, datetime, timedelta
import calendar

from ..database import get_db
from ..dependencies import get_current_admin_user, get_current_user, get_current_active_user, get_current_admin_user_query
from ..services.schedule_service import (
    publish_schedule_draft, 
    get_employee_schedule, 
    get_all_drafts_for_store, 
    get_schedules_history, 
    update_shift_hours, 
    update_draft_shift,
    add_shift_to_schedule,
    delete_shift_from_schedule,
    enrich_schedule_data,
    validate_labor_laws,
    get_store_settings_and_holidays,
    apply_shift_times
)
from ..services.sick_leave_service import process_sick_leave
from ..services.pdf_service import generate_schedule_pdf, generate_monthly_schedule_pdf
from ..services.notification_service import send_push_to_user, send_push_to_admins
from .. import schemas, models
import motor.motor_asyncio
import base64

router = APIRouter()
router_schedules = APIRouter()

logger = logging.getLogger(__name__)

@router.put("/shifts/{shift_id}", response_model=schemas.MessageResponse)
async def update_shift_hours_endpoint(
    shift_id: str,
    shift_data: schemas.ShiftHoursUpdate,
    current_user: dict = Depends(get_current_admin_user),
    db: motor.motor_asyncio.AsyncIOMotorDatabase = Depends(get_db)
):
    try:
        result = await update_shift_hours(db, shift_id, shift_data, current_user)
        return result
    except HTTPException as he:
        raise he
    except Exception as e:
        logger.error(f"Nieoczekiwany błąd podczas aktualizacji zmiany {shift_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Wystąpił wewnętrzny błąd serwera podczas aktualizacji zmiany."
        )

@router.put("/drafts/{draft_id}/shifts", response_model=schemas.MessageResponse)
async def update_draft_shift_endpoint(
    draft_id: str,
    shift_data: schemas.DraftShiftUpdate,
    current_user: dict = Depends(get_current_admin_user),
    db: motor.motor_asyncio.AsyncIOMotorDatabase = Depends(get_db)
):
    try:
        result = await update_draft_shift(db, draft_id, shift_data, current_user)
        return result
    except HTTPException as he:
        raise he
    except Exception as e:
        logger.error(f"Nieoczekiwany błąd podczas edycji draftu {draft_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Wystąpił wewnętrzny błąd serwera podczas edycji draftu."
        )

async def _get_schedules_logic(
    months: int,
    published_only: bool,
    start_date: Optional[date],
    end_date: Optional[date],
    current_user: dict,
    db: motor.motor_asyncio.AsyncIOMotorDatabase
):
    try:
        history = await get_schedules_history(
            db, 
            current_user, 
            months=months, 
            published_only=published_only,
            start_date=start_date,
            end_date=end_date
        )
        return history
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Nieoczekiwany błąd podczas pobierania historii grafików: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Wystąpił wewnętrzny błąd serwera.")

@router.get("/history", response_model=List[schemas.ScheduleDraftResponse])
async def get_schedules_history_endpoint(
    months: int = 6,
    published_only: bool = True,
    start_date: Optional[date] = Query(None),
    end_date: Optional[date] = Query(None),
    current_user: dict = Depends(get_current_active_user),
    db: motor.motor_asyncio.AsyncIOMotorDatabase = Depends(get_db)
):
    if current_user["role"] == models.UserRole.EMPLOYEE.value:
        published_only = True
    return await _get_schedules_logic(months, published_only, start_date, end_date, current_user, db)

@router_schedules.get("", response_model=List[schemas.ScheduleDraftResponse])
async def get_schedules_list_endpoint(
    months: int = 6,
    published_only: bool = True,
    start_date: Optional[date] = Query(None),
    end_date: Optional[date] = Query(None),
    current_user: dict = Depends(get_current_active_user),
    db: motor.motor_asyncio.AsyncIOMotorDatabase = Depends(get_db)
):
    if current_user["role"] == models.UserRole.EMPLOYEE.value:
        published_only = True
    return await _get_schedules_logic(months, published_only, start_date, end_date, current_user, db)

@router.get("/drafts", response_model=List[schemas.ScheduleDraftResponse])
async def get_schedule_drafts_endpoint(
    current_user: dict = Depends(get_current_admin_user),
    db: motor.motor_asyncio.AsyncIOMotorDatabase = Depends(get_db)
):
    try:
        drafts = await get_all_drafts_for_store(db, current_user)
        return drafts
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Nieoczekiwany błąd podczas pobierania wersji roboczych: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Wystąpił wewnętrzny błąd serwera.")

@router.post("/{draft_id}/publish", status_code=status.HTTP_200_OK)
async def publish_schedule_endpoint(
    draft_id: str,
    current_user: dict = Depends(get_current_admin_user),
    db: motor.motor_asyncio.AsyncIOMotorDatabase = Depends(get_db)
):
    try:
        # publish_schedule_draft invokes apply_shift_times and validate_labor_laws natively now
        result = await publish_schedule_draft(db, draft_id, current_user)
        
        employees = await db.users.find({
            "franchise_code": current_user.get("franchise_code"),
            "role": "employee"
        }).to_list(length=None)
        
        for emp in employees:
            await send_push_to_user(
                db,
                emp["_id"],
                "Nowy grafik opublikowany",
                "Sprawdź swoje zmiany w aplikacji.",
                data={"type": "schedule_published"}
            )
        
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Nieoczekiwany błąd podczas publikowania grafiku {draft_id}: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Wystąpił wewnętrzny błąd serwera.")

@router.get("/my-schedule", response_model=List[schemas.ScheduleResponse], status_code=status.HTTP_200_OK)
async def get_my_schedule_endpoint(
    month: Optional[int] = Query(None, ge=1, le=12),
    year: Optional[int] = Query(None, ge=2020),
    current_user: dict = Depends(get_current_user),
    db: motor.motor_asyncio.AsyncIOMotorDatabase = Depends(get_db)
):
    try:
        schedule = await get_employee_schedule(db, current_user, month, year)
        return schedule
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Nieoczekiwany błąd podczas pobierania grafiku: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Wystąpił wewnętrzny błąd serwera.")

@router.get("/month-pdf")
async def get_monthly_schedule_pdf_endpoint(
    month: int = Query(..., ge=1, le=12),
    year: int = Query(..., ge=2020),
    current_user: dict = Depends(get_current_admin_user_query),
    db: motor.motor_asyncio.AsyncIOMotorDatabase = Depends(get_db)
):
    try:
        franchise_code = current_user.get("franchise_code")
        
        _, last_day = calendar.monthrange(year, month)
        start_date = datetime(year, month, 1)
        end_date = datetime(year, month, last_day, 23, 59, 59)
        
        schedules_cursor = db.schedules.find({
            "franchise_code": franchise_code,
            "$or": [
                {"start_date": {"$lte": end_date}, "end_date": {"$gte": start_date}}
            ]
        })
        
        merged_schedule_content = {}
        async for sched in schedules_cursor:
            sched_content = sched.get("schedule", {})
            merged_schedule_content.update(sched_content)
            
        store_settings = await db.store_settings.find_one({"franchise_code": franchise_code}) or {}
        
        sick_leaves = await db.sick_leaves.find({
            "franchise_code": franchise_code,
            "$or": [
                {"start_date": {"$lte": end_date}, "end_date": {"$gte": start_date}}
            ]
        }).to_list(length=None)
        
        all_employees = await db.users.find({
            "franchise_code": franchise_code,
            "role": models.UserRole.EMPLOYEE.value
        }).to_list(length=None)
        
        schedule_data_wrapper = {
            "franchise_code": franchise_code,
            "schedule": merged_schedule_content,
            "start_date": start_date,
            "end_date": end_date
        }
        
        # WZBOGACENIE O GODZINY (Święta i Ustawienia)
        await enrich_schedule_data(db, franchise_code, schedule_data_wrapper)
        
        pdf_buffer = generate_monthly_schedule_pdf(
            month, year, 
            schedule_data_wrapper, 
            store_settings, 
            sick_leaves, 
            all_employees
        )
        
        filename = f"grafik_{month:02d}_{year}.pdf"
        
        return StreamingResponse(
            pdf_buffer, 
            media_type="application/pdf",
            headers={"Content-Disposition": f"inline; filename=\"{filename}\""}
        )
        
    except Exception as e:
        logger.error(f"Błąd generowania miesięcznego PDF: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Błąd generowania PDF")

@router.get("/{schedule_id}/pdf")
async def get_schedule_pdf_endpoint(
    schedule_id: str,
    current_user: dict = Depends(get_current_admin_user_query),
    db: motor.motor_asyncio.AsyncIOMotorDatabase = Depends(get_db)
):
    try:
        if schedule_id == "undefined":
            raise HTTPException(status_code=400, detail="Invalid Schedule ID (undefined). Please refresh the page.")

        try:
            oid = ObjectId(schedule_id)
        except:
             raise HTTPException(status_code=400, detail="Invalid ID format")

        franchise_code = current_user.get("franchise_code")
        
        schedule = await db.schedules.find_one({"_id": oid, "franchise_code": franchise_code})
        if not schedule:
            schedule = await db.schedule_drafts.find_one({"_id": oid, "franchise_code": franchise_code})
            
        if not schedule:
            raise HTTPException(status_code=404, detail="Grafik nie znaleziony")

        store_settings = await db.store_settings.find_one({"franchise_code": franchise_code}) or {}

        # WZBOGACENIE O GODZINY (Święta i Ustawienia)
        await enrich_schedule_data(db, franchise_code, schedule)

        start_date = schedule.get("start_date")
        end_date = schedule.get("end_date")
        
        if isinstance(start_date, str): start_date = datetime.fromisoformat(start_date)
        if isinstance(end_date, str): end_date = datetime.fromisoformat(end_date)
        
        if isinstance(start_date, date) and not isinstance(start_date, datetime):
            start_date = datetime.combine(start_date, time.min)
        if isinstance(end_date, date) and not isinstance(end_date, datetime):
            end_date = datetime.combine(end_date, time.max)

        sick_leaves = await db.sick_leaves.find({
            "franchise_code": franchise_code,
            "$or": [
                {"start_date": {"$lte": end_date}, "end_date": {"$gte": start_date}}
            ]
        }).to_list(length=None)

        all_employees = await db.users.find({
            "franchise_code": franchise_code,
            "role": models.UserRole.EMPLOYEE.value
        }).to_list(length=None)

        pdf_buffer = generate_schedule_pdf(schedule, store_settings, sick_leaves, all_employees)
        
        filename = f"grafik_{schedule.get('start_date')}_{schedule.get('end_date')}.pdf"
        
        return StreamingResponse(
            pdf_buffer, 
            media_type="application/pdf",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
    except HTTPException as he:
        raise he
    except Exception as e:
        logger.error(f"Błąd generowania PDF: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Błąd generowania PDF")

@router.get("/{schedule_id}", response_model=Union[schemas.ScheduleResponse, schemas.ScheduleDraftResponse])
async def get_schedule_details_endpoint(
    schedule_id: str,
    current_user: dict = Depends(get_current_active_user),
    db: motor.motor_asyncio.AsyncIOMotorDatabase = Depends(get_db)
):
    try:
        if schedule_id == "undefined":
             raise HTTPException(status_code=400, detail="Invalid Schedule ID (undefined).")

        try:
            oid = ObjectId(schedule_id)
        except:
             raise HTTPException(status_code=400, detail="Invalid ID format")

        franchise_code = current_user.get("franchise_code")
        
        schedule = await db.schedules.find_one({"_id": oid, "franchise_code": franchise_code})
        is_published = True
        
        if not schedule:
            if current_user["role"] == models.UserRole.EMPLOYEE.value:
                raise HTTPException(status_code=404, detail="Grafik nie znaleziony")
            schedule = await db.schedule_drafts.find_one({"_id": oid, "franchise_code": franchise_code})
            is_published = False

        if not schedule:
            raise HTTPException(status_code=404, detail="Grafik nie znaleziony")

        schedule["_id"] = str(schedule["_id"])
        if "status" not in schedule:
            schedule["status"] = "published" if is_published else "DRAFT"

        # WZBOGACENIE O GODZINY (Święta i Ustawienia)
        await enrich_schedule_data(db, franchise_code, schedule)
        
        users = await db.users.find({"franchise_code": franchise_code}).to_list(length=None)
        user_map = {str(u["_id"]): u for u in users}
        
        shifts_by_date = {}
        schedule_data = schedule.get("schedule", {})
        
        for date_str, day_schedule in schedule_data.items():
            shifts_list = []
            if isinstance(day_schedule, dict):
                # Obsługa is_closed (święta)
                if day_schedule.get("is_closed"):
                    shifts_by_date[date_str] = []
                    continue

                for shift_name, shift_details in day_schedule.items():
                    if isinstance(shift_details, dict):
                        # Puste/fallbacki zostały już nadpisane w enrich_schedule_data
                        start_time_str = shift_details.get("start_time", "00:00")
                        end_time_str = shift_details.get("end_time", "00:00")
                        
                        start_iso = f"{date_str}T{start_time_str}:00" if len(start_time_str) == 5 else f"{date_str}T{start_time_str}"
                        
                        try:
                            st = datetime.strptime(start_time_str, "%H:%M").time()
                            et = datetime.strptime(end_time_str, "%H:%M").time()
                            
                            dt_start = datetime.strptime(date_str, "%Y-%m-%d")
                            dt_end = dt_start
                            if et < st:
                                dt_end += timedelta(days=1)
                                
                            end_iso = dt_end.strftime("%Y-%m-%dT%H:%M:00")
                        except ValueError:
                            end_iso = f"{date_str}T{end_time_str}:00"

                        employees = shift_details.get("employees", [])
                        for emp in employees:
                            user_id = emp.get("id") if isinstance(emp, dict) else emp
                            if user_id:
                                # Poprawka KeyError: 'id' na '_id' dla MongoDB
                                raw_id = f"{schedule['_id']}:{date_str}:{shift_name}:{user_id}"
                                shift_id = base64.b64encode(raw_id.encode('utf-8')).decode('utf-8')

                                shift_obj = {
                                    "shift_id": shift_id,
                                    "user_id": user_id,
                                    "shift_type": shift_name,
                                    "start_time": start_iso,
                                    "end_time": end_iso
                                }
                                if user_id in user_map:
                                    user = user_map[user_id]
                                    first_name = user.get("first_name", "")
                                    last_name = user.get("last_name", "")
                                    
                                    shift_obj["first_name"] = first_name
                                    shift_obj["last_name"] = last_name
                                    shift_obj["firstName"] = first_name
                                    shift_obj["lastName"] = last_name
                                    shift_obj["employee"] = {
                                        "id": user_id,
                                        "first_name": first_name,
                                        "last_name": last_name,
                                        "firstName": first_name,
                                        "lastName": last_name
                                    }
                                    shift_obj["user"] = shift_obj["employee"]
                                    
                                shifts_list.append(shift_obj)
            shifts_by_date[date_str] = shifts_list
        
        schedule["shifts_by_date"] = shifts_by_date
        return schedule

    except HTTPException as he:
        raise he
    except Exception as e:
        logger.error(f"Błąd pobierania szczegółów grafiku {schedule_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Wystąpił wewnętrzny błąd serwera.")

@router.post("/sick-leave", response_model=schemas.SickLeaveResponse)
async def report_sick_leave_endpoint(
    request: schemas.SickLeaveRequest,
    current_user: dict = Depends(get_current_admin_user),
    db: motor.motor_asyncio.AsyncIOMotorDatabase = Depends(get_db)
):
    try:
        result = await process_sick_leave(db, request, current_user)
        return result
    except HTTPException as he:
        raise he
    except Exception as e:
        logger.error(f"Nieoczekiwany błąd podczas przetwarzania L4: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Wystąpił wewnętrzny błąd serwera podczas przetwarzania L4."
        )

@router.post("/requests", response_model=schemas.ShiftChangeRequestResponse)
async def create_shift_change_request(
    request_data: schemas.ShiftChangeRequestCreate,
    current_user: dict = Depends(get_current_active_user),
    db: motor.motor_asyncio.AsyncIOMotorDatabase = Depends(get_db)
):
    if current_user["role"] != models.UserRole.EMPLOYEE.value:
        raise HTTPException(status_code=403, detail="Tylko pracownicy mogą składać wnioski o zmianę grafiku.")
    
    today = date.today()
    min_date = today - timedelta(days=5)
    max_date = today + timedelta(days=5)
    
    if not (min_date <= request_data.date <= max_date):
        raise HTTPException(status_code=400, detail="Można edytować grafik tylko w zakresie +/- 5 dni od dzisiaj.")
        
    request_doc = models.ShiftChangeRequest(
        user_id=current_user["_id"],
        franchise_code=current_user["franchise_code"],
        **request_data.dict()
    )
    
    request_dict = jsonable_encoder(request_doc)
    
    request_dict["user_id"] = request_doc.user_id
    if "_id" in request_dict:
        request_dict["_id"] = request_doc.id
    
    await db.shift_change_requests.insert_one(request_dict)
    
    await send_push_to_admins(
        db, 
        current_user["franchise_code"], 
        "Nowy wniosek o zmianę grafiku", 
        f"Pracownik {current_user['first_name']} {current_user['last_name']} prosi o zmianę w dniu {request_data.date}."
    )
    
    return request_doc

@router.get("/requests", response_model=List[schemas.ShiftChangeRequestResponse])
async def get_shift_change_requests(
    status: Optional[str] = None,
    current_user: dict = Depends(get_current_active_user),
    db: motor.motor_asyncio.AsyncIOMotorDatabase = Depends(get_db)
):
    query = {"franchise_code": current_user["franchise_code"]}
    
    if current_user["role"] == models.UserRole.EMPLOYEE.value:
        query["user_id"] = current_user["_id"]
        
    if status:
        query["status"] = status
        
    requests = await db.shift_change_requests.find(query).sort("created_at", -1).to_list(length=100)
    return requests

@router.post("/requests/{request_id}/respond", response_model=schemas.ShiftChangeRequestResponse)
async def respond_to_shift_change_request(
    request_id: str,
    response_data: schemas.ShiftChangeResponseRequest,
    current_user: dict = Depends(get_current_admin_user),
    db: motor.motor_asyncio.AsyncIOMotorDatabase = Depends(get_db)
):
    try:
        req_oid = ObjectId(request_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Nieprawidłowy format ID.")
        
    request = await db.shift_change_requests.find_one({"_id": req_oid})
    if not request:
        raise HTTPException(status_code=404, detail="Wniosek nie znaleziony.")
        
    if request["franchise_code"] != current_user["franchise_code"]:
        raise HTTPException(status_code=403, detail="Brak uprawnień.")
        
    if request["status"] != models.ShiftChangeStatus.PENDING.value:
        raise HTTPException(status_code=400, detail="Wniosek został już rozpatrzony.")
        
    new_status = models.ShiftChangeStatus.ACCEPTED.value if response_data.response == "accepted" else models.ShiftChangeStatus.REJECTED.value
    
    await db.shift_change_requests.update_one(
        {"_id": req_oid},
        {
            "$set": {
                "status": new_status,
                "responded_at": datetime.utcnow(),
                "responded_by_id": current_user["_id"]
            }
        }
    )
    
    if new_status == models.ShiftChangeStatus.ACCEPTED.value:
        req_date = request["date"]
        
        if isinstance(req_date, str):
            try:
                req_date = date.fromisoformat(req_date)
            except ValueError:
                try:
                    req_date = datetime.fromisoformat(req_date).date()
                except ValueError:
                    logger.error(f"Invalid date format in request {request_id}: {req_date}")
                    raise HTTPException(status_code=500, detail="Błąd formatu daty w bazie danych.")
        elif isinstance(req_date, datetime):
            req_date = req_date.date()
            
        schedule = await db.schedules.find_one({
            "franchise_code": request["franchise_code"],
            "is_published": True,
            "start_date": {"$lte": datetime.combine(req_date, time.min)},
            "end_date": {"$gte": datetime.combine(req_date, time.max)}
        })
        
        if schedule:
            date_str = req_date.strftime("%Y-%m-%d")
            day_schedule = schedule.get("schedule", {}).get(date_str, {})
            
            for s_name, s_data in day_schedule.items():
                if isinstance(s_data, dict):
                    emps = s_data.get("employees", [])
                    s_data["employees"] = [e for e in emps if e.get("id") != str(request["user_id"])]
            
            custom_shift_name = f"custom_{str(request['user_id'])[-4:]}"
            
            user = await db.users.find_one({"_id": request["user_id"]})
            user_obj = {
                "id": str(user["_id"]),
                "first_name": user["first_name"],
                "last_name": user["last_name"]
            }
            
            day_schedule[custom_shift_name] = {
                "start_time": request["requested_start_time"], 
                "end_time": request["requested_end_time"],
                "employees": [user_obj]
            }
            
            await db.schedules.update_one(
                {"_id": schedule["_id"]},
                {"$set": {f"schedule.{date_str}": day_schedule}}
            )
            
            await db.schedule.delete_many({
                "franchise_code": request["franchise_code"],
                "user_id": request["user_id"],
                "date": datetime.combine(req_date, time.min)
            })
            
            await db.schedule.insert_one({
                "franchise_code": request["franchise_code"],
                "user_id": request["user_id"],
                "date": datetime.combine(req_date, time.min),
                "shift_name": custom_shift_name,
                "start_time": request["requested_start_time"],
                "end_time": request["requested_end_time"],
                "published_at": datetime.utcnow(),
                "published_by": current_user.get("email")
            })
            
    await send_push_to_user(
        db,
        request["user_id"],
        f"Wniosek o zmianę grafiku: {response_data.response}",
        f"Twój wniosek na dzień {request['date']} został {new_status}."
    )
    
    updated_request = await db.shift_change_requests.find_one({"_id": req_oid})
    return updated_request


@router.put("/published/{schedule_id}", response_model=schemas.ScheduleResponse)
async def update_published_schedule(
        schedule_id: str,
        update_data: Dict[str, Any],
        current_user: dict = Depends(get_current_admin_user),
        db: motor.motor_asyncio.AsyncIOMotorDatabase = Depends(get_db)
):
    try:
        sched_oid = ObjectId(schedule_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Nieprawidłowy format ID grafiku.")

    old_schedule = await db.schedules.find_one({"_id": sched_oid})
    if not old_schedule:
        raise HTTPException(status_code=404, detail="Grafik nie znaleziony.")

    if old_schedule["franchise_code"] != current_user["franchise_code"]:
        raise HTTPException(status_code=403, detail="Brak uprawnień do tego grafiku.")

    if "schedule" not in update_data:
        raise HTTPException(status_code=400, detail="Brak danych grafiku (pole 'schedule').")

    new_schedule_data = update_data["schedule"]
    franchise_code = current_user["franchise_code"]

    # ------------------------------------------------------------------
    # KROK 1: BEZWZGLĘDNA OCHRONA WŁASNYCH GODZIN (Kopia zapasowa)
    # ------------------------------------------------------------------
    custom_hours_backup = {}
    for date_str, day_data in new_schedule_data.items():
        if isinstance(day_data, dict):
            for s_name, s_data in day_data.items():
                if isinstance(s_data, dict):
                    # Jeśli frontend nadał flagę is_custom LUB wysłał niestandardowe start_time
                    if s_data.get("is_custom") or (s_data.get("start_time") and s_data.get("start_time") != "00:00"):
                        custom_hours_backup[(date_str, s_name)] = {
                            "start_time": s_data.get("start_time"),
                            "end_time": s_data.get("end_time")
                        }

    # Przygotowanie do wyliczenia reszty grafiku
    temp_schedule = {"schedule": new_schedule_data}
    store_settings, holidays_map = await get_store_settings_and_holidays(db, franchise_code)

    # Przeliczenie grafiku (niestety może nadpisać customowe godziny)
    apply_shift_times(temp_schedule, store_settings, holidays_map)

    # ------------------------------------------------------------------
    # KROK 2: PRZYWRÓCENIE WŁASNYCH GODZIN (Zmiażdżenie domyślnych)
    # ------------------------------------------------------------------
    for (date_str, s_name), backup in custom_hours_backup.items():
        if date_str in new_schedule_data and s_name in new_schedule_data[date_str]:
            new_schedule_data[date_str][s_name]["start_time"] = backup["start_time"]
            new_schedule_data[date_str][s_name]["end_time"] = backup["end_time"]
            new_schedule_data[date_str][s_name]["is_custom"] = True

    # ------------------------------------------------------------------
    # KROK 3: WALIDACJA PRAWA PRACY
    # ------------------------------------------------------------------
    # Pobieramy imiona, żeby błędy były czytelne
    users_cursor = db.users.find({"franchise_code": franchise_code})
    users_list = await users_cursor.to_list(length=None)
    user_map = {str(u["_id"]): u for u in users_list}

    # Teraz weryfikacja dostanie Twoje prawdziwe 10:00 - 18:00
    validate_labor_laws(temp_schedule, user_map)

    # ------------------------------------------------------------------
    # KROK 4: AKTUALIZACJA W BAZIE (W tym naprawa synchronizacji płaskiej)
    # ------------------------------------------------------------------
    await db.schedules.update_one(
        {"_id": sched_oid},
        {"$set": {"schedule": new_schedule_data, "updated_at": datetime.utcnow()}}
    )

    # Naprawa synchronizacji: Musimy nadpisać też pojedyncze zmiany dla aplikacji mobilnej pracowników!
    start_date = old_schedule["start_date"]
    end_date = old_schedule["end_date"]

    await db.schedule.delete_many({
        "franchise_code": franchise_code,
        "date": {"$gte": start_date, "$lte": end_date}
    })

    flat_shifts_to_insert = []
    published_at = datetime.utcnow()
    published_by = current_user.get("email")

    for date_str, shifts in new_schedule_data.items():
        if "is_closed" in shifts: continue
        try:
            current_date = datetime.strptime(date_str, "%Y-%m-%d")
        except:
            continue

        for shift_name, shift_details in shifts.items():
            if not isinstance(shift_details, dict): continue

            for employee in shift_details.get("employees", []):
                try:
                    flat_shifts_to_insert.append({
                        "franchise_code": franchise_code,
                        "user_id": ObjectId(employee["id"]),
                        "date": current_date,
                        "shift_name": shift_name,
                        "start_time": shift_details.get("start_time", "00:00"),
                        "end_time": shift_details.get("end_time", "00:00"),
                        "published_at": published_at,
                        "published_by": published_by
                    })
                except Exception as e:
                    logger.error(f"Błąd przy zapisie płaskiej zmiany: {e}")

    if flat_shifts_to_insert:
        await db.schedule.insert_many(flat_shifts_to_insert)

    updated_schedule = await db.schedules.find_one({"_id": sched_oid})
    updated_schedule["id"] = str(updated_schedule["_id"])
    return updated_schedule

@router.post("/{schedule_id}/shifts", response_model=schemas.MessageResponse)
async def add_shift_endpoint(
    schedule_id: str,
    shift_data: schemas.ShiftCreate,
    current_user: dict = Depends(get_current_admin_user),
    db: motor.motor_asyncio.AsyncIOMotorDatabase = Depends(get_db)
):
    try:
        result = await add_shift_to_schedule(db, schedule_id, shift_data.dict(), current_user)
        return result
    except HTTPException as he:
        raise he
    except Exception as e:
        logger.error(f"Błąd dodawania zmiany: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Błąd serwera")

@router.delete("/shifts/{shift_id}", response_model=schemas.MessageResponse)
async def delete_shift_endpoint(
    shift_id: str,
    current_user: dict = Depends(get_current_admin_user),
    db: motor.motor_asyncio.AsyncIOMotorDatabase = Depends(get_db)
):
    try:
        result = await delete_shift_from_schedule(db, shift_id, current_user)
        return result
    except HTTPException as he:
        raise he
    except Exception as e:
        logger.error(f"Błąd usuwania zmiany: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Błąd serwera")
