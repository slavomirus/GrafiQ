# Plik: backend/app/dependencies.py

import logging
from fastapi import Depends, HTTPException, status, Header, Query
from fastapi.security import OAuth2PasswordBearer
from jwt import PyJWTError
from bson import ObjectId
import motor.motor_asyncio
from typing import Optional
from datetime import datetime

from .database import get_db
from . import models, security

logger = logging.getLogger(__name__)
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/token")

async def get_current_user(
    token: str = Depends(oauth2_scheme),
    x_franchise_code: Optional[str] = Header(None, alias="X-Franchise-Code"),
    db: motor.motor_asyncio.AsyncIOMotorClient = Depends(get_db)
):
    """
    Dekoduje token JWT, znajduje użytkownika i ustawia kontekst sklepu.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = security.decode_token(token)
        if payload is None:
            logger.warning("Authentication failed: Token decoding resulted in None payload.")
            raise credentials_exception

        user_id: str = payload.get("sub")
        if user_id is None:
            logger.warning("Authentication failed: Token does not contain a 'sub' (subject) claim.")
            raise credentials_exception

        user = await db.users.find_one({"_id": ObjectId(user_id)})
        if user is None:
            logger.warning(f"Authentication failed: User with ID '{user_id}' from token not found in database.")
            raise credentials_exception

        # --- LOGIKA MULTI-STORE (Context Switching) ---
        
        # 1. Pobierz listę dostępnych sklepów dla użytkownika
        # Jeśli user ma pole 'franchise_codes' (lista), użyj go.
        # Jeśli nie, użyj pojedynczego 'franchise_code' jako listy jednoelementowej.
        # POPRAWKA: Sanityzacja danych z bazy (strip)
        raw_codes = user.get("franchise_codes", [])
        available_franchises = [code.strip() for code in raw_codes if isinstance(code, str)]
        
        if not available_franchises and user.get("franchise_code"):
            code = user.get("franchise_code")
            if isinstance(code, str):
                available_franchises = [code.strip()]
            
        # 2. Ustal aktywny sklep
        active_franchise = None
        
        if x_franchise_code:
            # Sanityzacja nagłówka
            requested_code = x_franchise_code.strip()
            
            # Użytkownik prosi o konkretny sklep
            if requested_code in available_franchises:
                active_franchise = requested_code
            else:
                # POPRAWKA: Auto-korekta dla franczyzobiorców
                # Jeśli użytkownik prosi o sklep, do którego nie ma dostępu, ale ma dostęp do INNEGO sklepu,
                # to zamiast rzucać błędem 403, przekieruj go do jego domyślnego sklepu.
                # Jest to przydatne, gdy frontend ma zapamiętany stary kod sklepu (np. z poprzedniej sesji).
                
                logger.warning(f"Security/Fallback: User {user_id} requested {requested_code} but has access only to {available_franchises}. Fallback to default.")
                
                if available_franchises:
                    active_franchise = available_franchises[0]
                else:
                    # Jeśli nie ma dostępu do żadnego sklepu, to wtedy rzuć błąd
                    raise HTTPException(status_code=403, detail="Brak dostępu do wybranego sklepu.")
        else:
            # Brak nagłówka - użyj domyślnego (pierwszego lub jedynego)
            if available_franchises:
                active_franchise = available_franchises[0]
            else:
                # User bez sklepu (np. nowy admin globalny?)
                active_franchise = None

        # 3. Wstrzyknij aktywny sklep do obiektu użytkownika
        # Dzięki temu reszta aplikacji "myśli", że user jest przypisany tylko do tego sklepu
        if active_franchise:
            user["franchise_code"] = active_franchise
            
        # ----------------------------------------------

    except (PyJWTError, ValueError) as e:
        logger.warning(f"Authentication failed: Could not decode or validate token. Error: {e}", exc_info=True)
        raise credentials_exception
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"An unexpected error occurred during user authentication: {e}", exc_info=True)
        raise credentials_exception

    return user

async def get_current_active_user(
    current_user: dict = Depends(get_current_user),
    db: motor.motor_asyncio.AsyncIOMotorClient = Depends(get_db)
):
    """Sprawdza, czy użytkownik jest aktywny lub musi zmienić hasło oraz czy ma ważną subskrypcję."""
    if current_user.get("status") not in [models.UserStatus.ACTIVE.value, "needs_password_change"]:
        raise HTTPException(status_code=403, detail="Inactive user")

    # --- Blokada Dostępu (Paywall) ---
    role = current_user.get("role")
    franchise_code = current_user.get("franchise_code")

    if role in [models.UserRole.FRANCHISEE.value, models.UserRole.EMPLOYEE.value] and franchise_code:
        # Znajdź właściciela (franczyzobiorcę) tego sklepu
        # Właściciel zazwyczaj ma franchise_codes zawierające ten kod i rolę FRANCHISEE
        franchisee = None
        if role == models.UserRole.FRANCHISEE.value:
            franchisee = current_user
        else:
            franchisee = await db.users.find_one({
                "role": models.UserRole.FRANCHISEE.value,
                "$or": [
                    {"franchise_code": franchise_code},
                    {"franchise_codes": franchise_code}
                ]
            })

        if franchisee:
            free_access_until = franchisee.get("free_access_until")
            is_subscription_active = franchisee.get("is_subscription_active", False)

            now = datetime.utcnow()
            has_free_access = free_access_until and free_access_until > now
            
            is_access_valid = True #has_free_access or is_subscription_active

            if not is_access_valid:
                if role == models.UserRole.FRANCHISEE.value:
                    raise HTTPException(
                        status_code=status.HTTP_402_PAYMENT_REQUIRED,
                        detail="subscription_expired"
                    )
                else:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="franchise_subscription_expired"
                    )

    return current_user


async def get_current_admin_user(current_user: dict = Depends(get_current_active_user)):
    """Sprawdza, czy aktywny użytkownik ma uprawnienia administracyjne."""
    if current_user.get("role") not in [models.UserRole.ADMIN.value, models.UserRole.FRANCHISEE.value]:
        raise HTTPException(status_code=403, detail="Not enough permissions")
    return current_user

# --- NOWA ZALEŻNOŚĆ DLA PDF (Query Param Token) ---
async def get_current_admin_user_query(
    token: Optional[str] = Query(None),
    authorization: Optional[str] = Header(None),
    x_franchise_code: Optional[str] = Header(None, alias="X-Franchise-Code"),
    db: motor.motor_asyncio.AsyncIOMotorClient = Depends(get_db)
):
    """
    Alternatywna metoda autentykacji akceptująca token w parametrze URL (dla pobierania plików).
    Priorytet: Header > Query Param.
    """
    token_to_use = None
    
    if authorization and authorization.startswith("Bearer "):
        token_to_use = authorization.split(" ")[1]
    elif token:
        token_to_use = token
        
    if not token_to_use:
         raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
        
    # 1. Pobierz usera (dekodowanie tokenu)
    user = await get_current_user(token=token_to_use, x_franchise_code=x_franchise_code, db=db)
    
    # 2. Sprawdź czy aktywny (manualne wywołanie, bo Depends nie działa przy bezpośrednim wywołaniu funkcji)
    user = await get_current_active_user(current_user=user, db=db)
    
    # 3. Sprawdź czy admin
    return await get_current_admin_user(current_user=user)
