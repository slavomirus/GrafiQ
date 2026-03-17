import logging
from datetime import date, datetime, timedelta
import math
from typing import List, Optional
import motor.motor_asyncio
from bson import ObjectId
from fastapi import HTTPException, status

from .. import schemas, models

logger = logging.getLogger(__name__)

def calculate_vacation_days(seniority_years: int, fte: float) -> int:
    """
    Oblicza wymiar urlopu wypoczynkowego zgodnie z Kodeksem Pracy.
    
    Art. 154 KP:
    - 20 dni - jeżeli pracownik jest zatrudniony krócej niż 10 lat.
    - 26 dni - jeżeli pracownik jest zatrudniony co najmniej 10 lat.
    
    Wymiar urlopu dla pracownika zatrudnionego w niepełnym wymiarze czasu pracy
    ustala się proporcjonalnie do wymiaru czasu pracy tego pracownika.
    Niepełny dzień urlopu zaokrągla się w górę do pełnego dnia.
    """
    base_days = 26 if seniority_years >= 10 else 20
    proportional_days = base_days * fte
    return math.ceil(proportional_days)

async def check_vacation_conflict(
    db: motor.motor_asyncio.AsyncIOMotorDatabase,
    franchise_code: str,
    start_date: date,
    end_date: date,
    exclude_user_id: Optional[ObjectId] = None
) -> List[dict]:
    """
    Sprawdza, czy w danym terminie inni pracownicy mają już zatwierdzony urlop.
    Zwraca listę kolizji.
    """
    query = {
        "franchise_code": franchise_code, # Zakładamy, że urlop ma to pole (trzeba dodać przy tworzeniu)
        "status": models.VacationStatus.APPROVED.value,
        "start_date": {"$lte": datetime.combine(end_date, datetime.max.time())},
        "end_date": {"$gte": datetime.combine(start_date, datetime.min.time())}
    }
    
    if exclude_user_id:
        query["user_id"] = {"$ne": exclude_user_id}

    # Ponieważ w kolekcji vacations może nie być franchise_code (stare rekordy),
    # musimy najpierw pobrać userów z tego sklepu, a potem szukać ich urlopów.
    # To bezpieczniejsze podejście.
    
    store_users = await db.users.find({"franchise_code": franchise_code}).to_list(length=None)
    store_user_ids = [u["_id"] for u in store_users]
    
    if exclude_user_id:
        store_user_ids = [uid for uid in store_user_ids if uid != exclude_user_id]

    conflict_query = {
        "user_id": {"$in": store_user_ids},
        "status": models.VacationStatus.APPROVED.value,
        "start_date": {"$lte": datetime.combine(end_date, datetime.max.time())},
        "end_date": {"$gte": datetime.combine(start_date, datetime.min.time())}
    }
    
    conflicts = await db.vacations.find(conflict_query).to_list(length=None)
    
    # Wzbogać o dane użytkownika
    result = []
    for conflict in conflicts:
        user = await db.users.find_one({"_id": conflict["user_id"]})
        if user:
            result.append({
                "user_id": str(user["_id"]),
                "first_name": user.get("first_name", ""),
                "last_name": user.get("last_name", ""),
                "start_date": conflict["start_date"],
                "end_date": conflict["end_date"]
            })
            
    return result
