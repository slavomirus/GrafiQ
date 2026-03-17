import logging
from datetime import datetime, date
import motor.motor_asyncio
from .vacation_service import calculate_vacation_days

logger = logging.getLogger(__name__)

async def update_employees_seniority(db: motor.motor_asyncio.AsyncIOMotorDatabase):
    """
    Sprawdza i aktualizuje staż pracy pracowników na podstawie daty zatrudnienia.
    Powinno być uruchamiane okresowo (np. raz dziennie).
    """
    logger.info("Rozpoczynanie aktualizacji stażu pracy pracowników...")
    
    today = date.today()
    
    # Znajdź wszystkich pracowników, którzy mają ustawioną datę rozpoczęcia pracy
    cursor = db.users.find({
        "employment_start_date": {"$ne": None},
        "role": "employee"
    })
    
    updated_count = 0
    
    async for user in cursor:
        start_date = user.get("employment_start_date")
        if isinstance(start_date, datetime):
            start_date = start_date.date()
            
        if not start_date:
            continue
            
        # Oblicz staż w latach
        # Różnica w dniach / 365.25 (przybliżenie) lub dokładniej
        # Najprościej: różnica lat, skorygowana o to czy rocznica już była w tym roku
        years_diff = today.year - start_date.year
        if (today.month, today.day) < (start_date.month, start_date.day):
            years_diff -= 1
            
        current_seniority = max(0, years_diff)
        stored_seniority = user.get("seniority_years", 0)
        
        if current_seniority > stored_seniority:
            logger.info(f"Aktualizacja stażu dla {user.get('email')}: {stored_seniority} -> {current_seniority} lat")
            
            # Aktualizuj staż
            update_fields = {"seniority_years": current_seniority}
            
            # Przelicz urlop, jeśli staż wzrósł
            # Uwaga: To może być skomplikowane, bo urlop zależy też od tego czy to pierwszy rok pracy, etc.
            # Ale przyjmijmy prostą zasadę: przeliczamy entitlement na podstawie nowego stażu.
            # Jeśli entitlement wzrósł (np. z 20 na 26), dodajemy różnicę do vacation_days_left.
            
            fte = user.get("fte", 1.0)
            old_entitlement = calculate_vacation_days(stored_seniority, fte)
            new_entitlement = calculate_vacation_days(current_seniority, fte)
            
            if new_entitlement > old_entitlement:
                diff = new_entitlement - old_entitlement
                update_fields["vacation_days_left"] = user.get("vacation_days_left", 0) + diff
                logger.info(f"Zwiększono wymiar urlopu dla {user.get('email')} o {diff} dni.")
            
            await db.users.update_one(
                {"_id": user["_id"]},
                {"$set": update_fields}
            )
            updated_count += 1
            
    logger.info(f"Zakończono aktualizację stażu. Zaktualizowano {updated_count} pracowników.")
