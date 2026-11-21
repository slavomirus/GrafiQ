# OSTATECZNA WERSJA: Dodano zapis domyślnych preferencji przy rejestracji franczyzobiorcy.

import logging
from fastapi import APIRouter, Depends, HTTPException, status, Request
from starlette.responses import JSONResponse
from datetime import datetime, timedelta
import motor.motor_asyncio
import secrets

from ..database import get_db
from .. import models, schemas, security
from ..email_service import send_verification_code_email, send_otp_email
from ..dependencies import get_current_user
from ..config import settings
from ..services.audit_service import log_audit_event, extract_client_ip

router = APIRouter()
logger = logging.getLogger(__name__)

# Ochrona przed Brute-Force per konto (nie per IP proxy), 5 prób -> blokada na 15 minut
MAX_FAILED_LOGINS = 5
LOCKOUT_MINUTES = 15
_LOGIN_FAILURES: dict = {}


def _check_account_lockout(identifier: str):
    key = identifier.strip().lower()
    rec = _LOGIN_FAILURES.get(key)
    if not rec:
        return
    locked_until = rec.get("locked_until")
    if locked_until and datetime.utcnow() < locked_until:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Zbyt wiele nieudanych prób logowania. Konto tymczasowo zablokowane na 15 minut."
        )
    if locked_until and datetime.utcnow() >= locked_until:
        _LOGIN_FAILURES.pop(key, None)


def _record_failed_login(identifier: str) -> bool:
    key = identifier.strip().lower()
    rec = _LOGIN_FAILURES.get(key, {"count": 0, "locked_until": None})
    rec["count"] += 1
    locked_now = False
    if rec["count"] >= MAX_FAILED_LOGINS:
        rec["locked_until"] = datetime.utcnow() + timedelta(minutes=LOCKOUT_MINUTES)
        locked_now = True
    _LOGIN_FAILURES[key] = rec
    return locked_now


def _clear_failed_logins(identifier: str):
    _LOGIN_FAILURES.pop(identifier.strip().lower(), None)


@router.post("/register/franchisee", response_model=schemas.RegistrationResponse)
async def register_franchisee(user: schemas.UserCreateFranchisee,
                              request: Request,
                              db: motor.motor_asyncio.AsyncIOMotorClient = Depends(get_db)):
    # Walidacja zgód (EULA, RODO, DPA)
    if not user.acceptEula or not user.acceptRodo or not user.acceptDpa:
        raise HTTPException(status_code=400, detail="Akceptacja EULA, RODO oraz umowy powierzenia (DPA) jest wymagana do założenia konta")

    # Sanityzacja kodu franczyzy (usuwanie spacji)
    clean_franchise_code = user.franchise_code.strip()

    query_conditions = [{"email": user.email}]
    if user.phone:
        query_conditions.append({"phone": user.phone})
        
    existing_user = await db.users.find_one({"$or": query_conditions})
    if existing_user:
        # Jeśli użytkownik istnieje, ale jest w fazie PENDING i nie zweryfikował maila,
        # uznajemy to za nieudaną wcześniejszą rejestrację i usuwamy stare konto.
        if existing_user.get("status") == models.UserStatus.PENDING.value and not existing_user.get("email_verified"):
            logger.info(f"Wykryto niezweryfikowane konto PENDING dla {user.email} lub {user.phone}. Usuwanie i ponowna rejestracja.")
            
            # Sprawdź czy istnieje sklep powiązany z tym niezweryfikowanym użytkownikiem
            existing_store = await db.stores.find_one({"franchise_code": clean_franchise_code})
            if existing_store and existing_store.get("owner_id") == existing_user["_id"]:
                await db.stores.delete_one({"_id": existing_store["_id"]})
                logger.info(f"Usunięto osierocony sklep dla kodu: {clean_franchise_code}")
            
            await db.users.delete_one({"_id": existing_user["_id"]})
        else:
            if existing_user.get("email") == user.email:
                raise HTTPException(status_code=400, detail="Użytkownik o podanym adresie e-mail już istnieje")
            if existing_user.get("phone") == user.phone:
                raise HTTPException(status_code=400, detail="Użytkownik o podanym numerze telefonu już istnieje")

    hashed_password = security.get_password_hash(user.password.get_secret_value())
    verification_code = ''.join(secrets.choice('0123456789') for _ in range(6))

    store_location = f"{user.city}, {user.street} {user.building_number}"
    
    # Przygotowanie struktury zgód
    now = datetime.utcnow()
    agreements_data = {
        "eula": {"acceptedAt": now, "version": settings.EULA_VERSION},
        "rodo": {"acceptedAt": now, "version": settings.RODO_VERSION},
        "dpa": {"acceptedAt": now, "version": settings.DPA_VERSION},
        "ipAddress": extract_client_ip(request)
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

    # Sprawdzenie kodu polecającego przed dodaniem usera
    referral_code_doc = None
    if user.referral_code:
        code_str = user.referral_code.strip()
        referral_code_doc = await db.referral_codes.find_one({"code": code_str})
        if not referral_code_doc:
            raise HTTPException(status_code=400, detail="Nieprawidłowy kod polecający.")
        if referral_code_doc.get("uses_left", 0) <= 0:
            raise HTTPException(status_code=400, detail="Ten kod osiągnął maksymalną liczbę użyć.")
            
        # Zastosuj bonus dla nowego usera od razu
        new_user_data["free_access_until"] = now + timedelta(days=90)
        new_user_data["used_referral_code"] = code_str

    result = await db.users.insert_one(new_user_data)
    user_id = str(result.inserted_id)
    
    # Aktualizacja właściciela kodu i samego kodu
    if referral_code_doc:
        owner = await db.users.find_one({"_id": referral_code_doc["owner_id"]})
        if owner:
            owner_free_until = owner.get("free_access_until")
            if not owner_free_until or owner_free_until < now:
                new_owner_date = now + timedelta(days=30)
            else:
                new_owner_date = owner_free_until + timedelta(days=30)
                
            await db.users.update_one(
                {"_id": owner["_id"]},
                {"$set": {"free_access_until": new_owner_date}}
            )
            
        await db.referral_codes.update_one(
            {"_id": referral_code_doc["_id"]},
            {
                "$inc": {"uses_left": -1},
                "$push": {"used_by": result.inserted_id}
            }
        )
    
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
                                 request: Request = None,
                                 db: motor.motor_asyncio.AsyncIOMotorClient = Depends(get_db)):
    login_identifier = login_data.email
    _check_account_lockout(login_identifier)
    
    if "@" in login_identifier:
        user = await db.users.find_one({"email": login_identifier})
    else:
        user = await db.users.find_one({"username": login_identifier})

    if not user or not security.verify_password(login_data.password, user.get("hashed_password", "")):
        locked_now = _record_failed_login(login_identifier)
        await log_audit_event(
            db,
            action="LOGIN_LOCKED" if locked_now else "LOGIN_FAILED",
            user_id=str(user["_id"]) if user else None,
            franchise_code=user.get("franchise_code") if user else None,
            ip_address=extract_client_ip(request),
            details={"identifier": login_identifier}
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Niepoprawny e-mail lub hasło.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    _clear_failed_logins(login_identifier)

    # Sprawdzanie wersji EULA
    requires_agreement_update = False
    if user.get("role") == models.UserRole.FRANCHISEE.value:
        user_eula_version = user.get("agreements", {}).get("eula", {}).get("version")
        if user_eula_version != settings.EULA_VERSION:
            requires_agreement_update = True

    token_version = int(user.get("token_version", 0))
    access_token = security.create_access_token(data={"sub": str(user["_id"]), "tv": token_version})

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
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"User account is not active. Current status: {user.get('status')}",
        )

    refresh_token = security.create_refresh_token(data={"sub": str(user["_id"]), "tv": token_version})
    refresh_payload = security.decode_token(refresh_token) or {}
    await db.users.update_one(
        {"_id": user["_id"]},
        {"$set": {"active_refresh_jti": refresh_payload.get("jti")}}
    )

    return {
        "access_token": access_token, 
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "requires_agreement_update": requires_agreement_update
    }


@router.post("/refresh", response_model=schemas.Token)
async def refresh_access_token(
    refresh_req: schemas.RefreshTokenRequest,
    request: Request = None,
    db: motor.motor_asyncio.AsyncIOMotorClient = Depends(get_db)
):
    """Rotacja Refresh Tokena i wystawienie nowego Access Tokena."""
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Nieprawidłowy lub wygasły token odświeżania.",
        headers={"WWW-Authenticate": "Bearer"},
    )
    payload = security.decode_token(refresh_req.refresh_token)
    if not payload or payload.get("type") != "refresh":
        raise credentials_exception

    user_id = payload.get("sub")
    if not user_id:
        raise credentials_exception

    from bson import ObjectId
    user = await db.users.find_one({"_id": ObjectId(user_id)})
    if not user or user.get("status") != models.UserStatus.ACTIVE.value:
        raise credentials_exception

    if int(payload.get("tv", 0)) != int(user.get("token_version", 0)):
        raise credentials_exception

    if user.get("active_refresh_jti") and payload.get("jti") != user.get("active_refresh_jti"):
        # Wykryto próbę ponownego użycia zrotowanego refresh tokena -> unieważnij wszystkie sesje
        await db.users.update_one(
            {"_id": user["_id"]},
            {"$inc": {"token_version": 1}, "$unset": {"active_refresh_jti": ""}}
        )
        await log_audit_event(
            db,
            action="REFRESH_TOKEN_REUSE_DETECTED",
            user_id=user_id,
            franchise_code=user.get("franchise_code"),
            ip_address=extract_client_ip(request),
        )
        raise credentials_exception

    token_version = int(user.get("token_version", 0))
    new_access = security.create_access_token(data={"sub": user_id, "tv": token_version})
    new_refresh = security.create_refresh_token(data={"sub": user_id, "tv": token_version})
    new_refresh_payload = security.decode_token(new_refresh) or {}
    await db.users.update_one(
        {"_id": user["_id"]},
        {"$set": {"active_refresh_jti": new_refresh_payload.get("jti")}}
    )
    return {
        "access_token": new_access,
        "refresh_token": new_refresh,
        "token_type": "bearer",
    }


@router.post("/revoke-sessions", response_model=schemas.MessageResponse)
async def revoke_all_sessions(
    request: Request = None,
    current_user: dict = Depends(get_current_user),
    db: motor.motor_asyncio.AsyncIOMotorClient = Depends(get_db)
):
    """Zdalne unieważnienie wszystkich aktywnych sesji użytkownika."""
    await db.users.update_one(
        {"_id": current_user["_id"]},
        {"$inc": {"token_version": 1}, "$unset": {"active_refresh_jti": ""}}
    )
    await log_audit_event(
        db,
        action="SESSIONS_REVOKED",
        user_id=str(current_user["_id"]),
        franchise_code=current_user.get("franchise_code"),
        ip_address=extract_client_ip(request),
    )
    return {"message": "Wszystkie aktywne sesje zostały unieważnione."}


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


@router.post("/forgot-password", response_model=schemas.ForgotPasswordResponse)
async def request_password_reset(
    request: Request,
    request_data: schemas.ForgotPasswordRequest,
    db: motor.motor_asyncio.AsyncIOMotorClient = Depends(get_db)
):
    """Generuje jednorazowe hasło OTP i wysyła na e-mail użytkownika. Wymusza zmianę hasła po zalogowaniu."""
    email = request_data.email
    user = await db.users.find_one({"email": email})
    
    if not user:
        # Zwracamy zawsze sukces, aby nie ujawniać czy e-mail istnieje w bazie
        return {"message": "Jeśli podany adres e-mail istnieje w naszej bazie, wysłano na niego nowe hasło tymczasowe."}
        
    otp = ''.join(secrets.choice('0123456789') for _ in range(6))
    hashed_otp = security.get_password_hash(otp)
    
    # Zapisujemy zahashowany OTP i czas wygaśnięcia (15 minut)
    await db.users.update_one(
        {"_id": user["_id"]},
        {"$set": {
            "password_reset_otp": hashed_otp,
            "password_reset_otp_expires_at": datetime.utcnow() + timedelta(minutes=15),
            "status": models.UserStatus.NEEDS_PASSWORD_CHANGE.value
        }}
    )
    
    # Wysyłanie e-maila w tle / bez blokowania, ewentualnie dodaj background task
    await send_otp_email(email, user.get("first_name", "Użytkowniku"), otp)
    
    return {"message": "Jeśli podany adres e-mail istnieje w naszej bazie, wysłano na niego nowe hasło tymczasowe."}


@router.post("/reset-password", response_model=schemas.MessageResponse)
async def reset_password_with_otp(
    request_data: schemas.ResetPasswordRequest,
    db: motor.motor_asyncio.AsyncIOMotorClient = Depends(get_db)
):
    """
    Weryfikuje kod OTP i ustawia nowe hasło — bez konieczności logowania tymczasowym hasłem.
    Flow: ForgotPassword (email) → ten endpoint (email + otp + nowe_hasło).
    """
    user = await db.users.find_one({"email": request_data.email})

    if not user:
        raise HTTPException(status_code=400, detail="Nieprawidłowy kod lub adres e-mail.")

    stored_otp = user.get("password_reset_otp")
    otp_expires = user.get("password_reset_otp_expires_at")

    if not stored_otp or not otp_expires:
        raise HTTPException(status_code=400, detail="Nie znaleziono aktywnego kodu resetowania hasła. Wyślij kod ponownie.")

    if datetime.utcnow() > otp_expires:
        raise HTTPException(status_code=400, detail="Kod resetowania hasła wygasł. Wyślij kod ponownie.")

    if not security.verify_password(request_data.otp_code, stored_otp):
        raise HTTPException(status_code=400, detail="Nieprawidłowy kod weryfikacyjny.")

    new_hashed_password = security.get_password_hash(request_data.new_password.get_secret_value())

    await db.users.update_one(
        {"_id": user["_id"]},
        {"$set": {
            "hashed_password": new_hashed_password,
            "status": models.UserStatus.ACTIVE.value,
        },
        "$unset": {
            "password_reset_otp": "",
            "password_reset_otp_expires_at": ""
        }}
    )

    return {"message": "Hasło zostało pomyślnie zmienione. Możesz teraz się zalogować."}

