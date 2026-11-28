# Plik: backend/app/services/schedule_solver.py
"""
Deterministyczny silnik optymalizacyjny do automatycznego generowania grafików pracy
oparty na Google OR-Tools (CP-SAT Solver: ortools.sat.python.cp_model).

Architektura:
1. Krok 1 (Diagnostic Audit / Pre-walidacja): Szybka weryfikacja twardych warunków brzegowych
   (dostępność uprawnień KASA na otwarcie/zamknięcie, bilans dostępnych godzin vs zapotrzebowanie).
2. Krok 2 (Hard CP-SAT Solve): Pełna optymalizacja z twardymi ograniczeniami Kodeksu Pracy
   (11h odpoczynku dobowego, doba pracownicza, 35h odpoczynku tygodniowego, max 5 dni z rzędu),
   uprawnieniami KASA/SKLEP, oknami dostaw (SRODEK) oraz bilansami godzin UoP / UZ.
3. Krok 3 (Fallback / Softening): Automatyczna relaksacja ze zmiennymi slack (wysoka kara -10000 pkt)
   dla obsady zmiany SRODEK w dni dostaw oraz odchyleń od target_hours/limitów UZ, z generowaniem
   czytelnych ostrzeżeń (warnings) dla administratora.
"""

from __future__ import annotations

import calendar
import logging
import time as time_module
from datetime import date, datetime, time, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple

import holidays
from ortools.sat.python import cp_model

from ..schemas import (
    DeliverySlot,
    EmployeeHoursSummary,
    EmployeeInput,
    GeneratedScheduleResponse,
    RolePermission,
    ScheduleGenerationRequest,
    ShiftAssignment,
    ShiftConfig,
    ShiftPreference,
    SolverContractType,
    SolverShiftType,
    UnavailabilitySlot,
)

logger = logging.getLogger(__name__)

# Skala wewnętrzna CP-SAT: 1 godzina = 60 minut, więc mnożymy wagi punktowe przez 60,
# aby zachować dokładność co do 1 minuty bez błędów zaokrągleń.
SCALE = 60

WEIGHT_TARGET_HOURS = 100       # Waga 100 za każdą godzinę odchylenia |actual - target| (100 pkt / 1h = 100 jedn. / 1 min)
WEIGHT_SHIFT_PREFERENCE = 50    # Waga 50 za dopasowanie do preferencji zmianowej
WEIGHT_WEEKEND_FAIRNESS = 30    # Waga 30 za sprawiedliwy podział weekendów
WEIGHT_RHYTHM_STABILITY = 20    # Waga 20 za stabilność rytmu dobowego (bloki tych samych zmian)
WEIGHT_COST_UOP_VS_UZ = 10      # Waga 10 za priorytet wyczerpania etatu UoP przed UZ
PENALTY_SLACK_FALLBACK = 10000  # Kara -10000 pkt za jednostkę naruszenia w trybie relaksacji (Krok 3)


def time_str_to_minutes(t_str: str) -> int:
    """Konwertuje ciąg 'HH:MM' na liczbę minut od północy (0..1439)."""
    parts = str(t_str).strip().split(":")
    h = int(parts[0])
    m = int(parts[1]) if len(parts) > 1 else 0
    return h * 60 + m


def minutes_to_time_str(minutes: int) -> str:
    """Konwertuje minuty od północy na format 'HH:MM'."""
    norm = minutes % 1440
    return f"{norm // 60:02d}:{norm % 60:02d}"


def shift_duration_minutes(start_str: str, end_str: str) -> int:
    """Wylicza długość zmiany w minutach (obsługuje zmiany przechodzące przez północ)."""
    s_min = time_str_to_minutes(start_str)
    e_min = time_str_to_minutes(end_str)
    if e_min <= s_min:
        e_min += 1440
    return e_min - s_min


def calculate_nominal_uop_hours(year: int, month: int, fte: float = 1.0) -> float:
    """
    Oblicza nominalny wymiar czasu pracy dla Umowy o Pracę zgodnie z Art. 130 Kodeksu Pracy:
    1. (pełne tygodnie * 40h) + (dni wystające pon-pt * 8h)
    2. Odjęcie 8h za każde święto ustawowe wypadające w dzień inny niż niedziela.
    3. Przemnożenie przez wymiar etatu (fte).
    """
    pl_holidays = holidays.Poland(years=year)
    first_weekday, num_days = calendar.monthrange(year, month)

    full_weeks = num_days // 7
    remaining_days = num_days % 7

    work_hours = full_weeks * 40
    for i in range(remaining_days):
        current_weekday = (first_weekday + i) % 7
        if current_weekday < 5:
            work_hours += 8

    for day_num in range(1, num_days + 1):
        dt = date(year, month, day_num)
        if dt in pl_holidays and dt.weekday() != 6:
            work_hours -= 8

    return float(work_hours) * float(fte)


class DayShiftSpec:
    """Specyfikacja pojedynczej zmiany w konkretnym dniu kalendarzowym."""

    def __init__(
        self,
        day: date,
        shift_type: SolverShiftType,
        start_time: str,
        end_time: str,
        min_staff: int,
        is_delivery_shift: bool = False,
        requires_kasa: bool = False,
    ):
        self.day = day
        self.shift_type = shift_type
        self.start_time = start_time
        self.end_time = end_time
        self.start_min = time_str_to_minutes(start_time)
        self.end_min = time_str_to_minutes(end_time)
        if self.end_min <= self.start_min:
            self.end_min += 1440
        self.duration_min = self.end_min - self.start_min
        self.min_staff = min_staff
        self.is_delivery_shift = is_delivery_shift
        self.requires_kasa = requires_kasa


class ScheduleOptimizerEngine:
    """
    Silnik optymalizacji grafików pracy oparty na Google OR-Tools CP-SAT.
    """

    ALL_SHIFTS: Tuple[SolverShiftType, ...] = (
        SolverShiftType.RANEK,
        SolverShiftType.SRODEK,
        SolverShiftType.ZAMKNIECIE,
    )

    def __init__(self, request: ScheduleGenerationRequest):
        self.request = request
        self.start_date, self.end_date = self._resolve_period(request)
        self.days: List[date] = []
        curr = self.start_date
        while curr <= self.end_date:
            self.days.append(curr)
            curr += timedelta(days=1)

        self.closed_dates: Set[date] = set(request.closed_dates or [])
        self.commercial_sundays: Set[date] = set(request.commercial_sundays or [])
        self.shift_configs: Dict[SolverShiftType, ShiftConfig] = self._build_shift_configs(request.shifts)
        self.day_shifts: Dict[Tuple[int, SolverShiftType], DayShiftSpec] = self._build_day_shift_specs()

        self.nominal_full_fte_hours: float = (
            float(request.nominal_monthly_hours)
            if request.nominal_monthly_hours is not None
            else calculate_nominal_uop_hours(self.start_date.year, self.start_date.month, 1.0)
        )

        # Wyliczenie celów i limitów godzinowych (w minutach) dla każdego pracownika
        self.emp_target_minutes: Dict[str, int] = {}
        self.emp_min_minutes: Dict[str, int] = {}
        self.emp_max_minutes: Dict[str, int] = {}
        self.emp_leave_days_count: Dict[str, int] = {}
        self.emp_leave_minutes: Dict[str, int] = {}
        self.emp_nominal_minutes: Dict[str, int] = {}

        self.max_single_shift_minutes: int = max(
            (spec.duration_min for spec in self.day_shifts.values()),
            default=510,
        )
        self._prepare_employee_hour_targets()

    @staticmethod
    def _resolve_period(request: ScheduleGenerationRequest) -> Tuple[date, date]:
        if request.start_date and request.end_date:
            s_date = date.fromisoformat(str(request.start_date)[:10])
            e_date = date.fromisoformat(str(request.end_date)[:10])
            if e_date < s_date:
                raise ValueError("Data końcowa okresu nie może być wcześniejsza niż początkowa.")
            return s_date, e_date

        if request.period:
            parts = request.period.strip().split("-")
            year, month = int(parts[0]), int(parts[1])
        elif request.year and request.month:
            year, month = int(request.year), int(request.month)
        else:
            today = date.today()
            year, month = today.year, today.month

        _, last_day = calendar.monthrange(year, month)
        return date(year, month, 1), date(year, month, last_day)

    @staticmethod
    def _build_shift_configs(shifts: List[ShiftConfig]) -> Dict[SolverShiftType, ShiftConfig]:
        defaults = {
            SolverShiftType.RANEK: ShiftConfig(
                shift_type=SolverShiftType.RANEK,
                start_time="05:30",
                end_time="14:00",
                min_staff=1,
            ),
            SolverShiftType.SRODEK: ShiftConfig(
                shift_type=SolverShiftType.SRODEK,
                start_time="10:00",
                end_time="18:00",
                min_staff=0,
            ),
            SolverShiftType.ZAMKNIECIE: ShiftConfig(
                shift_type=SolverShiftType.ZAMKNIECIE,
                start_time="14:30",
                end_time="23:15",
                min_staff=1,
            ),
        }
        for sc in shifts or []:
            defaults[sc.shift_type] = sc
        return defaults

    def is_day_open(self, d: date) -> bool:
        if d in self.closed_dates:
            return False
        if d.weekday() == 6 and not self.request.open_all_sundays:
            if d not in self.commercial_sundays:
                return False
        return True

    def _get_deliveries_for_day(self, d: date) -> List[DeliverySlot]:
        matched = []
        for slot in self.request.deliveries or []:
            if slot.date is not None:
                if slot.date == d:
                    matched.append(slot)
            elif slot.day_of_week is not None and slot.day_of_week == d.weekday():
                matched.append(slot)
        return matched

    def _build_day_shift_specs(self) -> Dict[Tuple[int, SolverShiftType], DayShiftSpec]:
        specs: Dict[Tuple[int, SolverShiftType], DayShiftSpec] = {}
        for d_idx, d in enumerate(self.days):
            open_day = self.is_day_open(d)
            deliveries = self._get_deliveries_for_day(d) if open_day else []
            d_iso = d.isoformat()
            day_custom_hours = (self.request.daily_shift_hours or {}).get(d_iso, {})

            for s_type in self.ALL_SHIFTS:
                cfg = self.shift_configs[s_type]
                custom_pair = day_custom_hours.get(s_type.value) or day_custom_hours.get(s_type)
                if custom_pair and len(custom_pair) == 2:
                    start_t, end_t = str(custom_pair[0])[:5], str(custom_pair[1])[:5]
                else:
                    start_t = cfg.start_time
                    end_t = cfg.end_time

                if not open_day:
                    specs[(d_idx, s_type)] = DayShiftSpec(
                        day=d,
                        shift_type=s_type,
                        start_time=start_t,
                        end_time=end_t,
                        min_staff=0,
                        is_delivery_shift=False,
                        requires_kasa=False,
                    )
                    continue

                base_min = cfg.min_staff
                if cfg.min_staff_by_weekday and d.weekday() in cfg.min_staff_by_weekday:
                    base_min = cfg.min_staff_by_weekday[d.weekday()]

                is_delivery = False
                if s_type == SolverShiftType.SRODEK and deliveries:
                    is_delivery = True
                    delivery_req = max(slot.required_middle_staff for slot in deliveries)
                    min_staff = max(base_min, delivery_req, 1)

                    # Dopasowanie okna zmiany SRODEK tak, aby w pełni pokrywała okno dostawy
                    s_min = time_str_to_minutes(start_t)
                    e_min = time_str_to_minutes(end_t)
                    if e_min <= s_min:
                        e_min += 1440
                    base_dur = e_min - s_min

                    for slot in deliveries:
                        if slot.shift_start_override and slot.shift_end_override:
                            start_t = slot.shift_start_override
                            end_t = slot.shift_end_override
                            break
                        if not slot.start_time or not slot.end_time:
                            continue
                        deliv_s = time_str_to_minutes(slot.start_time)
                        deliv_e = time_str_to_minutes(slot.end_time)
                        if deliv_e <= deliv_s:
                            deliv_e += 1440
                        if deliv_s < s_min:
                            s_min = deliv_s
                            e_min = max(deliv_e, s_min + base_dur)
                            start_t = minutes_to_time_str(s_min)
                            end_t = minutes_to_time_str(e_min)
                        elif deliv_e > e_min:
                            e_min = deliv_e
                            s_min = min(deliv_s, e_min - base_dur)
                            start_t = minutes_to_time_str(s_min)
                            end_t = minutes_to_time_str(e_min)
                else:
                    if s_type in (SolverShiftType.RANEK, SolverShiftType.ZAMKNIECIE):
                        min_staff = max(1, base_min)
                    else:
                        min_staff = max(0, base_min)

                requires_kasa = s_type in (SolverShiftType.RANEK, SolverShiftType.ZAMKNIECIE) and min_staff > 0
                specs[(d_idx, s_type)] = DayShiftSpec(
                    day=d,
                    shift_type=s_type,
                    start_time=start_t,
                    end_time=end_t,
                    min_staff=min_staff,
                    is_delivery_shift=is_delivery,
                    requires_kasa=requires_kasa,
                )
        return specs

    def _prepare_employee_hour_targets(self) -> None:
        period_dates = set(self.days)
        for emp in self.request.employees:
            valid_leaves = sorted({d for d in emp.leaves if d in period_dates})
            self.emp_leave_days_count[emp.id] = len(valid_leaves)

            if emp.contract_type == SolverContractType.UOP:
                fte = float(emp.fte if emp.fte is not None else 1.0)
                nominal_h = self.nominal_full_fte_hours * fte
                leave_h = len(valid_leaves) * 8.0 * fte
                self.emp_nominal_minutes[emp.id] = int(round(nominal_h * 60))
                self.emp_leave_minutes[emp.id] = int(round(leave_h * 60))

                if emp.target_hours is not None:
                    target_h = max(0.0, float(emp.target_hours))
                else:
                    target_h = max(0.0, nominal_h - leave_h)

                target_min = int(round(target_h * 60))
                self.emp_target_minutes[emp.id] = target_min
                # Tolerancja 1 zmiany dla UoP wyłącznie gdy układ długości zmian nie dzieli się bez reszty
                self.emp_min_minutes[emp.id] = max(0, target_min - self.max_single_shift_minutes)
                self.emp_max_minutes[emp.id] = target_min + self.max_single_shift_minutes
            else:
                # Umowa Zlecenie (UZ)
                self.emp_leave_minutes[emp.id] = 0
                min_h = float(emp.min_hours) if emp.min_hours is not None else 0.0
                if emp.max_hours is not None:
                    max_h = float(emp.max_hours)
                elif emp.target_hours is not None:
                    max_h = float(emp.target_hours)
                else:
                    max_h = 160.0

                if emp.target_hours is not None:
                    target_h = float(emp.target_hours)
                elif emp.min_hours is not None and emp.max_hours is not None:
                    target_h = (min_h + max_h) / 2.0
                else:
                    target_h = max_h

                self.emp_nominal_minutes[emp.id] = int(round(target_h * 60))
                self.emp_target_minutes[emp.id] = int(round(target_h * 60))
                self.emp_min_minutes[emp.id] = int(round(min_h * 60))
                self.emp_max_minutes[emp.id] = int(round(max_h * 60))

    def is_employee_available_for_shift(
        self, emp: EmployeeInput, d_idx: int, s_type: SolverShiftType
    ) -> bool:
        """
        Sprawdza czy pracownik może pracować na zmianie s_type w dniu d_idx
        pod względem: otwarcia sklepu, uprawnień (SKLEP nie może RANEK/ZAMKNIECIE),
        urlopów/L4 oraz zgłoszonej niedostępności godzinowej/dziennej.
        """
        spec = self.day_shifts[(d_idx, s_type)]
        d = spec.day
        if not self.is_day_open(d):
            return False

        # Twarde ograniczenie 3: Pracownik wyłącznie SKLEP ma zablokowane RANEK i ZAMKNIECIE
        if emp.role_permissions == RolePermission.SKLEP and s_type in (
            SolverShiftType.RANEK,
            SolverShiftType.ZAMKNIECIE,
        ):
            return False

        # Twarde ograniczenie 2a: Urlopy i L4
        if d in emp.leaves:
            return False

        # Twarde ograniczenie 2b: Dyspozycyjność (całodniowa lub przedział godzinowy)
        for unav in emp.unavailability:
            if unav.date != d:
                continue
            if not unav.start_time or not unav.end_time:
                return False
            u_start = time_str_to_minutes(unav.start_time)
            u_end = time_str_to_minutes(unav.end_time)
            if u_end <= u_start:
                u_end += 1440
            # Sprawdzenie przecięcia przedziałów [u_start, u_end) oraz [spec.start_min, spec.end_min)
            if max(u_start, spec.start_min) < min(u_end, spec.end_min):
                return False

        return True

    @staticmethod
    def has_kasa_permission(emp: EmployeeInput) -> bool:
        return emp.role_permissions in (RolePermission.KASA, RolePermission.KASA_I_SKLEP)

    @staticmethod
    def has_sklep_permission(emp: EmployeeInput) -> bool:
        return emp.role_permissions in (RolePermission.SKLEP, RolePermission.KASA_I_SKLEP)

    # ========================================================================
    # KROK 1: PRE-WALIDACJA (DIAGNOSTIC AUDIT)
    # ========================================================================
    def run_diagnostic_audit(self) -> List[str]:
        """
        Szybki audytor wykonalności uruchamiany przed solverem CP-SAT.
        Wykrywa twarde sprzeczności w danych wejściowych i zwraca listę czytelnych konfliktów.
        """
        conflicts: List[str] = []

        if not self.request.employees:
            conflicts.append("Brak pracowników w danych wejściowych do wygenerowania grafiku.")
            return conflicts

        total_required_minutes = 0
        for d_idx, d in enumerate(self.days):
            if not self.is_day_open(d):
                continue

            for s_type in self.ALL_SHIFTS:
                spec = self.day_shifts[(d_idx, s_type)]
                total_required_minutes += spec.min_staff * spec.duration_min

            # Sprawdzenie dostępności KASA i obsady na RANEK oraz ZAMKNIECIE
            avail_by_shift: Dict[SolverShiftType, List[EmployeeInput]] = {}
            kasa_by_shift: Dict[SolverShiftType, List[EmployeeInput]] = {}

            for s_type in (SolverShiftType.RANEK, SolverShiftType.ZAMKNIECIE):
                spec = self.day_shifts[(d_idx, s_type)]
                avail_emps = [
                    emp
                    for emp in self.request.employees
                    if self.is_employee_available_for_shift(emp, d_idx, s_type)
                ]
                kasa_emps = [emp for emp in avail_emps if self.has_kasa_permission(emp)]
                avail_by_shift[s_type] = avail_emps
                kasa_by_shift[s_type] = kasa_emps

                if spec.requires_kasa and len(kasa_emps) == 0:
                    conflicts.append(
                        f"Brak dostępnego pracownika z uprawnieniami KASA na zmianę {s_type.value} w dniu {d.isoformat()}"
                    )
                elif len(avail_emps) < spec.min_staff:
                    conflicts.append(
                        f"Niewystarczająca liczba dostępnych pracowników na zmianę {s_type.value} w dniu {d.isoformat()} "
                        f"(wymagane: {spec.min_staff}, dostępnych: {len(avail_emps)})"
                    )

            # Sprawdzenie czy unikalni kasjerzy mogą pokryć zarówno RANEK jak i ZAMKNIECIE (1 zmiana dziennie!)
            if (
                len(kasa_by_shift[SolverShiftType.RANEK]) > 0
                and len(kasa_by_shift[SolverShiftType.ZAMKNIECIE]) > 0
            ):
                unique_kasa_ids = {e.id for e in kasa_by_shift[SolverShiftType.RANEK]} | {
                    e.id for e in kasa_by_shift[SolverShiftType.ZAMKNIECIE]
                }
                if len(unique_kasa_ids) < 2:
                    only_emp_id = next(iter(unique_kasa_ids))
                    conflicts.append(
                        f"Tylko 1 pracownik z uprawnieniami KASA ({only_emp_id}) jest dostępny w dniu {d.isoformat()} "
                        f"– jedna osoba nie może obsadzić jednocześnie zmiany RANEK i ZAMKNIECIE"
                    )

            unique_open_close_ids = {e.id for e in avail_by_shift[SolverShiftType.RANEK]} | {
                e.id for e in avail_by_shift[SolverShiftType.ZAMKNIECIE]
            }
            req_open_close = (
                self.day_shifts[(d_idx, SolverShiftType.RANEK)].min_staff
                + self.day_shifts[(d_idx, SolverShiftType.ZAMKNIECIE)].min_staff
            )
            if (
                len(avail_by_shift[SolverShiftType.RANEK]) >= self.day_shifts[(d_idx, SolverShiftType.RANEK)].min_staff
                and len(avail_by_shift[SolverShiftType.ZAMKNIECIE]) >= self.day_shifts[(d_idx, SolverShiftType.ZAMKNIECIE)].min_staff
                and len(unique_open_close_ids) < req_open_close
            ):
                conflicts.append(
                    f"Niewystarczająca liczba unikalnych dostępnych pracowników do obsadzenia zmian RANEK i ZAMKNIECIE "
                    f"w dniu {d.isoformat()} (wymagane: {req_open_close}, dostępnych: {len(unique_open_close_ids)})"
                )

        # Sprawdzenie globalnego bilansu dostępnych godzin wszystkich pracowników
        total_available_minutes = 0
        for emp in self.request.employees:
            emp_phys_avail_minutes = 0
            avail_days_count = 0
            for d_idx, d in enumerate(self.days):
                if not self.is_day_open(d):
                    continue
                possible_durations = [
                    self.day_shifts[(d_idx, s_type)].duration_min
                    for s_type in self.ALL_SHIFTS
                    if self.is_employee_available_for_shift(emp, d_idx, s_type)
                ]
                if possible_durations:
                    avail_days_count += 1
                    emp_phys_avail_minutes += max(possible_durations)

            # Uwzględnienie maksymalnie 5 dni pracy na każde 6 dni
            if avail_days_count >= 6:
                max_legal_days = avail_days_count - (avail_days_count // 6)
                avg_dur = emp_phys_avail_minutes / avail_days_count
                emp_phys_avail_minutes = int(round(max_legal_days * avg_dur))

            # Ograniczenie przez maksymalny limit kontraktu pracownika
            contract_cap = self.emp_max_minutes[emp.id]
            total_available_minutes += min(emp_phys_avail_minutes, contract_cap)

        if total_available_minutes < total_required_minutes:
            avail_h = total_available_minutes / 60.0
            req_h = total_required_minutes / 60.0
            conflicts.append(
                f"Suma dostępnych godzin wszystkich pracowników ({avail_h:.1f}h) jest mniejsza niż suma godzin "
                f"wymaganych przez otwarcie sklepu i dostawy ({req_h:.1f}h)"
            )

        return conflicts

    # ========================================================================
    # KROK 2 & KROK 3: BUDOWA I ROZWIĄZANIE MODELU CP-SAT
    # ========================================================================
    def _solve_cp_sat(self, relax_constraints: bool = False) -> GeneratedScheduleResponse:
        t0 = time_module.perf_counter()
        model = cp_model.CpModel()

        employees = self.request.employees
        num_days = len(self.days)

        # 1. Zmienne decyzyjne: shift[(e_idx, d_idx, s_type)] in {0, 1}
        shift_vars: Dict[Tuple[int, int, SolverShiftType], cp_model.IntVar] = {}
        worked_day_vars: Dict[Tuple[int, int], cp_model.IntVar] = {}

        for e_idx, emp in enumerate(employees):
            for d_idx in range(num_days):
                day_shift_list = []
                for s_type in self.ALL_SHIFTS:
                    var = model.NewBoolVar(f"shift_e{e_idx}_d{d_idx}_{s_type.value}")
                    shift_vars[(e_idx, d_idx, s_type)] = var

                    # Twarde ograniczenie 2 i 3: Urlopy, Niedostępność i Uprawnienia SKLEP
                    if not self.is_employee_available_for_shift(emp, d_idx, s_type):
                        model.Add(var == 0)

                    day_shift_list.append(var)

                # Twarde ograniczenie 1: Maksymalnie 1 zmiana dziennie
                worked_var = model.NewBoolVar(f"worked_e{e_idx}_d{d_idx}")
                model.Add(sum(day_shift_list) == worked_var)
                worked_day_vars[(e_idx, d_idx)] = worked_var

        # 2. Twarde ograniczenie 3 & 4: Minimalna obsada zmian, uprawnienia KASA oraz dostawy (SRODEK)
        delivery_slack_vars: Dict[int, cp_model.IntVar] = {}
        critical_slack_vars: Dict[Tuple[int, SolverShiftType], cp_model.IntVar] = {}
        kasa_slack_vars: Dict[Tuple[int, SolverShiftType], cp_model.IntVar] = {}
        overstaff_vars: List[cp_model.IntVar] = []

        for d_idx, d in enumerate(self.days):
            if not self.is_day_open(d):
                continue

            for s_type in self.ALL_SHIFTS:
                spec = self.day_shifts[(d_idx, s_type)]
                all_assigned = [shift_vars[(e_idx, d_idx, s_type)] for e_idx in range(len(employees))]
                kasa_assigned = [
                    shift_vars[(e_idx, d_idx, s_type)]
                    for e_idx, emp in enumerate(employees)
                    if self.has_kasa_permission(emp)
                ]
                sklep_assigned = [
                    shift_vars[(e_idx, d_idx, s_type)]
                    for e_idx, emp in enumerate(employees)
                    if self.has_sklep_permission(emp)
                ]

                if s_type in (SolverShiftType.RANEK, SolverShiftType.ZAMKNIECIE):
                    if not relax_constraints:
                        # Krok 2: Twardy wymóg obsady otwarcia i zamknięcia + min. 1 KASA
                        model.Add(sum(all_assigned) >= spec.min_staff)
                        if spec.requires_kasa:
                            model.Add(sum(kasa_assigned) >= 1)
                    else:
                        # Krok 3: Najwyższy priorytet w systemie (-1_000_000 pkt za brak obsady RANEK/ZAMKNIECIE),
                        # ale ze zmienną slack, aby 1 problematyczny dzień nie unieważniał całego miesiąca
                        c_slack = model.NewIntVar(0, spec.min_staff, f"crit_slack_d{d_idx}_{s_type.value}")
                        critical_slack_vars[(d_idx, s_type)] = c_slack
                        model.Add(sum(all_assigned) + c_slack >= spec.min_staff)
                        if spec.requires_kasa:
                            k_slack = model.NewIntVar(0, 1, f"kasa_slack_d{d_idx}_{s_type.value}")
                            kasa_slack_vars[(d_idx, s_type)] = k_slack
                            model.Add(sum(kasa_assigned) + k_slack >= 1)

                    # Zmienna pomocnicza na ewentualną nadobsadę (tylko gdy potrzebna do wyrobienia etatu UoP)
                    over_v = model.NewIntVar(0, len(employees), f"over_d{d_idx}_{s_type.value}")
                    model.Add(sum(all_assigned) - spec.min_staff <= over_v)
                    overstaff_vars.append(over_v)

                elif s_type == SolverShiftType.SRODEK:
                    if spec.min_staff > 0:
                        if not relax_constraints:
                            model.Add(sum(all_assigned) >= spec.min_staff)
                            if spec.is_delivery_shift and sklep_assigned:
                                model.Add(sum(sklep_assigned) >= 1)
                        else:
                            # W trybie relaksacji (Krok 3) dopuszczamy slack na obsadzie zmiany SRODEK
                            slack_v = model.NewIntVar(0, spec.min_staff, f"deliv_slack_d{d_idx}")
                            delivery_slack_vars[d_idx] = slack_v
                            model.Add(sum(all_assigned) + slack_v >= spec.min_staff)

                        over_v = model.NewIntVar(0, len(employees), f"over_d{d_idx}_SRODEK")
                        model.Add(sum(all_assigned) - spec.min_staff <= over_v)
                        overstaff_vars.append(over_v)
                    else:
                        # Dzień bez dostawy i min_staff == 0: zmiana SRODEK opcjonalna (lekka kara za otwieranie bez potrzeby)
                        over_v = model.NewIntVar(0, len(employees), f"over_d{d_idx}_SRODEK_non_deliv")
                        model.Add(sum(all_assigned) == over_v)
                        overstaff_vars.append(over_v)

        # 3. Twarde ograniczenie 5: Kodeks Pracy (11h odpoczynku, doba pracownicza, 35h tygodniowo, max 5 dni z rzędu)
        kp_soft_penalty_terms: List[Any] = []
        for e_idx, emp in enumerate(employees):
            enforce_kp = (emp.contract_type == SolverContractType.UOP) or self.request.enforce_kp_for_uz
            if not enforce_kp:
                continue

            # 5a. 11-godzinny odpoczynek dobowy (Art. 132 KP) i zakaz naruszania doby pracowniczej (Art. 128 KP)
            for d_idx in range(num_days - 1):
                for s1 in self.ALL_SHIFTS:
                    spec1 = self.day_shifts[(d_idx, s1)]
                    for s2 in self.ALL_SHIFTS:
                        spec2 = self.day_shifts[(d_idx + 1, s2)]

                        rest_minutes = (1440 + spec2.start_min) - spec1.end_min
                        violates_11h_rest = rest_minutes < 11 * 60
                        violates_employee_day = spec2.start_min < spec1.start_min

                        if violates_11h_rest:
                            # 11h odpoczynku dobowego jest ZAWSZE bezwzględnie twarde (nawet w Kroku 3)
                            model.Add(
                                shift_vars[(e_idx, d_idx, s1)] + shift_vars[(e_idx, d_idx + 1, s2)] <= 1
                            )
                        elif violates_employee_day:
                            if not relax_constraints:
                                model.Add(
                                    shift_vars[(e_idx, d_idx, s1)] + shift_vars[(e_idx, d_idx + 1, s2)] <= 1
                                )
                            else:
                                # W Kroku 3 jeśli przerwa >= 11h (np. Sobota 23:00 -> Niedziela 10:00 = 11h przerwy),
                                # dopuszczamy ze zmienną kary, aby nie zostawić pustego otwarcia sklepu
                                emp_day_viol = model.NewBoolVar(f"emp_day_v_e{e_idx}_d{d_idx}_{s1.value}_{s2.value}")
                                model.Add(
                                    shift_vars[(e_idx, d_idx, s1)] + shift_vars[(e_idx, d_idx + 1, s2)] - 1 <= emp_day_viol
                                )
                                kp_soft_penalty_terms.append(emp_day_viol * (-500 * SCALE))

            # 5b. Maksymalny ciąg dni pracy: max 5 dni z rzędu (w każdym oknie 6 dni <= 5 dni pracy)
            if num_days >= 6:
                for w_start in range(num_days - 5):
                    if not relax_constraints:
                        model.Add(
                            sum(worked_day_vars[(e_idx, w_start + k)] for k in range(6)) <= 5
                        )
                    else:
                        consec_slack = model.NewIntVar(0, 1, f"consec_slack_e{e_idx}_w{w_start}")
                        model.Add(
                            sum(worked_day_vars[(e_idx, w_start + k)] for k in range(6)) - consec_slack <= 5
                        )
                        kp_soft_penalty_terms.append(consec_slack * (-25000 * SCALE))

            # 5c. 35-godzinny nieprzerwany odpoczynek tygodniowy (Art. 133 KP)
            if emp.contract_type == SolverContractType.UOP:
                rest35_day_vars: List[cp_model.IntVar] = []
                for d_idx in range(num_days):
                    r35 = model.NewBoolVar(f"rest35_e{e_idx}_d{d_idx}")
                    model.Add(r35 <= 1 - worked_day_vars[(e_idx, d_idx)])

                    if 0 < d_idx < num_days - 1:
                        for s1 in self.ALL_SHIFTS:
                            spec1 = self.day_shifts[(d_idx - 1, s1)]
                            for s2 in self.ALL_SHIFTS:
                                spec2 = self.day_shifts[(d_idx + 1, s2)]
                                rest_across_off_day = (2 * 1440 + spec2.start_min) - spec1.end_min
                                if rest_across_off_day < 35 * 60:
                                    if not relax_constraints:
                                        model.Add(
                                            shift_vars[(e_idx, d_idx - 1, s1)]
                                            + shift_vars[(e_idx, d_idx + 1, s2)]
                                            - worked_day_vars[(e_idx, d_idx)]
                                            <= 1
                                        )
                                    model.Add(
                                        r35
                                        + shift_vars[(e_idx, d_idx - 1, s1)]
                                        + shift_vars[(e_idx, d_idx + 1, s2)]
                                        <= 2
                                    )
                    rest35_day_vars.append(r35)

                if num_days >= 7:
                    for w_start in range(num_days - 6):
                        if not relax_constraints:
                            model.Add(sum(rest35_day_vars[w_start : w_start + 7]) >= 1)
                        else:
                            w35_slack = model.NewIntVar(0, 1, f"w35_slack_e{e_idx}_w{w_start}")
                            model.Add(sum(rest35_day_vars[w_start : w_start + 7]) + w35_slack >= 1)
                            kp_soft_penalty_terms.append(w35_slack * (-25000 * SCALE))

        # 4. Twarde ograniczenie 6: Bilans godzin miesięcznych (UoP oraz UZ) + Funkcja Celu Waga 100
        actual_minutes_vars: Dict[int, cp_model.IntVar] = {}
        abs_target_diff_vars: Dict[int, cp_model.IntVar] = {}
        hours_under_slack_vars: Dict[int, cp_model.IntVar] = {}
        hours_over_slack_vars: Dict[int, cp_model.IntVar] = {}

        max_month_minutes = num_days * 1440
        for e_idx, emp in enumerate(employees):
            terms = []
            for d_idx in range(num_days):
                for s_type in self.ALL_SHIFTS:
                    dur = self.day_shifts[(d_idx, s_type)].duration_min
                    if dur > 0:
                        terms.append(shift_vars[(e_idx, d_idx, s_type)] * dur)

            act_var = model.NewIntVar(0, max_month_minutes, f"actual_min_e{e_idx}")
            model.Add(act_var == sum(terms))
            actual_minutes_vars[e_idx] = act_var

            target_min = self.emp_target_minutes[emp.id]
            min_min = self.emp_min_minutes[emp.id]
            max_min = self.emp_max_minutes[emp.id]

            if not relax_constraints:
                model.Add(act_var >= min_min)
                model.Add(act_var <= max_min)
            else:
                under_s = model.NewIntVar(0, max_month_minutes, f"under_slack_e{e_idx}")
                over_s = model.NewIntVar(0, max_month_minutes, f"over_slack_e{e_idx}")
                hours_under_slack_vars[e_idx] = under_s
                hours_over_slack_vars[e_idx] = over_s
                model.Add(act_var + under_s >= min_min)
                model.Add(act_var - over_s <= max_min)

            # Wartość bezwzględna odchylenia od target_minutes: |actual_minutes - target_minutes|
            diff_signed = model.NewIntVar(-max_month_minutes, max_month_minutes, f"diff_signed_e{e_idx}")
            model.Add(diff_signed == act_var - target_min)
            abs_diff = model.NewIntVar(0, max_month_minutes, f"abs_diff_e{e_idx}")
            model.AddAbsEquality(abs_diff, diff_signed)
            abs_target_diff_vars[e_idx] = abs_diff

        # ====================================================================
        # FUNKCJA CELU (SOFT CONSTRAINTS)
        # ====================================================================
        objective_terms: List[Any] = []
        objective_terms.extend(kp_soft_penalty_terms)

        # 0. Kary za zmienne slack w trybie relaksacji (Krok 3) z rygorystyczną hierarchią ważności:
        #    1) Obsada kluczowych zmian RANEK i ZAMKNIECIE: -1_000_000 pkt (nigdy nie pomijaj otwarcia/zamknięcia!)
        #    2) Uprawnienia KASA na RANEK/ZAMKNIECIE: -500_000 pkt
        #    3) Obsada zmiany SRODEK w dzień dostawy: -50_000 pkt (niższy priorytet niż RANEK i ZAMKNIECIE!)
        #    4) Obsada zwykłej zmiany SRODEK bez dostawy: -5_000 pkt
        #    5) Przekroczenie / niedobór godzin kontraktowych: -200 pkt za minutę (-12_000 pkt za godzinę)
        if relax_constraints:
            for (d_idx, s_type), c_slack in critical_slack_vars.items():
                objective_terms.append(c_slack * (-1_000_000 * SCALE))
            for (d_idx, s_type), k_slack in kasa_slack_vars.items():
                objective_terms.append(k_slack * (-500_000 * SCALE))
            for d_idx, slack_v in delivery_slack_vars.items():
                is_deliv = self.day_shifts[(d_idx, SolverShiftType.SRODEK)].is_delivery_shift
                deliv_penalty = 50_000 if is_deliv else 5_000
                objective_terms.append(slack_v * (-deliv_penalty * SCALE))
            for e_idx in range(len(employees)):
                # Kara -200 za minutę (-12_000 pkt za 1h), czyli ~102_000 pkt za zmianę 8.5h —
                # znacznie mniej niż -1_000_000 pkt za brak obsady RANEK/ZAMKNIECIE, dzięki czemu
                # solver ZAWSZE woli przydzielić nadgodziny niż zostawić nieobsadzone otwarcie lub zamknięcie!
                objective_terms.append(hours_under_slack_vars[e_idx] * (-200))
                objective_terms.append(hours_over_slack_vars[e_idx] * (-200))

        # 1. Waga 100 (Najwyższy priorytet miękki): Idealne trafienie w target_hours
        # -100 pkt za każdą godzinę odchylenia = -100 jednostek (w skali *60) za każdą minutę odchylenia
        for e_idx in range(len(employees)):
            objective_terms.append(abs_target_diff_vars[e_idx] * (-WEIGHT_TARGET_HOURS))

        # 1b. Waga 250: Ręcznie zgłoszone dyspozycje na konkretną zmianę w danym dniu (requested_shifts)
        for e_idx, emp in enumerate(employees):
            if emp.requested_shifts:
                for d_idx, d in enumerate(self.days):
                    req_s = emp.requested_shifts.get(d.isoformat())
                    if req_s is not None:
                        for s_type in self.ALL_SHIFTS:
                            if s_type == req_s:
                                objective_terms.append(shift_vars[(e_idx, d_idx, s_type)] * (250 * SCALE))
                            else:
                                objective_terms.append(shift_vars[(e_idx, d_idx, s_type)] * (-80 * SCALE))

        # 2. Waga 50: Dopasowanie do preferencji zmianowych pracowników
        pref_map = {
            ShiftPreference.PREFER_RANEK: SolverShiftType.RANEK,
            ShiftPreference.PREFER_SRODEK: SolverShiftType.SRODEK,
            ShiftPreference.PREFER_ZAMKNIECIE: SolverShiftType.ZAMKNIECIE,
        }
        for e_idx, emp in enumerate(employees):
            preferred_shift = pref_map.get(emp.preferences)
            if preferred_shift is not None:
                for d_idx in range(num_days):
                    objective_terms.append(
                        shift_vars[(e_idx, d_idx, preferred_shift)] * (WEIGHT_SHIFT_PREFERENCE * SCALE)
                    )

        # 3. Waga 30: Sprawiedliwy podział weekendów (Soboty i Niedziele handlowe)
        weekend_day_indices = [
            d_idx for d_idx, d in enumerate(self.days) if d.weekday() >= 5 and self.is_day_open(d)
        ]
        if weekend_day_indices and len(employees) >= 2:
            weekend_counts: List[cp_model.IntVar] = []
            for e_idx in range(len(employees)):
                w_cnt = model.NewIntVar(0, len(weekend_day_indices), f"weekend_cnt_e{e_idx}")
                model.Add(w_cnt == sum(worked_day_vars[(e_idx, d_idx)] for d_idx in weekend_day_indices))
                weekend_counts.append(w_cnt)

            max_w = model.NewIntVar(0, len(weekend_day_indices), "max_weekends")
            min_w = model.NewIntVar(0, len(weekend_day_indices), "min_weekends")
            model.AddMaxEquality(max_w, weekend_counts)
            model.AddMinEquality(min_w, weekend_counts)
            weekend_spread = model.NewIntVar(0, len(weekend_day_indices), "weekend_spread")
            model.Add(weekend_spread == max_w - min_w)
            objective_terms.append(weekend_spread * (-WEIGHT_WEEKEND_FAIRNESS * SCALE))

        # 4. Waga 20: Stabilność rytmu dobowego (premia za bloki takich samych zmian w kolejnych dniach)
        for e_idx in range(len(employees)):
            for d_idx in range(num_days - 1):
                for s_type in self.ALL_SHIFTS:
                    same_shift = model.NewBoolVar(f"same_shift_e{e_idx}_d{d_idx}_{s_type.value}")
                    model.Add(same_shift <= shift_vars[(e_idx, d_idx, s_type)])
                    model.Add(same_shift <= shift_vars[(e_idx, d_idx + 1, s_type)])
                    objective_terms.append(same_shift * (WEIGHT_RHYTHM_STABILITY * SCALE))

        # 5. Waga 10: Optymalizacja kosztowa (UoP vs UZ) – priorytet wyczerpywania godzin UoP
        for e_idx, emp in enumerate(employees):
            if emp.contract_type == SolverContractType.UOP:
                for d_idx in range(num_days):
                    objective_terms.append(
                        worked_day_vars[(e_idx, d_idx)] * (WEIGHT_COST_UOP_VS_UZ * SCALE)
                    )

        # Delikatna kara (-2 pkt) za nadmiarową obsadę ponad min_staff, aby nie przypisywać zbędnych zmian UZ
        for over_v in overstaff_vars:
            objective_terms.append(over_v * (-2 * SCALE))

        model.Maximize(sum(objective_terms))

        # Uruchomienie Solvera CP-SAT
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = float(self.request.max_solve_time_seconds)
        solver.parameters.num_search_workers = 8

        status_code = solver.Solve(model)
        solve_time = round(time_module.perf_counter() - t0, 4)

        if status_code not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            return GeneratedScheduleResponse(
                status="INFEASIBLE",
                is_feasible=False,
                conflicts=[],
                warnings=[],
                solve_time_seconds=solve_time,
            )

        # Ekstrakcja rozwiązania
        assignments: List[ShiftAssignment] = []
        daily_schedule: Dict[str, Dict[str, List[str]]] = {}
        emp_worked_minutes: Dict[str, int] = {emp.id: 0 for emp in employees}
        emp_shifts_count: Dict[str, int] = {emp.id: 0 for emp in employees}
        emp_weekend_count: Dict[str, int] = {emp.id: 0 for emp in employees}

        for d_idx, d in enumerate(self.days):
            d_str = d.isoformat()
            daily_schedule[d_str] = {
                SolverShiftType.RANEK.value: [],
                SolverShiftType.SRODEK.value: [],
                SolverShiftType.ZAMKNIECIE.value: [],
            }
            if not self.is_day_open(d):
                continue

            for s_type in self.ALL_SHIFTS:
                spec = self.day_shifts[(d_idx, s_type)]
                for e_idx, emp in enumerate(employees):
                    if solver.Value(shift_vars[(e_idx, d_idx, s_type)]) == 1:
                        emp_name = f"{emp.first_name} {emp.last_name}".strip() or emp.id
                        assignments.append(
                            ShiftAssignment(
                                date=d,
                                shift_type=s_type,
                                employee_id=emp.id,
                                employee_name=emp_name,
                                start_time=spec.start_time,
                                end_time=spec.end_time,
                                duration_hours=round(spec.duration_min / 60.0, 2),
                                duration_minutes=spec.duration_min,
                            )
                        )
                        daily_schedule[d_str][s_type.value].append(emp.id)
                        emp_worked_minutes[emp.id] += spec.duration_min
                        emp_shifts_count[emp.id] += 1
                        if d.weekday() >= 5:
                            emp_weekend_count[emp.id] += 1

        # Podsumowanie godzin per pracownik
        hours_summary: Dict[str, EmployeeHoursSummary] = {}
        for emp in employees:
            worked_h = round(emp_worked_minutes[emp.id] / 60.0, 2)
            leave_h = round(self.emp_leave_minutes[emp.id] / 60.0, 2)
            target_h = round(self.emp_target_minutes[emp.id] / 60.0, 2)
            nominal_h = round(self.emp_nominal_minutes[emp.id] / 60.0, 2)
            total_counted_h = round(
                worked_h + leave_h if emp.contract_type == SolverContractType.UOP else worked_h, 2
            )
            diff_h = round(worked_h - target_h, 2)

            hours_summary[emp.id] = EmployeeHoursSummary(
                employee_id=emp.id,
                employee_name=f"{emp.first_name} {emp.last_name}".strip() or emp.id,
                contract_type=emp.contract_type,
                fte=emp.fte if emp.contract_type == SolverContractType.UOP else None,
                nominal_hours=nominal_h,
                leave_days=self.emp_leave_days_count[emp.id],
                leave_hours=leave_h,
                target_hours=target_h,
                worked_hours=worked_h,
                total_counted_hours=total_counted_h,
                difference_hours=diff_h,
                min_hours=round(self.emp_min_minutes[emp.id] / 60.0, 2),
                max_hours=round(self.emp_max_minutes[emp.id] / 60.0, 2),
                shifts_count=emp_shifts_count[emp.id],
                weekend_shifts_count=emp_weekend_count[emp.id],
            )

        # Zebranie ostrzeżeń (warnings) z relaksacji w Kroku 3
        warnings: List[str] = []
        used_relaxation = False
        if relax_constraints:
            for (d_idx, s_type), c_slack in critical_slack_vars.items():
                missing_crit = solver.Value(c_slack)
                if missing_crit > 0:
                    used_relaxation = True
                    d = self.days[d_idx]
                    warnings.append(
                        f"[KRYTYCZNE] W dniu {d.strftime('%d.%m')} ({d.isoformat()}) brak dostępnych pracowników "
                        f"na kluczową zmianę {s_type.value} (wszyscy na urlopie lub dniu wolnym)."
                    )
            for d_idx, slack_v in delivery_slack_vars.items():
                missing = solver.Value(slack_v)
                if missing > 0:
                    used_relaxation = True
                    d = self.days[d_idx]
                    warnings.append(
                        f"Z powodu braku dostępności kadr, w dniu {d.strftime('%d.%m')} ({d.isoformat()}) "
                        f"nie obsadzono zmiany Środek na dostawę (brakuje: {missing} os.)."
                    )
            for e_idx, emp in enumerate(employees):
                under_val = solver.Value(hours_under_slack_vars[e_idx])
                over_val = solver.Value(hours_over_slack_vars[e_idx])
                emp_label = f"{emp.first_name} {emp.last_name}".strip() or emp.id
                if under_val > 0:
                    used_relaxation = True
                    warnings.append(
                        f"Poluzowano dolny limit godzin dla pracownika {emp_label} ({emp.contract_type.value}): "
                        f"przydzielono {hours_summary[emp.id].worked_hours}h wobec celu {hours_summary[emp.id].target_hours}h."
                    )
                if over_val > 0:
                    used_relaxation = True
                    warnings.append(
                        f"Poluzowano górny limit godzin dla pracownika {emp_label} ({emp.contract_type.value}): "
                        f"przydzielono {hours_summary[emp.id].worked_hours}h wobec limitu {hours_summary[emp.id].max_hours}h."
                    )

        if relax_constraints and used_relaxation:
            final_status = "RELAXED"
        else:
            final_status = "OPTIMAL" if status_code == cp_model.OPTIMAL else "FEASIBLE"

        return GeneratedScheduleResponse(
            status=final_status,
            is_feasible=True,
            assignments=assignments,
            daily_schedule=daily_schedule,
            hours_summary=hours_summary,
            warnings=warnings,
            conflicts=[],
            objective_value=round(solver.ObjectiveValue() / float(SCALE), 2),
            solve_time_seconds=solve_time,
        )

    def generate(self) -> GeneratedScheduleResponse:
        """
        Główna metoda uruchamiająca 3-stopniowy proces generowania grafiku:
        1. Pre-walidacja (Diagnostic Audit)
        2. Rozwiązanie CP-SAT z twardymi więzami
        3. Automatyczna relaksacja (Fallback) w przypadku INFEASIBLE
        """
        if not self.request.employees:
            return GeneratedScheduleResponse(
                status="INFEASIBLE",
                is_feasible=False,
                conflicts=["Brak pracowników w danych wejściowych do wygenerowania grafiku."],
                warnings=[],
            )

        # Krok 1: Pre-walidacja
        audit_conflicts = self.run_diagnostic_audit()
        if audit_conflicts and self.request.strict_audit:
            return GeneratedScheduleResponse(
                status="INFEASIBLE",
                is_feasible=False,
                conflicts=audit_conflicts,
                warnings=[],
            )

        # Krok 2: Twarde więzy (tylko gdy audyt nie wykrył twardych braków kadrowych)
        hard_solve_time = 0.0
        if not audit_conflicts:
            hard_result = self._solve_cp_sat(relax_constraints=False)
            hard_solve_time = hard_result.solve_time_seconds
            if hard_result.is_feasible:
                return hard_result

        # Krok 3: Automatyczna relaksacja (Fallback) – priorytetyzuje RANEK i ZAMKNIECIE ponad wszystko
        logger.warning(
            "Uruchamiam Krok 3: Automatyczna relaksacja (Fallback) z bezwzględnym priorytetem dla zmian RANEK i ZAMKNIECIE."
        )
        relaxed_result = self._solve_cp_sat(relax_constraints=True)
        if relaxed_result.is_feasible:
            for c_msg in audit_conflicts:
                if c_msg not in relaxed_result.warnings:
                    relaxed_result.warnings.insert(0, c_msg)
            if not relaxed_result.warnings:
                relaxed_result.warnings.append(
                    "Wygenerowano grafik w trybie automatycznej relaksacji więzów miękkich/pobocznych."
                )
            return relaxed_result

        return GeneratedScheduleResponse(
            status="INFEASIBLE",
            is_feasible=False,
            conflicts=audit_conflicts or [
                "Brak dopuszczalnego grafiku spełniającego bezwzględne wymogi 11h odpoczynku dobowego "
                "i ciągłości obsady kasowej przy podanych dyspozycjach pracowników."
            ],
            warnings=[],
            solve_time_seconds=hard_solve_time + relaxed_result.solve_time_seconds,
        )

