import logging
from datetime import datetime, time, date
from typing import List, Optional
import motor.motor_asyncio
from bson import ObjectId
from fastapi import HTTPException, status

from .. import schemas, models
from .notification_service import send_push_to_user, send_push_to_admins
from .validator_service import ScheduleValidator

logger = logging.getLogger(__name__)

async def create_swap_request(
    db: motor.motor_asyncio.AsyncIOMotorDatabase,
    request: schemas.ShiftSwapRequest,
    current_user: dict
) -> schemas.ShiftSwapResponse:
    
    requester_id = current_user["_id"]
    target_user_id = request.target_user_id
    franchise_code = current_user.get("franchise_code")

    if str(requester_id) == str(target_user_id):
        raise HTTPException(status_code=400, detail="Nie możesz wymienić się sam ze sobą.")

    # 1. Weryfikacja zmian w grafiku
    # Zapewniamy spójny typ ObjectId dla user_id
    requester_oid = ObjectId(requester_id) if not isinstance(requester_id, ObjectId) else requester_id
    target_oid = ObjectId(target_user_id) if not isinstance(target_user_id, ObjectId) else target_user_id

    requester_shift = await db.schedule.find_one({
        "franchise_code": franchise_code,
        "user_id": requester_oid,
        "date": datetime.combine(request.my_date, time.min),
        "shift_name": request.my_shift_name
    })
    if not requester_shift:
        raise HTTPException(status_code=404, detail="Nie znaleziono Twojej zmiany w podanym dniu.")

    target_shift = await db.schedule.find_one({
        "franchise_code": franchise_code,
        "user_id": target_oid,
        "date": datetime.combine(request.target_date, time.min),
        "shift_name": request.target_shift_name
    })
    if not target_shift:
        raise HTTPException(status_code=404, detail="Nie znaleziono zmiany pracownika, z którym chcesz się wymienić.")

    # 2. Walidacja reguł (Hard Constraints)
    validator = ScheduleValidator(db)
    swap_data = request.model_dump()
    swap_data["requester_id"] = requester_oid
    swap_data["franchise_code"] = franchise_code
    
    is_valid, reason = await validator.validate_swap(swap_data)
    if not is_valid:
        raise HTTPException(status_code=400, detail=f"Wymiana niemożliwa: {reason}")

    # 3. Zapisz wniosek
    swap_doc = {
        "requester_id": requester_id,
        "target_user_id": target_user_id,
        "franchise_code": franchise_code,
        "my_date": datetime.combine(request.my_date, time.min),
        "my_shift_name": request.my_shift_name,
        "target_date": datetime.combine(request.target_date, time.min),
        "target_shift_name": request.target_shift_name,
        "status": schemas.SwapStatus.REQUESTED.value,
        "created_at": datetime.utcnow()
    }
    
    result = await db.shift_swaps.insert_one(swap_doc)
    created_swap = await db.shift_swaps.find_one({"_id": result.inserted_id})
    
    # --- POWIADOMIENIA ---
    requester_name = f"{current_user.get('first_name')} {current_user.get('last_name')}"
    
    await send_push_to_user(
        db, 
        target_user_id, 
        "Nowa prośba o wymianę", 
        f"{requester_name} chce się wymienić zmianą z dnia {request.my_date}.",
        data={"type": "swap_request", "swap_id": str(created_swap["_id"])}
    )
    
    await send_push_to_admins(
        db,
        franchise_code,
        "Oferta wymiany zmian",
        f"{requester_name} proponuje wymianę zmian.",
        data={"type": "swap_offer", "swap_id": str(created_swap["_id"])}
    )
    # ---------------------
    
    created_swap["my_date"] = created_swap["my_date"].date()
    created_swap["target_date"] = created_swap["target_date"].date()
    
    return created_swap

async def respond_to_swap(
    db: motor.motor_asyncio.AsyncIOMotorDatabase,
    swap_id: str,
    action: str, # "accept" or "reject"
    current_user: dict
) -> schemas.MessageResponse:
    
    try:
        oid = ObjectId(swap_id)
    except:
        raise HTTPException(status_code=400, detail="Invalid ID")

    swap = await db.shift_swaps.find_one({"_id": oid})
    if not swap:
        raise HTTPException(status_code=404, detail="Wniosek nie znaleziony.")

    if str(swap["target_user_id"]) != str(current_user["_id"]):
        raise HTTPException(status_code=403, detail="Nie jesteś adresatem tej prośby.")

    if swap["status"] != schemas.SwapStatus.REQUESTED.value:
        raise HTTPException(status_code=400, detail="Wniosek nie jest już aktywny.")

    # Ponowna walidacja przy akceptacji (stan grafiku mógł się zmienić)
    if action == "accept":
        validator = ScheduleValidator(db)
        # Odtwórz strukturę requestu dla walidatora
        swap_data = {
            "requester_id": swap["requester_id"],
            "target_user_id": swap["target_user_id"],
            "franchise_code": swap["franchise_code"],
            "my_date": swap["my_date"].date(),
            "my_shift_name": swap["my_shift_name"],
            "target_date": swap["target_date"].date(),
            "target_shift_name": swap["target_shift_name"]
        }
        is_valid, reason = await validator.validate_swap(swap_data)
        if not is_valid:
            # Automatyczne odrzucenie jeśli warunki nie są spełnione
            await db.shift_swaps.update_one(
                {"_id": oid},
                {"$set": {"status": schemas.SwapStatus.REJECTED.value, "updated_at": datetime.utcnow()}}
            )
            raise HTTPException(status_code=400, detail=f"Wymiana niemożliwa (zmiana warunków): {reason}")

    new_status = schemas.SwapStatus.ACCEPTED.value if action == "accept" else schemas.SwapStatus.REJECTED.value
    
    await db.shift_swaps.update_one(
        {"_id": oid},
        {"$set": {"status": new_status, "updated_at": datetime.utcnow()}}
    )

    # --- POWIADOMIENIA ---
    responder_name = f"{current_user.get('first_name')} {current_user.get('last_name')}"
    status_msg = "zaakceptowana" if action == "accept" else "odrzucona"
    
    await send_push_to_user(
        db,
        swap["requester_id"],
        f"Wymiana {status_msg}",
        f"{responder_name} {status_msg} Twoją prośbę o wymianę.",
        data={"type": "swap_response", "swap_id": str(oid), "status": new_status}
    )
    # ---------------------

    if action == "accept":
        await execute_swap(db, swap)
        return {"message": "Wymiana zaakceptowana i przetworzona."}
    else:
        return {"message": "Wymiana odrzucona."}

async def execute_swap(db: motor.motor_asyncio.AsyncIOMotorDatabase, swap: dict):
    """
    Wykonuje fizyczną zamianę w grafiku (flat 'schedule' + nested 'schedules').
    """
    franchise_code = swap["franchise_code"]
    requester_id = ObjectId(swap["requester_id"]) if not isinstance(swap["requester_id"], ObjectId) else swap["requester_id"]
    target_user_id = ObjectId(swap["target_user_id"]) if not isinstance(swap["target_user_id"], ObjectId) else swap["target_user_id"]
    
    # Dane zmian
    date1 = swap["my_date"] # datetime
    shift1 = swap["my_shift_name"]
    
    date2 = swap["target_date"] # datetime
    shift2 = swap["target_shift_name"]

    # --- 1. Aktualizacja kolekcji płaskiej 'schedule' ---
    
    doc1 = await db.schedule.find_one({"franchise_code": franchise_code, "user_id": requester_id, "date": date1, "shift_name": shift1})
    doc2 = await db.schedule.find_one({"franchise_code": franchise_code, "user_id": target_user_id, "date": date2, "shift_name": shift2})
    
    if not doc1 or not doc2:
        missing = []
        if not doc1: missing.append(f"zmiana requestera ({shift1} w {date1})")
        if not doc2: missing.append(f"zmiana targeta ({shift2} w {date2})")
        logger.error(f"Swap {swap['_id']} failed: brak dokumentów: {', '.join(missing)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Błąd wykonania wymiany: nie znaleziono zmian w grafiku ({', '.join(missing)}). Skontaktuj się z administratorem."
        )

    # Zamień user_id w flat schedule
    await db.schedule.update_one({"_id": doc1["_id"]}, {"$set": {"user_id": target_user_id}})
    await db.schedule.update_one({"_id": doc2["_id"]}, {"$set": {"user_id": requester_id}})

    # --- 2. Aktualizacja kolekcji widoku 'schedules' (nested) ---
    
    user1 = await db.users.find_one({"_id": requester_id})
    user2 = await db.users.find_one({"_id": target_user_id})
    
    async def update_nested_schedule(date_obj, shift_name, old_user, new_user):
        """Aktualizuje zagnieżdżony grafik: usuwa starego pracownika, dodaje nowego."""
        date_dt = date_obj if isinstance(date_obj, datetime) else datetime.combine(date_obj, time.min)
        date_str = date_dt.date().isoformat() if isinstance(date_dt, datetime) else date_obj.isoformat()
        
        emp_path = f"schedule.{date_str}.{shift_name}.employees"
        old_id_str = str(old_user["_id"])
        new_id_str = str(new_user["_id"])
        
        query = {
            "franchise_code": franchise_code,
            "start_date": {"$lte": date_dt},
            "end_date": {"$gte": date_dt}
        }
        
        # Usuń starego — próbuj obie formy ID (string i ObjectId)
        await db.schedules.update_one(query, {"$pull": {emp_path: {"id": old_id_str}}})
        await db.schedules.update_one(query, {"$pull": {emp_path: {"id": old_user["_id"]}}})
        
        # Dodaj nowego — ZAWSZE jako string (spójność z generatorem grafiku)
        new_user_data = {
            "id": new_id_str,
            "first_name": new_user.get("first_name", ""),
            "last_name": new_user.get("last_name", "")
        }
        await db.schedules.update_one(query, {"$push": {emp_path: new_user_data}})

    # Zmiana 1: Requester wychodzi, Target wchodzi
    await update_nested_schedule(date1, shift1, user1, user2)
    
    # Zmiana 2: Target wychodzi, Requester wchodzi
    await update_nested_schedule(date2, shift2, user2, user1)

    logger.info(f"Swap executed successfully: {requester_id} <-> {target_user_id} | {shift1}@{date1} <-> {shift2}@{date2}")