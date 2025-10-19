from fastapi import FastAPI, Request
from contextlib import asynccontextmanager
import logging
import sys
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# --- Konfiguracja Logowania ---
log_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
stream_handler = logging.StreamHandler(sys.stdout)
stream_handler.setFormatter(log_formatter)

root_logger = logging.getLogger()
if root_logger.hasHandlers():
    root_logger.handlers.clear()
root_logger.addHandler(stream_handler)
root_logger.setLevel(logging.DEBUG)
# ------------------------------

from .database import db_manager
from .endpoints import auth, vacation, availability, schedule, users, reports, verification, stores, schedule_generator, schedule_management, settings

logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Kontekst cyklu życia aplikacji do łączenia i zamykania bazy danych.
    """
    logger.info("Aplikacja startuje... Łączenie z bazą danych.")
    await db_manager.connect_to_db()
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

# --- DEBUG ENDPOINT TO LIST ALL ROUTES ---
@app.get("/routes")
def list_routes(req: Request):
    """Endpoint do debugowania - zwraca listę wszystkich zarejestrowanych ścieżek w aplikacji."""
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
app.include_router(users.router, prefix="/users", tags=["users"])
app.include_router(reports.router, prefix="/reports", tags=["reports"])
app.include_router(verification.router, prefix="/verification", tags=["verification"])
app.include_router(stores.router, prefix="/stores", tags=["stores"])
app.include_router(schedule_generator.router, prefix="/schedule-generator", tags=["schedule-generator"])
app.include_router(schedule_management.router, prefix="/schedule-management", tags=["schedule-management"])
app.include_router(settings.router, prefix="/settings", tags=["settings"])

logger.info("Aplikacja skonfigurowana i gotowa do przyjęcia zapytań.")
