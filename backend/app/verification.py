# backend/app/verification.py
import random
import logging
from datetime import datetime, timedelta
from bson import ObjectId

# Konfiguracja logowania
logger = logging.getLogger(__name__)


def generate_verification_code(length=6):
    """Generuje 6-cyfrowy kod weryfikacyjny"""
    return ''.join([str(random.randint(0, 9)) for _ in range(length)])


async def create_verification_code(db, email: str, code_length=6):
    """Tworzy i zapisuje kod weryfikacyjny w bazie danych"""
    try:
        # Wygeneruj kod
        code = generate_verification_code(code_length)
        expires_at = datetime.utcnow() + timedelta(minutes=15)

        # Przygotuj dane do zapisania
        verification_data = {
            "email": email,
            "code": code,
            "expires_at": expires_at,
            "created_at": datetime.utcnow(),
            "used": False
        }

        # Zapisz w kolekcji verification_codes
        result = await db.verification_codes.insert_one(verification_data)

        logger.info(f"Kod weryfikacyjny utworzony dla: {email}, ID: {result.inserted_id}")
        return code

    except Exception as e:
        logger.error(f"Błąd tworzenia kodu weryfikacyjnego dla {email}: {e}")
        return None


async def verify_code(db, email: str, code: str):
    """Weryfikuje kod weryfikacyjny"""
    try:
        # Znajdź najnowszy, nieużyty kod dla danego emaila
        verification = await db.verification_codes.find_one(
            {
                "email": email,
                "code": code,
                "used": False,
                "expires_at": {"$gt": datetime.utcnow()}
            },
            sort=[("created_at", -1)]  # Najnowszy pierwszy
        )

        if not verification:
            # Sprawdź dlaczego nie znaleziono
            expired_code = await db.verification_codes.find_one(
                {"email": email, "code": code, "used": False}
            )

            if expired_code:
                return False, "Kod wygasł. Wygeneruj nowy kod."
            else:
                return False, "Nieprawidłowy kod weryfikacyjny."

        # Oznacz kod jako użyty
        await db.verification_codes.update_one(
            {"_id": verification["_id"]},
            {"$set": {"used": True, "used_at": datetime.utcnow()}}
        )

        logger.info(f"Kod zweryfikowany pomyślnie dla: {email}")
        return True, "Kod poprawny. Konto aktywowane."

    except Exception as e:
        logger.error(f"Błąd weryfikacji kodu dla {email}: {e}")
        return False, "Wystąpił błąd podczas weryfikacji."


async def cleanup_expired_codes(db):
    """Czyści wygasłe kody weryfikacyjne (można uruchamiać okresowo)"""
    try:
        result = await db.verification_codes.delete_many({
            "expires_at": {"$lt": datetime.utcnow()}
        })
        logger.info(f"Usunięto {result.deleted_count} wygasłych kodów weryfikacyjnych")
        return result.deleted_count
    except Exception as e:
        logger.error(f"Błąd czyszczenia wygasłych kodów: {e}")
        return 0