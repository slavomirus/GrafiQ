# Plik: backend/app/services/schedule_service.py

import logging
from datetime import datetime, time, date, timedelta
from collections import defaultdict
from bson import ObjectId
from fastapi import HTTPException, status
import motor.motor_asyncio
from typing import List, Dict, Optional, Any, Tuple
import base64
from .. import models, schemas
import json
import calendar
import uuid
from .validator_service import ScheduleValidator
from .notification_service import send_push_to_user

# Ustawowe niedziele handlowe w Polsce (2024 - 2026)
STATUTORY_COMMERCIAL_SUNDAYS = {
    # 2024
    date(2024, 1, 28), date(2024, 3, 24), date(2024, 4, 28), 
    date(2024, 6, 30), date(2024, 8, 25), date(2024, 12, 15), date(2024, 12, 22),
    # 2025
    date(2025, 1, 26), date(2025, 4, 13), date(2025, 4, 27),
    date(2025, 6, 29), date(2025, 8, 31), date(2025, 12, 14), date(2025, 12, 21),
    # 2026
    date(2026, 1, 25), date(2026, 3, 29), date(2026, 4, 26),
    date(2026, 6, 28), date(2026, 8, 30), date(2026, 12, 13), date(2026, 12, 20),
}

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
    if s_type == "m": s_type = "middle"

    # Logika dla dni specjalnych (święta) i niedziel
    special_day = False
    open_time_str, close_time_str = None, None

    if date_str in holidays_map:
        holiday_info = holidays_map[date_str]
        if holiday_info.get("is_closed"):
            return ("00:00", "00:00")
        
        open_time_str = holiday_info.get("open_time")
        close_time_str = holiday_info.get("close_time")
        special_day = True
    elif date_obj.weekday() == 6: # Niedziela
        is_commercial = opening_hours.get("is_commercial_sunday", False)
        # Traktuj jako handlową tylko jeśli opcja włączona ORAZ data to ustawowa niedziela handlowa
        if is_commercial and date_obj in STATUTORY_COMMERCIAL_SUNDAYS:
            # Jeśli niedziela handlowa, traktuj jak zwykły dzień (korzystaj z godzin weekday)
            weekday_settings = opening_hours.get("weekday", {})
            open_time_str = weekday_settings.get("from", "06:00")
            close_time_str = weekday_settings.get("to", "23:00")
        else:
            sunday_settings = opening_hours.get("sunday", {})
            open_time_str = sunday_settings.get("from", "09:00")
            close_time_str = sunday_settings.get("to", "21:00")
            special_day = True

    if special_day:
        if isinstance(open_time_str, time): open_time_str = open_time_str.strftime("%H:%M")
        if isinstance(close_time_str, time): close_time_str = close_time_str.strftime("%H:%M")

        base_date = datetime(2000, 1, 1)
        start_dt = datetime.combine(base_date, parse_t(open_time_str))
        end_dt = datetime.combine(base_date, parse_t(close_time_str))
        if end_dt <= start_dt:
            end_dt += timedelta(days=1)
            
        duration_total = (end_dt - start_dt).total_seconds() / 3600.0
        
        # Dzielimy dzień na pół, aby uzyskać zmianę poranną i zamykającą
        mid_dt = start_dt + timedelta(hours=duration_total / 2)
        mid_time_str = mid_dt.strftime("%H:%M")
        
        if s_type == "morning":
            return (open_time_str, mid_time_str)
        elif s_type == "closing":
            return (mid_time_str, close_time_str)
        elif s_type == "middle":
            # Dla niedzieli niehandlowej/świąt zmiana M jest bezpiecznie wyśrodkowana i wykrojona wewnątrz godzin otwarcia (nie "całka")
            mid_span_hours = min(8.0, max(6.0, duration_total - 4.0)) if duration_total > 8.0 else max(4.0, duration_total * 0.6)
            half_span = timedelta(hours=mid_span_hours / 2)
            m_start_dt = max(start_dt, mid_dt - half_span)
            m_end_dt = min(end_dt, mid_dt + half_span)
            return (m_start_dt.strftime("%H:%M"), m_end_dt.strftime("%H:%M"))
        else:
             return (open_time_str, close_time_str)

    # Logika dla standardowych dni roboczych oraz niedziel handlowych/zwykłej zmiany M
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
    schedule_id_str = None
    date_str = None
    shift_name = None
    user_id_str = shift_data.user_id

    # 1. Dekodowanie ID (base64 lub string)
    try:
        decoded_id = base64.b64decode(shift_id).decode('utf-8')
        parts = decoded_id.split(':')
        if len(parts) >= 3:
            schedule_id_str = parts[0]
            date_str = parts[1]
            shift_name = parts[2]
            if len(parts) >= 4 and not user_id_str:
                user_id_str = parts[3]
    except Exception:
        pass

    if not schedule_id_str and ":" in shift_id:
        parts = shift_id.split(':')
        if len(parts) >= 3:
            schedule_id_str = parts[0]
            date_str = parts[1]
            shift_name = parts[2]
            if len(parts) >= 4 and not user_id_str:
                user_id_str = parts[3]

    if not schedule_id_str or not date_str or not shift_name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Nieprawidłowy format ID zmiany.")

    try:
        schedule_id = ObjectId(schedule_id_str)
    except Exception:
        schedule_id = schedule_id_str

    franchise_code = current_user.get("franchise_code")
    franchise_codes = current_user.get("franchise_codes", [])
    if franchise_code and franchise_code not in franchise_codes:
        franchise_codes.append(franchise_code)

    schedule = await db.schedules.find_one({
        "_id": schedule_id,
        "franchise_code": {"$in": franchise_codes} if franchise_codes else franchise_code
    })
    is_draft = False
    if not schedule:
        schedule = await db.schedules_drafts.find_one({
            "_id": schedule_id,
            "franchise_code": {"$in": franchise_codes} if franchise_codes else franchise_code
        })
        is_draft = True

    if not schedule:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Grafik nie został znaleziony.")

    target_franchise_code = schedule.get("franchise_code") or franchise_code
    collection = db.schedules_drafts if is_draft else db.schedules

    # Bezpieczna serializacja czasów (obsługa time oraz string "HH:MM")
    if hasattr(shift_data.start_time, 'strftime'):
        start_time_str = shift_data.start_time.strftime('%H:%M')
    else:
        start_time_str = str(shift_data.start_time)[:5]

    if hasattr(shift_data.end_time, 'strftime'):
        end_time_str = shift_data.end_time.strftime('%H:%M')
    else:
        end_time_str = str(shift_data.end_time)[:5]

    # Mapowanie klucza zmiany (np. mid vs middle)
    shift_keys = [shift_name]
    if shift_name == "mid":
        shift_keys.append("middle")
    elif shift_name == "middle":
        shift_keys.append("mid")

    day_schedule = schedule.get("schedule", {}).get(date_str, {})
    matched_key = shift_name
    for k in shift_keys:
        if k in day_schedule:
            matched_key = k
            break

    # Aktualizacja godzin w obiekcie grafiku (db.schedules / db.schedules_drafts)
    update_path_start = f"schedule.{date_str}.{matched_key}.start_time"
    update_path_end = f"schedule.{date_str}.{matched_key}.end_time"

    await collection.update_one(
        {"_id": schedule["_id"]},
        {"$set": {update_path_start: start_time_str, update_path_end: end_time_str}}
    )

    # Jeśli zmiana dotyczy konkretnego pracownika, aktualizujemy także jego wpis w tablicy employees
    if user_id_str:
        user_oid = ObjectId(user_id_str) if ObjectId.is_valid(user_id_str) else None
        await collection.update_one(
            {
                "_id": schedule["_id"],
                f"schedule.{date_str}.{matched_key}.employees.id": str(user_id_str)
            },
            {
                "$set": {
                    f"schedule.{date_str}.{matched_key}.employees.$.start_time": start_time_str,
                    f"schedule.{date_str}.{matched_key}.employees.$.end_time": end_time_str,
                }
            }
        )

        # Aktualizujemy płaską kolekcję db.schedule (dla Dashboardu pracownika)
        if not is_draft:
            shift_d = datetime.strptime(date_str, "%Y-%m-%d").date()
            u_target = user_oid if user_oid else user_id_str
            await db.schedule.update_one(
                {
                    "user_id": u_target,
                    "franchise_code": target_franchise_code,
                    "date": datetime.combine(shift_d, time.min)
                },
                {
                    "$set": {
                        "start_time": start_time_str,
                        "end_time": end_time_str,
                        "updated_at": datetime.utcnow()
                    }
                }
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

async def delete_published_schedule(db: motor.motor_asyncio.AsyncIOMotorDatabase, schedule_id: str, current_user: dict):
    try:
        schedule_object_id = ObjectId(schedule_id)
    except Exception:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Nieprawidłowy format ID grafiku.")

    schedule = await db.schedules.find_one({"_id": schedule_object_id})
    if not schedule:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Grafik nie został znaleziony.")

    if current_user.get("franchise_code") != schedule.get("franchise_code"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Brak uprawnień.")

    franchise_code = schedule["franchise_code"]
    start_date = schedule["start_date"]
    end_date = schedule["end_date"]

    # Usuń z schedules (główny dokument)
    await db.schedules.delete_one({"_id": schedule_object_id})

    # Usuń szczegółowe przypisania z schedule
    await db.schedule.delete_many({
        "franchise_code": franchise_code, 
        "date": {"$gte": start_date, "$lte": end_date}
    })

    return {"detail": "Opublikowany grafik został pomyślnie usunięty."}

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
        schedule["_id"] = str(schedule["_id"])
        schedule["id"] = schedule["_id"]
        if "status" not in schedule or not schedule["status"]:
            schedule["status"] = "published"
        
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
                        start_iso = start_time_str
                        end_iso = end_time_str

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

async def add_shift_to_schedule(db: motor.motor_asyncio.AsyncIOMotorDatabase, schedule_id: str, shift_data: dict, current_user: dict):
    user_id_str = shift_data.get("user_id")
    date_str = shift_data.get("date")
    shift_type = shift_data.get("shift_type", "morning")
    start_time_str = shift_data.get("start_time", "06:00")
    end_time_str = shift_data.get("end_time", "14:30")

    if not user_id_str or not date_str:
        raise HTTPException(status_code=400, detail="Wymagane pola: user_id oraz date.")

    # Format times to HH:MM
    if len(start_time_str) > 5:
        start_time_str = start_time_str[:5]
    if len(end_time_str) > 5:
        end_time_str = end_time_str[:5]

    franchise_code = current_user.get("franchise_code")
    franchise_codes = current_user.get("franchise_codes", [])
    if franchise_code and franchise_code not in franchise_codes:
        franchise_codes.append(franchise_code)

    sched_query = {}
    if ObjectId.is_valid(schedule_id):
        sched_query["_id"] = ObjectId(schedule_id)
    else:
        sched_query["_id"] = schedule_id

    schedule_doc = await db.schedules.find_one(sched_query)
    is_draft = False
    if not schedule_doc:
        schedule_doc = await db.schedules_drafts.find_one(sched_query)
        is_draft = True

    if not schedule_doc:
        raise HTTPException(status_code=404, detail="Grafik nie został znaleziony.")

    target_franchise_code = schedule_doc.get("franchise_code") or franchise_code
    sched_id = schedule_doc["_id"]
    collection = db.schedules_drafts if is_draft else db.schedules

    day_sched = schedule_doc.get("schedule", {}).get(date_str, {})
    shift_key = shift_type
    if shift_type in ["mid", "middle"]:
        if "middle" in day_sched:
            shift_key = "middle"
        elif "mid" in day_sched:
            shift_key = "mid"
        else:
            shift_key = "middle"

    user_oid = ObjectId(user_id_str) if ObjectId.is_valid(user_id_str) else None
    user_doc = None
    if user_oid:
        user_doc = await db.users.find_one({"_id": user_oid})
    if not user_doc:
        user_doc = await db.users.find_one({"_id": user_id_str})

    first_name = user_doc.get("first_name", "") if user_doc else ""
    last_name = user_doc.get("last_name", "") if user_doc else ""

    emp_obj = {
        "id": str(user_id_str),
        "first_name": first_name,
        "last_name": last_name,
        "start_time": start_time_str,
        "end_time": end_time_str
    }

    pull_filters = [
        {"id": str(user_id_str)},
        {"_id": str(user_id_str)},
        str(user_id_str)
    ]
    if user_oid:
        pull_filters.extend([{"id": user_oid}, {"_id": user_oid}, user_oid])

    # Usuń ewentualne stare przypisanie tego pracownika w tym dniu
    for k in ["morning", "mid", "middle", "closing", shift_key]:
        for pf in pull_filters:
            await collection.update_one(
                {"_id": sched_id},
                {"$pull": {f"schedule.{date_str}.{k}.employees": pf}}
            )

    update_ops = {
        f"schedule.{date_str}.{shift_key}.start_time": start_time_str,
        f"schedule.{date_str}.{shift_key}.end_time": end_time_str,
    }
    await collection.update_one(
        {"_id": sched_id},
        {
            "$set": update_ops,
            "$push": {f"schedule.{date_str}.{shift_key}.employees": emp_obj}
        }
    )

    if not is_draft:
        shift_d = datetime.strptime(date_str, "%Y-%m-%d").date()
        u_target = user_oid if user_oid else user_id_str
        await db.schedule.update_one(
            {
                "user_id": u_target,
                "franchise_code": target_franchise_code,
                "date": datetime.combine(shift_d, time.min)
            },
            {
                "$set": {
                    "user_id": u_target,
                    "franchise_code": target_franchise_code,
                    "date": datetime.combine(shift_d, time.min),
                    "start_time": start_time_str,
                    "end_time": end_time_str,
                    "shift_type": shift_key,
                    "updated_at": datetime.utcnow()
                }
            },
            upsert=True
        )

    if user_oid:
        try:
            await send_push_to_user(
                db,
                user_oid,
                "Nowa zmiana w grafiku",
                f"Zostałeś przypisany do zmiany {shift_key} ({start_time_str}-{end_time_str}) w dniu {date_str}."
            )
        except Exception as e:
            logger.warning(f"Nie udało się wysłać powiadomienia push: {e}")

    return {"message": "Zmiana została pomyślnie dodana."}

async def delete_shift_from_schedule(db: motor.motor_asyncio.AsyncIOMotorDatabase, shift_id_encoded: str, current_user: dict):
    schedule_id_str = None
    date_str = None
    shift_name = None
    user_id_str = None

    try:
        decoded = base64.b64decode(shift_id_encoded).decode('utf-8')
        parts = decoded.split(':')
        if len(parts) >= 3:
            schedule_id_str = parts[0]
            date_str = parts[1]
            shift_name = parts[2]
            if len(parts) >= 4:
                user_id_str = parts[3]
    except Exception:
        pass

    if not schedule_id_str and ":" in shift_id_encoded:
        parts = shift_id_encoded.split(':')
        if len(parts) >= 3:
            schedule_id_str = parts[0]
            date_str = parts[1]
            shift_name = parts[2]
            if len(parts) >= 4:
                user_id_str = parts[3]

    if not date_str and "-" in shift_id_encoded:
        parts = shift_id_encoded.split('-')
        if len(parts) >= 5:
            date_str = f"{parts[0]}-{parts[1]}-{parts[2]}"
            shift_name = parts[3]
            user_id_str = parts[4]

    if not date_str or not shift_name:
        raise HTTPException(status_code=400, detail="Nieprawidłowy format ID zmiany.")

    franchise_code = current_user.get("franchise_code")
    franchise_codes = current_user.get("franchise_codes", [])
    if franchise_code and franchise_code not in franchise_codes:
        franchise_codes.append(franchise_code)

    schedule_doc = None
    if schedule_id_str:
        sched_query = {}
        if ObjectId.is_valid(schedule_id_str):
            sched_query["_id"] = ObjectId(schedule_id_str)
        else:
            sched_query["_id"] = schedule_id_str

        schedule_doc = await db.schedules.find_one(sched_query)
        if not schedule_doc:
            schedule_doc = await db.schedules_drafts.find_one(sched_query)

    if not schedule_doc and franchise_codes:
        shift_d = datetime.strptime(date_str, "%Y-%m-%d").date()
        schedule_doc = await db.schedules.find_one({
            "franchise_code": {"$in": franchise_codes},
            "start_date": {"$lte": shift_d},
            "end_date": {"$gte": shift_d}
        })

    if not schedule_doc:
        raise HTTPException(status_code=404, detail="Grafik nie został znaleziony.")

    target_franchise_code = schedule_doc.get("franchise_code") or franchise_code
    sched_id = schedule_doc["_id"]
    is_draft = "draft_id" in schedule_doc or schedule_doc.get("status") == "DRAFT"
    collection = db.schedules_drafts if is_draft else db.schedules

    shift_keys = [shift_name]
    if shift_name == "mid":
        shift_keys.append("middle")
    elif shift_name == "middle":
        shift_keys.append("mid")

    for s_key in shift_keys:
        if user_id_str:
            user_oid = ObjectId(user_id_str) if ObjectId.is_valid(user_id_str) else None
            pull_filters = [
                {"id": str(user_id_str)},
                {"_id": str(user_id_str)},
                str(user_id_str)
            ]
            if user_oid:
                pull_filters.extend([{"id": user_oid}, {"_id": user_oid}, user_oid])

            for pf in pull_filters:
                await collection.update_one(
                    {"_id": sched_id},
                    {"$pull": {f"schedule.{date_str}.{s_key}.employees": pf}}
                )
        else:
            await collection.update_one(
                {"_id": sched_id},
                {"$set": {f"schedule.{date_str}.{s_key}.employees": []}}
            )

    if not is_draft and user_id_str:
        shift_d = datetime.strptime(date_str, "%Y-%m-%d").date()
        user_oid = ObjectId(user_id_str) if ObjectId.is_valid(user_id_str) else None
        user_query = [str(user_id_str)]
        if user_oid:
            user_query.append(user_oid)

        await db.schedule.delete_many({
            "user_id": {"$in": user_query},
            "franchise_code": target_franchise_code,
            "date": {"$in": [
                datetime.combine(shift_d, time.min),
                shift_d,
                date_str
            ]}
        })

    if user_id_str and ObjectId.is_valid(user_id_str):
        try:
            await send_push_to_user(
                db,
                ObjectId(user_id_str),
                "Zmiana w grafiku",
                f"Zostałeś usunięty ze zmiany w dniu {date_str}."
            )
        except Exception as e:
            logger.warning(f"Nie udało się wysłać powiadomienia push: {e}")

    return {"message": "Zmiana została pomyślnie usunięta."}