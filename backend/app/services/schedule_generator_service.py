# Plik: backend/app/services/schedule_generator_service.py

import logging
from datetime import date, timedelta, datetime, time
from typing import List, Dict, Any, Set, Tuple
import motor.motor_asyncio
from collections import defaultdict
from bson import ObjectId
from fastapi import HTTPException, status
import random
import calendar

from .. import models, schemas

logger = logging.getLogger(__name__)

POLISH_HOLIDAYS = [(1, 1), (1, 6), (5, 1), (5, 3), (8, 15), (11, 1), (11, 11), (12, 25), (12, 26)]
PROMO_REFERENCE_DATE = date(2025, 9, 23)

def is_holiday(day: date) -> bool:
    return (day.month, day.day) in POLISH_HOLIDAYS

class ScheduleGenerator:
    def __init__(self, db: motor.motor_asyncio.AsyncIOMotorDatabase, current_user: dict):
        self.db = db
        self.franchise_code = current_user.get("franchise_code")
        self.franchisee = current_user
        self.employees: List[Dict[str, Any]] = []
        self.store_settings: Dict[str, Any] = {}
        self.vacations: Dict[ObjectId, List[date]] = defaultdict(list)
        self.availabilities: Dict[ObjectId, Dict[date, Any]] = defaultdict(dict)
        self.schedule: Dict[date, Dict[str, List[ObjectId]]] = defaultdict(lambda: defaultdict(list))
        self.work_days_in_a_row: Dict[ObjectId, int] = defaultdict(int)
        self.total_hours_worked: Dict[ObjectId, float] = defaultdict(float)
        self.last_shift_worked: Dict[ObjectId, str] = {}

    async def _gather_data(self, start_date: date, end_date: date):
        logger.info(f"Rozpoczynanie zbierania danych dla grafiku ({self.franchise_code}) od {start_date} do {end_date}")
        self.store_settings = await self.db.storesettings.find_one({"franchise_code": self.franchise_code}) or {}
        self.employees = await self.db.users.find({
            "franchise_code": self.franchise_code,
            "role": models.UserRole.EMPLOYEE.value,
            "status": models.UserStatus.ACTIVE.value
        }).to_list(length=None)
        
        if not self.employees:
            raise ValueError("Brak aktywnych pracowników do wygenerowania grafiku.")
        logger.info(f"Znaleziono {len(self.employees)} pracowników.")

        employee_ids = [emp["_id"] for emp in self.employees]
        start_datetime = datetime.combine(start_date, time.min)
        end_datetime = datetime.combine(end_date, time.max)

        vacations_cursor = self.db.vacations.find({
            "user_id": {"$in": employee_ids},
            "status": models.VacationStatus.APPROVED.value,
            "start_date": {"$lte": end_datetime},
            "end_date": {"$gte": start_datetime}
        })
        async for vacation in vacations_cursor:
            d = vacation["start_date"].date()
            while d <= vacation["end_date"].date():
                self.vacations[vacation["user_id"]].append(d)
                d += timedelta(days=1)

    def _is_employee_available(self, employee_id: ObjectId, day: date) -> bool:
        if day in self.vacations.get(employee_id, []):
            return False
        if self.work_days_in_a_row.get(employee_id, 0) >= 5:
            return False
        return True

    def _assign_employee(self, emp_id: ObjectId, day: date, shift: str):
        self.schedule[day][shift].append(emp_id)
        self.work_days_in_a_row[emp_id] = self.work_days_in_a_row.get(emp_id, 0) + 1
        self.total_hours_worked[emp_id] = self.total_hours_worked.get(emp_id, 0) + 8
        self.last_shift_worked[emp_id] = shift

    async def generate(self, start_date: date, end_date: date):
        await self._gather_data(start_date, end_date)
        conflicts: List[str] = []
        current_date = start_date
        
        while current_date <= end_date:
            day_str = current_date.isoformat()
            is_promo_change_day = (current_date - PROMO_REFERENCE_DATE).days % 14 == 0
            needs = {
                schemas.ShiftType.MORNING.value: self.store_settings.get("employees_per_morning_shift", 1),
                schemas.ShiftType.MIDDLE.value: self.store_settings.get("employees_per_middle_shift", 0),
                schemas.ShiftType.CLOSING.value: self.store_settings.get("employees_on_promo_change", 2) if is_promo_change_day else self.store_settings.get("employees_per_closing_shift", 1)
            }

            morning_shift = schemas.ShiftType.MORNING.value
            closing_shift = schemas.ShiftType.CLOSING.value
            middle_shift = schemas.ShiftType.MIDDLE.value

            available_employees = {emp["_id"] for emp in self.employees if self._is_employee_available(emp["_id"], current_date)}
            morning_candidates = {eid for eid in available_employees if self.last_shift_worked.get(eid) != closing_shift}

            assigned_morning = []
            if needs[morning_shift] > 0:
                if not morning_candidates:
                    conflicts.append(f"Dnia {day_str} brak kandydatów na zmianę poranną!")
                else:
                    sorted_morning_candidates = sorted(list(morning_candidates), key=lambda eid: self.total_hours_worked.get(eid, 0))
                    for _ in range(needs[morning_shift]):
                        if not sorted_morning_candidates: break
                        emp_id = sorted_morning_candidates.pop(0)
                        self._assign_employee(emp_id, current_date, morning_shift)
                        assigned_morning.append(emp_id)

            remaining_employees = available_employees - set(assigned_morning)
            closing_candidates = list(remaining_employees)
            assigned_closing = []
            if needs[closing_shift] > 0:
                sorted_closing_candidates = sorted(closing_candidates, key=lambda eid: self.total_hours_worked.get(eid, 0))
                for _ in range(needs[closing_shift]):
                    if not sorted_closing_candidates: break
                    emp_id = sorted_closing_candidates.pop(0)
                    self._assign_employee(emp_id, current_date, closing_shift)
                    assigned_closing.append(emp_id)

            remaining_for_middle = remaining_employees - set(assigned_closing)
            franchisee_available = self._is_employee_available(self.franchisee["_id"], current_date)

            if needs[middle_shift] > 0:
                if franchisee_available:
                    self._assign_employee(self.franchisee["_id"], current_date, middle_shift)
                    franchisee_available = False
                elif remaining_for_middle:
                    emp_id = list(remaining_for_middle)[0]
                    self._assign_employee(emp_id, current_date, middle_shift)

            for shift in [morning_shift, closing_shift]:
                while len(self.schedule[current_date].get(shift, [])) < needs[shift]:
                    if franchisee_available:
                        self._assign_employee(self.franchisee["_id"], current_date, shift)
                        franchisee_available = False
                    else:
                        conflicts.append(f"Dnia {day_str} brakuje pracownika na zmianie '{shift}'.")
                        break

            assigned_today = set(self.schedule[current_date].get(morning_shift, [])) | set(self.schedule[current_date].get(closing_shift, [])) | set(self.schedule[current_date].get(middle_shift, []))
            all_user_ids = {emp["_id"] for emp in self.employees} | {self.franchisee["_id"]}
            for user_id in all_user_ids:
                if user_id not in assigned_today:
                    self.work_days_in_a_row[user_id] = 0
                    self.last_shift_worked[user_id] = "OFF"

            current_date += timedelta(days=1)

        final_schedule_for_json = {d.isoformat(): {s: [str(eid) for eid in eids] for s, eids in shifts.items()} for d, shifts in self.schedule.items()}
        draft_document = {
            "franchise_code": self.franchise_code,
            "start_date": datetime.combine(start_date, time.min),
            "end_date": datetime.combine(end_date, time.min), # <-- THE FIX IS HERE
            "schedule": final_schedule_for_json,
            "conflicts": conflicts,
            "status": "DRAFT",
            "created_at": datetime.utcnow()
        }
        result = await self.db.schedule_drafts.insert_one(draft_document)
        new_schedule_id = result.inserted_id
        logger.info(f"Zapisano nowy grafik roboczy dla {self.franchise_code} z ID: {new_schedule_id}")

async def generate_schedule_for_period(db: motor.motor_asyncio.AsyncIOMotorDatabase, current_user: dict, year: int, month: int):
    franchise_code = current_user.get("franchise_code")
    if not franchise_code:
        raise ValueError("Użytkownik nie jest przypisany do żadnego sklepu.")

    # Usuń stare, nieopublikowane grafiki dla tego sklepu
    delete_result = await db.schedule_drafts.delete_many({
        "franchise_code": franchise_code
    })
    if delete_result.deleted_count > 0:
        logger.info(f"Usunięto {delete_result.deleted_count} starych wersji roboczych dla sklepu {franchise_code}.")

    start_date = date(year, month, 1)
    _, num_days = calendar.monthrange(year, month)
    end_date = date(year, month, num_days)

    generator = ScheduleGenerator(db, current_user)
    await generator.generate(start_date, end_date)
