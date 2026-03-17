# Plik: backend/app/services/schedule_service.py

import logging
from datetime import datetime, time, date, timedelta
from collections import defaultdict
from bson import ObjectId
from fastapi import HTTPException, status
import motor.motor_asyncio
from typing import List, Dict, Optional, Any, Tuple
import base64
from .. import schemas
import json
import calendar
import uuid
from .validator_service import ScheduleValidator

logger = logging.getLogger(__name__)

def json_converter(o):
    if isinstance(o, ObjectId):
        return str(o)
    if isinstance(o, datetime):
        return o.isoformat()
    raise TypeError(f"Object of type {o.__class__.__name__} is not JSON serializable")

async def get_store_settings_and_holidays(db: motor.motor_asyncio.AsyncIOMotorDatabase, franchise_code: str):
    store_settings = await db.storesettings.find_one({"franchise_code": franchise_code})
    
    special_hours_cursor = db.specialopeninghours.find({"franchise_code": franchise_code})
    special_hours_list = await special_hours_cursor.to_list(length=None)
    
    holidays_map = {}
    for sh in special_hours_list:
        d = sh.get("date")
        if isinstance(d, datetime):
            date_str = d.strftime("%Y-%m-%d")
        elif isinstance(d, str):
            date_str = d[:10]
        else:
            continue
            
        holidays_map[date_str] = {
            "open_time": sh.get("open_time"),
            "close_time": sh.get("close_time"),
            "is_closed": sh.get("is_closed", False)
        }
        
        if sh.get("open_time") == sh.get("close_time") or not sh.get("open_time"):
            holidays_map[date_str]["is_closed"] = True

    return store_settings or {}, holidays_map

def resolve_shift_hours(date_obj: date, shift_type: str, store_settings: dict, holidays_map: dict) -> Tuple[str, str]:
    opening_hours = store_settings.get("opening_hours", {})
    if hasattr(opening_hours, "dict"):
        opening_hours = opening_hours.dict(by_alias=True)
        
    shift_hours = store_settings.get("shift_hours", {})

    def parse_t(t_val):
        if isinstance(t_val, time): return t_val
        try: return datetime.strptime(str(t_val), "%H:%M").time()
        except: 
            try: return datetime.strptime(str(t_val), "%H:%M:%S").time()
            except: return time(0, 0)

    date_str = date_obj.strftime("%Y-%m-%d")
    s_type = shift_type.lower()
    if s_type == "mid": s_type = "middle"

    if date_str in holidays_map:
        holiday_info = holidays_map[date_str]
        if holiday_info.get("is_closed"):
            return ("00:00", "00:00")
            
        open_time = holiday_info.get("open_time", "08:00")
        close_time = holiday_info.get("close_time", "15:00")
        
        if isinstance(open_time, time): open_time = open_time.strftime("%H:%M")
        if isinstance(close_time, time): close_time = close_time.strftime("%H:%M")
        
        return (open_time, close_time)

    if date_obj.weekday() == 6:
        sunday_settings = opening_hours.get("sunday", {})
        open_time = sunday_settings.get("from", "09:00")
        close_time = sunday_settings.get("to", "21:00")
        
        if isinstance(open_time, time): open_time = open_time.strftime("%H:%M")
        if isinstance(close_time, time): close_time = close_time.strftime("%H:%M")

        base_date = datetime(2000, 1, 1)
        start_dt = datetime.combine(base_date, parse_t(open_time))
        end_dt = datetime.combine(base_date, parse_t(close_time))
        if end_dt <= start_dt:
            end_dt += timedelta(days=1)
            
        duration_total = (end_dt - start_dt).total_seconds() / 3600.0
        
        mid_dt = start_dt + timedelta(hours=duration_total / 2)
        mid_time_str = mid_dt.strftime("%H:%M")
        
        if s_type == "morning":
            return (open_time, mid_time_str)
        elif s_type == "closing":
            return (mid_time_str, close_time)
        else:
             return (open_time, close_time)

    default_shifts = {
        "morning": ("06:00", "14:30"),
        "middle": ("10:00", "18:00"),
        "closing": ("14:30", "23:00")
    }
    
    shift_setting = shift_hours.get(s_type, {})
    std_start_str = shift_setting.get("start_time", default_shifts.get(s_type, ("06:00", "14:30"))[0])
    std_end_str = shift_setting.get("end_time", default_shifts.get(s_type, ("06:00", "14:30"))[1])
    
    if isinstance(std_start_str, time): std_start_str = std_start_str.strftime("%H:%M")
    if isinstance(std_end_str, time): std_end_str = std_end_str.strftime("%H:%M")

    return (std_start_str, std_end_str)

def calculate_actual_shift_times(shift_type: str, shift_date: date, store_settings: dict, holidays_map: dict) -> Tuple[str, str, float]:
    start_str, end_str = resolve_shift_hours(shift_date, shift_type, store_settings, holidays_map)
    
    def parse_t(t_val):
        try: return datetime.strptime(str(t_val), "%H:%M").time()
        except: return time(0, 0)
        
    base_date = datetime(2000, 1, 1)
    s_dt = datetime.combine(base_date, parse_t(start_str))
    e_dt = datetime.combine(base_date, parse_t(end_str))
    if e_dt <= s_dt:
        e_dt += timedelta(days=1)
    
    hours = round((e_dt - s_dt).total_seconds() / 3600.0, 2)
    if start_str == "00:00" and end_str == "00:00":
        hours = 0.0
        
    return (start_str, end_str, hours)


def apply_shift_times(schedule_data: dict, store_settings: dict, holidays_map: dict):
    if "schedule" not in schedule_data or not isinstance(schedule_data["schedule"], dict):
        return schedule_data

    for date_str, day_schedule in list(schedule_data["schedule"].items()):
        is_holiday = date_str in holidays_map
        holiday_info = holidays_map.get(date_str, {})

        if is_holiday and holiday_info.get("is_closed"):
            schedule_data["schedule"][date_str] = {"is_closed": True}
            continue

        try:
            shift_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            continue

        for shift_name, shift_details in day_schedule.items():
            if isinstance(shift_details, dict):
                # Zawsze wyliczamy domyślne godziny dla tej zmiany
                default_start, default_end = resolve_shift_hours(shift_date, shift_name, store_settings, holidays_map)

                # Sprawdzamy, czy w obiekcie (z bazy lub frontendu) wpisano już konkretne godziny
                existing_start = shift_details.get("start_time")

                # Jeśli są, nie są zerami i SĄ INNE niż domyślne -> Zostawiamy w spokoju!
                if existing_start and existing_start != "00:00" and existing_start != default_start:
                    shift_details["is_custom"] = True
                    if is_holiday:
                        shift_details["is_holiday"] = True
                    continue  # BARDZO WAŻNE: Pomijamy nadpisywanie

                # W przeciwnym razie nakładamy domyślne
                shift_details["start_time"] = default_start
                shift_details["end_time"] = default_end
                if is_holiday:
                    shift_details["is_holiday"] = True

    return schedule_data

def validate_labor_laws(schedule_data: dict, user_map: dict = None):
    """
    Waliduje wymogi prawne, m.in. 11 godzin nieprzerwanego odpoczynku między zmianami.
    """
    if user_map is None:
        user_map = {}

    employee_shifts = defaultdict(list)
    schedule = schedule_data.get("schedule", {})

    for date_str, day_schedule in schedule.items():
        if day_schedule.get("is_closed"): continue

        try:
            current_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            continue

        for shift_name, shift_details in day_schedule.items():
            if not isinstance(shift_details, dict): continue

            start_str = shift_details.get("start_time", "00:00")
            end_str = shift_details.get("end_time", "00:00")

            if start_str == "00:00" and end_str == "00:00":
                continue

            try:
                start_t = datetime.strptime(start_str, "%H:%M").time()
            except ValueError:
                try:
                    start_t = datetime.strptime(start_str, "%H:%M:%S").time()
                except ValueError:
                    start_t = time(0, 0)

            try:
                end_t = datetime.strptime(end_str, "%H:%M").time()
            except ValueError:
                try:
                    end_t = datetime.strptime(end_str, "%H:%M:%S").time()
                except ValueError:
                    end_t = time(0, 0)

            current_start_dt = datetime.combine(current_date, start_t)
            current_end_dt = datetime.combine(current_date, end_t)

            if current_end_dt <= current_start_dt:
                current_end_dt += timedelta(days=1)

            for emp in shift_details.get("employees", []):
                emp_id = emp.get("id")
                emp_id_str = str(emp_id) if emp_id else ""

                if emp_id in user_map:
                    u_info = user_map[emp_id_str]
                    emp_name = f"{u_info.get('first_name', '')} {u_info.get('last_name', '')}".strip()
                else:
                    emp_name = f"{emp.get('first_name', '')} {emp.get('last_name', '')}".strip()

                if not emp_name:
                    emp_name = f"Pracownik ({emp_id})"

                if emp_id:
                    employee_shifts[emp_id].append({
                        "name": emp_name,
                        "start": current_start_dt,
                        "end": current_end_dt
                    })

    for emp_id, shifts in employee_shifts.items():
        shifts.sort(key=lambda x: x["start"])

        for i in range(len(shifts) - 1):
            current_shift = shifts[i]
            next_shift = shifts[i + 1]

            break_hours = (next_shift["start"] - current_shift["end"]).total_seconds() / 3600.0

            if 0 < break_hours < 11.0:
                emp_name = current_shift["name"]
                date1 = current_shift["start"].strftime("%d.%m")
                date2 = next_shift["start"].strftime("%d.%m")

                msg = f"Naruszenie Prawa Pracy: {emp_name} ma tylko {break_hours:.1f} godz. przerwy między {date1} a {date2} (wymagane 11h)."

                raise HTTPException(
                    status_code=400,
                    detail=msg
                )
async def enrich_schedule_data(db: motor.motor_asyncio.AsyncIOMotorDatabase, franchise_code: str, schedule_data: dict):
    store_settings, holidays_map = await get_store_settings_and_holidays(db, franchise_code)
    return apply_shift_times(schedule_data, store_settings, holidays_map)

# Poniżej reszta pliku bez zmian
async def update_shift_hours(db: motor.motor_asyncio.AsyncIOMotorDatabase, shift_id: str, shift_data: schemas.ShiftHoursUpdate, current_user: dict):
    try:
        decoded_id = base64.b64decode(shift_id).decode('utf-8')
        schedule_id_str, date_str, shift_name = decoded_id.split(':')
        schedule_id = ObjectId(schedule_id_str)
    except Exception:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Nieprawidłowy format ID zmiany.")

    franchise_code = current_user.get("franchise_code")
    if not franchise_code:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Użytkownik nie jest przypisany.")

    schedule = await db.schedules.find_one({"_id": schedule_id, "franchise_code": franchise_code})

    if not schedule:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Grafik nie został znaleziony.")

    if date_str not in schedule.get("schedule", {}) or shift_name not in schedule.get("schedule", {}).get(date_str, {}):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Zmiana nie została znaleziona.")

    if shift_data.end_time <= shift_data.start_time:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Złe godziny.")

    update_path_start = f"schedule.{date_str}.{shift_name}.start_time"
    update_path_end = f"schedule.{date_str}.{shift_name}.end_time"

    start_time_str = shift_data.start_time.strftime('%H:%M')
    end_time_str = shift_data.end_time.strftime('%H:%M')

    result = await db.schedules.update_one(
        {"_id": schedule_id},
        {"$set": {update_path_start: start_time_str, update_path_end: end_time_str}}
    )

    return {"message": "Godziny zaktualizowane."}

async def get_schedules_history(
    db: motor.motor_asyncio.AsyncIOMotorDatabase, 
    current_user: dict,
    months: int = 6,
    published_only: bool = True,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None
) -> List[dict]:
    franchise_code = current_user.get("franchise_code")
    if not franchise_code:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Użytkownik nie jest przypisany.")

    store_settings, holidays_map = await get_store_settings_and_holidays(db, franchise_code)

    all_schedules = []
    query_filter = {"franchise_code": franchise_code}
    
    if start_date:
        dt_start = datetime.combine(start_date, time.min)
        query_filter["start_date"] = {"$gte": dt_start}
    else:
        cutoff_date = datetime.utcnow() - timedelta(days=30 * months)
        query_filter["start_date"] = {"$gte": cutoff_date}
    
    published_cursor = db.schedules.find(query_filter)
    async for schedule in published_cursor:
        if 'start_date' not in schedule:
            continue
        schedule["_id"] = str(schedule["_id"])
        if "status" not in schedule:
            schedule["status"] = "published"
            
        apply_shift_times(schedule, store_settings, holidays_map)
        all_schedules.append(schedule)
        
    if not published_only:
        drafts_cursor = db.schedule_drafts.find(query_filter)
        async for draft in drafts_cursor:
            if 'start_date' not in draft:
                continue
            draft["_id"] = str(draft["_id"])
            draft["is_published"] = False
            if "status" not in draft:
                draft["status"] = "DRAFT"
                
            apply_shift_times(draft, store_settings, holidays_map)
            all_schedules.append(draft)
        
    all_schedules.sort(key=lambda x: x['start_date'], reverse=True)
    return all_schedules

async def get_all_drafts_for_store(db: motor.motor_asyncio.AsyncIOMotorDatabase, current_user: dict) -> List[dict]:
    franchise_code = current_user.get("franchise_code")
    store_settings, holidays_map = await get_store_settings_and_holidays(db, franchise_code)

    drafts_cursor = db.schedule_drafts.find({"franchise_code": franchise_code})
    drafts = await drafts_cursor.to_list(length=None)
    
    for draft in drafts:
        draft["_id"] = str(draft["_id"])
        apply_shift_times(draft, store_settings, holidays_map)
        
    return drafts

async def publish_schedule_draft(db: motor.motor_asyncio.AsyncIOMotorDatabase, draft_id: str, current_user: dict):
    try:
        draft_object_id = ObjectId(draft_id)
    except Exception:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Nieprawidłowy format ID wersji roboczej.")

    draft = await db.schedule_drafts.find_one({"_id": draft_object_id})
    if not draft:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Wersja robocza grafiku nie została znaleziona.")

    if current_user.get("franchise_code") != draft.get("franchise_code"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Brak uprawnień.")

    franchise_code = draft["franchise_code"]
    
    store_settings, holidays_map = await get_store_settings_and_holidays(db, franchise_code)
    apply_shift_times(draft, store_settings, holidays_map)

    # Pobieranie słownika pracowników do imion
    users_cursor = db.users.find({"franchise_code": franchise_code})
    users_list = await users_cursor.to_list(length=None)
    user_map = {str(u["_id"]): u for u in users_list}

    # NOWA WALIDACJA: Prawo Pracy
    validate_labor_laws(draft, user_map)

    start_date = draft["start_date"] if isinstance(draft["start_date"], datetime) else datetime.fromisoformat(draft["start_date"])
    end_date = draft["end_date"] if isinstance(draft["end_date"], datetime) else datetime.fromisoformat(draft["end_date"])
    
    published_at = datetime.utcnow()
    published_by = current_user.get("email")

    await db.schedule.delete_many({"franchise_code": franchise_code, "date": {"$gte": start_date, "$lte": end_date}})

    flat_shifts_to_insert = []
    for date_str, shifts in draft.get("schedule", {}).items():
        if "is_closed" in shifts: continue
        
        current_date = datetime.strptime(date_str, "%Y-%m-%d")
        for shift_name, shift_details in shifts.items():
            if not isinstance(shift_details, dict):
                continue

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
                except (KeyError, TypeError) as e:
                    logger.error(f"Błąd przetwarzania pracownika: {e}")

    if flat_shifts_to_insert:
        await db.schedule.insert_many(flat_shifts_to_insert)

    nested_schedule_doc = {
        "franchise_code": franchise_code,
        "start_date": start_date,
        "end_date": end_date,
        "schedule": draft["schedule"],
        "is_published": True,
        "published_at": published_at,
        "published_by": published_by,
        "status": "published",
        "created_at": published_at
    }
    await db.schedules.update_one(
        {"franchise_code": franchise_code, "start_date": start_date, "end_date": end_date},
        {"$set": nested_schedule_doc},
        upsert=True
    )

    await db.schedule_drafts.delete_one({"_id": draft_object_id})
    return {"detail": "Grafik został pomyślnie opublikowany."}

async def get_employee_schedule(db: motor.motor_asyncio.AsyncIOMotorDatabase, current_user: dict, month: Optional[int] = None, year: Optional[int] = None) -> List[dict]:
    franchise_code = current_user.get("franchise_code")
    query = {"franchise_code": franchise_code, "is_published": True}

    if month and year:
        last_day = calendar.monthrange(year, month)[1]
        start_of_month = datetime(year, month, 1)
        end_of_month = datetime(year, month, last_day, 23, 59, 59)
        query["start_date"] = {"$lte": end_of_month}
        query["end_date"] = {"$gte": start_of_month}

    schedules_cursor = db.schedules.find(query, sort=[("start_date", 1)])
    schedules = await schedules_cursor.to_list(length=None)

    if not schedules: return []

    users = await db.users.find({"franchise_code": franchise_code}).to_list(length=None)
    user_map = {str(u["_id"]): u for u in users}
    result_list = []

    for schedule in schedules:
        schedule["id"] = str(schedule["_id"])
        del schedule["_id"]
        
        await enrich_schedule_data(db, franchise_code, schedule)
        
        shifts_by_date = {}
        schedule_data = schedule.get("schedule", {})
        
        for date_str, day_schedule in schedule_data.items():
            shifts_list = []
            if isinstance(day_schedule, dict):
                if day_schedule.get("is_closed"):
                    shifts_by_date[date_str] = []
                    continue

                for shift_name, shift_details in day_schedule.items():
                    if isinstance(shift_details, dict):
                        start_time_str = shift_details.get("start_time", "00:00")
                        end_time_str = shift_details.get("end_time", "00:00")
                        start_iso = f"{date_str}T{start_time_str}:00" if len(start_time_str) == 5 else f"{date_str}T{start_time_str}"
                        
                        try:
                            st = datetime.strptime(start_time_str, "%H:%M").time()
                            et = datetime.strptime(end_time_str, "%H:%M").time()
                            dt_start = datetime.strptime(date_str, "%Y-%m-%d")
                            dt_end = dt_start
                            if et < st: dt_end += timedelta(days=1)
                            end_iso = dt_end.strftime("%Y-%m-%dT%H:%M:00")
                        except ValueError:
                            end_iso = f"{date_str}T{end_time_str}:00"

                        employees = shift_details.get("employees", [])
                        for emp in employees:
                            user_id = emp.get("id") if isinstance(emp, dict) else emp
                            if user_id:
                                raw_id = f"{schedule['id']}:{date_str}:{shift_name}:{user_id}"
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
                                    shift_obj["first_name"] = user.get("first_name", "")
                                    shift_obj["last_name"] = user.get("last_name", "")
                                    shift_obj["firstName"] = shift_obj["first_name"]
                                    shift_obj["lastName"] = shift_obj["last_name"]
                                    shift_obj["employee"] = {
                                        "id": user_id,
                                        "first_name": shift_obj["first_name"],
                                        "last_name": shift_obj["last_name"],
                                        "firstName": shift_obj["first_name"],
                                        "lastName": shift_obj["last_name"]
                                    }
                                    shift_obj["user"] = shift_obj["employee"]
                                    
                                shifts_list.append(shift_obj)
            shifts_by_date[date_str] = shifts_list
        
        schedule["shifts_by_date"] = shifts_by_date
        result_list.append(schedule)
    
    return result_list

async def update_draft_shift(db, draft_id, shift_data, current_user):
    pass

async def add_shift_to_schedule(db, schedule_id, shift_data, current_user):
    pass

async def delete_shift_from_schedule(db, shift_id_encoded, current_user):
    pass
