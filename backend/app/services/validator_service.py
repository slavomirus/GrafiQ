import logging
from datetime import datetime, timedelta, time, date
from typing import List, Optional, Tuple
import motor.motor_asyncio
from bson import ObjectId

from .. import models

logger = logging.getLogger(__name__)

class ScheduleValidator:
    def __init__(self, db: motor.motor_asyncio.AsyncIOMotorDatabase):
        self.db = db

    async def validate_shift_assignment(
        self, 
        user_id: ObjectId, 
        shift_date: date, 
        start_time: time, 
        end_time: time, 
        franchise_code: str,
        ignore_shift_id: Optional[ObjectId] = None
    ) -> Tuple[bool, Optional[str]]:
        """
        Sprawdza czy przypisanie zmiany nie łamie twardych reguł (Hard Constraints).
        Zwraca (True, None) jeśli OK, lub (False, "Powód błędu").
        """
        
        # HC1: Maksymalna długość zmiany (<= 10h)
        duration = self._calculate_duration(start_time, end_time)
        if duration > 10:
            return False, f"Zmiana przekracza 10 godzin ({duration:.1f}h)."

        # HC5: Ochrona urlopów i L4
        if await self._is_on_leave(user_id, shift_date):
            return False, "Pracownik przebywa na urlopie lub L4."

        # Pobierz sąsiednie zmiany (wczoraj, dzisiaj, jutro)
        surrounding_shifts = await self._get_surrounding_shifts(user_id, shift_date, franchise_code)
        
        # Filtrujemy zmianę, którą ewentualnie edytujemy/wymieniamy
        if ignore_shift_id:
            surrounding_shifts = [s for s in surrounding_shifts if s["_id"] != ignore_shift_id]

        # Sprawdź czy już nie ma innej zmiany tego dnia (chyba że to ta sama - obsłużone wyżej)
        shifts_today = [s for s in surrounding_shifts if s["date"].date() == shift_date]
        if shifts_today:
            return False, "Pracownik ma już przypisaną zmianę w tym dniu."

        # HC2: Przerwa dobowa (>= 12h)
        # HC6: Konflikt Otwarcie/Zamknięcie (częściowo pokryte przez HC2, ale sprawdzamy explicite)
        if not self._check_rest_period(shift_date, start_time, end_time, surrounding_shifts):
            return False, "Naruszenie odpoczynku dobowego (min. 12h) lub konflikt zmian."

        # HC3: Maksymalna liczba dni pracujących z rzędu (<= 5)
        if not await self._check_consecutive_days(user_id, shift_date, franchise_code, ignore_shift_id):
            return False, "Przekroczono limit 5 dni pracy z rzędu."

        # HC4: Tygodniowy odpoczynek (35h)
        if not await self._check_weekly_rest(user_id, shift_date, start_time, end_time, franchise_code, ignore_shift_id):
             return False, "Brak wymaganego 35h nieprzerwanego odpoczynku w tygodniu."

        return True, None

    async def validate_swap(self, swap_request: dict) -> Tuple[bool, Optional[str]]:
        """
        Symuluje wymianę zmian i sprawdza poprawność dla obu stron.
        """
        requester_id = swap_request["requester_id"]
        target_user_id = swap_request["target_user_id"]
        franchise_code = swap_request["franchise_code"]
        
        # Dane zmiany requestera (którą oddaje)
        date1 = swap_request["my_date"]
        if isinstance(date1, datetime): date1 = date1.date()
        # Musimy pobrać godziny tej zmiany z bazy
        shift1_doc = await self.db.schedule.find_one({
            "franchise_code": franchise_code,
            "user_id": requester_id,
            "date": datetime.combine(date1, time.min),
            "shift_name": swap_request["my_shift_name"]
        })
        if not shift1_doc: return False, "Nie znaleziono zmiany inicjatora."
        
        # Dane zmiany targeta (którą oddaje)
        date2 = swap_request["target_date"]
        if isinstance(date2, datetime): date2 = date2.date()
        shift2_doc = await self.db.schedule.find_one({
            "franchise_code": franchise_code,
            "user_id": target_user_id,
            "date": datetime.combine(date2, time.min),
            "shift_name": swap_request["target_shift_name"]
        })
        if not shift2_doc: return False, "Nie znaleziono zmiany odbiorcy."

        # Parsowanie godzin
        s1_start = self._parse_time(shift1_doc["start_time"])
        s1_end = self._parse_time(shift1_doc["end_time"])
        s2_start = self._parse_time(shift2_doc["start_time"])
        s2_end = self._parse_time(shift2_doc["end_time"])

        # Symulacja 1: Requester bierze zmianę Targeta (shift2) w dniu date2
        # Ignorujemy jego obecną zmianę w date1 (bo ją oddaje), ale sprawdzamy date2
        # Uwaga: Jeśli date1 == date2, to po prostu zamieniają się godzinami w tym samym dniu.
        
        # Sprawdź dla Requestera (bierze shift2)
        valid_req, reason_req = await self.validate_shift_assignment(
            requester_id, date2, s2_start, s2_end, franchise_code, ignore_shift_id=shift1_doc["_id"]
        )
        if not valid_req:
            return False, f"Inicjator: {reason_req}"

        # Sprawdź dla Targeta (bierze shift1)
        valid_tgt, reason_tgt = await self.validate_shift_assignment(
            target_user_id, date1, s1_start, s1_end, franchise_code, ignore_shift_id=shift2_doc["_id"]
        )
        if not valid_tgt:
            return False, f"Odbiorca: {reason_tgt}"

        return True, None

    # --- Helper Methods ---

    def _parse_time(self, t) -> time:
        if isinstance(t, time): return t
        if isinstance(t, datetime): return t.time()
        if isinstance(t, str):
            try:
                return datetime.strptime(t, "%H:%M").time()
            except ValueError:
                return datetime.strptime(t, "%H:%M:%S").time()
        return time(0, 0)

    def _calculate_duration(self, start: time, end: time) -> float:
        dummy_date = date(2000, 1, 1)
        dt_start = datetime.combine(dummy_date, start)
        dt_end = datetime.combine(dummy_date, end)
        if dt_end < dt_start:
            dt_end += timedelta(days=1)
        return (dt_end - dt_start).total_seconds() / 3600.0

    async def _is_on_leave(self, user_id: ObjectId, check_date: date) -> bool:
        check_dt = datetime.combine(check_date, time.min)
        
        # Sprawdź urlopy
        vacation = await self.db.vacations.find_one({
            "user_id": user_id,
            "status": models.VacationStatus.APPROVED.value,
            "start_date": {"$lte": datetime.combine(check_date, time.max)},
            "end_date": {"$gte": check_dt}
        })
        if vacation: return True
        
        # Sprawdź L4
        sick = await self.db.sick_leaves.find_one({
            "user_id": user_id,
            "start_date": {"$lte": datetime.combine(check_date, time.max)},
            "end_date": {"$gte": check_dt}
        })
        if sick: return True
        
        return False

    async def _get_surrounding_shifts(self, user_id: ObjectId, center_date: date, franchise_code: str) -> List[dict]:
        """Pobiera zmiany z zakresu [center_date - 1, center_date + 1]"""
        start = datetime.combine(center_date - timedelta(days=1), time.min)
        end = datetime.combine(center_date + timedelta(days=1), time.max)
        
        cursor = self.db.schedule.find({
            "franchise_code": franchise_code,
            "user_id": user_id,
            "date": {"$gte": start, "$lte": end}
        })
        return await cursor.to_list(length=None)

    def _check_rest_period(self, current_date: date, start: time, end: time, surrounding_shifts: List[dict]) -> bool:
        current_start_dt = datetime.combine(current_date, start)
        current_end_dt = datetime.combine(current_date, end)
        if current_end_dt < current_start_dt:
            current_end_dt += timedelta(days=1)

        for shift in surrounding_shifts:
            s_date = shift["date"].date()
            s_start_t = self._parse_time(shift["start_time"])
            s_end_t = self._parse_time(shift["end_time"])
            
            s_start_dt = datetime.combine(s_date, s_start_t)
            s_end_dt = datetime.combine(s_date, s_end_t)
            if s_end_dt < s_start_dt:
                s_end_dt += timedelta(days=1)

            # Sprawdź odstęp
            # Jeśli badana zmiana jest PO tej z bazy (np. dzisiaj vs wczoraj)
            if current_start_dt > s_end_dt:
                diff = (current_start_dt - s_end_dt).total_seconds() / 3600.0
                if diff < 12: return False
            
            # Jeśli badana zmiana jest PRZED tą z bazy (np. dzisiaj vs jutro)
            if s_start_dt > current_end_dt:
                diff = (s_start_dt - current_end_dt).total_seconds() / 3600.0
                if diff < 12: return False
                
        return True

    async def _check_consecutive_days(self, user_id: ObjectId, current_date: date, franchise_code: str, ignore_shift_id: Optional[ObjectId] = None) -> bool:
        # Sprawdzamy ciągłość w tył i w przód
        start_range = datetime.combine(current_date - timedelta(days=6), time.min)
        end_range = datetime.combine(current_date + timedelta(days=6), time.max)
        
        shifts = await self.db.schedule.find({
            "franchise_code": franchise_code,
            "user_id": user_id,
            "date": {"$gte": start_range, "$lte": end_range}
        }).to_list(length=None)
        
        if ignore_shift_id:
            shifts = [s for s in shifts if s["_id"] != ignore_shift_id]
        
        work_dates = {s["date"].date() for s in shifts}
        work_dates.add(current_date)
        
        # Liczymy max ciąg zawierający current_date
        consecutive = 1 # Sama current_date
        
        # W tył
        d = current_date - timedelta(days=1)
        while d in work_dates:
            consecutive += 1
            d -= timedelta(days=1)
        
        # W przód
        d = current_date + timedelta(days=1)
        while d in work_dates:
            consecutive += 1
            d += timedelta(days=1)
            
        return consecutive <= 5

    async def _check_weekly_rest(self, user_id: ObjectId, current_date: date, start_time: time, end_time: time, franchise_code: str, ignore_shift_id: Optional[ObjectId] = None) -> bool:
        # Sprawdźmy tydzień kalendarzowy (pon-nd)
        start_of_week = current_date - timedelta(days=current_date.weekday())
        end_of_week = start_of_week + timedelta(days=6)
        
        start_dt = datetime.combine(start_of_week, time.min)
        end_dt = datetime.combine(end_of_week, time.max)
        
        shifts = await self.db.schedule.find({
            "franchise_code": franchise_code,
            "user_id": user_id,
            "date": {"$gte": start_dt, "$lte": end_dt}
        }).sort("date", 1).to_list(length=None)
        
        if ignore_shift_id:
            shifts = [s for s in shifts if s["_id"] != ignore_shift_id]
            
        # Budujemy listę interwałów pracy w tym tygodniu
        intervals = []
        for s in shifts:
            # Pomiń jeśli to ta sama data co current_date (bo ją nadpisujemy/dodajemy)
            if s["date"].date() == current_date:
                continue
                
            d = s["date"].date()
            st = self._parse_time(s["start_time"])
            et = self._parse_time(s["end_time"])
            
            s_start = datetime.combine(d, st)
            s_end = datetime.combine(d, et)
            if s_end < s_start: s_end += timedelta(days=1)
            intervals.append((s_start, s_end))
            
        # Dodaj nową zmianę
        c_start = datetime.combine(current_date, start_time)
        c_end = datetime.combine(current_date, end_time)
        if c_end < c_start: c_end += timedelta(days=1)
        intervals.append((c_start, c_end))
        
        # Sortuj po czasie startu
        intervals.sort(key=lambda x: x[0])
        
        # Szukaj luki >= 35h
        # Luka może być:
        # 1. Od początku tygodnia do pierwszej zmiany (jeśli pierwsza zmiana jest np. we wtorek)
        # 2. Pomiędzy zmianami
        # 3. Od ostatniej zmiany do końca tygodnia
        
        # Ale uwaga: odpoczynek tygodniowy musi obejmować 35h ciągiem.
        # Jeśli tydzień zaczyna się w poniedziałek 00:00, a pierwsza zmiana jest we wtorek 12:00, to mamy 36h wolnego.
        # Jednakże, odpoczynek tygodniowy może przechodzić na kolejny tydzień lub z poprzedniego.
        # Uproszczenie: Sprawdzamy czy wewnątrz tego tygodnia jest luka 35h.
        # Jeśli nie ma, to może być problem.
        # Bardziej precyzyjnie: Sprawdzamy czy w oknie [start_of_week, end_of_week + 35h] jest luka.
        # Ale trzymajmy się prostego sprawdzenia wewnątrz tygodnia + marginesy.
        
        # Sprawdźmy luki między zmianami
        max_gap = 0.0
        
        # Luka przed pierwszą zmianą (od poniedziałku 00:00)
        if intervals:
            first_start = intervals[0][0]
            gap = (first_start - start_dt).total_seconds() / 3600.0
            if gap > max_gap: max_gap = gap
            
            # Luki pomiędzy
            for i in range(len(intervals) - 1):
                end_prev = intervals[i][1]
                start_next = intervals[i+1][0]
                gap = (start_next - end_prev).total_seconds() / 3600.0
                if gap > max_gap: max_gap = gap
                
            # Luka po ostatniej zmianie (do niedzieli 23:59)
            last_end = intervals[-1][1]
            gap = (end_dt - last_end).total_seconds() / 3600.0
            if gap > max_gap: max_gap = gap
        else:
            # Brak zmian w tygodniu = cały tydzień wolny
            max_gap = 24 * 7
            
        return max_gap >= 35
