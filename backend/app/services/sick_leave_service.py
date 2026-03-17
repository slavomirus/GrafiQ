import logging
from datetime import date, datetime, timedelta, time
from typing import List, Dict, Optional
import motor.motor_asyncio
from bson import ObjectId
from fastapi import HTTPException, status

from .. import schemas, models
from .validator_service import ScheduleValidator

logger = logging.getLogger(__name__)

async def process_sick_leave(
    db: motor.motor_asyncio.AsyncIOMotorDatabase,
    request: schemas.SickLeaveRequest,
    current_user: dict
) -> schemas.SickLeaveResponse:
    
    franchise_code = current_user.get("franchise_code")
    if not franchise_code:
        raise HTTPException(status_code=400, detail="Brak przypisanego sklepu.")

    employee_id = request.employee_id
    start_date = request.start_date
    end_date = request.end_date

    # 1. Zapisz wniosek L4 w bazie (dla historii i blokowania przyszłych zmian)
    sick_leave_doc = {
        "user_id": employee_id,
        "franchise_code": franchise_code,
        "start_date": datetime.combine(start_date, time.min),
        "end_date": datetime.combine(end_date, time.max),
        "created_at": datetime.utcnow(),
        "created_by": current_user.get("email")
    }
    await db.sick_leaves.insert_one(sick_leave_doc)

    # 2. Znajdź zmiany pracownika w tym okresie (tylko przyszłe/dzisiejsze)
    # Szukamy w płaskiej kolekcji 'schedule'
    query = {
        "franchise_code": franchise_code,
        "user_id": employee_id,
        "date": {
            "$gte": datetime.combine(start_date, time.min),
            "$lte": datetime.combine(end_date, time.max)
        }
    }
    
    shifts_to_cover = await db.schedule.find(query).to_list(length=None)
    
    replacements_report = []

    # Pobierz wszystkich pracowników sklepu (potencjalni kandydaci)
    all_employees = await db.users.find({
        "franchise_code": franchise_code,
        "role": models.UserRole.EMPLOYEE.value,
        "status": models.UserStatus.ACTIVE.value,
        "_id": {"$ne": employee_id} # Wyklucz chorego
    }).to_list(length=None)
    
    # Dodaj też franczyzobiorcę jako kandydata ostatecznego
    franchisee = await db.users.find_one({"_id": current_user["_id"]})
    if franchisee:
        all_employees.append(franchisee)

    validator = ScheduleValidator(db)

    for shift in shifts_to_cover:
        shift_date = shift["date"].date()
        shift_name = shift["shift_name"]
        
        # Parsowanie godzin
        s_start = shift["start_time"]
        s_end = shift["end_time"]
        if isinstance(s_start, str): s_start = datetime.strptime(s_start, "%H:%M").time()
        if isinstance(s_end, str): s_end = datetime.strptime(s_end, "%H:%M").time()
        
        # Znajdź zastępstwo
        candidate = await find_replacement(validator, franchise_code, all_employees, shift_date, s_start, s_end)
        
        if candidate:
            # Wykonaj zastępstwo w bazie
            # 1. Usuń chorego ze zmiany (kolekcja płaska)
            await db.schedule.delete_one({"_id": shift["_id"]})
            
            # 2. Dodaj kandydata (kolekcja płaska)
            new_shift = shift.copy()
            del new_shift["_id"] # Nowe ID zostanie wygenerowane
            new_shift["user_id"] = candidate["_id"]
            new_shift["is_replacement"] = True
            new_shift["replaced_user_id"] = employee_id
            
            await db.schedule.insert_one(new_shift)
            
            # --- SYNCHRONIZACJA Z WIDOKIEM (kolekcja 'schedules') ---
            date_str = shift_date.isoformat()
            
            # A. Usuń chorego z listy employees
            pull_path = f"schedule.{date_str}.{shift_name}.employees"
            # Szukamy dokumentu grafiku, który obejmuje tę datę
            schedule_query = {
                "franchise_code": franchise_code,
                "start_date": {"$lte": datetime.combine(shift_date, time.min)},
                "end_date": {"$gte": datetime.combine(shift_date, time.max)}
            }
            
            await db.schedules.update_one(
                schedule_query,
                {"$pull": {pull_path: {"id": str(employee_id)}}}
            )
            
            # B. Dodaj zastępcę do listy employees
            push_path = f"schedule.{date_str}.{shift_name}.employees"
            replacement_data = {
                "id": str(candidate["_id"]),
                "first_name": candidate.get("first_name", ""),
                "last_name": candidate.get("last_name", ""),
                "is_replacement": True
            }
            await db.schedules.update_one(
                schedule_query,
                {"$push": {push_path: replacement_data}}
            )
            # -------------------------------------------------------

            replacements_report.append(schemas.ReplacementInfo(
                date=shift_date,
                shift_name=shift_name,
                original_employee_id=employee_id,
                replacement_employee_id=candidate["_id"],
                status="replaced"
            ))
            logger.info(f"Zastąpiono pracownika {employee_id} pracownikiem {candidate['_id']} dnia {shift_date}")
            
        else:
            # Nie znaleziono kandydata - usuwamy chorego (kolekcja płaska)
            await db.schedule.delete_one({"_id": shift["_id"]})
            
            # --- SYNCHRONIZACJA Z WIDOKIEM (kolekcja 'schedules') ---
            date_str = shift_date.isoformat()
            pull_path = f"schedule.{date_str}.{shift_name}.employees"
            schedule_query = {
                "franchise_code": franchise_code,
                "start_date": {"$lte": datetime.combine(shift_date, time.min)},
                "end_date": {"$gte": datetime.combine(shift_date, time.max)}
            }
            await db.schedules.update_one(
                schedule_query,
                {"$pull": {pull_path: {"id": str(employee_id)}}}
            )
            # -------------------------------------------------------
            
            replacements_report.append(schemas.ReplacementInfo(
                date=shift_date,
                shift_name=shift_name,
                original_employee_id=employee_id,
                replacement_employee_id=None,
                status="no_candidate"
            ))
            logger.warning(f"Brak kandydata na zastępstwo dnia {shift_date}")

    return schemas.SickLeaveResponse(
        message="L4 zostało wprowadzone. Sprawdź raport zastępstw.",
        replacements=replacements_report
    )

async def find_replacement(
    validator: ScheduleValidator,
    franchise_code: str,
    candidates: List[dict],
    date_obj: date,
    start_time: time,
    end_time: time
) -> Optional[dict]:
    """
    Znajduje najlepszego kandydata na zastępstwo używając Validatora.
    """
    valid_candidates = []
    
    for candidate in candidates:
        cand_id = candidate["_id"]
        
        # Używamy walidatora do sprawdzenia wszystkich reguł (odpoczynek, ciągłość, urlopy, itp.)
        is_valid, reason = await validator.validate_shift_assignment(
            cand_id, date_obj, start_time, end_time, franchise_code
        )
        
        if is_valid:
            valid_candidates.append(candidate)
        else:
            # Można logować powód odrzucenia dla debugowania
            # logger.debug(f"Kandydat {cand_id} odrzucony: {reason}")
            pass

    if not valid_candidates:
        return None

    # Sortowanie kandydatów (heurystyka)
    # SC1: Priorytetyzacja UoP (jeśli brakuje godzin) - TODO: Dodać logikę sprawdzania godzin
    # Na razie proste sortowanie: UoP > UZ
    
    def sort_key(emp):
        contract = emp.get("contract_type", "")
        if contract == models.ContractType.UOP.value:
            return 0
        return 1
        
    valid_candidates.sort(key=sort_key)
    
    return valid_candidates[0]
