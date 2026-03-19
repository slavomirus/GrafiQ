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

from .. import models, schemas
from .schedule_service import get_store_settings_and_holidays, resolve_shift_hours

logger = logging.getLogger(__name__)

POLISH_HOLIDAYS = [(1, 1), (1, 6), (5, 1), (5, 3), (8, 15), (11, 1), (11, 11), (12, 25), (12, 26)]
PROMO_REFERENCE_DATE = date(2025, 9, 23)

class ScheduleGenerator:
    def __init__(self, db: motor.motor_asyncio.AsyncIOMotorDatabase, current_user: dict):
        self.db = db
        self.franchise_code = current_user.get("franchise_code")
        self.franchisee = current_user
        self.employees: List[Dict[str, Any]] = []
        self.store_settings: Dict[str, Any] = {}
        self.holidays_map: Dict[str, Any] = {}
        
        self.vacations: Dict[ObjectId, List[date]] = defaultdict(list)
        self.availabilities: Dict[ObjectId, Dict[date, Any]] = defaultdict(dict)
        
        self.schedule: Dict[date, Dict[str, List[ObjectId]]] = defaultdict(lambda: defaultdict(list))
        
        self.employee_state: Dict[ObjectId, Dict[str, Any]] = defaultdict(lambda: {
            "hours": 0.0
        })
        
        self.employee_map: Dict[str, Dict[str, Any]] = {}
        self.employees_by_id: Dict[ObjectId, Dict[str, Any]] = {}

    async def _gather_data(self, start_date: date, end_date: date):
        logger.info(f"Rozpoczynanie zbierania danych dla grafiku ({self.franchise_code}) od {start_date} do {end_date}")
        
        self.store_settings, self.holidays_map = await get_store_settings_and_holidays(self.db, self.franchise_code)
        
        self.employees = await self.db.users.find({
            "franchise_code": self.franchise_code,
            "role": models.UserRole.EMPLOYEE.value,
            "status": models.UserStatus.ACTIVE.value
        }).to_list(length=None)
        
        if not self.employees:
            raise ValueError("Brak aktywnych pracowników do wygenerowania grafiku.")
        
        for emp in self.employees:
            self.employee_map[str(emp["_id"])] = {
                "id": str(emp["_id"]),
                "first_name": emp.get("first_name", ""),
                "last_name": emp.get("last_name", "")
            }
            self.employees_by_id[emp["_id"]] = emp

        self.employee_map[str(self.franchisee["_id"])] = {
            "id": str(self.franchisee["_id"]),
            "first_name": self.franchisee.get("first_name", ""),
            "last_name": self.franchisee.get("last_name", "")
        }
        self.employees_by_id[self.franchisee["_id"]] = self.franchisee

        employee_ids = [emp["_id"] for emp in self.employees]
        employee_ids.append(self.franchisee["_id"])

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

        avail_cursor = self.db.availability.find({
            "user_id": {"$in": employee_ids},
            "date": {"$gte": start_datetime, "$lte": end_datetime}
        })
        async for av in avail_cursor:
            d = av["date"].date()
            self.availabilities[av["user_id"]][d] = av

    def _parse_time(self, t_str: str) -> time:
        if isinstance(t_str, time):
            return t_str
        try:
            return datetime.strptime(t_str, "%H:%M").time()
        except ValueError:
            try:
                return datetime.strptime(t_str, "%H:%M:%S").time()
            except ValueError:
                return time(0, 0)

    def _get_shift_datetime_range(self, date_obj: date, shift_name: str) -> Tuple[datetime, datetime]:
        start_str, end_str = resolve_shift_hours(date_obj, shift_name, self.store_settings, self.holidays_map)
        
        start_t = self._parse_time(start_str)
        end_t = self._parse_time(end_str)
        
        start_dt = datetime.combine(date_obj, start_t)
        end_dt = datetime.combine(date_obj, end_t)
        # Fix nocnych zmian
        if end_dt <= start_dt: 
            end_dt += timedelta(days=1)
        return start_dt, end_dt

    def _is_working_on_day(self, emp_id: ObjectId, date_obj: date) -> bool:
        if date_obj not in self.schedule: return False
        for shift_list in self.schedule[date_obj].values():
            if emp_id in shift_list: return True
        return False

    def _get_consecutive_work_days(self, emp_id: ObjectId, center_date: date) -> int:
        consecutive = 1
        d = center_date - timedelta(days=1)
        while self._is_working_on_day(emp_id, d):
            consecutive += 1
            d -= timedelta(days=1)
        d = center_date + timedelta(days=1)
        while self._is_working_on_day(emp_id, d):
            consecutive += 1
            d += timedelta(days=1)
        return consecutive

    def _check_rest_period(self, emp_id: ObjectId, current_start: datetime, current_end: datetime) -> bool:
        current_date = current_start.date()
        
        # Wczoraj
        prev_date = current_date - timedelta(days=1)
        if prev_date in self.schedule:
            for s_name, employees in self.schedule[prev_date].items():
                if emp_id in employees:
                    _, s_end = self._get_shift_datetime_range(prev_date, s_name)
                    if (current_start - s_end).total_seconds() / 3600.0 < 11: # ZMIANA: Zazwyczaj 11h
                        return False

        # Dzisiaj
        if current_date in self.schedule:
            for s_name, employees in self.schedule[current_date].items():
                if emp_id in employees:
                    return False 

        # Jutro
        next_date = current_date + timedelta(days=1)
        if next_date in self.schedule:
            for s_name, employees in self.schedule[next_date].items():
                if emp_id in employees:
                    s_start, _ = self._get_shift_datetime_range(next_date, s_name)
                    if (s_start - current_end).total_seconds() / 3600.0 < 11: # ZMIANA: Zazwyczaj 11h
                        return False
                        
        return True

    def _check_weekly_rest(self, emp_id: ObjectId, date_obj: date, shift_name: str) -> bool:
        start_of_week = date_obj - timedelta(days=date_obj.weekday())
        end_of_week = start_of_week + timedelta(days=6)
        
        intervals = []
        
        curr = start_of_week
        while curr <= end_of_week:
            if curr in self.schedule:
                for s_name, employees in self.schedule[curr].items():
                    if emp_id in employees:
                        s_start, s_end = self._get_shift_datetime_range(curr, s_name)
                        intervals.append((s_start, s_end))
            curr += timedelta(days=1)
            
        new_start, new_end = self._get_shift_datetime_range(date_obj, shift_name)
        intervals.append((new_start, new_end))
        
        intervals.sort(key=lambda x: x[0])
        
        max_gap = 0.0
        week_start_dt = datetime.combine(start_of_week, time.min)
        week_end_dt = datetime.combine(end_of_week, time.max)
        
        if intervals:
            gap = (intervals[0][0] - week_start_dt).total_seconds() / 3600.0
            if gap > max_gap: max_gap = gap
            
            for i in range(len(intervals) - 1):
                gap = (intervals[i+1][0] - intervals[i][1]).total_seconds() / 3600.0
                if gap > max_gap: max_gap = gap
                
            gap = (week_end_dt - intervals[-1][1]).total_seconds() / 3600.0
            if gap > max_gap: max_gap = gap
        else:
            return True
            
        return max_gap >= 35

    def _check_hard_constraints(self, emp_id: ObjectId, date_obj: date, shift_name: str) -> bool:
        if date_obj in self.vacations.get(emp_id, []):
            return False

        if self._get_consecutive_work_days(emp_id, date_obj) > 6: # Standard Kodeksu to 6 dni
            return False

        start_dt, end_dt = self._get_shift_datetime_range(date_obj, shift_name)
        
        # Jeśli zmiana zwraca ramy zamknięte lub błędne
        if (end_dt - start_dt).total_seconds() <= 0:
            return False

        duration = (end_dt - start_dt).total_seconds() / 3600.0
        if duration > 12: # Standardowo max 12 w handlu
            return False

        if not self._check_rest_period(emp_id, start_dt, end_dt):
            return False
            
        if not self._check_weekly_rest(emp_id, date_obj, shift_name):
            return False

        has_availability = False
        if emp_id in self.availabilities and date_obj in self.availabilities[emp_id]:
            has_availability = True
            av = self.availabilities[emp_id][date_obj]
            p_type = av.get("period_type", "").lower()
            
            # KROK 1: Twarda Blokada (Dni Wolne / Dyspozycje "W")
            if p_type in ["wolne", "urlop", "niedostępny", "unavailable", "day_off", "w", "off"]:
                return False
            
            # Część KROKU 3: Blokada przypisania do innej zmiany, jeśli pracownik poprosił o konkretną
            mapped_p_type = p_type
            if p_type in ["rano", "morning"]: mapped_p_type = schemas.ShiftType.MORNING.value
            elif p_type in ["środek", "middle"]: mapped_p_type = schemas.ShiftType.MIDDLE.value
            elif p_type in ["wieczór", "zamknięcie", "closing"]: mapped_p_type = schemas.ShiftType.CLOSING.value
            
            if mapped_p_type in [schemas.ShiftType.MORNING.value, schemas.ShiftType.MIDDLE.value, schemas.ShiftType.CLOSING.value]:
                if shift_name != mapped_p_type:
                    return False
            
            if av.get("start_time") is not None and av.get("end_time") is not None:
                av_start = self._parse_time(av["start_time"])
                av_end = self._parse_time(av["end_time"])
                
                shift_start_t = start_dt.time()
                shift_end_t = end_dt.time()
                
                if shift_start_t < av_start or shift_end_t > av_end:
                    return False

        # KROK 2: Blokada z Preferencji Dni (pn-pt / sb-nd) jeśli NIE MA dyspozycji
        if not has_availability:
            emp = self.employees_by_id.get(emp_id)
            if emp:
                prefs = emp.get("preferences", {}) or {}
                day_pref = prefs.get("day_preference")
                is_weekend = date_obj.weekday() >= 5
                
                if day_pref == schemas.DayPreference.WEEKDAYS.value and is_weekend:
                    return False
                if day_pref == schemas.DayPreference.WEEKENDS.value and not is_weekend:
                    return False

        return True

    def _assign_employee(self, emp_id: ObjectId, date_obj: date, shift_name: str):
        self.schedule[date_obj][shift_name].append(emp_id)
        
        start_dt, end_dt = self._get_shift_datetime_range(date_obj, shift_name)
        duration = (end_dt - start_dt).total_seconds() / 3600.0
        self.employee_state[emp_id]["hours"] += duration

    def _calculate_score(self, emp_id: ObjectId, day: date, shift_name: str) -> float:
        score = 0.0
        emp = self.employees_by_id.get(emp_id)
        if not emp: return 0
        
        has_specific_shift_request = False
        
        # KROK 3: Nadpisywanie Zmian (Dyspozycja > Preferencja)
        if emp_id in self.availabilities and day in self.availabilities[emp_id]:
            av = self.availabilities[emp_id][day]
            p_type = av.get("period_type", "").lower()
            
            mapped_p_type = p_type
            if p_type in ["rano", "morning"]: mapped_p_type = schemas.ShiftType.MORNING.value
            elif p_type in ["środek", "middle"]: mapped_p_type = schemas.ShiftType.MIDDLE.value
            elif p_type in ["wieczór", "zamknięcie", "closing"]: mapped_p_type = schemas.ShiftType.CLOSING.value
            
            if mapped_p_type in [schemas.ShiftType.MORNING.value, schemas.ShiftType.MIDDLE.value, schemas.ShiftType.CLOSING.value]:
                has_specific_shift_request = True
                if mapped_p_type == shift_name:
                    score += 1000  # Maksymalny priorytet
                    return score

        prefs = emp.get("preferences", {}) or {}
        
        # Ignoruj preferred_shifts jeśli pracownik złożył dyspozycję na konkretną zmianę tego dnia
        if not has_specific_shift_request:
            if shift_name in prefs.get("preferred_shifts", []):
                score += 10
            
        day_pref = prefs.get("day_preference")
        is_weekend = day.weekday() >= 5
        if day_pref == schemas.DayPreference.WEEKENDS.value and is_weekend:
            score += 5
        elif day_pref == schemas.DayPreference.WEEKDAYS.value and not is_weekend:
            score += 5
            
        return score

    async def generate(self, start_date: date, end_date: date):
        await self._gather_data(start_date, end_date)
        conflicts: List[str] = []
        
        # Przetasowanie bazy kandydatów na start, by zapewnić różnorodność ("solver" niedeterministyczny)
        random.shuffle(self.employees)
        
        uop_employees = [e for e in self.employees if e.get("contract_type") == models.ContractType.UOP.value]
        uz_employees = [e for e in self.employees if e.get("contract_type") == models.ContractType.UZ.value]
        
        targets = {}
        for e in self.employees:
            if e.get("contract_type") == models.ContractType.UOP.value:
                targets[e["_id"]] = 160.0 * e.get("fte", 1.0)
            else:
                targets[e["_id"]] = float(e.get("monthly_hours_target", 100))

        global_critical_shifts = []
        global_standard_shifts = []
        
        curr = start_date
        while curr <= end_date:
            date_str = curr.strftime("%Y-%m-%d")
            
            holiday_info = self.holidays_map.get(date_str, {})
            if holiday_info.get("is_closed"):
                self.schedule[curr]["is_closed"] = True 
                curr += timedelta(days=1)
                continue

            is_promo_change_day = (curr - PROMO_REFERENCE_DATE).days % 14 == 0
            closing_needs = self.store_settings.get("employees_on_promo_change", 2) if is_promo_change_day else self.store_settings.get("employees_per_closing_shift", 1)
            
            # HARD CONSTRAINT: Wymuszamy minimum 1 osobę na otwarciu i zamknięciu
            needs = {
                schemas.ShiftType.MORNING.value: max(1, int(self.store_settings.get("employees_per_morning_shift", 1))),
                schemas.ShiftType.MIDDLE.value: max(0, int(self.store_settings.get("employees_per_middle_shift", 0))),
                schemas.ShiftType.CLOSING.value: max(1, int(closing_needs))
            }
            
            for shift_name, count in needs.items():
                for _ in range(count):
                    shift_info = {"date": curr, "name": shift_name}
                    if shift_name in [schemas.ShiftType.MORNING.value, schemas.ShiftType.CLOSING.value]:
                        global_critical_shifts.append(shift_info)
                    else:
                        global_standard_shifts.append(shift_info)
            curr += timedelta(days=1)

        # Mieszamy nieznacznie kolejność przydzielania zmian krytycznych tego samego dnia,
        # co też doda więcej losowości na krawędzi dostępności pracowników
        day_critical_shifts = defaultdict(list)
        for shift in global_critical_shifts:
            day_critical_shifts[shift["date"]].append(shift)
            
        global_critical_shifts_shuffled = []
        for d in sorted(day_critical_shifts.keys()):
            shifts_today = day_critical_shifts[d]
            random.shuffle(shifts_today)
            global_critical_shifts_shuffled.extend(shifts_today)

        day_standard_shifts = defaultdict(list)
        for shift in global_standard_shifts:
            day_standard_shifts[shift["date"]].append(shift)
            
        global_standard_shifts_shuffled = []
        for d in sorted(day_standard_shifts.keys()):
            shifts_today = day_standard_shifts[d]
            random.shuffle(shifts_today)
            global_standard_shifts_shuffled.extend(shifts_today)

        # Generowanie zmian krytycznych
        for shift in global_critical_shifts_shuffled:
            current_date = shift["date"]
            shift_name = shift["name"]
            day_str = current_date.isoformat()
            
            start_dt, end_dt = self._get_shift_datetime_range(current_date, shift_name)
            if (end_dt - start_dt).total_seconds() <= 0:
                continue

            candidates = []
            group1 = [e["_id"] for e in uop_employees if self.employee_state[e["_id"]]["hours"] < targets[e["_id"]]]
            candidates.extend([(uid, 1) for uid in group1])
            group2 = [e["_id"] for e in uz_employees if self.employee_state[e["_id"]]["hours"] < targets[e["_id"]] * 1.2]
            candidates.extend([(uid, 2) for uid in group2])
            candidates.append((self.franchisee["_id"], 3))
            group4 = [e["_id"] for e in self.employees if e["_id"] not in group1 and e["_id"] not in group2]
            candidates.extend([(uid, 4) for uid in group4])

            valid_candidates = []
            for uid, priority in candidates:
                if self._is_working_on_day(uid, current_date): continue
                if self._check_hard_constraints(uid, current_date, shift_name):
                    score = self._calculate_score(uid, current_date, shift_name)
                    # Wprowadzamy szum losowy, aby solver nie był deterministyczny
                    score += random.uniform(-3.0, 3.0)
                    valid_candidates.append((uid, priority, score))
            
            valid_candidates.sort(key=lambda x: (x[1], -x[2], random.random()))
            
            if valid_candidates:
                selected_uid = valid_candidates[0][0]
                self._assign_employee(selected_uid, current_date, shift_name)
            else:
                # FALLBACK: HARD CONSTRAINT - Nie wolno pominąć zmiany zamykającej/otwierającej. 
                # Znajdź pracownika, który dziś nie pracuje i nie ma urlopu
                emergency_candidates = []
                for uid, priority in candidates:
                    if not self._is_working_on_day(uid, current_date):
                        if current_date not in self.vacations.get(uid, []):
                            emergency_candidates.append(uid)
                
                if emergency_candidates:
                    # Sortujemy po najmniejszej liczbie przepracowanych dotychczas godzin
                    emergency_candidates.sort(key=lambda u: self.employee_state[u]["hours"])
                    selected_uid = emergency_candidates[0]
                    self._assign_employee(selected_uid, current_date, shift_name)
                    conflicts.append(f"[KRYTYCZNE - WYMUSZONO] Dnia {day_str} awaryjnie przypisano pracownika na zmianę '{shift_name}', łamiąc miękkie reguły odpoczynku/preferencji, by zapewnić obsadę.")
                else:
                    conflicts.append(f"[KRYTYCZNE - FATAL] Dnia {day_str} CAŁKOWITY BRAK pracownika na zmianę '{shift_name}'. Wszyscy są przypisani lub mają urlop.")

        # Generowanie zmian standardowych
        for shift in global_standard_shifts_shuffled:
            current_date = shift["date"]
            shift_name = shift["name"]
            day_str = current_date.isoformat()
            
            start_dt, end_dt = self._get_shift_datetime_range(current_date, shift_name)
            if (end_dt - start_dt).total_seconds() <= 0:
                continue

            candidates = []
            group1 = [e["_id"] for e in uop_employees if self.employee_state[e["_id"]]["hours"] < targets[e["_id"]]]
            candidates.extend([(uid, 1) for uid in group1])
            group2 = [e["_id"] for e in uz_employees if self.employee_state[e["_id"]]["hours"] < targets[e["_id"]] * 1.2]
            candidates.extend([(uid, 2) for uid in group2])
            candidates.append((self.franchisee["_id"], 3))
            group4 = [e["_id"] for e in self.employees if e["_id"] not in group1 and e["_id"] not in group2]
            candidates.extend([(uid, 4) for uid in group4])

            valid_candidates = []
            for uid, priority in candidates:
                if self._is_working_on_day(uid, current_date): continue
                if self._check_hard_constraints(uid, current_date, shift_name):
                    score = self._calculate_score(uid, current_date, shift_name)
                    score += random.uniform(-3.0, 3.0)
                    valid_candidates.append((uid, priority, score))
            
            valid_candidates.sort(key=lambda x: (x[1], -x[2], random.random()))
            
            if valid_candidates:
                selected_uid = valid_candidates[0][0]
                self._assign_employee(selected_uid, current_date, shift_name)
            else:
                # W zmianach standardowych też używamy fallbacku
                emergency_candidates = []
                for uid, priority in candidates:
                    if not self._is_working_on_day(uid, current_date):
                        if current_date not in self.vacations.get(uid, []):
                            emergency_candidates.append(uid)
                
                if emergency_candidates:
                    emergency_candidates.sort(key=lambda u: self.employee_state[u]["hours"])
                    selected_uid = emergency_candidates[0]
                    self._assign_employee(selected_uid, current_date, shift_name)
                    conflicts.append(f"[WYMUSZONO] Dnia {day_str} awaryjnie przypisano pracownika na zmianę '{shift_name}'.")
                else:
                    conflicts.append(f"Dnia {day_str} brakuje pracownika na zmianie '{shift_name}'.")

        # BUDOWA KOŃCOWEGO JSONA
        final_schedule_for_json = {}
        curr = start_date
        while curr <= end_date:
            date_str = curr.isoformat()
            final_schedule_for_json[date_str] = {}
            
            if getattr(self.schedule.get(curr, {}), "get", lambda k,d: d)("is_closed", False):
                final_schedule_for_json[date_str] = {"is_closed": True}
                
                # ZADANIE: Logowanie dni zamkniętych
                if curr.weekday() == 6 or date_str in self.holidays_map:
                     logger.info(f"✅ DEBUG GENERATORA - Zapisano zamknięty dzień ({date_str}): {final_schedule_for_json[date_str]}")
                
                curr += timedelta(days=1)
                continue
                
            shifts = self.schedule.get(curr, {})
            for shift_name, emp_ids in shifts.items():
                if not emp_ids or shift_name == "is_closed": continue
                
                # Używamy uniwersalnego resolwera z schedule_service
                start_str, end_str = resolve_shift_hours(curr, shift_name, self.store_settings, self.holidays_map)
                
                employees_data = []
                for eid in emp_ids:
                    emp_data = self.employee_map.get(str(eid))
                    if emp_data: employees_data.append(emp_data)
                    else: employees_data.append({"id": str(eid), "first_name": "Unknown", "last_name": "User"})
                    
                final_schedule_for_json[date_str][shift_name] = {
                    "start_time": start_str,
                    "end_time": end_str,
                    "employees": employees_data
                }
                
            # ZADANIE: Logowanie gotowego obiektu dla dni specjalnych przed zapisem
            if curr.weekday() == 6 or date_str in self.holidays_map:
                logger.info(f"✅ DEBUG GENERATORA - Zmiany w dzień specjalny ({date_str}): {final_schedule_for_json[date_str]}")

            vacation_employees = []
            all_ids = set(self.employees_by_id.keys())
            for uid in all_ids:
                if curr in self.vacations.get(uid, []):
                    emp_data = self.employee_map.get(str(uid))
                    if emp_data: vacation_employees.append(emp_data)
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
            "conflicts": conflicts,
            "status": "DRAFT",
            "created_at": datetime.utcnow()
        }
        result = await self.db.schedule_drafts.insert_one(draft_document)
        logger.info(f"Zapisano nowy grafik roboczy dla {self.franchise_code} z ID: {result.inserted_id}")

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