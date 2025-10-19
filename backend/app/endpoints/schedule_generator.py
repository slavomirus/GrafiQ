# Plik: backend/app/endpoints/schedule_generator.py

from fastapi import APIRouter, Depends, HTTPException, status, BackgroundTasks
from datetime import date
import logging
from pydantic import BaseModel

from ..database import get_db
from ..dependencies import get_current_admin_user
from ..services.schedule_generator_service import generate_schedule_for_period
import motor.motor_asyncio

router = APIRouter()
logger = logging.getLogger(__name__)

class ScheduleGenerationRequest(BaseModel):
    year: int
    month: int

@router.post("/generate", status_code=status.HTTP_202_ACCEPTED)
async def generate_schedule_endpoint(
    request: ScheduleGenerationRequest,
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(get_current_admin_user),
    db: motor.motor_asyncio.AsyncIOMotorDatabase = Depends(get_db)
):
    """Uruchamia w tle proces generowania grafiku dla danego sklepu i okresu."""
    franchise_code = current_user.get("franchise_code")
    if not franchise_code:
        raise HTTPException(status_code=400, detail="Użytkownik nie jest przypisany do żadnego sklepu.")

    try:
        logger.info(f"Przyjęto zadanie generowania grafiku dla sklepu {franchise_code} przez użytkownika {current_user.get('email')}")
        
        # Dodanie czasochłonnego zadania do wykonania w tle
        background_tasks.add_task(
            generate_schedule_for_period,
            db=db,
            current_user=current_user,
            year=request.year,
            month=request.month
        )
        
        # Natychmiastowe zwrócenie odpowiedzi
        return {"message": "Generowanie grafiku zostało rozpoczęte. Odśwież listę za chwilę, aby zobaczyć wyniki."}
        
    except Exception as e:
        # Ten blok złapie teraz tylko błędy podczas zlecania zadania, a nie jego wykonania
        logger.error(f"Nieoczekiwany błąd podczas zlecania generowania grafiku: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Wystąpił wewnętrzny błąd serwera podczas zlecania generowania grafiku.")
