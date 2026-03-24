# Plik: backend/app/services/schedule_generator_service.py

import logging
from datetime import date, timedelta, datetime, time
from typing import List, Dict, Any, Set, Tuple, Optional
import motor.motor_asyncio
from collections import defaultdict
from bson import ObjectId
from fastapi import HTTPException, status
import random
import calendar
import holidays
from dataclasses import dataclass, field

from .. import models, schemas
from .schedule_service import get_store_settings_and_holidays, resolve_shift_hours

logger = logging.getLogger(__name__)

PROMO_REFERENCE_DATE = date(2025, 9, 23)

@dataclass
class TimeRange:
    start: datetime
    end: datetime

    def overlaps(self, other: 'TimeRange') -> bool:
        # Dwie zmiany nakładają się na siebie, jeśli start jednej jest przed końcem drugiej (i vice versa)
        return max(self.start, other.start) < min(self.end, other.end)

    @property
    def hours(self) -> float:
        return (self.end - self.start).total_seconds() / 3600.0

@dataclass
class ShiftDemand:
    id: str
    time_range: TimeRange
    required_employees: int
    shift_type: str

@dataclass
class Employee:
    id: str
    db_id: ObjectId
    first_name: str
    last_name: str
    contract_type: str
    fte_or_target: float
    
    # Input Constraints
    unavailabilities: List[TimeRange] = field(default_factory=list)
    requested_shifts: Dict[date, str] = field(default_factory=dict)
    preferences: Dict[str, Any] = field(default_factory=dict) 
    
    # Stan wewnętrzny algorytmu (Solver State)
    assigned_shifts: List[ShiftDemand] = field(default_factory=list)
    target_hours: float = 0.0
    worked_hours: float = 0.0

    @property
    def remaining_hours(self) -> float:
        return self.target_hours - self.worked_hours

def calculate_uop_hours(year: int, month: int, fte: float) -> float:
    """
    Wzór z art. 130 KP:
    1. (tygodnie pełne * 40h) + (pozostałe dni wystające od pon-pt * 8h)
    2. Odejmujemy 8h za każde święto wypadające w innym dniu niż niedziela.
    """
    pl_holidays = holidays.Poland(years=year)
    first_weekday, num_days = calendar.monthrange(year, month)
    
    full_weeks = num_days // 7
    remaining_days = num_days % 7
    
    work_hours = full_weeks * 40
    for i in range(remaining_days):
        current_weekday = (first_weekday + i) % 7
        if current_weekday < 5:  # 0 to poniedziałek, 4 to piątek
            work_hours += 8
            
    for day in range(1, num_days + 1):
        dt = date(year, month, day)
        if dt in pl_holidays:
            if dt.weekday() != 6:
                work_hours -= 8
                
    return work_hours * fte

class ScheduleGenerator:
    def __init__(self, db: motor.motor_asyncio.AsyncIOMotorDatabase, current_user: dict):
        self.db = db
        self.franchise_code = current_user.get("franchise_code")
        self.franchisee = current_user
        
        self.store_settings: Dict[str, Any] = {}
        self.holidays_map: Dict[str, Any] = {}
        
        self.db_employees: List[Dict[str, Any]] = []
        self.db_vacations: List[Dict[str, Any]] = []
        self.db_availabilities: List[Dict[str, Any]] = []
        
        self.employees: List[Employee] = []
        self.demands: List[ShiftDemand] = []
        self.logs: List[str] = []

    async def _gather_data(self, start_date: date, end_date: date):
        self.store_settings, self.holidays_map = await get_store_settings_and_holidays(self.db, self.franchise_code)
        
        self.db_employees = await self.db.users.find({
            "franchise_code": self.franchise_code,
            "role": models.UserRole.EMPLOYEE.value,
            "status": models.UserStatus.ACTIVE.value
        }).to_list(length=None)
        
        # Właściciel też jest pracownikiem na potrzeby grafiku
        self.db_employees.append(self.franchisee)

        employee_ids = [emp["_id"] for emp in self.db_employees]

        start_datetime = datetime.combine(start_date, time.min)
        end_datetime = datetime.combine(end_date, time.max)

        self.db_vacations = await self.db.vacations.find({
            "user_id": {"$in": employee_ids},
            "status": models.VacationStatus.APPROVED.value,
            "start_date": {"$lte": end_datetime},
            "end_date": {"$gte": start_datetime}
        }).to_list(length=None)

        self.db_availabilities = await self.db.availability.find({
            "user_id": {"$in": employee_ids},
            "date": {"$gte": start_datetime, "$lte": end_datetime}
        }).to_list(length=None)

    def _parse_time(self, t_str: str) -> time:
        if isinstance(t_str, time): return t_str
        try: return datetime.strptime(t_str, "%H:%M").time()
        except ValueError:
            try: return datetime.strptime(t_str, "%H:%M:%S").time()
            except ValueError: return time(0, 0)

    def _get_shift_datetime_range(self, date_obj: date, shift_name: str) -> Tuple[datetime, datetime]:
        start_str, end_str = resolve_shift_hours(date_obj, shift_name, self.store_settings, self.holidays_map)
        start_dt = datetime.combine(date_obj, self._parse_time(start_str))
        end_dt = datetime.combine(date_obj, self._parse_time(end_str))
        if end_dt <= start_dt: 
            end_dt += timedelta(days=1)
        return start_dt, end_dt

    def _map_to_domain(self, start_date: date, end_date: date):
        vacations_by_user = defaultdict(list)
        for v in self.db_vacations:
            d = v["start_date"].date()
            while d <= v["end_date"].date():
                if start_date <= d <= end_date:
                    dt_start = datetime.combine(d, time.min)
                    dt_end = datetime.combine(d, time.max)
                    vacations_by_user[v["user_id"]].append(TimeRange(dt_start, dt_end))
                d += timedelta(days=1)

        avail_by_user = defaultdict(dict)
        for a in self.db_availabilities:
            d = a["date"].date()
            avail_by_user[a["user_id"]][d] = a

        for db_emp in self.db_employees:
            contract = db_emp.get("contract_type", "UZL")
            if contract == models.ContractType.UOP.value:
                fte_or_target = float(db_emp.get("fte", 1.0))
            else:
                fte_or_target = float(db_emp.get("monthly_hours_target", 120))

            emp = Employee(
                id=str(db_emp["_id"]),
                db_id=db_emp["_id"],
                first_name=db_emp.get("first_name", ""),
                last_name=db_emp.get("last_name", ""),
                contract_type=contract,
                fte_or_target=fte_or_target,
                preferences=db_emp.get("preferences", {}) or {}
            )

            # 1. Unavailabilities z Urlopów
            emp.unavailabilities.extend(vacations_by_user[db_emp["_id"]])
            
            # 2. Unavailabilities i Requesty z Dyspozycyjności
            for d, a in avail_by_user[db_emp["_id"]].items():
                p_type = a.get("period_type", "").lower()
                # OFF
                if p_type in ["wolne", "urlop", "niedostępny", "unavailable", "day_off", "w", "off"]:
                    dt_start = datetime.combine(d, time.min)
                    dt_end = datetime.combine(d, time.max)
                    if a.get("start_time") and a.get("end_time"):
                        dt_start = datetime.combine(d, self._parse_time(a["start_time"]))
                        dt_end = datetime.combine(d, self._parse_time(a["end_time"]))
                        if dt_end <= dt_start: dt_end += timedelta(days=1)
                    emp.unavailabilities.append(TimeRange(dt_start, dt_end))
                else:
                    # Request (chce pracować)
                    mapped = None
                    if p_type in ["rano", "morning", schemas.ShiftType.MORNING.value]: mapped = schemas.ShiftType.MORNING.value
                    elif p_type in ["środek", "middle", schemas.ShiftType.MIDDLE.value]: mapped = schemas.ShiftType.MIDDLE.value
                    elif p_type in ["wieczór", "zamknięcie", "closing", schemas.ShiftType.CLOSING.value]: mapped = schemas.ShiftType.CLOSING.value
                    # if mapped:
                    #     emp.requested_shifts[d] = mapped
                    #     # Wąskie ramy dostępności jako Unavailability dla pozostałej części dnia
                    #     if a.get("start_time") and a.get("end_time"):
                    #         av_start = datetime.combine(d, self._parse_time(a["start_time"]))
                    #         av_end = datetime.combine(d, self._parse_time(a["end_time"]))
                    #         if av_end <= av_start: av_end += timedelta(days=1)
                    #
                    #         # Czas PRZED dostępnością
                    #         if av_start > datetime.combine(d, time.min):
                    #             emp.unavailabilities.append(TimeRange(datetime.combine(d, time.min), av_start))
                    #         # Czas PO dostępności
                    #         if av_end < datetime.combine(d, time.max):
                    #             emp.unavailabilities.append(TimeRange(av_end, datetime.combine(d, time.max)))

            self.employees.append(emp)

        # 3. Zapotrzebowanie pracodawcy (Shift Demand)
        curr = start_date
        while curr <= end_date:
            date_str = curr.strftime("%Y-%m-%d")
            holiday_info = self.holidays_map.get(date_str, {})
            if holiday_info.get("is_closed"):
                curr += timedelta(days=1)
                continue

            is_promo_change_day = (curr - PROMO_REFERENCE_DATE).days % 14 == 0
            closing_needs = self.store_settings.get("employees_on_promo_change", 2) if is_promo_change_day else self.store_settings.get("employees_per_closing_shift", 1)
            
            # HARD CONSTRAINT na minimalną liczbę pracowników
            needs = {
                schemas.ShiftType.MORNING.value: max(1, int(self.store_settings.get("employees_per_morning_shift", 1))),
                schemas.ShiftType.MIDDLE.value: max(0, int(self.store_settings.get("employees_per_middle_shift", 0))),
                schemas.ShiftType.CLOSING.value: max(1, int(closing_needs))
            }

            for shift_name, count in needs.items():
                if count > 0:
                    st, et = self._get_shift_datetime_range(curr, shift_name)
                    self.demands.append(ShiftDemand(
                        id=f"{date_str}_{shift_name}",
                        time_range=TimeRange(st, et),
                        required_employees=count,
                        shift_type=shift_name
                    ))

            curr += timedelta(days=1)

    def _check_hard_constraints(self, emp: Employee, shift: ShiftDemand) -> bool:
        for unav in emp.unavailabilities:
            if unav.overlaps(shift.time_range):
                return False
                
        for assigned in emp.assigned_shifts:
            if assigned.time_range.overlaps(shift.time_range):
                return False
            # Max 1 shift per day logic (dodatkowe zabezpieczenie w polskim prawie)
            if assigned.time_range.start.date() == shift.time_range.start.date():
                return False

        for assigned in emp.assigned_shifts:
            if assigned.time_range.end <= shift.time_range.start:
                gap = (shift.time_range.start - assigned.time_range.end).total_seconds() / 3600.0
                if gap < 11: return False
            elif shift.time_range.end <= assigned.time_range.start:
                gap = (assigned.time_range.start - shift.time_range.end).total_seconds() / 3600.0
                if gap < 11: return False

        if emp.contract_type == 'UOP':
            if round(emp.worked_hours + shift.time_range.hours, 2) > round(emp.target_hours, 2):
                return False
                
        return True

    def _assign(self, emp: Employee, shift: ShiftDemand):
        emp.assigned_shifts.append(shift)
        emp.worked_hours += shift.time_range.hours
        shift.required_employees -= 1

    def _calculate_soft_score(self, emp: Employee, shift: ShiftDemand) -> float:
        score = 0.0
        shift_date = shift.time_range.start.date()
        
        if emp.requested_shifts.get(shift_date) == shift.shift_type:
            score += 100.0
            
        prefs = emp.preferences.get("preferred_shifts", [])
        if shift.shift_type in prefs:
            score += 10.0
            
        day_pref = emp.preferences.get("day_preference")
        is_weekend = shift.time_range.start.weekday() >= 5
        if day_pref == schemas.DayPreference.WEEKDAYS.value and not is_weekend:
            score += 5.0
        if day_pref == schemas.DayPreference.WEEKENDS.value and is_weekend:
            score += 5.0
            
        return score

    async def generate(self, start_date: date, end_date: date):
        await self._gather_data(start_date, end_date)
        self._map_to_domain(start_date, end_date)
        
        uop_emps = [e for e in self.employees if e.contract_type == 'UOP']
        uzl_emps = [e for e in self.employees if e.contract_type == 'UZL']

        # Wyliczenie Puli (Art. 130)
        full_uop_hours = calculate_uop_hours(start_date.year, start_date.month, 1.0)
        for emp in uop_emps:
            emp.target_hours = full_uop_hours * emp.fte_or_target
        for emp in uzl_emps:
            emp.target_hours = emp.fte_or_target

        # Przygotowanie slotów (klonowanie dla każdego wymaganego pracownika)
        slots = []
        for demand in self.demands:
            for _ in range(demand.required_employees):
                slots.append(ShiftDemand(
                    id=demand.id,
                    time_range=demand.time_range,
                    required_employees=1,
                    shift_type=demand.shift_type
                ))
            
        # Podział na zmiany krytyczne i poboczne
        critical_types = [schemas.ShiftType.MORNING.value, schemas.ShiftType.CLOSING.value]
        critical_slots = sorted([s for s in slots if s.shift_type in critical_types], key=lambda x: x.time_range.start)
        non_critical_slots = sorted([s for s in slots if s.shift_type not in critical_types], key=lambda x: x.time_range.start)

        # Priorytetyzacja: najpierw zrób wszystkie ranki i zamknięcia, potem resztę
        slots = critical_slots + non_critical_slots

        # KROK 2: Pre-assign (Requested Shifts)
        for shift in slots:
            if shift.required_employees <= 0: continue
            
            shift_date = shift.time_range.start.date()
            candidates = [
                e for e in self.employees 
                if e.requested_shifts.get(shift_date) == shift.shift_type 
                and self._check_hard_constraints(e, shift)
            ]
            if candidates:
                # Oblicz burn_rate i weź tego o najniższym
                valid_candidates = []
                for e in candidates:
                    if e.contract_type == 'UOP' and e.worked_hours + shift.time_range.hours > e.target_hours:
                        continue
                    if e.contract_type == 'UZL' and e.worked_hours + shift.time_range.hours > e.target_hours * 1.2:
                        continue
                    burn_rate = e.worked_hours / e.target_hours if e.target_hours > 0 else 1.0
                    valid_candidates.append((e, burn_rate))
                
                if valid_candidates:
                    valid_candidates.sort(key=lambda x: x[1])
                    chosen = valid_candidates[0][0]
                    self._assign(chosen, shift)

        # KROK 3: Główny Przydział Zmian
        for shift in slots:
            if shift.required_employees <= 0: continue
            
            candidates = []
            for e in self.employees:
                if self._check_hard_constraints(e, shift):
                    if e.contract_type == 'UOP' and e.worked_hours + shift.time_range.hours > e.target_hours:
                        continue
                    if e.contract_type == 'UZL' and e.worked_hours + shift.time_range.hours > e.target_hours * 1.2:
                        continue
                    candidates.append(e)
            
            if candidates:
                # Przypisz punktację do kandydatów
                scored_candidates = []
                for e in candidates:
                    burn_rate = e.worked_hours / e.target_hours if e.target_hours > 0 else 1.0
                    score = self._calculate_soft_score(e, shift)
                    scored_candidates.append((e, score, burn_rate))
                
                # Wybierz grupę o najwyższym soft_score
                scored_candidates.sort(key=lambda x: x[1], reverse=True)
                top_score = scored_candidates[0][1]
                top_candidates = [item for item in scored_candidates if item[1] == top_score]
                
                # Z grupy o najwyższym soft_score wybierz tego z najmniejszym burn_rate (w przypadku remisu losuj)
                top_candidates.sort(key=lambda x: x[2])
                min_burn_rate = top_candidates[0][2]
                best_candidates = [item for item in top_candidates if item[2] == min_burn_rate]
                
                chosen = random.choice(best_candidates)[0]
                if chosen: self._assign(chosen, shift)

        # KROK 5: Fallback & Alerty
        for shift in slots:
            if shift.required_employees > 0:
                fallback_candidates = []
                for e in self.employees:
                    # Zmodyfikowane Hard Constraints (omijamy preferencje dni, limity godzin, 
                    # ale ZACHOWUJEMY minimum 11h, 1 zmianę/dzień i brak nachodzenia na urlop/inną zmianę)
                    can_work = True
                    for unav in e.unavailabilities:
                        if unav.overlaps(shift.time_range): can_work = False
                    for assigned in e.assigned_shifts:
                        if assigned.time_range.overlaps(shift.time_range): can_work = False
                        if assigned.time_range.start.date() == shift.time_range.start.date(): can_work = False
                        if assigned.time_range.end <= shift.time_range.start:
                            if (shift.time_range.start - assigned.time_range.end).total_seconds() / 3600.0 < 11: can_work = False
                        elif shift.time_range.end <= assigned.time_range.start:
                            if (assigned.time_range.start - shift.time_range.end).total_seconds() / 3600.0 < 11: can_work = False
                            
                    if can_work:
                        fallback_candidates.append(e)
                
                if fallback_candidates:
                    fallback_candidates.sort(key=lambda e: e.worked_hours)
                    chosen = fallback_candidates[0]
                    self._assign(chosen, shift)
                    self.logs.append(f"[KRYTYCZNE - WYMUSZONO] Awaryjnie przypisano pracownika {chosen.first_name} {chosen.last_name} do zmiany '{shift.shift_type}' ({shift.time_range.start.date()}), ignorując docelowe czasy pracy.")
                else:
                    date_str = shift.time_range.start.strftime("%Y-%m-%d")
                    self.logs.append(f"[KRYTYCZNE - FATAL] Nie można obsadzić zmiany '{shift.shift_type}' w dniu {date_str} – brak dostępnego personelu (wszyscy zablokowani twardymi ograniczeniami).")

        # Mapowanie z powrotem do docelowego formatu JSON
        final_schedule_for_json = {}
        curr = start_date
        while curr <= end_date:
            date_str = curr.isoformat()
            final_schedule_for_json[date_str] = {}
            
            holiday_info = self.holidays_map.get(date_str, {})
            if holiday_info.get("is_closed"):
                final_schedule_for_json[date_str] = {"is_closed": True}
                curr += timedelta(days=1)
                continue
            
            for shift_type in [schemas.ShiftType.MORNING.value, schemas.ShiftType.MIDDLE.value, schemas.ShiftType.CLOSING.value]:
                emp_list = []
                for emp in self.employees:
                    for assigned in emp.assigned_shifts:
                        if assigned.time_range.start.date() == curr and assigned.shift_type == shift_type:
                            emp_list.append({
                                "id": emp.id,
                                "first_name": emp.first_name,
                                "last_name": emp.last_name
                            })
                
                if emp_list:
                    start_str, end_str = resolve_shift_hours(curr, shift_type, self.store_settings, self.holidays_map)
                    final_schedule_for_json[date_str][shift_type] = {
                        "start_time": start_str,
                        "end_time": end_str,
                        "employees": emp_list
                    }

            vacation_employees = []
            for v in self.db_vacations:
                if v["start_date"].date() <= curr <= v["end_date"].date():
                    uid = str(v["user_id"])
                    emp_data = next((e for e in self.db_employees if str(e["_id"]) == uid), None)
                    if emp_data:
                        vacation_employees.append({
                            "id": uid,
                            "first_name": emp_data.get("first_name", ""),
                            "last_name": emp_data.get("last_name", "")
                        })
            
            if vacation_employees:
                final_schedule_for_json[date_str]["vacations"] = {
                    "employees": vacation_employees,
                    "is_vacation": True
                }

            curr += timedelta(days=1)

        draft_document = {
            "franchise_code": self.franchise_code,
            "start_date": datetime.combine(start_date, time.min),
            "end_date": datetime.combine(end_date, time.min),
            "schedule": final_schedule_for_json,
            "conflicts": self.logs,
            "status": "DRAFT",
            "created_at": datetime.utcnow()
        }
        result = await self.db.schedule_drafts.insert_one(draft_document)
        logger.info(f"Zapisano nowy grafik roboczy z architekturą Dataclass dla {self.franchise_code} z ID: {result.inserted_id}")

async def generate_schedule_for_period(db: motor.motor_asyncio.AsyncIOMotorDatabase, current_user: dict, year: int, month: int):
    franchise_code = current_user.get("franchise_code")
    if not franchise_code:
        raise ValueError("Użytkownik nie jest przypisany do żadnego sklepu.")

    await db.schedule_drafts.delete_many({"franchise_code": franchise_code})
    
    start_date = date(year, month, 1)
    _, num_days = calendar.monthrange(year, month)
    end_date = date(year, month, num_days)

    generator = ScheduleGenerator(db, current_user)
    await generator.generate(start_date, end_date)