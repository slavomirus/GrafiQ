# OSTATECZNA WERSJA: Dodano zapis domyślnych preferencji przy rejestracji franczyzobiorcy.

import logging
from fastapi import APIRouter, Depends, HTTPException, status, Request
from starlette.responses import JSONResponse
from datetime import datetime, timedelta
import motor.motor_asyncio
import secrets

from ..database import get_db
from .. import models, schemas, security
from ..email_service import send_verification_code_email
from ..dependencies import get_current_user
from ..config import settings

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/register/franchisee", response_model=schemas.RegistrationResponse)
async def register_franchisee(user: schemas.UserCreateFranchisee,
                              request: Request,
                              db: motor.motor_asyncio.AsyncIOMotorClient = Depends(get_db)):
    # Walidacja zgód
    if not user.acceptEula or not user.acceptRodo:
        raise HTTPException(status_code=400, detail="Akceptacja EULA i RODO jest wymagana do założenia konta")

    # Sanityzacja kodu franczyzy (usuwanie spacji)
    clean_franchise_code = user.franchise_code.strip()

    existing_user = await db.users.find_one({"email": user.email})
    if existing_user:
        # Jeśli użytkownik istnieje, ale jest w fazie PENDING i nie zweryfikował maila,
        # uznajemy to za nieudaną wcześniejszą rejestrację i usuwamy stare konto.
        if existing_user.get("status") == models.UserStatus.PENDING.value and not existing_user.get("email_verified"):
            logger.info(f"Wykryto niezweryfikowane konto PENDING dla {user.email}. Usuwanie i ponowna rejestracja.")
            
            # Sprawdź czy istnieje sklep powiązany z tym niezweryfikowanym użytkownikiem
            existing_store = await db.stores.find_one({"franchise_code": clean_franchise_code})
            if existing_store and existing_store.get("owner_id") == existing_user["_id"]:
                await db.stores.delete_one({"_id": existing_store["_id"]})
                logger.info(f"Usunięto osierocony sklep dla kodu: {clean_franchise_code}")
            
            await db.users.delete_one({"_id": existing_user["_id"]})
        else:
            raise HTTPException(status_code=400, detail="Użytkownik o podanym adresie e-mail już istnieje")

    hashed_password = security.get_password_hash(user.password.get_secret_value())
    verification_code = ''.join(secrets.choice('0123456789') for _ in range(6))

    store_location = f"{user.city}, {user.street} {user.building_number}"
    
    # Przygotowanie struktury zgód
    now = datetime.utcnow()
    agreements_data = {
        "eula": {"acceptedAt": now, "version": settings.EULA_VERSION},
        "rodo": {"acceptedAt": now, "version": settings.RODO_VERSION},
        "ipAddress": request.client.host
    }

    new_user_data = {
        "email": user.email,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "phone": user.phone,
        "hashed_password": hashed_password,
        "role": models.UserRole.FRANCHISEE.value,
        "status": models.UserStatus.PENDING.value,
        "franchise_code": clean_franchise_code,
        "store_location": store_location,
        "vacation_days_left": 26,
        "email_verified": False,
        "created_at": now,
        "verification_code": verification_code,
        "verification_code_expires_at": now + timedelta(minutes=15),
        "preferences": schemas.UserPreferences().dict(),
        "agreements": agreements_data
    }

    result = await db.users.insert_one(new_user_data)
    user_id = str(result.inserted_id)
    
    # Automatyczne utworzenie sklepu dla franczyzobiorcy
    existing_store = await db.stores.find_one({"franchise_code": clean_franchise_code})
    if not existing_store:
        new_store_data = {
            "franchise_code": clean_franchise_code,
            "owner_id": result.inserted_id,
            "province": user.province,
            "city": user.city,
            "postal_code": user.postal_code,
            "street": user.street,
            "building_number": user.building_number,
            "created_at": now
        }
        await db.stores.insert_one(new_store_data)
        logger.info(f"Utworzono nowy sklep dla kodu: {clean_franchise_code}")

    await send_verification_code_email(
        email=user.email,
        first_name=user.first_name,
        verification_code=verification_code
    )

    return {
        "message": "Pomyślnie zarejestrowano. Sprawdź swoją skrzynkę odbiorczą, aby zweryfikować adres e-mail.",
        "user_id": user_id
    }


@router.post("/token", response_model=schemas.LoginResponse)
async def login_for_access_token(login_data: schemas.LoginRequest,
                                 db: motor.motor_asyncio.AsyncIOMotorClient = Depends(get_db)):
    login_identifier = login_data.email
    
    if "@" in login_identifier:
        user = await db.users.find_one({"email": login_identifier})
    else:
        user = await db.users.find_one({"username": login_identifier})

    if not user or not security.verify_password(login_data.password, user.get("hashed_password", "")):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Niepoprawny e-mail lub hasło.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Sprawdzanie wersji EULA
    requires_agreement_update = False
    if user.get("role") == models.UserRole.FRANCHISEE.value:
        user_eula_version = user.get("agreements", {}).get("eula", {}).get("version")
        if user_eula_version != settings.EULA_VERSION:
            requires_agreement_update = True

    access_token = security.create_access_token(data={"sub": str(user["_id"])})

    if user.get("status") == "needs_password_change":
        return JSONResponse(
            status_code=status.HTTP_426_UPGRADE_REQUIRED,
            content={
                "temp_access_token": access_token, 
                "token_type": "bearer",
                "requires_agreement_update": requires_agreement_update
            }
        )

    if user.get("status") != models.UserStatus.ACTIVE.value:
        raise HTTPException(
            status_code=status.HTTP_403,
            detail=f"User account is not active. Current status: {user.get('status')}",
        )

    return {
        "access_token": access_token, 
        "token_type": "bearer",
        "requires_agreement_update": requires_agreement_update
    }


@router.post("/set-new-password", response_model=schemas.Token)
async def set_initial_password(
    password_data: schemas.SetInitialPasswordRequest,
    current_user: dict = Depends(get_current_user),
    db: motor.motor_asyncio.AsyncIOMotorClient = Depends(get_db)
):
    """Pozwala pracownikowi na ustawienie swojego pierwszego hasła."""
    if current_user.get("status") != "needs_password_change":
        raise HTTPException(status_code=403, detail="This endpoint is only for the initial password change.")

    new_hashed_password = security.get_password_hash(password_data.new_password.get_secret_value())

    await db.users.update_one(
        {"_id": current_user["_id"]},
        {"$set": {"hashed_password": new_hashed_password, "status": models.UserStatus.ACTIVE.value}}
    )

    access_token = security.create_access_token(data={"sub": str(current_user["_id"])})
    return {"access_token": access_token, "token_type": "bearer"}
