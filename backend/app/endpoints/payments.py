# Plik: backend/app/endpoints/payments.py

from fastapi import APIRouter, Depends, HTTPException, status
from typing import Optional
from datetime import datetime, timedelta
import motor.motor_asyncio
import random
import string
import logging

from ..database import get_db
from ..dependencies import get_current_active_user
from .. import models, schemas

logger = logging.getLogger(__name__)

router = APIRouter()

def generate_random_code(length=6):
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))

@router.post("/generate-code", response_model=schemas.ReferralCodeResponse)
async def generate_referral_code(
    current_user: dict = Depends(get_current_active_user),
    db: motor.motor_asyncio.AsyncIOMotorClient = Depends(get_db)
):
    """
    Generuje i zapisuje kod polecający dla franczyzobiorcy.
    """
    if current_user.get("role") != models.UserRole.FRANCHISEE.value:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Tylko franczyzobiorca może generować kody polecające."
        )
        
    owner_id = current_user["_id"]
    
    # Sprawdź, czy użytkownik ma już aktywny kod
    existing_code = await db.referral_codes.find_one({
        "owner_id": owner_id,
        "uses_left": {"$gt": 0}
    })
    
    if existing_code:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Masz już aktywny kod z niezużytymi użyciami."
        )

    # Wygeneruj unikalny kod
    while True:
        new_code_str = generate_random_code()
        collision = await db.referral_codes.find_one({"code": new_code_str})
        if not collision:
            break

    new_code = {
        "code": new_code_str,
        "owner_id": owner_id,
        "uses_left": 5,
        "max_uses": 5,
        "used_by": [],
        "created_at": datetime.utcnow()
    }
    
    result = await db.referral_codes.insert_one(new_code)
    new_code["_id"] = result.inserted_id
    
    return new_code

@router.get("/my-code", response_model=schemas.ReferralCodeResponse)
async def get_my_referral_code(
    current_user: dict = Depends(get_current_active_user),
    db: motor.motor_asyncio.AsyncIOMotorClient = Depends(get_db)
):
    """
    Zwraca aktywny kod użytkownika, liczbę użyć i datę ważności darmowego dostępu.
    """
    owner_id = current_user["_id"]
    code_doc = await db.referral_codes.find_one(
        {"owner_id": owner_id},
        sort=[("created_at", -1)]
    )
    
    if not code_doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Nie znaleziono kodu polecającego."
        )
        
    response_data = code_doc.copy()
    response_data["free_access_until"] = current_user.get("free_access_until")
    
    return response_data

@router.post("/redeem-code/{code}", response_model=schemas.MessageResponse)
async def redeem_referral_code(
    code: str,
    current_user: dict = Depends(get_current_active_user),
    db: motor.motor_asyncio.AsyncIOMotorClient = Depends(get_db)
):
    """
    Wykorzystuje kod polecający.
    Nowy użytkownik (franczyzobiorca) zyskuje 90 dni, a właściciel kodu 30 dni.
    """
    # Sprawdzenia wstępne
    if current_user.get("role") != models.UserRole.FRANCHISEE.value:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Tylko franczyzobiorca może realizować kody polecające."
        )
        
    user_id = current_user["_id"]

    # a) Sprawdza czy kod istnieje i czy uses_left > 0
    code_doc = await db.referral_codes.find_one({"code": code})
    if not code_doc:
         raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Nieprawidłowy kod polecający."
        )
        
    if code_doc.get("uses_left", 0) <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Ten kod osiągnął maksymalną liczbę użyć."
        )
        
    if code_doc.get("owner_id") == user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Nie możesz użyć własnego kodu polecającego."
        )

    # b) Sprawdza czy current_user już nie używał TEGO kodu (albo jakiegokolwiek, zależy od polityki. Zakładam, że jakiegokolwiek to pole)
    # Zgodnie z poleceniem: "Sprawdza czy current_user już nie używał tego kodu." / "już nie użył jakiegoś kodu."
    has_used_any_code = await db.users.find_one({
        "_id": user_id,
        "used_referral_code": {"$exists": True}
    })
    
    if has_used_any_code and has_used_any_code.get("used_referral_code"):
         raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Już skorzystałeś z kodu polecającego."
        )

    # OBLICZENIA DAT
    now = datetime.utcnow()
    
    # c) Dla Nowego (Zwycięzcy): Wydłuża o 90 dni
    user_free_until = current_user.get("free_access_until")
    if not user_free_until or user_free_until < now:
        new_user_date = now + timedelta(days=90)
    else:
        new_user_date = user_free_until + timedelta(days=90)

    # Pobierz właściciela kodu
    owner = await db.users.find_one({"_id": code_doc["owner_id"]})
    if not owner:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Właściciel kodu nie istnieje."
        )
        
    # d) Dla Polecającego (Ownera): Wydłuża o 30 dni
    owner_free_until = owner.get("free_access_until")
    if not owner_free_until or owner_free_until < now:
        new_owner_date = now + timedelta(days=30)
    else:
        new_owner_date = owner_free_until + timedelta(days=30)

    # ZAPIS DO BAZY
    
    # 1. Zaktualizuj current_user (używającego)
    await db.users.update_one(
        {"_id": user_id},
        {
            "$set": {
                "free_access_until": new_user_date,
                "used_referral_code": code
            }
        }
    )
    
    # 2. Zaktualizuj Ownera (polecającego)
    await db.users.update_one(
        {"_id": owner["_id"]},
        {"$set": {"free_access_until": new_owner_date}}
    )
    
    # 3. Zaktualizuj kod (uses_left - 1)
    await db.referral_codes.update_one(
        {"_id": code_doc["_id"]},
        {
            "$inc": {"uses_left": -1},
            "$push": {"used_by": user_id}
        }
    )

    return {"message": "Kod został pomyślnie zrealizowany. Otrzymujesz 90 dni darmowego dostępu!"}
