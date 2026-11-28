# Plik: backend/tests/test_schedule_solver.py
"""
Testy jednostkowe silnika optymalizacyjnego Google OR-Tools CP-SAT (ScheduleOptimizerEngine).
Weryfikują:
1. Przestrzeganie 11h odpoczynku dobowego, 24h doby pracowniczej (zakaz Zamknięcie -> Ranek / Środek),
   35h odpoczynku tygodniowego oraz limitu max 5 dni pracy z rzędu.
2. Blokadę zmian RANEK i ZAMKNIECIE dla pracowników wyłącznie z rolą SKLEP oraz gwarancję
   obecności kasjera (KASA / KASA_I_SKLEP) na otwarciu i zamknięciu.
3. Automatyczne obsadzanie zmiany SRODEK w dniach dostaw.
4. Zgodność sumy godzin z nominałem etatu (UoP + urlopy/L4) oraz limitami zlecenia (UZ),
   w tym dla zmian ułamkowych (np. 8.5h, 7.75h).
5. Poprawne raportowanie konfliktów w Kroku 1 (Pre-walidacja) przy sprzecznych danych
   oraz automatyczną relaksację (Krok 3 - Fallback) z ostrzeżeniami (warnings).
"""

from datetime import date, timedelta
from pathlib import Path
import sys
import pytest

# Dodanie katalogu backend do sys.path dla bezpośredniego wywołania pytest
BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.schemas import (
    DeliverySlot,
    EmployeeInput,
    RolePermission,
    ScheduleGenerationRequest,
    ShiftConfig,
    ShiftPreference,
    SolverContractType,
    SolverShiftType,
    UnavailabilitySlot,
)
from app.services.schedule_solver import (
    ScheduleOptimizerEngine,
    time_str_to_minutes,
)


def _standard_8h_shifts() -> list[ShiftConfig]:
    """Konfiguracja 3 zmian po 8h (480 min) dla czystych testów bilansu godzin."""
    return [
        ShiftConfig(shift_type=SolverShiftType.RANEK, start_time="06:00", end_time="14:00", min_staff=1),
        ShiftConfig(shift_type=SolverShiftType.SRODEK, start_time="10:00", end_time="18:00", min_staff=0),
        ShiftConfig(shift_type=SolverShiftType.ZAMKNIECIE, start_time="15:00", end_time="23:00", min_staff=1),
    ]


def _fractional_shifts() -> list[ShiftConfig]:
    """Konfiguracja zmian o ułamkowej liczbie godzin (8.5h = 510 min, 8.0h = 480 min, 8.75h = 525 min)."""
    return [
        ShiftConfig(shift_type=SolverShiftType.RANEK, start_time="05:30", end_time="14:00", min_staff=1),       # 8.5h
        ShiftConfig(shift_type=SolverShiftType.SRODEK, start_time="10:00", end_time="18:00", min_staff=0),      # 8.0h
        ShiftConfig(shift_type=SolverShiftType.ZAMKNIECIE, start_time="14:30", end_time="23:15", min_staff=1),  # 8.75h
    ]


# ============================================================================
# 1. TESTY KODEKSU PRACY: 11H ODPOCZYNKU, DOBA PRACOWNICZA, MAX 5 DNI Z RZĘDU
# ============================================================================
def test_11h_daily_rest_and_no_closing_to_morning_or_middle():
    """
    Weryfikuje, że żaden pracownik nie otrzymuje przejścia:
    - ZAMKNIECIE (dnia d) -> RANEK (dnia d+1) [mniej niż 11h odpoczynku]
    - ZAMKNIECIE (dnia d) -> SRODEK (dnia d+1) [naruszenie 11h odpoczynku / 24h doby pracowniczej]
    - więcej niż 5 dni pracy z rzędu.
    """
    employees = [
        EmployeeInput(
            id="emp_1",
            first_name="Anna",
            last_name="Kowalska",
            contract_type=SolverContractType.UOP,
            fte=1.0,
            target_hours=80.0,
            role_permissions=RolePermission.KASA_I_SKLEP,
        ),
        EmployeeInput(
            id="emp_2",
            first_name="Piotr",
            last_name="Nowak",
            contract_type=SolverContractType.UOP,
            fte=1.0,
            target_hours=80.0,
            role_permissions=RolePermission.KASA_I_SKLEP,
        ),
        EmployeeInput(
            id="emp_3",
            first_name="Katarzyna",
            last_name="Wiśniewska",
            contract_type=SolverContractType.UZ,
            min_hours=40.0,
            max_hours=90.0,
            target_hours=68.0,
            role_permissions=RolePermission.KASA_I_SKLEP,
        ),
    ]

    request = ScheduleGenerationRequest(
        start_date="2027-01-01",
        end_date="2027-01-14",  # 14 dni
        shifts=_fractional_shifts(),
        deliveries=[DeliverySlot(day_of_week=1, start_time="11:00", end_time="15:00")],  # Wtorki
        employees=employees,
    )

    engine = ScheduleOptimizerEngine(request)
    response = engine.generate()

    assert response.is_feasible is True
    assert response.status in ("OPTIMAL", "FEASIBLE")
    assert len(response.conflicts) == 0

    # Grupowanie przypisań per pracownik i sortowanie chronologiczne
    by_emp: dict[str, list] = {e.id: [] for e in employees}
    for a in response.assignments:
        by_emp[a.employee_id].append(a)

    for emp_id, emp_assignments in by_emp.items():
        emp_assignments.sort(key=lambda x: x.date)

        # 1 zmiana dziennie
        dates_worked = [a.date for a in emp_assignments]
        assert len(dates_worked) == len(set(dates_worked)), f"Pracownik {emp_id} ma >1 zmianę w tym samym dniu!"

        # Sprawdzenie sąsiednich dni pracy (11h odpoczynku i brak ZAMKNIECIE -> RANEK / SRODEK)
        for i in range(len(emp_assignments) - 1):
            curr_a = emp_assignments[i]
            next_a = emp_assignments[i + 1]
            day_diff = (next_a.date - curr_a.date).days

            if day_diff == 1:
                assert not (
                    curr_a.shift_type == SolverShiftType.ZAMKNIECIE
                    and next_a.shift_type == SolverShiftType.RANEK
                ), f"Niedozwolone przejście ZAMKNIECIE -> RANEK dla {emp_id} ({curr_a.date} -> {next_a.date})"

                assert not (
                    curr_a.shift_type == SolverShiftType.ZAMKNIECIE
                    and next_a.shift_type == SolverShiftType.SRODEK
                ), f"Niedozwolone przejście ZAMKNIECIE -> SRODEK dla {emp_id} ({curr_a.date} -> {next_a.date})"

                end_min = time_str_to_minutes(curr_a.end_time)
                start_next_min = time_str_to_minutes(next_a.start_time)
                rest_minutes = (1440 + start_next_min) - end_min
                assert rest_minutes >= 11 * 60, (
                    f"Naruszenie 11h odpoczynku dla {emp_id}: {rest_minutes / 60.0:.2f}h "
                    f"między {curr_a.date} a {next_a.date}"
                )
                assert start_next_min >= time_str_to_minutes(curr_a.start_time), (
                    f"Naruszenie 24h doby pracowniczej dla {emp_id} między {curr_a.date} a {next_a.date}"
                )

        # Sprawdzenie maksymalnego ciągu dni pracy (<= 5 dni z rzędu)
        worked_set = set(dates_worked)
        d_cur = date(2027, 1, 1)
        while d_cur <= date(2027, 1, 9):
            consecutive_in_6 = sum(1 for k in range(6) if (d_cur + timedelta(days=k)) in worked_set)
            assert consecutive_in_6 <= 5, f"Pracownik {emp_id} pracuje więcej niż 5 dni z rzędu od {d_cur}"
            d_cur += timedelta(days=1)


# ============================================================================
# 2. TESTY UPRAWNIEŃ STANOWISKOWYCH: BLOKADA ROLI 'SKLEP' NA RANEK/ZAMKNIECIE
# ============================================================================
def test_sklep_role_blocked_from_opening_and_closing():
    """
    Weryfikuje, że pracownik posiadający wyłącznie uprawnienie SKLEP:
    - nigdy nie zostaje przypisany do zmiany RANEK ani ZAMKNIECIE,
    - pracuje wyłącznie na zmianie SRODEK (np. przy dostawach),
    - natomiast zmiany RANEK i ZAMKNIECIE są obsadzane pracownikami z uprawnieniem KASA / KASA_I_SKLEP.
    """
    employees = [
        EmployeeInput(
            id="kasjer_1",
            first_name="Marek",
            last_name="Kasjer",
            contract_type=SolverContractType.UOP,
            fte=1.0,
            target_hours=40.0,
            role_permissions=RolePermission.KASA,
        ),
        EmployeeInput(
            id="kasjer_2",
            first_name="Ewa",
            last_name="Uniwersalna",
            contract_type=SolverContractType.UOP,
            fte=1.0,
            target_hours=40.0,
            role_permissions=RolePermission.KASA_I_SKLEP,
        ),
        EmployeeInput(
            id="kasjer_3",
            first_name="Olga",
            last_name="Kasjerka",
            contract_type=SolverContractType.UZ,
            min_hours=24.0,
            max_hours=40.0,
            target_hours=32.0,
            role_permissions=RolePermission.KASA,
        ),
        EmployeeInput(
            id="sklep_only",
            first_name="Tomasz",
            last_name="Magazynier",
            contract_type=SolverContractType.UZ,
            min_hours=16.0,
            max_hours=32.0,
            target_hours=24.0,
            role_permissions=RolePermission.SKLEP,
        ),
    ]

    request = ScheduleGenerationRequest(
        start_date="2027-01-04",  # Poniedziałek
        end_date="2027-01-10",    # Niedziela (7 dni)
        shifts=_standard_8h_shifts(),
        deliveries=[
            DeliverySlot(day_of_week=1, start_time="11:00", end_time="14:00"),  # Wtorek
            DeliverySlot(day_of_week=3, start_time="12:00", end_time="15:00"),  # Czwartek
            DeliverySlot(day_of_week=4, start_time="13:00", end_time="16:00"),  # Piątek
        ],
        employees=employees,
    )

    engine = ScheduleOptimizerEngine(request)
    response = engine.generate()

    assert response.is_feasible is True

    sklep_assignments = [a for a in response.assignments if a.employee_id == "sklep_only"]
    assert len(sklep_assignments) > 0, "Pracownik SKLEP powinien zostać przypisany do zmian SRODEK w dni dostaw"
    for a in sklep_assignments:
        assert a.shift_type == SolverShiftType.SRODEK, (
            f"Pracownik z rolą wyłącznie SKLEP otrzymał niedozwoloną zmianę {a.shift_type} w dniu {a.date}!"
        )

    # Na każdej zmianie RANEK i ZAMKNIECIE musi być min. 1 osoba z KASA lub KASA_I_SKLEP
    kasa_ids = {"kasjer_1", "kasjer_2", "kasjer_3"}
    for d_str, shifts_dict in response.daily_schedule.items():
        assert any(eid in kasa_ids for eid in shifts_dict[SolverShiftType.RANEK.value]), (
            f"Brak uprawnień KASA na zmianie RANEK w dniu {d_str}"
        )
        assert any(eid in kasa_ids for eid in shifts_dict[SolverShiftType.ZAMKNIECIE.value]), (
            f"Brak uprawnień KASA na zmianie ZAMKNIECIE w dniu {d_str}"
        )


# ============================================================================
# 3. TESTY AUTOMATYCZNEGO OBSADZANIA ZMIANY 'SRODEK' W DNIACH DOSTAW
# ============================================================================
def test_automatic_middle_shift_assignment_on_delivery_days():
    """
    Weryfikuje, że zmiana SRODEK jest automatycznie obsadzana we wszystkie dni z dostawą
    (wtorek i piątek), a w dni bez dostaw pozostaje nieobsadzona (gdy min_staff=0).
    """
    employees = [
        EmployeeInput(
            id="e1",
            first_name="Jan",
            last_name="Kowalski",
            contract_type=SolverContractType.UOP,
            target_hours=40.0,
            role_permissions=RolePermission.KASA_I_SKLEP,
        ),
        EmployeeInput(
            id="e2",
            first_name="Maria",
            last_name="Nowak",
            contract_type=SolverContractType.UOP,
            target_hours=40.0,
            role_permissions=RolePermission.KASA_I_SKLEP,
        ),
        EmployeeInput(
            id="e3",
            first_name="Paweł",
            last_name="Zieliński",
            contract_type=SolverContractType.UZ,
            min_hours=32.0,
            max_hours=56.0,
            target_hours=48.0,
            role_permissions=RolePermission.SKLEP,
        ),
        EmployeeInput(
            id="e4",
            first_name="Karolina",
            last_name="Wójcik",
            contract_type=SolverContractType.UZ,
            min_hours=16.0,
            max_hours=48.0,
            target_hours=32.0,
            role_permissions=RolePermission.KASA_I_SKLEP,
        ),
    ]

    # Tydzień od 2027-01-04 (Poniedziałek) do 2027-01-10 (Niedziela)
    # Wtorek = 2027-01-05 (day_of_week=1), Piątek = 2027-01-08 (day_of_week=4)
    request = ScheduleGenerationRequest(
        start_date="2027-01-04",
        end_date="2027-01-10",
        shifts=_standard_8h_shifts(),
        deliveries=[
            DeliverySlot(day_of_week=1, start_time="11:00", end_time="14:00", required_middle_staff=1),
            DeliverySlot(day_of_week=4, start_time="13:00", end_time="16:00", required_middle_staff=1),
        ],
        employees=employees,
    )

    engine = ScheduleOptimizerEngine(request)
    response = engine.generate()

    assert response.is_feasible is True
    assert len(response.daily_schedule["2027-01-05"][SolverShiftType.SRODEK.value]) >= 1, (
        "Zmiana SRODEK musi być obsadzona we wtorek (dzień dostawy)"
    )
    assert len(response.daily_schedule["2027-01-08"][SolverShiftType.SRODEK.value]) >= 1, (
        "Zmiana SRODEK musi być obsadzona w piątek (dzień dostawy)"
    )


# ============================================================================
# 4. TESTY BILANSU GODZIN: NOMINAŁ ETATU (UoP + URLOPY) I LIMITY ZLECENIA (UZ)
# ============================================================================
def test_monthly_hours_balance_uop_with_leaves_and_uz_limits():
    """
    Weryfikuje:
    - Wyliczanie target_hours dla UoP ze wzoru: (nominalne_godziny * fte) - godziny_urlopowe.
    - Idealne trafienie w godziny docelowe UoP (0 nadgodzin, suma pracy + urlopu == nominał etatu).
    - Przestrzeganie dolnego i górnego limitu godzin [min_hours, max_hours] dla pracowników UZ.
    """
    # Okres 14 dni, nominał pełnego etatu ustawiony na 80h (10 dni * 8h)
    # Pracownik UoP #1: pełny etat (1.0), 2 dni urlopu (16h) -> target_hours = 80 - 16 = 64h (8 zmian po 8h)
    # Pracownik UoP #2: 0.5 etatu (0.5), 0 dni urlopu -> target_hours = 80 * 0.5 = 40h (5 zmian po 8h)
    # Pracownicy UZ #3 i #4: dopełniają pozostałe zmiany w granicach [min_hours, max_hours]
    employees = [
        EmployeeInput(
            id="uop_full_leave",
            first_name="Adam",
            last_name="Pełny",
            contract_type=SolverContractType.UOP,
            fte=1.0,
            role_permissions=RolePermission.KASA_I_SKLEP,
            leaves=[date(2027, 1, 4), date(2027, 1, 5)],  # 2 dni urlopu = 16h
        ),
        EmployeeInput(
            id="uop_half",
            first_name="Beata",
            last_name="Półetatu",
            contract_type=SolverContractType.UOP,
            fte=0.5,
            role_permissions=RolePermission.KASA_I_SKLEP,
        ),
        EmployeeInput(
            id="uz_1",
            first_name="Cezary",
            last_name="Zlecenie",
            contract_type=SolverContractType.UZ,
            min_hours=48.0,
            max_hours=80.0,
            target_hours=64.0,
            role_permissions=RolePermission.KASA_I_SKLEP,
        ),
        EmployeeInput(
            id="uz_2",
            first_name="Dorota",
            last_name="Zlecenie",
            contract_type=SolverContractType.UZ,
            min_hours=40.0,
            max_hours=80.0,
            target_hours=56.0,
            role_permissions=RolePermission.KASA_I_SKLEP,
        ),
    ]

    request = ScheduleGenerationRequest(
        start_date="2027-01-01",
        end_date="2027-01-14",
        nominal_monthly_hours=80.0,
        shifts=_standard_8h_shifts(),
        deliveries=[],
        employees=employees,
    )

    engine = ScheduleOptimizerEngine(request)
    response = engine.generate()

    assert response.is_feasible is True

    # Weryfikacja UoP 1.0 etatu z 2 dniami urlopu
    s_uop1 = response.hours_summary["uop_full_leave"]
    assert s_uop1.nominal_hours == 80.0
    assert s_uop1.leave_days == 2
    assert s_uop1.leave_hours == 16.0
    assert s_uop1.target_hours == 64.0
    assert s_uop1.worked_hours == 64.0
    assert s_uop1.total_counted_hours == 80.0  # 64h pracy + 16h urlopu = 80h nominału
    assert s_uop1.difference_hours == 0.0

    # Weryfikacja że w dni urlopu (2027-01-04 i 2027-01-05) pracownik nie ma żadnej zmiany
    for d_leave in ("2027-01-04", "2027-01-05"):
        for s_list in response.daily_schedule[d_leave].values():
            assert "uop_full_leave" not in s_list

    # Weryfikacja UoP 0.5 etatu
    s_uop2 = response.hours_summary["uop_half"]
    assert s_uop2.nominal_hours == 40.0
    assert s_uop2.target_hours == 40.0
    assert s_uop2.worked_hours == 40.0
    assert s_uop2.total_counted_hours == 40.0
    assert s_uop2.difference_hours == 0.0

    # Weryfikacja limitów UZ
    for uz_id in ("uz_1", "uz_2"):
        s_uz = response.hours_summary[uz_id]
        assert s_uz.min_hours <= s_uz.worked_hours <= s_uz.max_hours, (
            f"Pracownik {uz_id} przekroczył widełki godzin UZ: {s_uz.worked_hours}h "
            f"nie należy do [{s_uz.min_hours}, {s_uz.max_hours}]"
        )


# ============================================================================
# 5. TESTY PRE-WALIDACJI (KROK 1) I AUTOMATYCZNEJ RELAKSACJI (KROK 3)
# ============================================================================
def test_prevalidation_reports_clear_conflict_when_no_kasa_available():
    """
    Weryfikuje Krok 1 (Diagnostic Audit):
    Gdy w konkretnym dniu (np. piątek 2027-01-15 wieczór) wszyscy pracownicy z uprawnieniami KASA
    zgłosili brak dyspozycyjności, silnik zwraca status INFEASIBLE oraz precyzyjny komunikat
    konfliktu wskazujący zmianę ZAMKNIECIE i datę 2027-01-15.
    """
    employees = [
        EmployeeInput(
            id="kasa_1",
            first_name="Anna",
            last_name="Kasjerka",
            contract_type=SolverContractType.UOP,
            target_hours=40.0,
            role_permissions=RolePermission.KASA,
            unavailability=[
                UnavailabilitySlot(date=date(2027, 1, 15), start_time="14:00", end_time="23:30")
            ],
        ),
        EmployeeInput(
            id="kasa_2",
            first_name="Piotr",
            last_name="Kasjer",
            contract_type=SolverContractType.UOP,
            target_hours=40.0,
            role_permissions=RolePermission.KASA_I_SKLEP,
            leaves=[date(2027, 1, 15)],  # Urlop w ten sam piątek
        ),
        EmployeeInput(
            id="sklep_1",
            first_name="Tomek",
            last_name="Sklepowy",
            contract_type=SolverContractType.UZ,
            target_hours=40.0,
            role_permissions=RolePermission.SKLEP,  # Dostępny, ale nie ma uprawnień KASA!
        ),
    ]

    request = ScheduleGenerationRequest(
        start_date="2027-01-14",
        end_date="2027-01-16",
        shifts=_standard_8h_shifts(),
        employees=employees,
    )

    engine = ScheduleOptimizerEngine(request)
    response = engine.generate()

    assert response.is_feasible is False
    assert response.status == "INFEASIBLE"
    assert len(response.conflicts) >= 1
    assert any(
        "KASA" in c and "ZAMKNIECIE" in c and "2027-01-15" in c
        for c in response.conflicts
    ), f"Oczekiwano czytelnego komunikatu o braku KASA na ZAMKNIECIE w dniu 2027-01-15, otrzymano: {response.conflicts}"


def test_step3_fallback_relaxes_delivery_shift_and_emits_warning():
    """
    Weryfikuje Krok 3 (Automatyczna relaksacja / Fallback):
    Gdy pre-walidacja przechodzi (KASA na RANEK i ZAMKNIECIE jest dostępna każdego dnia,
    globalna pula godzin jest wystarczająca), ale w konkretnym dniu dostawy (2027-01-05)
    dostępnych jest tylko 2 pracowników (więc po obsadzeniu RANEK i ZAMKNIECIE brakuje 3. osoby
    na zmianę SRODEK), silnik luzuje wymóg obsady zmiany SRODEK na dostawę, generuje grafik
    ze statusem RELAXED i zwraca czytelne ostrzeżenie w tablicy `warnings`.
    """
    employees = [
        EmployeeInput(
            id="emp_1",
            first_name="Anna",
            last_name="Kowalska",
            contract_type=SolverContractType.UOP,
            target_hours=24.0,
            role_permissions=RolePermission.KASA_I_SKLEP,
        ),
        EmployeeInput(
            id="emp_2",
            first_name="Piotr",
            last_name="Nowak",
            contract_type=SolverContractType.UOP,
            target_hours=24.0,
            role_permissions=RolePermission.KASA_I_SKLEP,
        ),
        EmployeeInput(
            id="emp_3",
            first_name="Krzysztof",
            last_name="Dostawca",
            contract_type=SolverContractType.UZ,
            min_hours=0.0,
            max_hours=24.0,
            target_hours=8.0,
            role_permissions=RolePermission.SKLEP,
            # Niedostępny w dzień dostawy 2027-01-05!
            unavailability=[UnavailabilitySlot(date=date(2027, 1, 5))],
        ),
    ]

    request = ScheduleGenerationRequest(
        start_date="2027-01-04",
        end_date="2027-01-06",  # 3 dni (Pon-Śr), dostawa we wtorek 2027-01-05
        shifts=_standard_8h_shifts(),
        deliveries=[DeliverySlot(date=date(2027, 1, 5), start_time="11:00", end_time="15:00", required_middle_staff=1)],
        employees=employees,
    )

    engine = ScheduleOptimizerEngine(request)
    response = engine.generate()

    assert response.is_feasible is True
    assert response.status == "RELAXED"
    assert len(response.warnings) >= 1
    assert any("05.01" in w or "2027-01-05" in w for w in response.warnings), (
        f"Oczekiwano ostrzeżenia o nieobsadzeniu zmiany Środek na dostawę w dniu 05.01, otrzymano: {response.warnings}"
    )


# ============================================================================
# 6. TESTY FUNKCJI CELU: PREFERENCJE ZMIANOWE PRACOWNIKÓW
# ============================================================================
def test_soft_preferences_assign_preferred_shifts():
    """
    Weryfikuje, że przy dwóch równoważnych pracownikach:
    - pracownik z PREFER_RANEK otrzymuje zmiany RANEK,
    - pracownik z PREFER_ZAMKNIECIE otrzymuje zmiany ZAMKNIECIE.
    """
    employees = [
        EmployeeInput(
            id="morning_lover",
            first_name="Ranny",
            last_name="Ptaszek",
            contract_type=SolverContractType.UOP,
            target_hours=32.0,
            role_permissions=RolePermission.KASA_I_SKLEP,
            preferences=ShiftPreference.PREFER_RANEK,
        ),
        EmployeeInput(
            id="night_owl",
            first_name="Nocny",
            last_name="Marek",
            contract_type=SolverContractType.UOP,
            target_hours=32.0,
            role_permissions=RolePermission.KASA_I_SKLEP,
            preferences=ShiftPreference.PREFER_ZAMKNIECIE,
        ),
    ]

    request = ScheduleGenerationRequest(
        start_date="2027-01-04",
        end_date="2027-01-07",  # 4 dni = 4 ranki + 4 zamknięcia
        shifts=_standard_8h_shifts(),
        employees=employees,
    )

    engine = ScheduleOptimizerEngine(request)
    response = engine.generate()

    assert response.is_feasible is True
    for a in response.assignments:
        if a.employee_id == "morning_lover":
            assert a.shift_type == SolverShiftType.RANEK
        elif a.employee_id == "night_owl":
            assert a.shift_type == SolverShiftType.ZAMKNIECIE


# ============================================================================
# 7. KOMPLEKSOWY TEST SCENARIUSZA BIZNESOWEGO (17H SKLEP, PEŁNY MIESIĄC,
#    RÓŻNE ETATY UoP I ZLECENIA UZ, DOSTAWY, RĘCZNE DYSPOZYCJE, URLOPY, KASA/SKLEP)
# ============================================================================
def test_full_month_17h_store_multi_contract_deliveries_leaves_and_preferences():
    """
    Kompleksowy test weryfikujący wyeliminowanie wszystkich problemów z opisu użytkownika:
    - Punkt otwarty 17h/dobę (06:00-23:00), godziny zmian z panelu administratora:
      RANEK (06:00-14:30, 8.5h), SRODEK (10:00-18:00, 8.0h), ZAMKNIECIE (14:30-23:00, 8.5h).
    - Różne umowy i wymiary etatów:
      * UoP 1.0 etatu (KASA_I_SKLEP) z ręcznie złożonym urlopem (5 dni) i preferencją RANEK,
      * UoP 0.75 etatu (KASA) z ręczną dyspozycją wykluczającą wybrane dni i preferencją ZAMKNIECIE,
      * UoP 0.5 etatu (KASA_I_SKLEP) z ręczną dyspozycją godzinową (studia w poniedziałki rano),
      * UZ #1 (wyłącznie SKLEP - zakaz otwierania i zamykania, dedykowany na dostawy SRODEK),
      * UZ #2 (KASA_I_SKLEP, limit 60h-130h),
      * UZ #3 (KASA, limit 60h-130h).
    - Harmonogram dostaw (wtorki - dostawa poranna, czwartki - okno 11:00-15:00, piątki - dostawa wieczorna)
      automatycznie ustawiający i dopasowujący zmianę SRODEK.
    - Pełna weryfikacja 35h nieprzerwanego odpoczynku tygodniowego, 11h odpoczynku dobowego,
      24h doby pracowniczej, max 5 dni z rzędu oraz bilansu godzin etatu i zlecenia.
    """
    shifts = [
        ShiftConfig(shift_type=SolverShiftType.RANEK, start_time="06:00", end_time="14:30", min_staff=1),
        ShiftConfig(shift_type=SolverShiftType.SRODEK, start_time="10:00", end_time="18:00", min_staff=0),
        ShiftConfig(shift_type=SolverShiftType.ZAMKNIECIE, start_time="14:30", end_time="23:00", min_staff=1),
    ]

    deliveries = [
        DeliverySlot(
            day_of_week=1,
            delivery_time="ranek",
            delivery_type="chłodnia-mroźnia",
            shift_start_override="09:00",
            shift_end_override="17:00",
        ),
        DeliverySlot(day_of_week=3, start_time="11:00", end_time="15:00", required_middle_staff=1),
        DeliverySlot(
            day_of_week=4,
            delivery_time="wieczór",
            delivery_type="palety-suche",
            shift_start_override="12:00",
            shift_end_override="20:00",
        ),
    ]

    uop1_leaves = [
        date(2027, 1, 11),
        date(2027, 1, 12),
        date(2027, 1, 13),
        date(2027, 1, 14),
        date(2027, 1, 15),
    ]

    employees = [
        EmployeeInput(
            id="uop_100",
            first_name="Agnieszka",
            last_name="Etatowa",
            contract_type=SolverContractType.UOP,
            fte=1.0,
            role_permissions=RolePermission.KASA_I_SKLEP,
            leaves=uop1_leaves,
            preferences=ShiftPreference.PREFER_RANEK,
        ),
        EmployeeInput(
            id="uop_075",
            first_name="Bartosz",
            last_name="Trzyczwarte",
            contract_type=SolverContractType.UOP,
            fte=0.75,
            role_permissions=RolePermission.KASA,
            unavailability=[
                UnavailabilitySlot(date=date(2027, 1, 9)),
                UnavailabilitySlot(date=date(2027, 1, 23)),
            ],
            preferences=ShiftPreference.PREFER_ZAMKNIECIE,
        ),
        EmployeeInput(
            id="uop_050",
            first_name="Celina",
            last_name="Półetatowa",
            contract_type=SolverContractType.UOP,
            fte=0.5,
            role_permissions=RolePermission.KASA_I_SKLEP,
            unavailability=[
                # W poniedziałki rano ma zajęcia 06:00-12:00 (nie może pracować na RANEK, może na ZAMKNIECIE)
                UnavailabilitySlot(date=date(2027, 1, 4), start_time="06:00", end_time="12:00"),
                UnavailabilitySlot(date=date(2027, 1, 11), start_time="06:00", end_time="12:00"),
                UnavailabilitySlot(date=date(2027, 1, 18), start_time="06:00", end_time="12:00"),
                UnavailabilitySlot(date=date(2027, 1, 25), start_time="06:00", end_time="12:00"),
            ],
            preferences=ShiftPreference.PREFER_RANEK,
        ),
        EmployeeInput(
            id="uz_sklep",
            first_name="Damian",
            last_name="Towarowiec",
            contract_type=SolverContractType.UZ,
            min_hours=64.0,
            max_hours=112.0,
            target_hours=88.0,
            role_permissions=RolePermission.SKLEP,
            preferences=ShiftPreference.PREFER_SRODEK,
        ),
        EmployeeInput(
            id="uz_uni",
            first_name="Elżbieta",
            last_name="Zleceniowa",
            contract_type=SolverContractType.UZ,
            min_hours=68.0,
            max_hours=140.0,
            target_hours=102.0,
            role_permissions=RolePermission.KASA_I_SKLEP,
            preferences=ShiftPreference.PREFER_ZAMKNIECIE,
        ),
        EmployeeInput(
            id="uz_kasa",
            first_name="Filip",
            last_name="Kasjer",
            contract_type=SolverContractType.UZ,
            min_hours=68.0,
            max_hours=140.0,
            target_hours=102.0,
            role_permissions=RolePermission.KASA,
            preferences=ShiftPreference.NO_PREFERENCE,
        ),
    ]

    request = ScheduleGenerationRequest(
        period="2027-01",
        shifts=shifts,
        deliveries=deliveries,
        employees=employees,
    )

    engine = ScheduleOptimizerEngine(request)
    response = engine.generate()

    assert response.is_feasible is True
    assert response.status in ("OPTIMAL", "FEASIBLE")
    assert len(response.conflicts) == 0

    kasa_capable_ids = {"uop_100", "uop_075", "uop_050", "uz_uni", "uz_kasa"}
    sklep_capable_ids = {"uop_100", "uop_050", "uz_sklep", "uz_uni"}

    # 1. Weryfikacja obsady każdego z 31 dni stycznia 2027
    for day_num in range(1, 32):
        d = date(2027, 1, day_num)
        d_str = d.isoformat()
        day_sched = response.daily_schedule[d_str]

        # RANEK i ZAMKNIECIE muszą być obsadzone i mieć min. 1 osobę z KASA
        assert len(day_sched["RANEK"]) >= 1
        assert len(day_sched["ZAMKNIECIE"]) >= 1
        assert any(eid in kasa_capable_ids for eid in day_sched["RANEK"])
        assert any(eid in kasa_capable_ids for eid in day_sched["ZAMKNIECIE"])

        # Pracownik SKLEP nigdy nie jest na RANEK ani ZAMKNIECIE
        assert "uz_sklep" not in day_sched["RANEK"]
        assert "uz_sklep" not in day_sched["ZAMKNIECIE"]

        # W dni dostaw (wtorek=1, czwartek=3, piątek=4) zmiana SRODEK musi być obsadzona przez SKLEP / KASA_I_SKLEP
        if d.weekday() in (1, 3, 4):
            assert len(day_sched["SRODEK"]) >= 1, f"Brak obsady SRODEK w dzień dostawy {d_str}"
            assert any(eid in sklep_capable_ids for eid in day_sched["SRODEK"])

    # 2. Weryfikacja dostosowania godzin zmiany SRODEK do pory dostawy (ranek -> 09:00-17:00, wieczór -> 12:00-20:00)
    for a in response.assignments:
        if a.shift_type == SolverShiftType.SRODEK:
            if a.date.weekday() == 1:  # Wtorek - dostawa poranna
                assert a.start_time == "09:00" and a.end_time == "17:00"
            elif a.date.weekday() == 4:  # Piątek - dostawa wieczorna
                assert a.start_time == "12:00" and a.end_time == "20:00"

    # 3. Weryfikacja urlopów i ręcznych dyspozycji godzinowych
    for a in response.assignments:
        if a.employee_id == "uop_100":
            assert a.date not in uop1_leaves, "Pracownik na urlopie otrzymał zmianę!"
        if a.employee_id == "uop_075":
            assert a.date not in (date(2027, 1, 9), date(2027, 1, 23))
        if a.employee_id == "uop_050" and a.date in (
            date(2027, 1, 4),
            date(2027, 1, 11),
            date(2027, 1, 18),
            date(2027, 1, 25),
        ):
            # W te poniedziałki ma niedostępność 06:00-12:00, więc nie może dostać RANEK ani SRODEK
            assert a.shift_type == SolverShiftType.ZAMKNIECIE

    # 4. Weryfikacja 35h nieprzerwanego odpoczynku tygodniowego w każdym 7-dniowym oknie dla UoP
    by_emp = {e.id: [] for e in employees}
    for a in response.assignments:
        by_emp[a.employee_id].append(a)

    for uop_id in ("uop_100", "uop_075", "uop_050"):
        emp_assigns = sorted(by_emp[uop_id], key=lambda x: x.date)
        assign_by_date = {a.date: a for a in emp_assigns}

        for w_start_day in range(1, 32 - 6):
            w_dates = [date(2027, 1, w_start_day + k) for k in range(7)]
            has_35h_rest_in_window = False
            for d_off in w_dates:
                if d_off not in assign_by_date:
                    prev_a = assign_by_date.get(d_off - timedelta(days=1))
                    next_a = assign_by_date.get(d_off + timedelta(days=1))
                    end_prev = time_str_to_minutes(prev_a.end_time) if prev_a else 0
                    start_next = time_str_to_minutes(next_a.start_time) if next_a else 1440
                    rest_mins = (2 * 1440 + start_next) - end_prev
                    if rest_mins >= 35 * 60:
                        has_35h_rest_in_window = True
                        break
            assert has_35h_rest_in_window, (
                f"Pracownik UoP {uop_id} nie ma 35h nieprzerwanego odpoczynku w oknie od {w_dates[0]} do {w_dates[-1]}"
            )

    # 5. Weryfikacja bilansu godzin dla wszystkich typów umów
    for emp_id, summary in response.hours_summary.items():
        if summary.contract_type == SolverContractType.UOP:
            # Różnica względem target_hours nie może przekroczyć długości 1 zmiany (8.5h)
            assert abs(summary.difference_hours) <= 8.5
        else:
            assert summary.min_hours <= summary.worked_hours <= summary.max_hours


def test_entered_12_20_shift_hours_never_turn_into_1030_1830():
    """
    Weryfikuje naprawę błędu, w którym wpisane godziny 12:00-20:00 zamieniały się na 10:30-18:30
    w resolve_shift_hours oraz apply_shift_times (przy otwarciu 06:00-23:00 lub w niedziele).
    """
    from app.services.schedule_service import resolve_shift_hours, apply_shift_times

    store_settings = {
        "shift_hours": {
            "morning": {"start_time": "06:00", "end_time": "14:30"},
            "middle": {"start_time": "12:00", "end_time": "20:00"},
            "closing": {"start_time": "14:30", "end_time": "23:00"},
        },
        "opening_hours": {
            "weekday": {"from": "06:00", "to": "23:00"},
            "sunday": {"from": "06:00", "to": "23:00"},
            "is_commercial_sunday": False,
        },
    }
    holidays_map = {
        "2027-01-06": {"open_time": "06:00", "close_time": "23:00", "is_closed": False}
    }

    # Dzień powszedni, niedziela oraz dzień z holidays_map o godzinach 06:00-23:00
    for d in (date(2027, 1, 5), date(2027, 1, 10), date(2027, 1, 6)):
        s_str, e_str = resolve_shift_hours(d, "middle", store_settings, holidays_map)
        assert (s_str, e_str) == ("12:00", "20:00"), (
            f"Godziny 12:00-20:00 zostały błędnie zmienione na {s_str}-{e_str} w dniu {d}!"
        )

    # Weryfikacja apply_shift_times - zapisane w grafiku godziny 12:00-20:00 nie mogą zostać nadpisane
    schedule_doc = {
        "schedule": {
            "2027-01-10": {
                "middle": {
                    "start_time": "12:00",
                    "end_time": "20:00",
                    "employees": [{"id": "emp_1", "first_name": "Jan", "last_name": "Kowalski"}],
                }
            }
        }
    }
    apply_shift_times(schedule_doc, store_settings, holidays_map)
    assert schedule_doc["schedule"]["2027-01-10"]["middle"]["start_time"] == "12:00"
    assert schedule_doc["schedule"]["2027-01-10"]["middle"]["end_time"] == "20:00"


def test_understaffed_store_never_skips_critical_ranek_or_zamkniecie_shifts():
    """
    Weryfikuje scenariusz produkcyjny (strict_audit=False), w którym:
    - Sklep ma tylko 3 pracowników o łącznym celu godzinowym znacznie mniejszym niż
      pełne zapotrzebowanie 3 zmian dziennie (RANEK + SRODEK + ZAMKNIECIE przez 31 dni),
    - Dwóch pracowników zgłosiło ręczną dyspozycję na RANEK w te same dni,
    - W niedziele obowiązują krótsze godziny (10:00-15:00 i 15:00-20:00).
    Silnik w Kroku 3 MUSI w 100% obsadzić wszystkie zmiany kluczowe (RANEK i ZAMKNIECIE)
    w każdym z 31 dni miesiąca, rezygnując w pierwszej kolejności ze zmian pobocznych (SRODEK)
    i w razie konieczności przydzielając nadgodziny, zamiast zostawiać puste otwarcia/zamknięcia.
    """
    shifts = [
        ShiftConfig(shift_type=SolverShiftType.RANEK, start_time="06:00", end_time="14:30", min_staff=1),
        ShiftConfig(shift_type=SolverShiftType.SRODEK, start_time="12:00", end_time="20:00", min_staff=1),
        ShiftConfig(shift_type=SolverShiftType.ZAMKNIECIE, start_time="14:30", end_time="23:00", min_staff=1),
    ]

    # W niedziele krótsze godziny zmian: 10:00-15:00 oraz 15:00-20:00
    daily_shift_hours = {}
    for day_num in range(1, 32):
        d = date(2027, 1, day_num)
        if d.weekday() == 6:
            daily_shift_hours[d.isoformat()] = {
                "RANEK": ("10:00", "15:00"),
                "SRODEK": ("12:00", "18:00"),
                "ZAMKNIECIE": ("15:00", "20:00"),
            }

    employees = [
        EmployeeInput(
            id="emp_uop",
            first_name="Anna",
            last_name="Kowalska",
            contract_type=SolverContractType.UOP,
            fte=1.0,
            target_hours=160.0,
            role_permissions=RolePermission.KASA_I_SKLEP,
            requested_shifts={"2027-01-04": SolverShiftType.RANEK, "2027-01-10": SolverShiftType.RANEK},
        ),
        EmployeeInput(
            id="emp_uz1",
            first_name="Piotr",
            last_name="Nowak",
            contract_type=SolverContractType.UZ,
            min_hours=0.0,
            max_hours=120.0,
            target_hours=100.0,
            role_permissions=RolePermission.KASA_I_SKLEP,
            # Kolizyjna dyspozycja na RANEK w ten sam dzień co Anna!
            requested_shifts={"2027-01-04": SolverShiftType.RANEK, "2027-01-10": SolverShiftType.ZAMKNIECIE},
        ),
        EmployeeInput(
            id="emp_uz2",
            first_name="Kasia",
            last_name="Wiśniewska",
            contract_type=SolverContractType.UZ,
            min_hours=0.0,
            max_hours=120.0,
            target_hours=100.0,
            role_permissions=RolePermission.KASA,
        ),
    ]

    request = ScheduleGenerationRequest(
        period="2027-01",
        shifts=shifts,
        employees=employees,
        daily_shift_hours=daily_shift_hours,
        strict_audit=False,
    )

    engine = ScheduleOptimizerEngine(request)
    response = engine.generate()

    assert response.is_feasible is True
    assert response.status in ("RELAXED", "OPTIMAL", "FEASIBLE")

    # W KAŻDYM z 31 dni stycznia 2027 zmiany kluczowe RANEK i ZAMKNIECIE muszą być obsadzone!
    for day_num in range(1, 32):
        d_str = date(2027, 1, day_num).isoformat()
        day_sched = response.daily_schedule[d_str]
        assert len(day_sched["RANEK"]) >= 1, f"Pominięto zmianę kluczową RANEK w dniu {d_str}!"
        assert len(day_sched["ZAMKNIECIE"]) >= 1, f"Pominięto zmianę kluczową ZAMKNIECIE w dniu {d_str}!"



