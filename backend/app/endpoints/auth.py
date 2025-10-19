# OSTATECZNA WERSJA: Dodano zapis domyślnych preferencji przy rejestracji franczyzobiorcy.

import logging
from fastapi import APIRouter, Depends, HTTPException, status
from starlette.responses import JSONResponse
from datetime import datetime, timedelta
import motor.motor_asyncio
import secrets

from ..database import get_db
from .. import models, schemas, security
from ..email_service import send_verification_code_email
from ..dependencies import get_current_user

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/register/franchisee", response_model=schemas.RegistrationResponse)
async def register_franchisee(user: schemas.UserCreateFranchisee,
                              db: motor.motor_asyncio.AsyncIOMotorClient = Depends(get_db)):
    existing_user = await db.users.find_one({"email": user.email})
    if existing_user:
        raise HTTPException(status_code=400, detail="Użytkownik o podanym adresie e-mail już istnieje")

    hashed_password = security.get_password_hash(user.password.get_secret_value())
    verification_code = ''.join(secrets.choice('0123456789') for _ in range(6))

    store_location = f"{user.city}, {user.street} {user.building_number}"

    new_user_data = {
        "email": user.email,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "phone": user.phone,
        "hashed_password": hashed_password,
        "role": models.UserRole.FRANCHISEE.value,
        "status": models.UserStatus.PENDING.value,
        "franchise_code": user.franchise_code,
        "store_location": store_location,
        "vacation_days_left": 26,
        "email_verified": False,
        "created_at": datetime.utcnow(),
        "verification_code": verification_code,
        "verification_code_expires_at": datetime.utcnow() + timedelta(minutes=15),
        "preferences": schemas.UserPreferences().dict(), # <-- POPRAWKA: Dodanie domyślnych preferencji
    }

    result = await db.users.insert_one(new_user_data)
    user_id = str(result.inserted_id)

    await send_verification_code_email(
        email=user.email,
        first_name=user.first_name,
        verification_code=verification_code
    )

    return {
        "message": "Pomyślnie zarejestrowano. Sprawdź swoją skrzynkę odbiorczą, aby zweryfikować adres e-mail.",
        "user_id": user_id
    }


@router.post("/token")
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
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    access_token = security.create_access_token(data={"sub": str(user["_id"])})

    if user.get("status") == "needs_password_change":
        return JSONResponse(
            status_code=status.HTTP_426_UPGRADE_REQUIRED,
            content={"temp_access_token": access_token, "token_type": "bearer"}
        )

    if user.get("status") != models.UserStatus.ACTIVE.value:
        raise HTTPException(
            status_code=status.HTTP_403,
            detail=f"User account is not active. Current status: {user.get('status')}",
        )

    return {"access_token": access_token, "token_type": "bearer"}


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
