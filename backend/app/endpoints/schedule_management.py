# Plik: backend/app/endpoints/schedule_management.py

from fastapi import APIRouter, Depends, HTTPException, status
import motor.motor_asyncio
from bson import ObjectId
import logging
from typing import List, Dict, Any
from datetime import date, time, datetime, timedelta

from ..database import get_db
from ..dependencies import get_current_admin_user
from .. import schemas, models
from ..services.notification_service import send_push_to_user
from ..services.validator_service import ScheduleValidator

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
            pass 

async def _compare_and_notify_changes(db, old_schedule_data, new_schedule_data):
    """Porównuje dwie wersje grafiku i wysyła powiadomienia o zmianach."""
    # Iteruj po dniach
    all_dates = set(old_schedule_data.keys()) | set(new_schedule_data.keys())
    
    for date_str in all_dates:
        old_day = old_schedule_data.get(date_str, {})
        new_day = new_schedule_data.get(date_str, {})
        
        # Iteruj po zmianach w danym dniu
        all_shifts = set(old_day.keys()) | set(new_day.keys())
        
        for shift_name in all_shifts:
            old_shift = old_day.get(shift_name, {})
            new_shift = new_day.get(shift_name, {})
            
            # Pobierz listy pracowników (zakładamy nową strukturę z listą obiektów)
            old_emps = old_shift.get("employees", [])
            new_emps = new_shift.get("employees", [])
            
            old_ids = {e["id"] for e in old_emps if isinstance(e, dict) and "id" in e}
            new_ids = {e["id"] for e in new_emps if isinstance(e, dict) and "id" in e}
            
            # Znajdź dodanych i usuniętych
            added_ids = new_ids - old_ids
            removed_ids = old_ids - new_ids
            
            # Powiadom dodanych
            for uid in added_ids:
                try:
                    await send_push_to_user(
                        db, 
                        ObjectId(uid), 
                        "Zmiana w grafiku", 
                        f"Zostałeś dodany do zmiany {shift_name} w dniu {date_str}."
                    )
                except Exception as e:
                    logger.error(f"Failed to notify user {uid}: {e}")

            # Powiadom usuniętych
            for uid in removed_ids:
                try:
                    await send_push_to_user(
                        db, 
                        ObjectId(uid), 
                        "Zmiana w grafiku", 
                        f"Zostałeś usunięty ze zmiany {shift_name} w dniu {date_str}."
                    )
                except Exception as e:
                    logger.error(f"Failed to notify user {uid}: {e}")
            
            # Sprawdź zmianę godzin dla pozostających (jeśli godziny zmiany się zmieniły)
            if old_ids & new_ids:
                old_start = old_shift.get("start_time")
                new_start = new_shift.get("start_time")
                old_end = old_shift.get("end_time")
                new_end = new_shift.get("end_time")
                
                if old_start != new_start or old_end != new_end:
                    for uid in (old_ids & new_ids):
                        try:
                            await send_push_to_user(
                                db,
                                ObjectId(uid),
                                "Zmiana godzin pracy",
                                f"Godziny zmiany {shift_name} w dniu {date_str} uległy zmianie: {new_start} - {new_end}."
                            )
                        except Exception as e:
                            logger.error(f"Failed to notify user {uid}: {e}")


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
        
        for shift_name, shift_details in shifts.items():
            # Obsługa nowej struktury danych (słownik) vs starej (lista ID)
            if isinstance(shift_details, dict):
                # Nowa struktura
                start_time_str = shift_details.get("start_time", "00:00")
                end_time_str = shift_details.get("end_time", "00:00")
                try:
                    start_time = time.fromisoformat(start_time_str)
                    end_time = time.fromisoformat(end_time_str)
                except ValueError:
                    start_time, end_time = (time(0,0), time(0,0))
                
                employees_list = shift_details.get("employees", [])
                # employees_list to lista obiektów {id, first_name, ...}
                employee_ids = [e["id"] for e in employees_list if isinstance(e, dict) and "id" in e]
                
            elif isinstance(shift_details, list):
                # Stara struktura (fallback)
                start_time, end_time = (time(6, 0), time(14, 0)) if shift_name == "morning" else (time(14, 0), time(22, 0))
                employee_ids = shift_details
            else:
                continue

            for emp_id in employee_ids:
                try:
                    user_object_id = ObjectId(emp_id)
                except Exception:
                    logger.warning(f"Pominięto nieprawidłowy format ID pracownika ('{emp_id}') podczas publikowania grafiku.")
                    continue
                
                schedule_entries.append({
                    "user_id": user_object_id,
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

    # Zapisz też w formacie zagnieżdżonym (dla kompatybilności wstecznej i widoku pracownika)
    nested_schedule_doc = {
        "franchise_code": draft["franchise_code"],
        "start_date": draft["start_date"],
        "end_date": draft["end_date"],
        "schedule": draft["schedule"],
        "is_published": True,
        "published_at": datetime.utcnow(),
        "published_by": current_user.get("email"),
        "status": "published",
        "created_at": datetime.utcnow()
    }
    await db.schedules.update_one(
        {"franchise_code": draft["franchise_code"], "start_date": draft["start_date"], "end_date": draft["end_date"]},
        {"$set": nested_schedule_doc},
        upsert=True
    )

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
    
    # W nowej strukturze musimy zaktualizować listę employees wewnątrz obiektu zmiany
    # Pobieramy aktualne dane zmiany, żeby zachować godziny
    current_shift_data = draft.get("schedule", {}).get(date_str, {}).get(shift_name, {})
    
    if isinstance(current_shift_data, dict):
        # Nowa struktura - zachowujemy godziny, aktualizujemy pracowników
        # Musimy pobrać dane pracowników z bazy, żeby zapisać pełne obiekty
        employees_data = []
        for eid in update_data.employee_ids:
            emp = await db.users.find_one({"_id": eid})
            if emp:
                employees_data.append({
                    "id": str(emp["_id"]),
                    "first_name": emp.get("first_name", ""),
                    "last_name": emp.get("last_name", "")
                })
        
        update_field = f"schedule.{date_str}.{shift_name}.employees"
        await db.schedule_drafts.update_one(
            {"_id": draft_oid},
            {"$set": {update_field: employees_data}}
        )
    else:
        # Stara struktura (lista ID) - po prostu nadpisujemy listę
        update_field = f"schedule.{date_str}.{shift_name}"
        await db.schedule_drafts.update_one(
            {"_id": draft_oid},
            {"$set": {update_field: [str(eid) for eid in update_data.employee_ids]}}
        )

    # Pobierz i zwróć zaktualizowany szkic
    updated_draft = await db.schedule_drafts.find_one({"_id": draft_oid})
    return updated_draft

@router.put("/published/{schedule_id}", response_model=schemas.ScheduleResponse)
async def update_published_schedule(
    schedule_id: str,
    update_data: Dict[str, Any], # Przyjmujemy słownik, bo struktura jest złożona
    current_user: dict = Depends(get_current_admin_user),
    db: motor.motor_asyncio.AsyncIOMotorDatabase = Depends(get_db)
):
    """
    Pozwala franczyzobiorcy na edycję opublikowanego grafiku.
    Wykrywa zmiany i wysyła powiadomienia push do pracowników.
    """
    try:
        sched_oid = ObjectId(schedule_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Nieprawidłowy format ID grafiku.")

    old_schedule = await db.schedules.find_one({"_id": sched_oid})
    if not old_schedule:
        raise HTTPException(status_code=404, detail="Grafik nie znaleziony.")
        
    if old_schedule["franchise_code"] != current_user["franchise_code"]:
        raise HTTPException(status_code=403, detail="Brak uprawnień do tego grafiku.")

    # Aktualizacja w bazie
    # Zakładamy, że update_data zawiera pole "schedule" z nową strukturą
    if "schedule" not in update_data:
        raise HTTPException(status_code=400, detail="Brak danych grafiku (pole 'schedule').")

    new_schedule_data = update_data["schedule"]
    
    await db.schedules.update_one(
        {"_id": sched_oid},
        {"$set": {"schedule": new_schedule_data, "updated_at": datetime.utcnow()}}
    )
    
    # Porównaj i wyślij powiadomienia
    await _compare_and_notify_changes(db, old_schedule.get("schedule", {}), new_schedule_data)
    
    updated_schedule = await db.schedules.find_one({"_id": sched_oid})
    
    # Konwersja ObjectId na str dla response_model
    updated_schedule["id"] = str(updated_schedule["_id"])
    return updated_schedule

@router.post("/published/{schedule_id}/shift/{date_str}/{shift_name}/employee", status_code=status.HTTP_200_OK)
async def add_employee_to_published_shift(
    schedule_id: str,
    date_str: str,
    shift_name: str,
    assignment: schemas.EmployeeAssignment,
    current_user: dict = Depends(get_current_admin_user),
    db: motor.motor_asyncio.AsyncIOMotorDatabase = Depends(get_db)
):
    """
    Dodaje pracownika do konkretnej zmiany w opublikowanym grafiku.
    Wymusza walidację Hard Constraints (ScheduleValidator).
    """
    try:
        sched_oid = ObjectId(schedule_id)
        emp_oid = assignment.employee_id
    except Exception:
        raise HTTPException(status_code=400, detail="Nieprawidłowy format ID.")

    # 1. Pobierz grafik
    schedule_doc = await db.schedules.find_one({"_id": sched_oid})
    if not schedule_doc:
        raise HTTPException(status_code=404, detail="Grafik nie znaleziony.")
    
    if schedule_doc["franchise_code"] != current_user["franchise_code"]:
        raise HTTPException(status_code=403, detail="Brak uprawnień.")

    # 2. Pobierz dane zmiany
    try:
        shift_data = schedule_doc["schedule"][date_str][shift_name]
    except KeyError:
        raise HTTPException(status_code=404, detail="Nie znaleziono takiej zmiany w grafiku.")

    # 3. Sprawdź czy pracownik już jest przypisany
    current_employees = shift_data.get("employees", [])
    # Obsługa starej (lista ID) i nowej (lista obiektów) struktury
    current_ids = []
    if current_employees and isinstance(current_employees[0], dict):
        current_ids = [e["id"] for e in current_employees]
    else:
        current_ids = current_employees # Zakładamy listę stringów/ID

    if str(emp_oid) in current_ids:
        raise HTTPException(status_code=400, detail="Pracownik jest już przypisany do tej zmiany.")

    # 4. Pobierz godziny zmiany
    start_time_str = shift_data.get("start_time", "00:00")
    end_time_str = shift_data.get("end_time", "00:00")
    try:
        start_time = time.fromisoformat(start_time_str)
        end_time = time.fromisoformat(end_time_str)
    except ValueError:
        # Fallback dla formatu HH:MM:SS
        try:
            start_time = datetime.strptime(start_time_str, "%H:%M:%S").time()
            end_time = datetime.strptime(end_time_str, "%H:%M:%S").time()
        except ValueError:
             raise HTTPException(status_code=500, detail="Błąd formatu czasu w grafiku.")

    shift_date = date.fromisoformat(date_str)

    # 5. WALIDACJA (Hard Constraints)
    validator = ScheduleValidator(db)
    is_valid, error_msg = await validator.validate_shift_assignment(
        user_id=emp_oid,
        shift_date=shift_date,
        start_time=start_time,
        end_time=end_time,
        franchise_code=schedule_doc["franchise_code"]
    )

    if not is_valid:
        raise HTTPException(status_code=400, detail=f"Naruszenie zasad: {error_msg}")

    # 6. Dodaj pracownika do bazy (Flat Collection - db.schedule)
    # To jest kluczowe dla poprawności kolejnych walidacji!
    new_entry = {
        "user_id": emp_oid,
        "franchise_code": schedule_doc["franchise_code"],
        "date": datetime.combine(shift_date, time.min),
        "start_time": start_time,
        "end_time": end_time,
        "assigned_by_id": current_user["_id"],
        "created_at": datetime.utcnow(),
        "shift_name": shift_name # Opcjonalnie, jeśli używane
    }
    await db.schedule.insert_one(new_entry)

    # 7. Zaktualizuj dokument grafiku (Nested Collection - db.schedules)
    employee = await db.users.find_one({"_id": emp_oid})
    if not employee:
        raise HTTPException(status_code=404, detail="Pracownik nie istnieje.")

    emp_obj = {
        "id": str(emp_oid),
        "first_name": employee.get("first_name", ""),
        "last_name": employee.get("last_name", "")
    }

    # Jeśli struktura to lista ID, musimy przekonwertować na obiekty lub dodać ID
    # Ale zakładamy, że dążymy do nowej struktury (lista obiektów)
    if current_employees and not isinstance(current_employees[0], dict):
        # Konwersja starej struktury na nową przy okazji?
        # Ryzykowne. Lepiej dopasować się do istniejącej.
        update_op = {"$push": {f"schedule.{date_str}.{shift_name}.employees": str(emp_oid)}}
    else:
        update_op = {"$push": {f"schedule.{date_str}.{shift_name}.employees": emp_obj}}

    await db.schedules.update_one(
        {"_id": sched_oid},
        update_op
    )

    # 8. Powiadomienie
    await send_push_to_user(
        db, 
        emp_oid, 
        "Nowa zmiana", 
        f"Zostałeś dodany do zmiany {shift_name} w dniu {date_str}."
    )

    return {"message": "Pracownik dodany pomyślnie."}

@router.delete("/published/{schedule_id}/shift/{date_str}/{shift_name}/employee/{employee_id}", status_code=status.HTTP_200_OK)
async def remove_employee_from_published_shift(
    schedule_id: str,
    date_str: str,
    shift_name: str,
    employee_id: str,
    current_user: dict = Depends(get_current_admin_user),
    db: motor.motor_asyncio.AsyncIOMotorDatabase = Depends(get_db)
):
    """
    Usuwa pracownika z konkretnej zmiany w opublikowanym grafiku.
    """
    try:
        sched_oid = ObjectId(schedule_id)
        emp_oid = ObjectId(employee_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Nieprawidłowy format ID.")

    # 1. Pobierz grafik
    schedule_doc = await db.schedules.find_one({"_id": sched_oid})
    if not schedule_doc:
        raise HTTPException(status_code=404, detail="Grafik nie znaleziony.")
    
    if schedule_doc["franchise_code"] != current_user["franchise_code"]:
        raise HTTPException(status_code=403, detail="Brak uprawnień.")

    # 2. Sprawdź czy zmiana istnieje
    try:
        shift_data = schedule_doc["schedule"][date_str][shift_name]
    except KeyError:
        raise HTTPException(status_code=404, detail="Nie znaleziono takiej zmiany.")

    # 3. Usuń z Flat Collection (db.schedule)
    shift_date = date.fromisoformat(date_str)
    # Musimy znaleźć konkretny wpis. 
    # Uwaga: w db.schedule może być wiele wpisów dla tego usera w tym dniu (teoretycznie, choć walidator zabrania).
    # Ale usuwamy ten konkretny pasujący do godzin zmiany.
    
    start_time_str = shift_data.get("start_time", "00:00")
    # Parsowanie czasu jak w Add
    try:
        start_time = time.fromisoformat(start_time_str)
    except ValueError:
        try:
            start_time = datetime.strptime(start_time_str, "%H:%M:%S").time()
        except ValueError:
            start_time = time(0,0) # Fallback

    # Usuwamy wpis pasujący do usera, daty i (opcjonalnie) czasu startu, żeby nie usunąć innej zmiany tego dnia (jeśli by była możliwa)
    # W praktyce walidator zabrania >1 zmiany/dzień, więc wystarczy user+data.
    # Ale dla bezpieczeństwa dodajmy start_time jeśli mamy pewność.
    # Jednak start_time w bazie jest obiektem datetime (w polu date) lub time?
    # W validator_service: date: datetime.combine(date1, time.min)
    # A start_time/end_time są polami.
    
    delete_result = await db.schedule.delete_one({
        "user_id": emp_oid,
        "franchise_code": schedule_doc["franchise_code"],
        "date": datetime.combine(shift_date, time.min)
        # Możemy dodać "start_time": start_time, ale formaty mogą się różnić (sekundy itp.)
    })

    if delete_result.deleted_count == 0:
        logger.warning(f"Nie znaleziono wpisu w db.schedule dla usera {emp_oid} w dniu {date_str}, ale usuwam z db.schedules.")

    # 4. Usuń z Nested Collection (db.schedules)
    # Musimy usunąć obiekt z listy, który ma id == employee_id
    # $pull obsługuje usuwanie obiektu pasującego do kryteriów
    
    # Sprawdźmy strukturę
    current_employees = shift_data.get("employees", [])
    if current_employees and isinstance(current_employees[0], dict):
        # Lista obiektów
        await db.schedules.update_one(
            {"_id": sched_oid},
            {"$pull": {f"schedule.{date_str}.{shift_name}.employees": {"id": str(emp_oid)}}}
        )
    else:
        # Lista ID
        await db.schedules.update_one(
            {"_id": sched_oid},
            {"$pull": {f"schedule.{date_str}.{shift_name}.employees": str(emp_oid)}}
        )

    # 5. Powiadomienie
    await send_push_to_user(
        db, 
        emp_oid, 
        "Zmiana w grafiku", 
        f"Zostałeś usunięty ze zmiany {shift_name} w dniu {date_str}."
    )

    return {"message": "Pracownik usunięty pomyślnie."}
