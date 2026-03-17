# Plik: backend/app/endpoints/users.py

import motor.motor_asyncio
from fastapi import APIRouter, Depends, HTTPException, status, Request
from typing import List, Optional
from bson import ObjectId
from bson.errors import InvalidId
import logging
import secrets
import string
import unidecode
from datetime import datetime, date, time, timedelta
from pydantic import ValidationError

from ..database import get_db
from .. import models, schemas, crud
from ..dependencies import get_current_active_user, get_current_admin_user
from ..email_service import send_new_employee_credentials_email
from ..services.schedule_replacement_service import find_best_solution_for_shift
from ..services.vacation_service import calculate_vacation_days
from ..config import settings

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
    hashed_password = crud.get_password_hash(temp_password)
    username = await generate_unique_username(db, employee_data.first_name, employee_data.last_name)

    # Obliczanie urlopu
    vacation_days = calculate_vacation_days(employee_data.seniority_years, employee_data.fte)
    if employee_data.leave_entitlement is not None:
        vacation_days = employee_data.leave_entitlement

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
        "vacation_days_left": vacation_days, # Zapisz obliczony urlop
        "fte": employee_data.fte,
        "seniority_years": employee_data.seniority_years,
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

@router.put("/{user_id}", response_model=schemas.UserResponse)
async def update_user(
    user_id: str,
    user_update: schemas.UserUpdate, # Zmieniono z UserBase na UserUpdate
    current_user: dict = Depends(get_current_admin_user),
    db: motor.motor_asyncio.AsyncIOMotorDatabase = Depends(get_db)
):
    """Aktualizuje dane pracownika (w tym etat i staż, co przelicza urlop)."""
    try:
        user_oid = ObjectId(user_id)
    except InvalidId:
        raise HTTPException(status_code=400, detail="Invalid user ID")

    user = await db.users.find_one({"_id": user_oid})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
        
    if user.get("franchise_code") != current_user.get("franchise_code"):
        raise HTTPException(status_code=403, detail="Forbidden")

    update_data = user_update.dict(exclude_unset=True)
    
    # Jeśli zmieniono parametry wpływające na urlop, przelicz go
    if "fte" in update_data or "seniority_years" in update_data or "leave_entitlement" in update_data:
        fte = update_data.get("fte", user.get("fte", 1.0))
        seniority = update_data.get("seniority_years", user.get("seniority_years", 0))
        manual_entitlement = update_data.get("leave_entitlement")
        
        if manual_entitlement is not None:
            new_vacation_days = manual_entitlement
        else:
            new_vacation_days = calculate_vacation_days(seniority, fte)
            
        update_data["vacation_days_left"] = new_vacation_days

    if "preferences" in update_data and update_data["preferences"]:
        update_data["preferences"] = user_update.preferences.dict()

    await db.users.update_one({"_id": user_oid}, {"$set": update_data})
    
    updated_user = await db.users.find_one({"_id": user_oid})
    return updated_user

@router.post("/{user_id}/l4-leave", status_code=status.HTTP_201_CREATED, response_model=schemas.ScheduleResponse)
async def add_l4_leave_endpoint(
    user_id: str,
    leave_request: schemas.L4LeaveRequest,
    current_user: dict = Depends(get_current_admin_user),
    db: motor.motor_asyncio.AsyncIOMotorDatabase = Depends(get_db)
):
    """
    Zgłasza zwolnienie L4, modyfikując istniejący OPUBLIKOWANY grafik i zwraca jego zaktualizowaną wersję.
    """
    try:
        user_object_id = ObjectId(user_id)
    except InvalidId:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Nieprawidłowy format ID użytkownika.")

    user_to_update = await crud.user_crud.get_user_by_id(db, user_object_id)
    if not user_to_update:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pracownik nie został znaleziony.")

    franchise_code = user_to_update.get("franchise_code")
    if not franchise_code:
        raise HTTPException(status_code=500, detail="Pracownik nie jest przypisany do żadnego sklepu.")

    published_schedule = await db.schedules.find_one({
        "franchise_code": franchise_code,
        "is_published": True,
        "start_date": {"$lte": datetime.combine(leave_request.startDate, time.min)},
        "end_date": {"$gte": datetime.combine(leave_request.endDate, time.max)}
    })

    if not published_schedule:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Nie znaleziono odpowiedniego opublikowanego grafiku dla podanego zakresu dat."
        )

    logger.info(f"Znaleziono opublikowany grafik o ID: {published_schedule['_id']} do modyfikacji.")

    try:
        await crud.create_leave(db, user_object_id, leave_request)
        logger.info(f"Zapisano zwolnienie L4 dla pracownika {user_id} od {leave_request.startDate} do {leave_request.endDate}.")

        schedule_in_memory = published_schedule.get("schedule", {})
        modified_shifts_count = 0
        
        current_date = leave_request.startDate
        while current_date <= leave_request.endDate:
            date_str = current_date.strftime("%Y-%m-%d")
            
            if date_str in schedule_in_memory:
                shifts_in_day = schedule_in_memory[date_str]
                
                for shift_name, shift_details in shifts_in_day.items():
                    employees_list = []
                    if isinstance(shift_details, dict):
                        employees_list = shift_details.get("employees", [])
                    elif isinstance(shift_details, list):
                        employees_list = shift_details
                    
                    employee_to_remove = None
                    for emp in employees_list:
                        emp_id = emp if isinstance(emp, str) else emp.get("id")
                        if emp_id == user_id:
                            employee_to_remove = emp
                            break
                    
                    if employee_to_remove:
                        employees_list.remove(employee_to_remove)
                        modified_shifts_count += 1
                        logger.info(f"Usunięto pracownika {user_id} ze zmiany {shift_name} w dniu {date_str}.")

                        action, new_user_id, _ = await find_best_solution_for_shift(
                            db=db,
                            franchise_code=franchise_code,
                            target_date=current_date,
                            shift_name=shift_name,
                            excluded_employee_id=user_object_id
                        )

                        if action in ("ASSIGN", "REASSIGN") and new_user_id:
                            new_employee = await db.users.find_one({"_id": new_user_id})
                            if new_employee:
                                employees_list.append({
                                    "id": str(new_user_id),
                                    "first_name": new_employee["first_name"],
                                    "last_name": new_employee["last_name"]
                                })
                                logger.info(f"Dodano zastępstwo: {new_employee['first_name']} (ID: {new_user_id}) za zmianę {shift_name} w dniu {date_str}.")
                    
                    schedule_in_memory[date_str][shift_name] = {"employees": employees_list}

            current_date += timedelta(days=1)

        if modified_shifts_count == 0:
            raise ValueError("Nie znaleziono żadnych zmian do modyfikacji dla podanego pracownika i zakresu dat.")

        logger.info(f"Przetworzono L4. Zmodyfikowano {modified_shifts_count} zmian w opublikowanym grafiku.")

        await db.schedules.update_one(
            {"_id": published_schedule["_id"]},
            {"$set": {"schedule": schedule_in_memory}}
        )

        # Poprawka: Zapewnienie zgodności z Pydantic ScheduleResponse
        response_data = {
            **published_schedule,
            "id": str(published_schedule["_id"]),
            "schedule": schedule_in_memory,
            "status": published_schedule.get("status", "published"),
            "created_at": published_schedule.get("created_at", datetime.utcnow())
        }
        
        return schemas.ScheduleResponse(**response_data)

    except ValueError as ve:
        logger.warning(f"Błąd walidacji podczas zgłaszania L4 dla {user_id}: {ve}")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(ve))
    except Exception as e:
        logger.error(f"Nieoczekiwany błąd podczas zgłaszania L4 dla użytkownika {user_id}: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Wystąpił wewnętrzny błąd serwera.")


@router.put("/me/preferences", response_model=schemas.UserPreferences)
async def update_my_preferences(
    preferences_data: schemas.UserPreferences,
    current_user: dict = Depends(get_current_active_user),
    db: motor.motor_asyncio.AsyncIOMotorDatabase = Depends(get_db)
):
    """Pozwala zalogowanemu użytkownikowi na aktualizację swoich preferencji pracy."""
    # Używamy by_alias=True, aby zapisać w bazie używając aliasów (frontend format)?
    # NIE! W bazie chcemy trzymać wewnętrzne nazwy (work_scope), a frontend dostaje aliasy.
    # schemas.UserPreferences ma populate_by_name=True.
    # preferences_data.dict() zwróci nazwy pól modelu (work_scope).
    # Jeśli chcemy zapisać to co przyszło z frontu (aliasy), to dict(by_alias=True).
    # Ale backend używa work_scope w logice. Więc zapisujemy work_scope.
    
    result = await db.users.update_one(
        {"_id": current_user["_id"]},
        {"$set": {"preferences": preferences_data.dict()}}
    )

    if result.matched_count == 0:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Nie znaleziono użytkownika.")

    logger.info(f"Preferencje użytkownika {current_user.get('email') or current_user.get('username')} zostały zaktualizowane.")
    return preferences_data

# NOWY ENDPOINT: Akceptacja nowych zgód
@router.post("/me/agreements/accept", response_model=schemas.MessageResponse)
async def accept_new_agreements(
    request: Request,
    agreements_data: Optional[schemas.AcceptAgreementsRequest] = None, # Body jest teraz opcjonalne
    current_user: dict = Depends(get_current_active_user),
    db: motor.motor_asyncio.AsyncIOMotorDatabase = Depends(get_db)
):
    """Pozwala użytkownikowi na zaakceptowanie nowych wersji EULA/RODO."""
    
    # Ustalanie wersji do zaakceptowania (albo z body, albo domyślne aktualne)
    eula_ver = agreements_data.eula_version if agreements_data and agreements_data.eula_version else settings.EULA_VERSION
    rodo_ver = agreements_data.rodo_version if agreements_data and agreements_data.rodo_version else settings.RODO_VERSION

    # Walidacja czy wersje są aktualne (nie pozwalamy zaakceptować starej)
    if eula_ver != settings.EULA_VERSION or rodo_ver != settings.RODO_VERSION:
        raise HTTPException(status_code=400, detail="Wersja regulaminu jest nieaktualna. Odśwież stronę.")

    now = datetime.utcnow()
    ip_address = request.client.host
    
    update_data = {
        "agreements.eula.acceptedAt": now,
        "agreements.eula.version": eula_ver,
        "agreements.rodo.acceptedAt": now,
        "agreements.rodo.version": rodo_ver,
        "agreements.ipAddress": ip_address
    }

    result = await db.users.update_one(
        {"_id": current_user["_id"]},
        {"$set": update_data}
    )

    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Nie znaleziono użytkownika.")

    logger.info(f"Użytkownik {current_user.get('email')} zaakceptował nowe zgody (v{eula_ver}).")
    return {"message": "Zgody zostały zaktualizowane."}


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
    try:
        # Logowanie surowych danych z bazy, aby zobaczyć co tam siedzi
        logger.info(f"Pobieranie profilu dla: {current_user.get('email')}")
        logger.debug(f"Surowe dane preferencji: {current_user.get('preferences')}")
        
        # Próba walidacji modelu przed zwróceniem
        user_response = schemas.UserResponse(**current_user)
        return user_response
    except ValidationError as ve:
        logger.error(f"Błąd walidacji profilu użytkownika: {ve}")
        raise HTTPException(status_code=500, detail=f"Błąd integralności danych profilu: {ve}")
    except Exception as e:
        logger.error(f"Nieoczekiwany błąd przy pobieraniu profilu: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Wystąpił błąd podczas pobierania profilu.")


@router.get("/{user_id}", response_model=schemas.UserResponse)
async def get_user(
    user_id: str,
    current_user: dict = Depends(get_current_active_user),
    db: motor.motor_asyncio.AsyncIOMotorDatabase = Depends(get_db)
):
    """ Pobierz szczegóły użytkownika """
    try:
        user_to_get_id = ObjectId(user_id)
        user_to_get = await db.users.find_one({"_id": user_to_get_id})

        if not user_to_get:
            raise HTTPException(status_code=404, detail="Użytkownik nie znaleziony")

        # Sprawdzenie, czy użytkownicy należą do tej samej franczyzy
        is_same_franchise = current_user.get("franchise_code") == user_to_get.get("franchise_code")
        is_admin_or_franchisee = current_user["role"] in [models.UserRole.ADMIN.value, models.UserRole.FRANCHISEE.value]
        is_getting_own_data = str(current_user["_id"]) == user_id

        # Zezwól, jeśli admin/franczyzobiorca LUB użytkownicy są z tej samej franczyzy
        if not (is_admin_or_franchisee or is_same_franchise):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Brak uprawnień do wyświetlenia danych tego użytkownika.")

        # Jeśli zwykły pracownik pyta o kogoś innego, zwróć tylko podstawowe dane
        if not (is_admin_or_franchisee or is_getting_own_data):
            return schemas.UserResponse(
                id=user_to_get["_id"],
                first_name=user_to_get["first_name"],
                last_name=user_to_get["last_name"],
                # Uzupełnij pozostałe wymagane pola z domyślnymi lub pustymi wartościami
                email="hidden@example.com",
                role=user_to_get["role"],
                status=user_to_get["status"],
                email_verified=False,
                vacation_days_left=0,
                created_at=datetime.min,
            )

        return user_to_get

    except HTTPException as he:
        raise he
    except Exception as e:
        logger.error(f"Błąd podczas pobierania użytkownika {user_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Wewnętrzny błąd serwera podczas pobierania użytkownika: {e}")

@router.post("/me/fcm-token", response_model=schemas.MessageResponse)
async def register_fcm_token(
    token_data: schemas.FCMTokenRequest,
    current_user: dict = Depends(get_current_active_user),
    db: motor.motor_asyncio.AsyncIOMotorDatabase = Depends(get_db)
):
    """Rejestruje token FCM dla powiadomień push."""
    token = token_data.token
    
    # Dodaj token do listy (jeśli go nie ma)
    await db.users.update_one(
        {"_id": current_user["_id"]},
        {"$addToSet": {"fcm_tokens": token}}
    )
    
    return {"message": "Token FCM zarejestrowany."}
