from fastapi import APIRouter, Request, HTTPException, Depends
from datetime import datetime, timedelta
import logging
import motor.motor_asyncio
from bson import ObjectId

from ..database import get_db

router = APIRouter()
logger = logging.getLogger(__name__)

@router.post("/revenuecat")
async def revenuecat_webhook(request: Request, db: motor.motor_asyncio.AsyncIOMotorClient = Depends(get_db)):
    """
    Webhook odbierający zdarzenia z serwerów RevenueCat.
    Zdarzenia informują o zakupach, odnowieniach, anulacjach i wstrzymaniach (PAUSE).
    """
    try:
        body = await request.json()
        event = body.get("event", {})
        
        event_type = event.get("type")
        app_user_id = event.get("app_user_id") # To powinno być równe ObjectId użytkownika
        
        if not app_user_id:
            logger.warning("RevenueCat Webhook: Brak app_user_id w zdarzeniu.")
            return {"status": "ok", "message": "Ignored - no app_user_id"}
            
        logger.info(f"Otrzymano zdarzenie RevenueCat: {event_type} dla użytkownika {app_user_id}")

        # Weryfikacja formatu ID użytkownika
        try:
            user_object_id = ObjectId(app_user_id)
        except Exception:
            logger.error(f"RevenueCat Webhook: app_user_id '{app_user_id}' nie jest poprawnym ObjectId.")
            return {"status": "ok", "message": "Ignored - invalid user ID"}

        # Zdarzenia, które aktywują / przedłużają subskrypcję
        active_events = ["INITIAL_PURCHASE", "RENEWAL", "UNCANCELLATION", "NON_RENEWING_PURCHASE"]
        
        # Zdarzenia, które dezaktywują / wstrzymują subskrypcję (PAUSE to funkcja Google Play)
        inactive_events = ["CANCELLATION", "EXPIRATION", "BILLING_ISSUE", "PAUSE"]

        if event_type in active_events:
            # Użytkownik opłacił subskrypcję
            expiration_at_ms = event.get("expiration_at_ms")
            if expiration_at_ms:
                expiration_date = datetime.utcfromtimestamp(expiration_at_ms / 1000.0)
            else:
                expiration_date = datetime.utcnow() + timedelta(days=30) # Default
                
            await db.users.update_one(
                {"_id": user_object_id},
                {"$set": {
                    "is_subscription_active": True,
                    "subscription_valid_until": expiration_date,
                    "subscription_status_reason": event_type
                }}
            )
            logger.info(f"Subskrypcja aktywowana dla {app_user_id} do {expiration_date}")

        elif event_type in inactive_events:
            # Subskrypcja wygasła, została anulowana lub WSTRZYMANA (PAUSE)
            await db.users.update_one(
                {"_id": user_object_id},
                {"$set": {
                    "is_subscription_active": False,
                    "subscription_status_reason": event_type
                }}
            )
            logger.info(f"Subskrypcja wyłączona ({event_type}) dla {app_user_id}")
            
        else:
            logger.info(f"Zdarzenie {event_type} zignorowane (brak akcji)")

        return {"status": "ok"}
        
    except Exception as e:
        logger.error(f"RevenueCat Webhook Error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal webhook error")
