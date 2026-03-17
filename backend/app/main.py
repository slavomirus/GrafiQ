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
from .endpoints import auth, vacation, availability, schedule, users, reports, verification, stores, schedule_generator, schedule_management, settings, shift_swap, updates, payments
from .services.seniority_service import update_employees_seniority

logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Kontekst cyklu życia aplikacji do łączenia i zamykania bazy danych.
    """
    logger.info("Aplikacja startuje... Łączenie z bazą danych.")
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

app = FastAPI(
    title="Schedule Backend",
    description="API do zarządzania grafikami, urlopami i dostępnością",
    version="0.1.0",
    lifespan=lifespan
)

# Konfiguracja CORS
origins = ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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
app.include_router(settings.router, prefix="/settings", tags=["settings"])
app.include_router(shift_swap.router, prefix="/swaps", tags=["swaps"])
app.include_router(updates.router, prefix="/updates", tags=["updates"])
app.include_router(payments.router, prefix="/payments", tags=["payments"])

logger.info("Aplikacja skonfigurowana i gotowa do przyjęcia zapytań.")
