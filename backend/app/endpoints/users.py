# OSTATECZNA WERSJA: Poprawiono importy, aby rozwiązać błąd cyklicznej zależności.

import motor.motor_asyncio
from fastapi import APIRouter, Depends, HTTPException, status
from typing import List, Optional
from bson import ObjectId
import logging
import secrets
import string
import unidecode
from datetime import datetime

from ..database import get_db
from .. import models, schemas, security
# POPRAWKA: Importowanie zależności z nowego, dedykowanego modułu
from ..dependencies import get_current_active_user, get_current_admin_user
from ..email_service import send_new_employee_credentials_email

router = APIRouter()
logger = logging.getLogger(__name__)

def generate_temp_password(length: int = 10) -> str:
    """Generuje bezpieczne, losowe hasło tymczasowe."""
    alphabet = string.ascii_letters + string.digits + "-!@#$%^&*()"
    return ''.join(secrets.choice(alphabet) for _ in range(length))

async def generate_unique_username(db: motor.motor_asyncio.AsyncIOMotorDatabase, first_name: str, last_name: str) -> str:
    """Generuje unikalny username w formacie imie.nazwisko."""
    base_username = f"{unidecode.unidecode(first_name).lower()}.{unidecode.unidecode(last_name).lower()}"
    username = base_username
    counter = 1
    while await db.users.find_one({"username": username}):
        counter += 1
        username = f"{base_username}{counter}"
    return username

@router.post("/add-employee", response_model=schemas.MessageResponse)
async def add_employee(
    employee_data: schemas.UserCreateByAdmin,
    current_user: dict = Depends(get_current_admin_user),
    db: motor.motor_asyncio.AsyncIOMotorDatabase = Depends(get_db)
):
    """Tworzy nowego pracownika i wysyła mu dane logowania na e-mail."""
    existing_user = await db.users.find_one({"email": employee_data.email})
    if existing_user:
        raise HTTPException(status_code=400, detail="Użytkownik o podanym adresie e-mail już istnieje")

    contract_type_mapping = {
        "zlecenie": models.ContractType.UZ,
        "praca": models.ContractType.UOP
    }
    contract_type = contract_type_mapping.get(employee_data.employment_type.lower())

    if not contract_type:
        raise HTTPException(status_code=400, detail=f"Nieprawidłowy typ zatrudnienia: {employee_data.employment_type}")

    temp_password = generate_temp_password()
    hashed_password = security.get_password_hash(temp_password)
    username = await generate_unique_username(db, employee_data.first_name, employee_data.last_name)

    new_user_data = {
        "email": employee_data.email,
        "first_name": employee_data.first_name,
        "last_name": employee_data.last_name,
        "username": username,
        "hashed_password": hashed_password,
        "role": models.UserRole.EMPLOYEE.value,
        "status": "needs_password_change",
        "franchise_code": current_user.get("franchise_code"),
        "store_location": current_user.get("store_location"),
        "contract_type": contract_type.value,
        "vacation_days_left": 26,
        "email_verified": True,
        "created_at": datetime.utcnow(),
        "preferences": schemas.UserPreferences().dict(),
    }

    await db.users.insert_one(new_user_data)
    
    try:
        await send_new_employee_credentials_email(
            email=employee_data.email,
            first_name=employee_data.first_name,
            username=username,
            temp_password=temp_password
        )
    except Exception as e:
        logger.error(f"Nie udało się wysłać e-maila z danymi logowania do {employee_data.email}, ale użytkownik został utworzony. Błąd: {e}")
        raise HTTPException(status_code=500, detail="Użytkownik został utworzony, ale nie udało się wysłać e-maila z danymi logowania.")

    return {"message": "Pracownik został pomyślnie dodany. Dane do logowania zostały wysłane na jego adres e-mail."}


@router.put("/me/preferences", response_model=schemas.UserPreferences)
async def update_my_preferences(
    preferences_data: schemas.UserPreferences,
    current_user: dict = Depends(get_current_active_user),
    db: motor.motor_asyncio.AsyncIOMotorDatabase = Depends(get_db)
):
    """Pozwala zalogowanemu użytkownikowi na aktualizację swoich preferencji pracy."""
    result = await db.users.update_one(
        {"_id": current_user["_id"]},
        {"$set": {"preferences": preferences_data.dict()}}
    )

    if result.matched_count == 0:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Nie znaleziono użytkownika.")

    logger.info(f"Preferencje użytkownika {current_user.get('email') or current_user.get('username')} zostały zaktualizowane.")
    return preferences_data


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: str,
    current_user: dict = Depends(get_current_admin_user),
    db: motor.motor_asyncio.AsyncIOMotorDatabase = Depends(get_db)
):
    """Usuwa pracownika i wszystkie jego powiązane dane."""
    try:
        user_to_delete_id = ObjectId(user_id)
    except Exception:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Nieprawidłowy format ID użytkownika.")

    user_to_delete = await db.users.find_one({"_id": user_to_delete_id})
    if not user_to_delete:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Użytkownik nie znaleziony")

    if current_user.get("role") == models.UserRole.FRANCHISEE.value:
        if user_to_delete.get("franchise_code") != current_user.get("franchise_code"):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Brak uprawnień do usunięcia tego użytkownika.")

    await db.availability.delete_many({"user_id": user_to_delete_id})
    await db.schedule.delete_many({"user_id": user_to_delete_id})
    await db.vacations.delete_many({"user_id": user_to_delete_id})

    result = await db.users.delete_one({"_id": user_to_delete_id})

    if result.deleted_count == 0:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Nie udało się usunąć użytkownika.")

    logger.info(f"Użytkownik {user_id} i jego dane zostały pomyślnie usunięte przez {current_user.get('email')}.")

    return


@router.get("/", response_model=List[schemas.UserResponse])
async def get_users(
    skip: int = 0,
    limit: int = 100,
    franchise_code: Optional[str] = None,
    role: Optional[str] = None,
    current_user: dict = Depends(get_current_admin_user),
    db: motor.motor_asyncio.AsyncIOMotorDatabase = Depends(get_db)
):
    """Pobierz listę użytkowników z rygorystycznym filtrowaniem dla franczyzobiorcy."""
    try:
        query = {}
        
        if current_user.get("role") == models.UserRole.FRANCHISEE.value:
            query["franchise_code"] = current_user.get("franchise_code")
        elif franchise_code:
            query["franchise_code"] = franchise_code

        if role:
            query["role"] = role

        users_cursor = db.users.find(query).skip(skip).limit(limit)
        users = await users_cursor.to_list(length=limit)
        return users
    except Exception as e:
        logger.error(f"Błąd podczas pobierania użytkowników: {e}")
        raise HTTPException(status_code=500, detail="Błąd podczas pobierania użytkowników")


@router.get("/my-employees", response_model=List[schemas.UserResponse])
async def get_my_employees(
    current_user: dict = Depends(get_current_admin_user),
    db: motor.motor_asyncio.AsyncIOMotorDatabase = Depends(get_db)
):
    """Zwraca listę pracowników dla zalogowanego franczyzobiorcy."""
    if current_user.get("role") != models.UserRole.FRANCHISEE.value:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Dostępne tylko dla franczyzobiorców.")
    
    franchise_code = current_user.get("franchise_code")
    employees = await db.users.find({
        "franchise_code": franchise_code,
        "role": models.UserRole.EMPLOYEE.value
    }).to_list(length=None)
    
    return employees


@router.get("/me/profile", response_model=schemas.UserResponse)
async def get_my_profile(
    current_user: dict = Depends(get_current_active_user)
):
    """
    Pobierz profil bieżącego, zalogowanego użytkownika.
    """
    return schemas.UserResponse(**current_user)


@router.get("/{user_id}", response_model=schemas.UserResponse)
async def get_user(
    user_id: str,
    current_user: dict = Depends(get_current_active_user),
    db: motor.motor_asyncio.AsyncIOMotorDatabase = Depends(get_db)
):
    """ Pobierz szczegóły użytkownika """
    try:
        if (current_user["role"] not in [models.UserRole.ADMIN.value, models.UserRole.FRANCHISEE.value] and
                str(current_user["_id"]) != user_id):
            raise HTTPException(status_code=403, detail="Brak uprawnień")

        user = await db.users.find_one({"_id": ObjectId(user_id)})
        if not user:
            raise HTTPException(status_code=404, detail="Użytkownik nie znaleziony")
        return user
    except Exception as e:
        logger.error(f"Błąd podczas pobierania użytkownika {user_id}: {e}")
        raise HTTPException(status_code=500, detail="Błąd podczas pobierania użytkownika")
