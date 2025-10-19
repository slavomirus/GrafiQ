# OSTATECZNA WERSJA: Ujednolicono generowanie tokenów na podstawie ID użytkownika.

from fastapi import APIRouter, HTTPException, status, Depends
from motor.motor_asyncio import AsyncIOMotorClient
from datetime import datetime, timedelta
import logging
import random

from ..database import get_db
from .. import models, schemas, security
from ..email_service import send_verification_code_email, send_welcome_email

router = APIRouter()
logger = logging.getLogger(__name__)

def generate_verification_code(length: int = 6) -> str:
    """Generuje 6-cyfrowy kod weryfikacyjny jako string."""
    return "".join([str(random.randint(0, 9)) for _ in range(length)])


@router.post("/verify-email-code", response_model=schemas.Token)
async def verify_email_with_code(
        verification_data: schemas.VerificationCodeSchema,
        db: AsyncIOMotorClient = Depends(get_db)
):
    """
    Weryfikuje konto użytkownika i zwraca token oparty na ID użytkownika.
    """
    try:
        user = await db.users.find_one({"email": verification_data.email})

        if not user:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Użytkownik nie istnieje.")

        if user.get("status") == models.UserStatus.ACTIVE.value:
            access_token = security.create_access_token(data={"sub": str(user["_id"])})
            return {"access_token": access_token, "token_type": "bearer"}

        expires_at = user.get("verification_code_expires_at")
        if not expires_at or expires_at < datetime.utcnow():
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Kod weryfikacyjny wygasł.")

        if user.get("verification_code") != verification_data.code:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Nieprawidłowy kod weryfikacyjny.")

        # Aktualizacja statusu użytkownika
        await db.users.update_one(
            {"_id": user["_id"]},
            {
                "$set": {"status": models.UserStatus.ACTIVE.value, "email_verified": True},
                "$unset": {"verification_code": "", "verification_code_expires_at": ""}
            }
        )
        
        try:
            await send_welcome_email(email=user["email"], first_name=user["first_name"])
        except Exception as e:
            logger.error(f"Krytyczny błąd konfiguracji e-mail: Nie udało się wysłać e-maila powitalnego do {user['email']}. Powód: {e}")

        # POPRAWKA: Generowanie tokenu na podstawie ID użytkownika
        access_token = security.create_access_token(data={"sub": str(user["_id"])})
        logger.info(f"Konto pomyślnie zweryfikowane dla: {verification_data.email}")
        return {"access_token": access_token, "token_type": "bearer"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Błąd podczas weryfikacji kodu dla {verification_data.email}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Wewnętrzny błąd serwera.")


@router.post("/resend-verification-code", response_model=schemas.MessageResponse)
async def resend_verification_code(
        email_data: schemas.EmailRequest,
        db: AsyncIOMotorClient = Depends(get_db)
):
    """
    Generuje i wysyła nowy kod weryfikacyjny dla użytkownika.
    """
    try:
        user = await db.users.find_one({"email": email_data.email})

        if not user:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Użytkownik nie istnieje.")

        if user.get("status") == models.UserStatus.ACTIVE.value:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="To konto jest już aktywne.")

        new_code = generate_verification_code()
        new_expires_at = datetime.utcnow() + timedelta(minutes=15)

        await db.users.update_one(
            {"_id": user["_id"]},
            {"$set": {"verification_code": new_code, "verification_code_expires_at": new_expires_at}}
        )

        await send_verification_code_email(
            email=user["email"],
            first_name=user["first_name"],
            verification_code=new_code
        )

        logger.info(f"Nowy kod weryfikacyjny wysłany do: {email_data.email}")
        return {"message": "Nowy kod weryfikacyjny został wysłany na Twój adres e-mail."}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Błąd podczas ponownego wysyłania kodu dla {email_data.email}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Wewnętrzny błąd serwera.")
