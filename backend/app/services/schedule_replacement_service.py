# Plik: backend/app/services/schedule_replacement_service.py

import logging
from datetime import date, timedelta, datetime, time
from typing import List, Dict, Any, Optional, Tuple
import motor.motor_asyncio
from collections import defaultdict
from bson import ObjectId
import random

from .. import models, schemas

logger = logging.getLogger(__name__)

class ReplacementFinder:
    def __init__(self, db: motor.motor_asyncio.AsyncIOMotorDatabase, franchise_code: str):
        self.db = db
        self.franchise_code = franchise_code
        self.employees: List[Dict[str, Any]] = []
        self.absences: Dict[ObjectId, List[str]] = defaultdict(list)
        self.existing_shifts: Dict[ObjectId, List[Dict[str, Any]]] = defaultdict(list)

    async def _gather_data(self, target_date: date):
        logger.debug(f"Zbieranie danych dla zastępstwa na dzień: {target_date}")
        
        self.employees = await self.db.users.find({
            "franchise_code": self.franchise_code,
            "role": models.UserRole.EMPLOYEE.value,
            "status": models.UserStatus.ACTIVE.value
        }).to_list(length=None)

        if not self.employees:
            logger.warning("Brak aktywnych pracowników w sklepie.")
            return

        employee_ids = [emp["_id"] for emp in self.employees]
        
        start_of_day = datetime.combine(target_date, time.min)
        end_of_day = datetime.combine(target_date, time.max)
        yesterday_start_of_day = datetime.combine(target_date - timedelta(days=1), time.min)

        absences_cursor = self.db.vacations.find({
            "user_id": {"$in": employee_ids},
            "status": models.VacationStatus.APPROVED.value,
            "start_date": {"$lte": end_of_day},
            "end_date": {"$gte": start_of_day}
        })
        async for absence in absences_cursor:
            self.absences[absence["user_id"]].append("vacation")

        leaves_cursor = self.db.leaves.find({
            "user_id": {"$in": employee_ids},
            "start_date": {"$lte": end_of_day},
            "end_date": {"$gte": start_of_day}
        })
        async for leave in leaves_cursor:
            self.absences[leave["user_id"]].append("leave")

        shifts_cursor = self.db.schedule.find({
            "franchise_code": self.franchise_code, # Pobieramy wszystkie zmiany w sklepie
            "date": {"$gte": yesterday_start_of_day, "$lte": end_of_day}
        })
        async for shift in shifts_cursor:
            self.existing_shifts[shift["user_id"]].append(shift)

    def _is_candidate_available(self, employee_id: ObjectId, target_date: date, shift_name: str) -> bool:
        if employee_id in self.absences:
            return False

        for shift in self.existing_shifts.get(employee_id, []):
            if shift['date'].date() == target_date:
                return False

        if shift_name == schemas.ShiftType.MORNING.value:
            for shift in self.existing_shifts.get(employee_id, []):
                if shift['date'].date() == target_date - timedelta(days=1) and shift.get("shift_name") == schemas.ShiftType.CLOSING.value:
                    return False
        
        return True

    def _find_free_candidate(self, target_date: date, shift_name: str, excluded_employee_id: ObjectId) -> Optional[ObjectId]:
        """Plan A: Znajdź pracownika, który jest całkowicie wolny."""
        candidates = []
        for emp in self.employees:
            emp_id = emp["_id"]
            if emp_id == excluded_employee_id:
                continue
            if self._is_candidate_available(emp_id, target_date, shift_name):
                candidates.append(emp_id)
        
        return random.choice(candidates) if candidates else None

    def _find_reassignment_candidate(self, target_date: date, shift_name: str) -> Optional[Tuple[ObjectId, ObjectId]]:
        """Plan B: Znajdź pracownika na międzyzmianie, którego można przenieść."""
        if shift_name not in [schemas.ShiftType.MORNING.value, schemas.ShiftType.CLOSING.value]:
            return None # Przenosimy tylko na zmiany priorytetowe

        middle_shift_employees = []
        for user_id, shifts in self.existing_shifts.items():
            for shift in shifts:
                if shift['date'].date() == target_date and shift.get("shift_name") == schemas.ShiftType.MIDDLE.value:
                    middle_shift_employees.append((user_id, shift["_id"]))
        
        logger.debug(f"Znaleziono {len(middle_shift_employees)} pracowników na międzyzmianie do potencjalnego przeniesienia.")

        for emp_id, original_shift_id in middle_shift_employees:
            # Sprawdź, czy ten pracownik może objąć zmianę docelową (głównie reguła clopening)
            is_valid_for_reassignment = True
            if shift_name == schemas.ShiftType.MORNING.value:
                for shift in self.existing_shifts.get(emp_id, []):
                    if shift['date'].date() == target_date - timedelta(days=1) and shift.get("shift_name") == schemas.ShiftType.CLOSING.value:
                        is_valid_for_reassignment = False
                        break
            
            if is_valid_for_reassignment:
                logger.info(f"Znaleziono kandydata do przeniesienia: {emp_id} ze zmiany {original_shift_id}.")
                return emp_id, original_shift_id
        
        return None

    async def find_best_solution(self, target_date: date, shift_name: str, excluded_employee_id: ObjectId) -> Tuple[str, Optional[ObjectId], Optional[ObjectId]]:
        await self._gather_data(target_date)

        if not self.employees:
            return "DELETE", None, None

        # Plan A
        free_candidate = self._find_free_candidate(target_date, shift_name, excluded_employee_id)
        if free_candidate:
            logger.info(f"Plan A sukces: Znaleziono wolnego pracownika {free_candidate}.")
            return "ASSIGN", free_candidate, None

        # Plan B
        reassignment_solution = self._find_reassignment_candidate(target_date, shift_name)
        if reassignment_solution:
            reassigned_employee_id, original_shift_id = reassignment_solution
            logger.info(f"Plan B sukces: Znaleziono pracownika do przeniesienia {reassigned_employee_id}.")
            return "REASSIGN", reassigned_employee_id, original_shift_id

        # Ostateczność
        logger.warning(f"Plan A i B nie powiodły się. Zmiana {shift_name} w dniu {target_date} zostanie usunięta.")
        return "DELETE", None, None

async def find_best_solution_for_shift(
    db: motor.motor_asyncio.AsyncIOMotorDatabase,
    franchise_code: str,
    target_date: date,
    shift_name: str,
    excluded_employee_id: ObjectId
) -> Tuple[str, Optional[ObjectId], Optional[ObjectId]]:
    finder = ReplacementFinder(db, franchise_code)
    return await finder.find_best_solution(target_date, shift_name, excluded_employee_id)
