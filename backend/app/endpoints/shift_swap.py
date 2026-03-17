from fastapi import APIRouter, Depends, HTTPException, status
from typing import List
from bson import ObjectId
import logging
import motor.motor_asyncio

from ..database import get_db
from ..dependencies import get_current_active_user, get_current_admin_user
from .. import models, schemas
from ..services.shift_swap_service import create_swap_request, respond_to_swap

router = APIRouter()
logger = logging.getLogger(__name__)

@router.post("/request", response_model=schemas.ShiftSwapResponse)
async def request_shift_swap(
    request: schemas.ShiftSwapRequest,
    current_user: dict = Depends(get_current_active_user),
    db: motor.motor_asyncio.AsyncIOMotorClient = Depends(get_db)
):
    """Zgłoś chęć wymiany zmiany."""
    try:
        return await create_swap_request(db, request, current_user)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating swap request: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

@router.get("/my-requests", response_model=List[schemas.ShiftSwapResponse])
async def get_my_swap_requests(
    current_user: dict = Depends(get_current_active_user),
    db: motor.motor_asyncio.AsyncIOMotorClient = Depends(get_db)
):
    """Pobierz moje wymiany (wychodzące i przychodzące)."""
    user_id = current_user["_id"]
    query = {
        "$or": [
            {"requester_id": user_id},
            {"target_user_id": user_id}
        ]
    }
    swaps = await db.shift_swaps.find(query).sort("created_at", -1).to_list(length=None)
    
    # Fix dates for Pydantic
    for s in swaps:
        s["my_date"] = s["my_date"].date()
        s["target_date"] = s["target_date"].date()
        
    return swaps

@router.put("/{swap_id}/respond", response_model=schemas.MessageResponse)
async def respond_to_swap_endpoint(
    swap_id: str,
    response_data: schemas.SwapResponseRequest,
    current_user: dict = Depends(get_current_active_user),
    db: motor.motor_asyncio.AsyncIOMotorClient = Depends(get_db)
):
    """Odpowiedz na prośbę o wymianę (akceptuj/odrzuć)."""
    action_map = {
        "accepted": "accept",
        "rejected": "reject"
    }
    
    action = action_map.get(response_data.response)
    if not action:
        raise HTTPException(status_code=400, detail="Invalid response value. Use 'accepted' or 'rejected'.")

    return await respond_to_swap(db, swap_id, action, current_user)

@router.get("/store-history", response_model=List[schemas.ShiftSwapResponse])
async def get_store_swap_history(
    current_user: dict = Depends(get_current_admin_user),
    db: motor.motor_asyncio.AsyncIOMotorClient = Depends(get_db)
):
    """Dla franczyzobiorcy: Historia wymian w sklepie."""
    franchise_code = current_user.get("franchise_code")
    swaps = await db.shift_swaps.find({"franchise_code": franchise_code}).sort("created_at", -1).to_list(length=None)
    
    for s in swaps:
        s["my_date"] = s["my_date"].date()
        s["target_date"] = s["target_date"].date()
        
    return swaps
