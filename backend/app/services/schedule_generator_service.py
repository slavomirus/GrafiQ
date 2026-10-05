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
from .schedule_service import get_store_settings_and_holidays, resolve_shift_hours, STATUTORY_COMMERCIAL_SUNDAYS

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
    is_uop: bool = False
    is_franchisee: bool = False
    store_roles: List[str] = field(default_factory=list)
    
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

    def _parse_date_safe(self, d_val) -> Optional[date]:
        if isinstance(d_val, datetime): return d_val.date()
        if isinstance(d_val, date): return d_val
        if isinstance(d_val, str):
            try: return datetime.strptime(d_val[:10], "%Y-%m-%d").date()
            except: pass
        return None

    async def _gather_data(self, start_date: date, end_date: date):
        self.store_settings, self.holidays_map = await get_store_settings_and_holidays(self.db, self.franchise_code)
        
        self.db_employees = await self.db.users.find({
            "franchise_code": self.franchise_code,
            "role": models.UserRole.EMPLOYEE.value,
            "status": models.UserStatus.ACTIVE.value
        }).to_list(length=None)
        
        # Właściciel (ajent) też jest uwzględniany na potrzeby grafiku
        if self.franchisee:
            f_id_str = str(self.franchisee.get("_id"))
            if not any(str(e.get("_id")) == f_id_str for e in self.db_employees):
                self.db_employees.append(self.franchisee)

        employee_ids = [emp["_id"] for emp in self.db_employees]
        employee_ids_str = [str(eid) for eid in employee_ids]
        employee_ids_obj = [ObjectId(eid) if not isinstance(eid, ObjectId) and ObjectId.is_valid(str(eid)) else eid for eid in employee_ids]
        all_emp_identifiers = list(set(employee_ids + employee_ids_str + employee_ids_obj))

        start_datetime = datetime.combine(start_date, time.min)
        end_datetime = datetime.combine(end_date, time.max)
        start_date_str = start_date.strftime("%Y-%m-%d")
        end_date_str = end_date.strftime("%Y-%m-%d")

        # Pobieranie urlopów z odpornością na typy ObjectId/str oraz formaty dat
        self.db_vacations = await self.db.vacations.find({
            "user_id": {"$in": all_emp_identifiers},
            "status": {"$in": ["approved", "APPROVED", models.VacationStatus.APPROVED.value]},
            "$or": [
                {"start_date": {"$lte": end_datetime}, "end_date": {"$gte": start_datetime}},
                {"start_date": {"$lte": end_date_str}, "end_date": {"$gte": start_date_str}}
            ]
        }).to_list(length=None)

        # Pobieranie zwolnień lekarskich / L4 z kolekcji leaves
        try:
            db_leaves = await self.db.leaves.find({
                "user_id": {"$in": all_emp_identifiers},
                "$or": [
                    {"start_date": {"$lte": end_datetime}, "end_date": {"$gte": start_datetime}},
                    {"start_date": {"$lte": end_date_str}, "end_date": {"$gte": start_date_str}}
                ]
            }).to_list(length=None)
            self.db_vacations.extend(db_leaves)
        except Exception as e:
            logger.warning(f"Nie udało się pobrać leaves: {e}")

        # Pobieranie dyspozycji
        self.db_availabilities = await self.db.availability.find({
            "user_id": {"$in": all_emp_identifiers},
            "$or": [
                {"date": {"$gte": start_datetime, "$lte": end_datetime}},
                {"date": {"$gte": start_date_str, "$lte": end_date_str}}
            ]
        }).to_list(length=None)

        prev_date = start_date - timedelta(days=1)
        # Próbujemy pobrać grafik z poprzedniego miesiąca/tygodnia, by sprawdzić przerwę dobową na styku grafików
        self.db_prev_schedule = await self.db.schedules.find_one({
            "franchise_code": self.franchise_code,
            f"schedule.{prev_date.isoformat()}": {"$exists": True}
        })

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
            d_start = self._parse_date_safe(v.get("start_date"))
            d_end = self._parse_date_safe(v.get("end_date")) or d_start
            if not d_start or not d_end: continue
            
            uid = str(v.get("user_id"))
            curr_v = d_start
            while curr_v <= d_end:
                if start_date <= curr_v <= end_date:
                    dt_start = datetime.combine(curr_v, time.min)
                    dt_end = datetime.combine(curr_v, time.max)
                    vacations_by_user[uid].append(TimeRange(dt_start, dt_end))
                curr_v += timedelta(days=1)

        avail_by_user = defaultdict(lambda: defaultdict(list))
        for a in self.db_availabilities:
            d = self._parse_date_safe(a.get("date"))
            if not d: continue
            uid = str(a.get("user_id"))
            avail_by_user[uid][d].append(a)

        prev_assigned_by_user = defaultdict(list)
        if hasattr(self, 'db_prev_schedule') and self.db_prev_schedule:
            prev_date = start_date - timedelta(days=1)
            prev_date_str = prev_date.isoformat()
            prev_day_data = self.db_prev_schedule.get("schedule", {}).get(prev_date_str, {})
            
            for shift_type, shift_info in prev_day_data.items():
                if shift_type in ["morning", "middle", "closing"] and isinstance(shift_info, dict):
                    start_str = shift_info.get("start_time", "")
                    end_str = shift_info.get("end_time", "")
                    if not start_str or not end_str: continue
                    
                    try:
                        s_dt = datetime.combine(prev_date, self._parse_time(start_str))
                        e_dt = datetime.combine(prev_date, self._parse_time(end_str))
                        if e_dt <= s_dt: e_dt += timedelta(days=1)
                        
                        for e_data in shift_info.get("employees", []):
                            uid = str(e_data.get("id"))
                            prev_assigned_by_user[uid].append(ShiftDemand(
                                id=f"prev_{shift_type}",
                                time_range=TimeRange(s_dt, e_dt),
                                required_employees=1,
                                shift_type=shift_type
                            ))
                    except Exception:
                        pass

        for db_emp in self.db_employees:
            is_franchisee = str(db_emp.get("_id")) == str(self.franchisee.get("_id"))
            
            raw_contract = str(db_emp.get("contract_type", "UZ")).strip()
            is_uop = raw_contract.lower() in [
                "uop", "umowa o pracę", "umowa o prace", 
                models.ContractType.UOP.value.lower()
            ]
            
            if is_franchisee:
                raw_fh = self.store_settings.get("franchisee_monthly_hours")
                fte_or_target = float(raw_fh) if raw_fh is not None and raw_fh != '' else 0.0
                contract = "FRANCHISEE"
            elif is_uop:
                fte_or_target = float(db_emp.get("fte", 1.0))
                contract = "UOP"
            else:
                fte_or_target = float(db_emp.get("monthly_hours_target", 120))
                contract = "UZ"

            raw_prefs = db_emp.get("preferences", {}) or {}
            normalized_prefs = self._normalize_preferences(raw_prefs)

            emp = Employee(
                id=str(db_emp["_id"]),
                db_id=db_emp["_id"],
                first_name=db_emp.get("first_name", ""),
                last_name=db_emp.get("last_name", ""),
                contract_type=contract,
                fte_or_target=fte_or_target,
                is_uop=is_uop,
                is_franchisee=is_franchisee,
                store_roles=db_emp.get("store_roles", []),
                preferences=normalized_prefs
            )

            emp_id_str = str(db_emp["_id"])

            # 1. Unavailabilities z Urlopów (Bezwzględna ochrona)
            emp.unavailabilities.extend(vacations_by_user[emp_id_str])
            
            # 1a. Zmiany z dnia poprzedzającego wygenerowany grafik (dla 11h break)
            emp.assigned_shifts.extend(prev_assigned_by_user[emp_id_str])
            
            # 2. Unavailabilities i Requesty z Dyspozycyjności
            for d, avail_list in avail_by_user[emp_id_str].items():
                for a in avail_list:
                    p_type = str(a.get("period_type", "")).lower().strip()
                    start_t = a.get("start_time")
                    end_t = a.get("end_time")
                    
                    # 1. Wyraźne zgłoszenie WOLNEGO / NIEDOSTĘPNOŚCI
                    if p_type in ["wolne", "urlop", "niedostępny", "unavailable", "day_off", "w", "off", "l4", "remove"]:
                        dt_start = datetime.combine(d, time.min)
                        dt_end = datetime.combine(d, time.max)
                        if start_t and end_t:
                            dt_start = datetime.combine(d, self._parse_time(start_t))
                            dt_end = datetime.combine(d, self._parse_time(end_t))
                            if dt_end <= dt_start: dt_end += timedelta(days=1)
                        emp.unavailabilities.append(TimeRange(dt_start, dt_end))
                    else:
                        # 2. Zgłoszenie dyspozycji na konkretną zmianę
                        mapped = None
                        if p_type in ["rano", "morning", "r", "ranki", schemas.ShiftType.MORNING.value]: 
                            mapped = schemas.ShiftType.MORNING.value
                        elif p_type in ["środek", "middle", "m", "pośrednia", "zmiana środkowa", schemas.ShiftType.MIDDLE.value]: 
                            mapped = schemas.ShiftType.MIDDLE.value
                        elif p_type in ["wieczór", "zamknięcie", "closing", "z", "zamknięcia", "zetki", schemas.ShiftType.CLOSING.value]: 
                            mapped = schemas.ShiftType.CLOSING.value
                        elif "|" in p_type or "\n" in p_type:
                            if "mid" in p_type or "middle" in p_type: mapped = schemas.ShiftType.MIDDLE.value
                            elif "morn" in p_type or "r" in p_type: mapped = schemas.ShiftType.MORNING.value
                            elif "clos" in p_type or "z" in p_type: mapped = schemas.ShiftType.CLOSING.value
                        
                        if mapped:
                            emp.requested_shifts[d] = mapped
                            start_str, end_str = resolve_shift_hours(d, mapped, self.store_settings, self.holidays_map)
                            av_start = datetime.combine(d, self._parse_time(start_str))
                            av_end = datetime.combine(d, self._parse_time(end_str))
                            if av_end <= av_start: av_end += timedelta(days=1)
                            
                            # Czas PRZED dostępnością
                            if av_start > datetime.combine(d, time.min):
                                emp.unavailabilities.append(TimeRange(datetime.combine(d, time.min), av_start))
                            # Czas PO dostępności
                            if av_end < datetime.combine(d, time.max):
                                emp.unavailabilities.append(TimeRange(av_end, datetime.combine(d, time.max)))

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
            
            is_sunday = curr.weekday() == 6
            opening_hours = self.store_settings.get("opening_hours", {})
            if hasattr(opening_hours, "dict"): opening_hours = opening_hours.dict(by_alias=True)
            is_commercial_sun = opening_hours.get("is_commercial_sunday", False) and curr in STATUTORY_COMMERCIAL_SUNDAYS
            
            middle_count = int(self.store_settings.get("employees_per_middle_shift", 0))
            if is_sunday and not is_commercial_sun:
                # W niedziele niehandlowe nie tworzymy międzyzmiany chyba że jest jawnie wymagana
                middle_count = max(0, middle_count)
            
            needs = {
                schemas.ShiftType.MORNING.value: max(1, int(self.store_settings.get("employees_per_morning_shift", 1))),
                schemas.ShiftType.MIDDLE.value: max(0, middle_count),
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

    def _normalize_shift_name(self, name: str) -> Optional[str]:
        """Normalizuje nazwę zmiany z różnych formatów do wartości enum ShiftType."""
        if not name or not isinstance(name, str):
            return None
        n = name.lower().strip()
        if n in ["morning", "rano", "poranna", "ranki", "zmiana poranna"]:
            return schemas.ShiftType.MORNING.value
        if n in ["middle", "środek", "pośrednia", "zmiana środkowa"]:
            return schemas.ShiftType.MIDDLE.value
        if n in ["closing", "wieczór", "zamykająca", "zamknięcia", "zamknięcie", "zetki", "zmiana zamykająca"]:
            return schemas.ShiftType.CLOSING.value
        # Spróbuj dopasować bezpośrednio do wartości enuma
        for st in schemas.ShiftType:
            if st.value == n:
                return st.value
        return None

    def _normalize_preferences(self, raw_prefs: dict) -> dict:
        """Normalizuje surowe preferencje z bazy danych do spójnego formatu."""
        normalized = dict(raw_prefs)

        # Normalizacja preferred_shifts
        raw_shifts = raw_prefs.get("preferred_shifts", [])
        if isinstance(raw_shifts, list):
            norm_shifts = []
            for s in raw_shifts:
                mapped = self._normalize_shift_name(s)
                if mapped and mapped not in norm_shifts:
                    norm_shifts.append(mapped)
            normalized["preferred_shifts"] = norm_shifts
        else:
            normalized["preferred_shifts"] = []

        # Normalizacja day_preference
        raw_day = raw_prefs.get("day_preference") or raw_prefs.get("preferred_days")
        if isinstance(raw_day, list):
            raw_day = raw_day[0] if raw_day else None
        if raw_day and isinstance(raw_day, str):
            d = raw_day.lower().strip()
            if d in ["pn-pt", "weekdays", "dni robocze"]:
                normalized["day_preference"] = schemas.DayPreference.WEEKDAYS.value
            elif d in ["sb-nd", "weekends", "weekendy"]:
                normalized["day_preference"] = schemas.DayPreference.WEEKENDS.value
            elif d in ["cały tydzień", "whole week", "wszystkie dni"]:
                normalized["day_preference"] = schemas.DayPreference.WHOLE_WEEK.value

        return normalized

    def _count_consecutive_days(self, emp: Employee, shift_date: date) -> int:
        """Liczy ile kolejnych dni pracy (wliczając shift_date) miałby pracownik."""
        worked_dates = set(a.time_range.start.date() for a in emp.assigned_shifts)
        worked_dates.add(shift_date)
        
        consecutive = 1
        # Sprawdź wstecz
        d = shift_date - timedelta(days=1)
        while d in worked_dates:
            consecutive += 1
            d -= timedelta(days=1)
        # Sprawdź do przodu (na wypadek wypełnionych przyszłych slotów)
        d = shift_date + timedelta(days=1)
        while d in worked_dates:
            consecutive += 1
            d += timedelta(days=1)
        
        return consecutive

    def _check_weekly_rest(self, emp: Employee, shift: ShiftDemand) -> bool:
        """
        Art. 133 KP: Pracownik ma prawo do co najmniej 35h nieprzerwanego
        odpoczynku w każdym tygodniu (7-dniowym oknie).
        Sprawdza czy dodanie tej zmiany nie naruszy tego wymogu.
        """
        shift_date = shift.time_range.start.date()
        
        # Zbierz wszystkie zmiany w oknie 7 dni wokół nowej zmiany
        window_start = shift_date - timedelta(days=6)
        window_end = shift_date + timedelta(days=6)
        
        relevant_shifts = [a for a in emp.assigned_shifts 
                          if window_start <= a.time_range.start.date() <= window_end]
        relevant_shifts.append(shift)  # Dodaj proponowaną zmianę
        relevant_shifts.sort(key=lambda s: s.time_range.start)
        
        # Dla każdego 7-dniowego okna, sprawdź czy jest przerwa >= 35h
        for week_start_offset in range(-6, 1):
            week_start = shift_date + timedelta(days=week_start_offset)
            week_end = week_start + timedelta(days=6)
            
            week_shifts = sorted(
                [s for s in relevant_shifts if week_start <= s.time_range.start.date() <= week_end],
                key=lambda s: s.time_range.start
            )
            
            if len(week_shifts) <= 1:
                continue  # Wystarczająco dużo odpoczynku
            
            # Sprawdź najdłuższą przerwę między zmianami w tym tygodniu
            max_gap_hours = 0.0
            
            # Przerwa przed pierwszą zmianą w tygodniu
            week_start_dt = datetime.combine(week_start, time.min)
            first_gap = (week_shifts[0].time_range.start - week_start_dt).total_seconds() / 3600.0
            max_gap_hours = max(max_gap_hours, first_gap)
            
            # Przerwy między zmianami
            for i in range(len(week_shifts) - 1):
                gap = (week_shifts[i+1].time_range.start - week_shifts[i].time_range.end).total_seconds() / 3600.0
                max_gap_hours = max(max_gap_hours, gap)
            
            # Przerwa po ostatniej zmianie w tygodniu
            week_end_dt = datetime.combine(week_end + timedelta(days=1), time.min)
            last_gap = (week_end_dt - week_shifts[-1].time_range.end).total_seconds() / 3600.0
            max_gap_hours = max(max_gap_hours, last_gap)
            
            if max_gap_hours < 35.0:
                return False  # Brak wystarczającego odpoczynku tygodniowego
        
        return True

    def _check_hard_constraints(self, emp: Employee, shift: ShiftDemand) -> bool:
        for unav in emp.unavailabilities:
            if unav.overlaps(shift.time_range):
                return False
                
        for assigned in emp.assigned_shifts:
            if assigned.time_range.overlaps(shift.time_range):
                return False
            # Max 1 zmiana dziennie (Art. 129/132 KP)
            if assigned.time_range.start.date() == shift.time_range.start.date():
                return False

        # Art. 132 KP: Przerwa dobowa min. 11h
        for assigned in emp.assigned_shifts:
            if assigned.time_range.end <= shift.time_range.start:
                gap = (shift.time_range.start - assigned.time_range.end).total_seconds() / 3600.0
                if gap < 11.0: return False
            elif shift.time_range.end <= assigned.time_range.start:
                gap = (assigned.time_range.start - shift.time_range.end).total_seconds() / 3600.0
                if gap < 11.0: return False

        # Art. 147 KP: Maksymalna liczba kolejnych dni pracy
        shift_date = shift.time_range.start.date()
        consecutive = self._count_consecutive_days(emp, shift_date)
        if emp.is_uop:
            if consecutive > 5:  # UOP: max 5 kolejnych dni (Art. 147 KP)
                return False
        else:
            if consecutive > 6:  # UZ/Franczyzobiorca: max 6 kolejnych dni
                return False

        # Art. 133 KP: 35h nieprzerwanego odpoczynku tygodniowego (tylko UOP)
        if emp.is_uop:
            if not self._check_weekly_rest(emp, shift):
                return False

        # Art. 130 KP: Sztywny limit godzin dla UOP
        if emp.is_uop:
            if round(emp.worked_hours + shift.time_range.hours, 2) > round(emp.target_hours, 2):
                return False
        # Dla franczyzobiorcy i UZ, pozwól na lekkie przekroczenie (10%), ale nie jeśli cel to 0
        elif emp.target_hours > 0 and round(emp.worked_hours + shift.time_range.hours, 2) > round(emp.target_hours * 1.1, 2):
             return False
        elif emp.target_hours <= 0 and shift.time_range.hours > 0:
            return False

        return True

    def _assign(self, emp: Employee, shift: ShiftDemand):
        emp.assigned_shifts.append(shift)
        emp.worked_hours += shift.time_range.hours
        shift.required_employees -= 1

    def _calculate_soft_score(self, emp: Employee, shift: ShiftDemand) -> float:
        score = 0.0
        shift_date = shift.time_range.start.date()
        
        # 1. Requested shift na konkretny dzień (najwyższy priorytet)
        if emp.requested_shifts.get(shift_date) == shift.shift_type:
            score += 200.0

        # 2. Preferencje typu zmiany (KLUCZOWE, w tym preferencje ajenta np. na środek)
        prefs = emp.preferences.get("preferred_shifts", [])
        if prefs:
            if shift.shift_type in prefs:
                score += 60.0   # BONUS za preferowaną zmianę (np. środek/middle)
            else:
                score -= 30.0   # KARA za nie-preferowaną zmianę

        # 3. Preferencje dni tygodnia
        day_pref = emp.preferences.get("day_preference")
        is_weekend = shift.time_range.start.weekday() >= 5
        if day_pref == schemas.DayPreference.WEEKDAYS.value:
            if not is_weekend:
                score += 10.0
            else:
                score -= 5.0
        elif day_pref == schemas.DayPreference.WEEKENDS.value:
            if is_weekend:
                score += 10.0
            else:
                score -= 5.0

        # 4. Kara za klasteryzację — rozkład zmian równomiernie w miesiącu
        worked_dates = set(a.time_range.start.date() for a in emp.assigned_shifts)
        
        # 4a. Progresywna kara za kolejne dni pracy z rzędu
        consecutive_before = 0
        d = shift_date - timedelta(days=1)
        while d in worked_dates:
            consecutive_before += 1
            d -= timedelta(days=1)
        score -= consecutive_before * (consecutive_before + 1) * 2.5

        # 4b. Kara za brak dnia wolnego w ostatnich 7 dniach
        recent_work_days = sum(1 for i in range(1, 7) if (shift_date - timedelta(days=i)) in worked_dates)
        if recent_work_days >= 5:
            score -= 40.0
        elif recent_work_days >= 4:
            score -= 15.0

        # 5. Punktacja za przypisane role (Kasa, Sklep)
        roles = emp.store_roles or []
        has_kasa = 'kasa' in roles
        has_sklep = 'sklep' in roles

        if has_kasa and has_sklep:
            if shift.shift_type in [schemas.ShiftType.MORNING.value, schemas.ShiftType.CLOSING.value]:
                score += 37.5
            elif shift.shift_type == schemas.ShiftType.MIDDLE.value:
                score += 12.5
        elif has_kasa:
            if shift.shift_type in [schemas.ShiftType.MORNING.value, schemas.ShiftType.CLOSING.value]:
                score += 50.0
        elif has_sklep:
            if shift.shift_type in [schemas.ShiftType.MORNING.value, schemas.ShiftType.CLOSING.value]:
                score -= 50.0
            elif shift.shift_type == schemas.ShiftType.MIDDLE.value:
                score += 50.0

        return score

    async def generate(self, start_date: date, end_date: date):
        await self._gather_data(start_date, end_date)
        self._map_to_domain(start_date, end_date)
        
        uop_emps = [e for e in self.employees if e.is_uop]
        other_emps = [e for e in self.employees if not e.is_uop]

        # Wyliczenie Puli (Art. 130 KP)
        full_uop_hours = calculate_uop_hours(start_date.year, start_date.month, 1.0)
        for emp in uop_emps:
            emp.target_hours = full_uop_hours * emp.fte_or_target
        for emp in other_emps:
            emp.target_hours = emp.fte_or_target

        # Filtruj pracowników/ajenta z 0 godzin docelowych — nie biorą udziału w grafiku
        zero_hour_employees = [e for e in self.employees if e.target_hours <= 0]
        for e in zero_hour_employees:
            self.logs.append(f"[INFO] Pracownik/Ajent {e.first_name} {e.last_name} pominięty — docelowe godziny = 0.")
        self.employees = [e for e in self.employees if e.target_hours > 0]

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

        # KROK 2: Pre-assign (Requested Shifts z dyspozycji)
        for shift in slots:
            if shift.required_employees <= 0: continue
            
            shift_date = shift.time_range.start.date()
            candidates = [
                e for e in self.employees 
                if e.requested_shifts.get(shift_date) == shift.shift_type 
                and self._check_hard_constraints(e, shift)
            ]
            if candidates:
                valid_candidates = []
                for e in candidates:
                    if e.is_uop and e.worked_hours + shift.time_range.hours > e.target_hours:
                        continue
                    if not e.is_uop and e.target_hours > 0 and e.worked_hours + shift.time_range.hours > e.target_hours * 1.2:
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
                    if e.is_uop and e.worked_hours + shift.time_range.hours > e.target_hours:
                        continue
                    if not e.is_uop and e.target_hours > 0 and e.worked_hours + shift.time_range.hours > e.target_hours * 1.2:
                        continue
                    candidates.append(e)
            
            if candidates:
                scored_candidates = []
                for e in candidates:
                    burn_rate = e.worked_hours / e.target_hours if e.target_hours > 0 else 1.0
                    score = self._calculate_soft_score(e, shift)
                    scored_candidates.append((e, score, burn_rate))
                
                scored_candidates.sort(key=lambda x: x[1], reverse=True)
                top_score = scored_candidates[0][1]
                top_candidates = [item for item in scored_candidates if item[1] == top_score]
                
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
                    # Zmodyfikowane Hard Constraints (omijamy limity godzin, 
                    # ale BEZWZGLĘDNIE ZACHOWUJEMY: urlopy, brak dostępności, 11h odpoczynek, 1 zmianę/dzień, kolejne dni)
                    can_work = True
                    for unav in e.unavailabilities:
                        if unav.overlaps(shift.time_range): can_work = False
                    for assigned in e.assigned_shifts:
                        if assigned.time_range.overlaps(shift.time_range): can_work = False
                        if assigned.time_range.start.date() == shift.time_range.start.date(): can_work = False
                        if assigned.time_range.end <= shift.time_range.start:
                            if (shift.time_range.start - assigned.time_range.end).total_seconds() / 3600.0 < 11.0: can_work = False
                        elif shift.time_range.end <= assigned.time_range.start:
                            if (assigned.time_range.start - shift.time_range.end).total_seconds() / 3600.0 < 11.0: can_work = False
                    
                    # Limit kolejnych dni pracy (nawet w fallback)
                    if can_work:
                        shift_date = shift.time_range.start.date()
                        consecutive = self._count_consecutive_days(e, shift_date)
                        max_consecutive = 6 if e.is_uop else 7
                        if consecutive > max_consecutive:
                            can_work = False
                            
                    # Nie przydzielaj zmian pracownikom/ajentowi z docelowymi 0 godzin
                    if can_work and e.target_hours <= 0:
                        can_work = False
                            
                    if can_work:
                        fallback_candidates.append(e)
                
                if fallback_candidates:
                    fallback_candidates.sort(key=lambda e: e.worked_hours)
                    chosen = fallback_candidates[0]
                    self._assign(chosen, shift)
                    self.logs.append(f"[KRYTYCZNE - WYMUSZONO] Awaryjnie przypisano pracownika {chosen.first_name} {chosen.last_name} do zmiany '{shift.shift_type}' ({shift.time_range.start.date()}), ignorując docelowe czasy pracy.")
                else:
                    date_str = shift.time_range.start.strftime("%Y-%m-%d")
                    self.logs.append(f"[KRYTYCZNE - FATAL] Nie można obsadzić zmiany '{shift.shift_type}' w dniu {date_str} – brak dostępnego personelu (wszyscy zablokowani twardymi ograniczeniami/urlopami).")

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
                d_start = self._parse_date_safe(v.get("start_date"))
                d_end = self._parse_date_safe(v.get("end_date")) or d_start
                if d_start and d_end and d_start <= curr <= d_end:
                    uid = str(v.get("user_id"))
                    emp_data = next((e for e in self.db_employees if str(e["_id"]) == uid), None)
                    if emp_data and not any(ve["id"] == uid for ve in vacation_employees):
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

async def generate_schedule_for_period(db: motor.motor_asyncio.AsyncIOMotorDatabase, current_user: dict, year: int = None, month: int = None, start_date_str: str = None, end_date_str: str = None):
    franchise_code = current_user.get("franchise_code")
    if not franchise_code:
        raise ValueError("Użytkownik nie jest przypisany do żadnego sklepu.")

    await db.schedule_drafts.delete_many({"franchise_code": franchise_code})
    
    if start_date_str and end_date_str:
        start_date = datetime.strptime(start_date_str, "%Y-%m-%d").date()
        end_date = datetime.strptime(end_date_str, "%Y-%m-%d").date()
    else:
        start_date = date(year, month, 1)
        _, num_days = calendar.monthrange(year, month)
        end_date = date(year, month, num_days)

    generator = ScheduleGenerator(db, current_user)
    await generator.generate(start_date, end_date)