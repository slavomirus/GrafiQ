# OSTATECZNA WERSJA: Poprawiono importy, aby rozwiązać błąd cyklicznej zależności.
from fastapi import APIRouter, Depends, HTTPException, Request
from datetime import datetime, timedelta, date, time
import logging
import json
from typing import List, Dict, Any, Optional
from bson import ObjectId
import motor.motor_asyncio
from pymongo import UpdateOne

from ..database import get_db
from .. import models, schemas
from ..dependencies import get_current_active_user # <-- POPRAWKA

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/", response_model=List[schemas.Availability])
async def create_or_update_availability_unified(
        request: Request,
        current_user: dict = Depends(get_current_active_user),
        db: motor.motor_asyncio.AsyncIOMotorClient = Depends(get_db)
):
    body = await request.json()
    availabilities_to_process = []

    if "data" in body and isinstance(body.get("data"), str):
        try:
            payload = json.loads(body["data"])
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid JSON format in 'data' field.")
    else:
        payload = body

    if "dates" in payload and "user_id" in payload:
        try:
            for date_str, details in payload["dates"].items():
                start_time_obj, end_time_obj = None, None
                if details.get("hours") and details["hours"].strip():
                    try:
                        start_str, end_str = details["hours"].split(" - ")
                        start_time_obj = datetime.strptime(start_str.strip(), "%H:%M").time()
                        end_time_obj = datetime.strptime(end_str.strip(), "%H:%M").time()
                    except (ValueError, TypeError):
                        logger.warning(f"Could not parse hours: {details.get('hours')} for date {date_str}")

                availabilities_to_process.append(
                    schemas.AvailabilityCreate(
                        date=date.fromisoformat(date_str),
                        start_time=start_time_obj,
                        end_time=end_time_obj,
                        period_type=details["type"]
                    )
                )
        except Exception as e:
            logger.error(f"Error processing client batch payload: {e}", exc_info=True)
            raise HTTPException(status_code=400, detail="Invalid batch payload format.")
    else:
        try:
            single_availability = schemas.AvailabilityCreate(**payload)
            availabilities_to_process.append(single_availability)
        except Exception as e:
            logger.error(f"Invalid payload for single availability: {e}", exc_info=True)
            raise HTTPException(status_code=422, detail="Invalid request payload.")

    if not availabilities_to_process:
        raise HTTPException(status_code=400, detail="No valid availability data provided.")

    return await create_or_update_availability_batch(availabilities_to_process, current_user, db)


@router.get("/available-dates")
async def get_available_dates(
        current_user: dict = Depends(get_current_active_user),
        db: motor.motor_asyncio.AsyncIOMotorClient = Depends(get_db)
):
    current_date = datetime.now().date()
    max_future_date = current_date + timedelta(days=60)

    user_availabilities = await db.availability.find({
        "user_id": ObjectId(current_user["_id"]),
        "date": {
            "$gte": datetime.combine(current_date, time.min),
            "$lte": datetime.combine(max_future_date, time.max)
        }
    }).to_list(length=None)

    user_dates = [availability["date"].date().isoformat() for availability in user_availabilities]

    return {
        "min_date": current_date.isoformat(),
        "max_date": max_future_date.isoformat(),
        "user_dates": user_dates
    }


@router.put("/{availability_id}", response_model=schemas.Availability)
async def update_availability(
        availability_id: str,
        availability: schemas.AvailabilityCreate,
        current_user: dict = Depends(get_current_active_user),
        db: motor.motor_asyncio.AsyncIOMotorClient = Depends(get_db)
):
    db_availability = await db.availability.find_one({
        "_id": ObjectId(availability_id),
        "user_id": ObjectId(current_user["_id"])
    })

    if not db_availability:
        raise HTTPException(status_code=404, detail="Dyspozycja nie znaleziona")

    update_data = {
        "date": datetime.combine(availability.date, time.min),
        "start_time": availability.start_time.isoformat() if availability.start_time else None,
        "end_time": availability.end_time.isoformat() if availability.end_time else None,
        "period_type": availability.period_type
    }

    await db.availability.update_one(
        {"_id": ObjectId(availability_id)},
        {"$set": update_data}
    )

    updated_availability = await db.availability.find_one({"_id": ObjectId(availability_id)})
    return updated_availability


@router.delete("/{availability_id}")
async def delete_availability(
        availability_id: str,
        current_user: dict = Depends(get_current_active_user),
        db: motor.motor_asyncio.AsyncIOMotorClient = Depends(get_db)
):
    db_availability = await db.availability.find_one({
        "_id": ObjectId(availability_id),
        "user_id": ObjectId(current_user["_id"])
    })

    if not db_availability:
        raise HTTPException(status_code=404, detail="Dyspozycja nie znaleziona")

    await db.availability.delete_one({"_id": ObjectId(availability_id)})

    await db.users.update_one(
        {"_id": ObjectId(current_user["_id"])},
        {"$pull": {"availability": ObjectId(availability_id)}}
    )

    return {"message": "Dyspozycja usunięta"}


@router.get("/my-availability", response_model=List[schemas.Availability])
async def get_my_availability(
        current_user: dict = Depends(get_current_active_user),
        db: motor.motor_asyncio.AsyncIOMotorClient = Depends(get_db)
):
    availabilities = await db.availability.find({
        "user_id": ObjectId(current_user["_id"])
    }).to_list(length=None)
    return availabilities


@router.post("/batch", response_model=List[schemas.Availability])
async def create_or_update_availability_batch(
        availabilities: List[schemas.AvailabilityCreate],
        current_user: dict = Depends(get_current_active_user),
        db: motor.motor_asyncio.AsyncIOMotorClient = Depends(get_db)
):
    current_date = datetime.now().date()
    max_future_date = current_date + timedelta(days=60)

    valid_availabilities = [
        av for av in availabilities if current_date <= av.date <= max_future_date
    ]

    if not valid_availabilities:
        return []

    datetime_dates = [datetime.combine(av.date, time.min) for av in valid_availabilities]
    user_id = ObjectId(current_user["_id"])

    existing_cursor = db.availability.find({
        "user_id": user_id,
        "date": {"$in": datetime_dates}
    })
    existing_map = {av['date'].date(): av async for av in existing_cursor}

    update_operations = []
    new_availability_docs = []
    updated_ids = []

    for av in valid_availabilities:
        av_datetime = datetime.combine(av.date, time.min)
        availability_data = {
            "date": av_datetime,
            "start_time": av.start_time.isoformat() if av.start_time else None,
            "end_time": av.end_time.isoformat() if av.end_time else None,
            "period_type": av.period_type,
        }

        existing = existing_map.get(av.date)
        if existing:
            updated_ids.append(existing["_id"])
            update_operations.append(UpdateOne(
                {"_id": existing["_id"]},
                {"$set": {**availability_data, "user_id": user_id}}
            ))
        else:
            new_availability_docs.append({
                **availability_data,
                "user_id": user_id,
                "submitted_at": datetime.utcnow()
            })

    result_ids = updated_ids

    if update_operations:
        await db.availability.bulk_write(update_operations, ordered=False)

    if new_availability_docs:
        insert_result = await db.availability.insert_many(new_availability_docs, ordered=False)
        inserted_ids = insert_result.inserted_ids
        result_ids.extend(inserted_ids)

        if inserted_ids:
            await db.users.update_one(
                {"_id": user_id},
                {"$push": {"availability": {"$each": inserted_ids}}}
            )

    if not result_ids:
        return []

    final_results = await db.availability.find({"_id": {"$in": result_ids}}).to_list(length=None)

    return final_results


@router.get("/user/{user_id}")
async def get_user_availability(
        user_id: str,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        current_user: dict = Depends(get_current_active_user),
        db: motor.motor_asyncio.AsyncIOMotorClient = Depends(get_db)
):
    """
    Pobiera dyspozycje dla wskazanego użytkownika w opcjonalnym przedziale dat.
    """
    try:
        user_obj_id = ObjectId(user_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Nieprawidłowy format ID użytkownika.")
        
    # Sprawdzenie uprawnień
    if str(current_user["_id"]) != user_id and current_user.get("role") not in [models.UserRole.FRANCHISEE.value, models.UserRole.ADMIN.value, "franchisee", "admin"]:
        raise HTTPException(status_code=403, detail="Brak uprawnień do przeglądania dyspozycji tego użytkownika.")

    query: Dict[str, Any] = {
        "user_id": {"$in": [user_obj_id, user_id]}
    }
    
    if start_date and end_date:
        start_dt = datetime.combine(start_date, time.min)
        end_dt = datetime.combine(end_date, time.max)
        start_str = start_date.strftime("%Y-%m-%d")
        end_str = end_date.strftime("%Y-%m-%d")
        query["$or"] = [
            {"date": {"$gte": start_dt, "$lte": end_dt}},
            {"date": {"$gte": start_str, "$lte": end_str}}
        ]
    elif start_date:
        start_dt = datetime.combine(start_date, time.min)
        start_str = start_date.strftime("%Y-%m-%d")
        query["$or"] = [
            {"date": {"$gte": start_dt}},
            {"date": {"$gte": start_str}}
        ]

    availabilities = await db.availability.find(query).to_list(length=None)
    
    formatted = []
    for av in availabilities:
        raw_date = av.get("date")
        if isinstance(raw_date, datetime):
            date_str = raw_date.strftime("%Y-%m-%d")
        elif isinstance(raw_date, date):
            date_str = raw_date.strftime("%Y-%m-%d")
        else:
            date_str = str(raw_date or "")

        formatted.append({
            "_id": str(av["_id"]),
            "user_id": str(av.get("user_id")),
            "date": date_str,
            "start_time": av.get("start_time"),
            "end_time": av.get("end_time"),
            "period_type": av.get("period_type"),
            "submitted_at": av.get("submitted_at")
        })
    return formatted


@router.get("/monitor/status")
async def monitor_availability_status(
        start_date: date,
        end_date: date,
        current_user: dict = Depends(get_current_active_user),
        db: motor.motor_asyncio.AsyncIOMotorClient = Depends(get_db)
):
    """
    Monitoruje status składania dyspozycji przez pracowników franczyzy.
    Zwraca listę pracowników z flagą, czy złożyli dyspozycję w danym okresie.
    """
    user_role = str(current_user.get("role", "")).lower()
    if user_role not in [models.UserRole.FRANCHISEE.value, models.UserRole.ADMIN.value, "franchisee", "admin"]:
        raise HTTPException(status_code=403, detail="Dostęp tylko dla franczyzobiorców.")
        
    franchise_code = current_user.get("franchise_code")
    if not franchise_code:
        raise HTTPException(status_code=400, detail="Brak przypisanego kodu franczyzy.")
        
    # Pobierz wszystkich pracowników sklepu (obsługa multi-store i case-insensitive roli)
    employees = await db.users.find({
        "$or": [
            {"franchise_code": franchise_code},
            {"franchise_codes": franchise_code}
        ],
        "role": {"$in": [models.UserRole.EMPLOYEE.value, "employee", "EMPLOYEE"]}
    }).to_list(length=None)
    
    # Sortowanie alfabetyczne po nazwisku i imieniu
    employees.sort(key=lambda u: (u.get("last_name", "").lower(), u.get("first_name", "").lower()))

    # Przygotuj zapytanie o dyspozycje
    start_dt = datetime.combine(start_date, time.min)
    end_dt = datetime.combine(end_date, time.max)
    start_str = start_date.strftime("%Y-%m-%d")
    end_str = end_date.strftime("%Y-%m-%d")
    
    emp_ids = [emp["_id"] for emp in employees]
    emp_identifiers = emp_ids + [str(eid) for eid in emp_ids]
    
    availabilities = await db.availability.find({
        "user_id": {"$in": emp_identifiers},
        "$or": [
            {"date": {"$gte": start_dt, "$lte": end_dt}},
            {"date": {"$gte": start_str, "$lte": end_str}}
        ]
    }).to_list(length=None)
    
    # Zlicz ile deklaracji złożył dany pracownik
    counts_by_user = {}
    for av in availabilities:
        uid_str = str(av.get("user_id"))
        counts_by_user[uid_str] = counts_by_user.get(uid_str, 0) + 1
    
    result = []
    for emp in employees:
        emp_id_str = str(emp["_id"])
        declared_count = counts_by_user.get(emp_id_str, 0)
        result.append({
            "user_id": emp_id_str,
            "first_name": emp.get("first_name", ""),
            "last_name": emp.get("last_name", ""),
            "email": emp.get("email", ""),
            "submitted": declared_count > 0,
            "declared_days_count": declared_count
        })
        
    return result


@router.post("/monitor/remind")
async def remind_about_availability(
        request: Request,
        current_user: dict = Depends(get_current_active_user),
        db: motor.motor_asyncio.AsyncIOMotorClient = Depends(get_db)
):
    """
    Wysyła powiadomienie push oraz email do wskazanych użytkowników z przypomnieniem o złożeniu dyspozycji.
    """
    user_role = str(current_user.get("role", "")).lower()
    if user_role not in [models.UserRole.FRANCHISEE.value, models.UserRole.ADMIN.value, "franchisee", "admin"]:
        raise HTTPException(status_code=403, detail="Dostęp tylko dla franczyzobiorców.")
        
    try:
        body = await request.json()
    except Exception:
        body = {}

    user_ids = body.get("user_ids", []) if isinstance(body, dict) else (body if isinstance(body, list) else [])
    
    if not user_ids:
        raise HTTPException(status_code=400, detail="Nie podano użytkowników do powiadomienia.")
        
    from ..services.notification_service import send_push_to_user
    from ..email_service import send_availability_reminder_email
    
    success_count = 0
    for uid in user_ids:
        try:
            user_obj_id = ObjectId(uid)
            user_doc = await db.users.find_one({"_id": user_obj_id})
            if not user_doc:
                continue

            first_name = user_doc.get("first_name", "Pracowniku")
            user_email = user_doc.get("email")

            # 1. Wysyłka Push (FCM)
            try:
                await send_push_to_user(
                    db=db,
                    user_id=user_obj_id,
                    title="Przypomnienie o Dyspozycji",
                    body="Kierownik sklepu prosi o uzupełnienie dyspozycji na nadchodzący okres grafikowy.",
                    data={"type": "availability_reminder"}
                )
            except Exception as push_err:
                logger.warning(f"Błąd wysyłki push do {uid}: {push_err}")

            # 2. Wysyłka Email (gwarantowane dotarcie nawet przy wyłączonych powiadomieniach push)
            if user_email:
                try:
                    await send_availability_reminder_email(
                        email=user_email,
                        first_name=first_name
                    )
                except Exception as mail_err:
                    logger.warning(f"Błąd wysyłki email do {user_email}: {mail_err}")

            success_count += 1
        except Exception as e:
            logger.error(f"Nie udało się wysłać przypomnienia do {uid}: {e}")
            
    return {
        "message": f"Wysłano przypomnienia do {success_count} pracowników.",
        "success_count": success_count
    }
