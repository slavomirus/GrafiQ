# Plik: backend/app/endpoints/schedule.py

from fastapi import APIRouter, Depends, HTTPException, status
import logging
from typing import List

from ..database import get_db
from ..dependencies import get_current_admin_user, get_current_user
# POPRAWKA: Dodano import nowej funkcji
from ..services.schedule_service import publish_schedule_draft, get_employee_schedule, get_all_drafts_for_store, get_schedules_history
from .. import schemas
import motor.motor_asyncio

router = APIRouter()
logger = logging.getLogger(__name__)

# NOWY ENDPOINT: Do pobierania historii grafików dla admina
@router.get("/history", response_model=List[schemas.ScheduleDraftResponse])
async def get_schedules_history_endpoint(
    current_user: dict = Depends(get_current_admin_user),
    db: motor.motor_asyncio.AsyncIOMotorDatabase = Depends(get_db)
):
    """Pobiera historię wszystkich grafików (opublikowanych i roboczych) dla sklepu."""
    try:
        history = await get_schedules_history(db, current_user)
        return history
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Nieoczekiwany błąd podczas pobierania historii grafików: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Wystąpił wewnętrzny błąd serwera.")


@router.get("/drafts", response_model=List[schemas.ScheduleDraftResponse])
async def get_schedule_drafts_endpoint(
    current_user: dict = Depends(get_current_admin_user),
    db: motor.motor_asyncio.AsyncIOMotorDatabase = Depends(get_db)
):
    """Pobiera listę wszystkich nieopublikowanych grafików (wersji roboczych) dla sklepu."""
    try:
        drafts = await get_all_drafts_for_store(db, current_user)
        return drafts
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Nieoczekiwany błąd podczas pobierania wersji roboczych: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Wystąpił wewnętrzny błąd serwera.")


@router.post("/{draft_id}/publish", status_code=status.HTTP_200_OK)
async def publish_schedule_endpoint(
    draft_id: str,
    current_user: dict = Depends(get_current_admin_user),
    db: motor.motor_asyncio.AsyncIOMotorDatabase = Depends(get_db)
):
    """Publikuje roboczą wersję grafiku, czyniąc ją oficjalną."""
    try:
        result = await publish_schedule_draft(db, draft_id, current_user)
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Nieoczekiwany błąd podczas publikowania grafiku {draft_id}: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Wystąpił wewnętrzny błąd serwera.")


@router.get("/my-schedule", status_code=status.HTTP_200_OK)
async def get_my_schedule_endpoint(
    current_user: dict = Depends(get_current_user),
    db: motor.motor_asyncio.AsyncIOMotorDatabase = Depends(get_db)
):
    """Pobiera aktualnie obowiązujący, opublikowany grafik dla pracownika."""
    try:
        schedule = await get_employee_schedule(db, current_user)
        return schedule
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Nieoczekiwany błąd podczas pobierania grafiku: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Wystąpił wewnętrzny błąd serwera.")
