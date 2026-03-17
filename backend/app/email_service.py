from fastapi_mail import FastMail, MessageSchema, ConnectionConfig
from typing import Optional, List
import os
import logging
from pathlib import Path
from .config import settings

# Konfiguracja logowania
logger = logging.getLogger(__name__)

# Ustalanie ścieżki do folderu templates
# Pobieramy ścieżkę do katalogu, w którym znajduje się ten plik (backend/app)
BASE_DIR = Path(__file__).resolve().parent
TEMPLATE_FOLDER = BASE_DIR / "templates"

# Upewnij się, że folder istnieje, jeśli nie - użyj bieżącego katalogu jako fallback (choć to i tak rzuci błąd jeśli folderu nie ma)
if not TEMPLATE_FOLDER.exists():
    logger.warning(f"Folder szablonów nie istnieje: {TEMPLATE_FOLDER}. Próba użycia ścieżki względnej.")
    TEMPLATE_FOLDER = Path("app/templates")

# Konfiguracja email (użyj zmiennych środowiskowych)
conf = ConnectionConfig(
    MAIL_USERNAME=settings.MAIL_USERNAME,
    MAIL_PASSWORD=settings.MAIL_PASSWORD,
    MAIL_FROM=settings.MAIL_FROM,
    MAIL_PORT=settings.MAIL_PORT,
    MAIL_SERVER=settings.MAIL_SERVER,
    MAIL_FROM_NAME=settings.MAIL_FROM_NAME,
    MAIL_STARTTLS=settings.MAIL_STARTTLS,
    MAIL_SSL_TLS=settings.MAIL_SSL_TLS,
    TEMPLATE_FOLDER=str(TEMPLATE_FOLDER), # Konwersja Path na str
)

fm = FastMail(conf)

async def send_new_employee_credentials_email(email: str, first_name: str, username: str, temp_password: str):
    """Wyślij email z danymi logowania do nowego pracownika."""
    body = f"""Witaj {first_name}!

Twoje konto w systemie grafiku pracy Żabka zostało utworzone.

Twoje dane do pierwszego logowania:
Login: {username}
Hasło tymczasowe: {temp_password}

Po pierwszym zalogowaniu zostaniesz poproszony/a o ustawienie nowego, własnego hasła.

Link do logowania: {os.getenv('FRONTEND_URL', 'http://localhost:3000')}/login

Pozdrawiamy,
Zespół Żabka Grafiki
"""

    message = MessageSchema(
        subject="Twoje konto w systemie Żabka Grafiki zostało utworzone!",
        recipients=[email],
        body=body,
        subtype="plain"
    )

    try:
        await fm.send_message(message)
        logger.info(f"✅ Email z danymi logowania wysłany do nowego pracownika: {email}")
    except Exception as e:
        logger.error(f"❌ Błąd wysyłania emaila z danymi logowania do {email}: {e}")
        raise

async def send_verification_code_email(email: str, first_name: str, verification_code: str):
    """
    Wyślij email z 6-cyfrowym kodem weryfikacyjnym.
    W przypadku błędu zgłasza wyjątek.
    """
    body = f"""Witaj {first_name}!

Dziękujemy za rejestrację w systemie grafiku pracy Żabka.

Twój kod weryfikacyjny: 
🔐 **{verification_code}**

Kod jest ważny przez 15 minut.

Aby aktywować konto, wprowadź ten kod na stronie weryfikacji:
{os.getenv('FRONTEND_URL', 'http://localhost:3000')}/verify-email

Jeśli to nie Ty zakładałeś konto, zignoruj tę wiadomość.

Pozdrawiamy,
Zespół Żabka Grafiki
"""

    message = MessageSchema(
        subject="Kod weryfikacyjny - Aktywacja konta Żabka Grafiki",
        recipients=[email],
        body=body,
        subtype="plain"
    )

    try:
        await fm.send_message(message)
        logger.info(f"✅ Email z kodem weryfikacyjnym wysłany do: {email}")
    except Exception as e:
        logger.error(f"❌ Błąd wysyłania emaila do {email}: {e}")
        raise

async def send_welcome_email(email: str, first_name: str, username: Optional[str] = None,
                             password: Optional[str] = None, user_id: Optional[str] = None):
    """
    Wyślij email powitalny
    """
    if username and password:
        # Email z danymi logowania dla pracownika
        body = f"""
        Witaj {first_name}!

        Twoje konto w systemie grafiku pracy Żabka zostało utworzone.

        Dane logowania:
        Login: {username}
        Hasło: {password}

        Zalecamy zmianę hasła po pierwszym logowaniu.

        Link do logowania: {os.getenv('FRONTEND_URL', 'http://localhost:3000')}/login

        Pozdrawiamy,
        Zespół Żabka Grafiki
        """
    else:
        # Email powitalny dla franczyzobiorcy
        body = f"""
        Witaj {first_name}!

        Twoje konto w systemie grafiku pracy Żabka zostało aktywowane.

        Możesz się teraz zalogować używając swojego adresu email i hasła.

        Link do logowania: {os.getenv('FRONTEND_URL', 'http://localhost:3000')}/login

        Pozdrawiamy,
        Zespół Żabka Grafiki
        """

    message = MessageSchema(
        subject="Witamy w systemie Żabka Grafiki!",
        recipients=[email],
        body=body,
        subtype="plain"
    )

    try:
        await fm.send_message(message)
        logger.info(f"Email powitalny wysłany do: {email}, user_id: {user_id}")

    except Exception as e:
        logger.error(f"Błąd wysyłania emaila powitalnego do {email}: {e}")
        raise

async def send_password_reset_email(email: str, token: str, user_id: str):
    """
    Wyślij email resetowania hasła
    """
    reset_url = f"{os.getenv('FRONTEND_URL', 'http://localhost:3000')}/reset-password/{token}"

    message = MessageSchema(
        subject="Resetowanie hasła - Żabka Grafiki",
        recipients=[email],
        body=f"""
        Witaj!

        Otrzymaliśmy prośbę o resetowanie hasła do Twojego konta.

        Aby zresetować hasło, kliknij w poniższy link:
        {reset_url}

        Link jest ważny przez 1 godzinę.

        Jeśli nie prosiłeś o resetowanie hasła, zignoruj tę wiadomość.

        Pozdrawiamy,
        Zespół Żabka Grafiki
        """,
        subtype="plain"
    )

    try:
        await fm.send_message(message)
        logger.info(f"Email resetowania hasła wysłany do: {email}, user_id: {user_id}")

    except Exception as e:
        logger.error(f"Błąd wysyłania emaila resetowania hasła do {email}: {e}")
        raise

async def send_vacation_status_email(email: str, first_name: str, vacation_data: dict):
    """
    Wyślij email ze statusem wniosku urlopowego
    """
    status = vacation_data.get("status", "")
    start_date = vacation_data.get("start_date", "")
    end_date = vacation_data.get("end_date", "")
    reason = vacation_data.get("reason", "")

    if status == "approved":
        subject = "Twój wniosek urlopowy został zaakceptowany"
        body = f"""
        Witaj {first_name}!

        Twój wniosek urlopowy został zaakceptowany.

        Szczegóły urlopu:
        Data rozpoczęcia: {start_date}
        Data zakończenia: {end_date}
        Powód: {reason}

        Pozdrawiamy,
        Zespół Żabka Grafiki
        """
    else:
        subject = "Twój wniosek urlopowy został odrzucony"
        body = f"""
        Witaj {first_name}!

        Niestety, Twój wniosek urlopowy został odrzucony.

        Szczegóły wniosku:
        Data rozpoczęcia: {start_date}
        Data zakończenia: {end_date}
        Powód: {reason}

        Skontaktuj się z przełożonym w celu uzyskania więcej informacji.

        Pozdrawiamy,
        Zespół Żabka Grafiki
        """

    message = MessageSchema(
        subject=subject,
        recipients=[email],
        body=body,
        subtype="plain"
    )

    try:
        await fm.send_message(message)
        logger.info(f"Email statusu urlopu wysłany do: {email}")

    except Exception as e:
        logger.error(f"Błąd wysyłania emaila statusu urlopu do {email}: {e}")
        raise
