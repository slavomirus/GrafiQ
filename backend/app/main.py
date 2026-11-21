from fastapi import FastAPI, Request
from contextlib import asynccontextmanager
import logging
import sys
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
import os
from dotenv import load_dotenv

# To wymusza wczytanie pliku .env
load_dotenv()

print("=== TEST ZMIENNYCH ===")
print("MAIL_USERNAME widoczny w systemie:", os.getenv("MAIL_USERNAME"))
print("MAIL_FROM widoczny w systemie:", os.getenv("MAIL_FROM"))
print("======================")
# --- Konfiguracja Logowania ---
# Ustawienie poziomu logowania dla `pymongo` na WARNING, aby uniknąć zalewu logów DEBUG.
logging.getLogger("pymongo").setLevel(logging.WARNING)

log_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
stream_handler = logging.StreamHandler(sys.stdout)
stream_handler.setFormatter(log_formatter)

root_logger = logging.getLogger()
if root_logger.hasHandlers():
    root_logger.handlers.clear()
root_logger.addHandler(stream_handler)
# Ustawienie głównego loggera na INFO, aby logi aplikacji były bardziej zwięzłe.
root_logger.setLevel(logging.INFO)
# ------------------------------

from .database import db_manager
from .config import settings
from .endpoints import auth, vacation, availability, schedule, users, reports, verification, stores, schedule_generator, schedule_management, settings as settings_ep, shift_swap, updates, payments, webhooks
from .services.seniority_service import update_employees_seniority
from .services.notification_service import initialize_firebase

logger = logging.getLogger(__name__)

# Opcjonalna inicjalizacja Sentry z maskowaniem PII (gdy ustawiono SENTRY_DSN)
if settings.SENTRY_DSN:
    try:
        import sentry_sdk
        def _scrub_pii(event, hint):
            req = event.get("request", {})
            headers = req.get("headers", {})
            if isinstance(headers, dict):
                for h in list(headers.keys()):
                    if h.lower() in {"authorization", "cookie", "x-api-key"}:
                        headers[h] = "[REDACTED]"
            return event
        sentry_sdk.init(dsn=settings.SENTRY_DSN, traces_sample_rate=0.1, send_default_pii=False, before_send=_scrub_pii)
        logger.info("Sentry SDK zainicjalizowany z PII scrubbing.")
    except ImportError:
        logger.warning("SENTRY_DSN ustawione, ale pakiet sentry-sdk nie jest zainstalowany.")

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Kontekst cyklu życia aplikacji do łączenia i zamykania bazy danych.
    """
    logger.info("Aplikacja startuje... Łączenie z bazą danych.")
    initialize_firebase()
    await db_manager.connect_to_db()
    
    # Automatyczna aktualizacja stażu pracy przy starcie aplikacji
    try:
        if db_manager.db is not None:
            await update_employees_seniority(db_manager.db)
        else:
            logger.warning("Baza danych nie jest dostępna, pominięto aktualizację stażu pracy.")
    except Exception as e:
        logger.error(f"Błąd podczas aktualizacji stażu pracy przy starcie: {e}")
        
    yield
    logger.info("Aplikacja kończy działanie... Zamykanie połączenia z bazą danych.")
    await db_manager.close_db_connection()

from slowapi.errors import RateLimitExceeded
from .limiter import limiter

async def custom_rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded):
    return JSONResponse(
        status_code=429,
        content={"detail": "Zbyt wiele prób. Spróbuj ponownie za chwilę."},
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Credentials": "true",
            "Access-Control-Allow-Methods": "*",
            "Access-Control-Allow-Headers": "*",
        }
    )

app = FastAPI(
    title="Schedule Backend",
    description="API do zarządzania grafikami, urlopami i dostępnością",
    version="0.1.0",
    lifespan=lifespan
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, custom_rate_limit_exceeded_handler)

# Konfiguracja CORS
origins = ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    return response

# --- Montowanie plików statycznych (EULA/RODO) ---
static_dir = os.path.join(os.path.dirname(__file__), "static")
if not os.path.exists(static_dir):
    os.makedirs(os.path.join(static_dir, "documents"))
    logger.info(f"Utworzono katalog na pliki statyczne: {static_dir}")

app.mount("/static", StaticFiles(directory=static_dir), name="static")
# ----------------------------------------------------

# --- DEBUG ENDPOINT TO LIST ALL ROUTES ---
@app.get("/routes")
def list_routes(req: Request):
    """Endpoint do debugowania - zwraca listę wszystkich zarejestrowrowanych ścieżek w aplikacji."""
    url_list = [
        {"path": route.path, "name": route.name}
        for route in req.app.routes
    ]
    return JSONResponse(content=url_list)
# ----------------------------------------

# Dołączanie routerów
app.include_router(auth.router, prefix="/auth", tags=["auth"])
app.include_router(vacation.router, prefix="/vacation", tags=["vacations"])
app.include_router(availability.router, prefix="/availability", tags=["availabilities"])
app.include_router(schedule.router, prefix="/schedule", tags=["schedule"])
# Dodano router dla liczby mnogiej /schedules
app.include_router(schedule.router_schedules, prefix="/schedules", tags=["schedules"])
app.include_router(users.router, prefix="/users", tags=["users"])
app.include_router(reports.router, prefix="/reports", tags=["reports"])
app.include_router(verification.router, prefix="/verification", tags=["verification"])
app.include_router(stores.router, prefix="/stores", tags=["stores"])
app.include_router(schedule_generator.router, prefix="/schedule-generator", tags=["schedule-generator"])
app.include_router(schedule_management.router, prefix="/schedule-management", tags=["schedule-management"])
app.include_router(settings_ep.router, prefix="/settings", tags=["settings"])
app.include_router(shift_swap.router, prefix="/swaps", tags=["swaps"])
app.include_router(updates.router, prefix="/updates", tags=["updates"])
app.include_router(payments.router, prefix="/payments", tags=["payments"])
app.include_router(webhooks.router, prefix="/webhooks", tags=["webhooks"])

logger.info("Aplikacja skonfigurowana i gotowa do przyjęcia zapytań.")
