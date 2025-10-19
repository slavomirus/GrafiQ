# OSTATECZNA WERSJA: Poprawiono importy, aby rozwiązać błąd cyklicznej zależności.
from fastapi import APIRouter, Depends, HTTPException, Request
from datetime import datetime, timedelta, date, time
import logging
import json
from typing import List, Dict, Any
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
