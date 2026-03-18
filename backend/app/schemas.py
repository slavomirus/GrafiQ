# Plik: backend/app/schemas.py

from pydantic import BaseModel, EmailStr, SecretStr, Field, field_validator, model_validator, field_serializer
from enum import Enum
from datetime import date, time, datetime, timedelta
from typing import Optional, List, Dict, Any, Union
from bson import ObjectId

# ====================================================================
# Helper do poprawnej obsługi ObjectId z MongoDB w Pydantic
# ====================================================================
from pydantic_core import core_schema

class PyObjectId(ObjectId):
    @classmethod
    def __get_pydantic_core_schema__(cls, source_type, handler):
        return core_schema.json_or_python_schema(
            json_schema=core_schema.str_schema(),
            python_schema=core_schema.union_schema([
                core_schema.is_instance_schema(ObjectId),
                core_schema.no_info_after_validator_function(
                    cls.validate,
                    core_schema.str_schema(),
                ),
            ]),
            serialization=core_schema.to_string_ser_schema(),
        )

    @classmethod
    def validate(cls, v):
        if not ObjectId.is_valid(v):
            raise ValueError("Invalid objectid")
        return ObjectId(v)


# ====================================================================
# Enumy
# ====================================================================
class UserRole(str, Enum):
    FRANCHISEE = "franchisee"
    EMPLOYEE = "employee"
    ADMIN = "admin"


class UserStatus(str, Enum):
    PENDING = "pending"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    NEEDS_PASSWORD_CHANGE = "needs_password_change"


class ContractType(str, Enum):
    UOP = "umowa o pracę"
    UZ = "umowa zlecenie"
    UOD = "umowa o dzieło"
    B2B = "kontrakt B2B"


class PeriodType(str, Enum):
    WEEKLY = "weekly"
    MONTHLY = "monthly"


class VacationStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"

class ShiftType(str, Enum):
    MORNING = "morning"
    MIDDLE = "middle"
    CLOSING = "closing"

class DayPreference(str, Enum):
    WEEKDAYS = "pn-pt"
    WEEKENDS = "sb-nd"
    WHOLE_WEEK = "cały tydzień"

class WorkScope(str, Enum):
    SINGLE_STORE = "jeden sklep"
    MULTIPLE_STORES = "więcej sklepów"

class HolidayWorkPreference(str, Enum):
    CAN_WORK = "mogę"
    CANNOT_WORK = "nie mogę"

class Platform(str, Enum):
    ANDROID = "android"
    IOS = "ios"

# ====================================================================
# Wspólna konfiguracja dla modeli MongoDB
# ====================================================================
class MongoConfig:
    from_attributes = True
    populate_by_name = True
    arbitrary_types_allowed = True
    json_encoders = {ObjectId: str}

# ====================================================================
# Schematy Zgód (EULA/RODO)
# ====================================================================
class AgreementInfo(BaseModel):
    acceptedAt: Optional[datetime] = None
    version: Optional[str] = None

class Agreements(BaseModel):
    eula: Optional[AgreementInfo] = None
    rodo: Optional[AgreementInfo] = None
    ipAddress: Optional[str] = None

class AcceptAgreementsRequest(BaseModel):
    eula_version: Optional[str] = None
    rodo_version: Optional[str] = None

# ====================================================================
# Schematy Sklepów
# ====================================================================
class StoreBase(BaseModel):
    franchise_code: str
    province: str
    city: str
    postal_code: str
    street: str
    building_number: str

class StoreCreate(StoreBase):
    pass

class Store(StoreBase):
    id: PyObjectId = Field(alias="_id", serialization_alias="_id")
    owner_id: PyObjectId

    class Config(MongoConfig):
        pass

# ====================================================================
# Schematy Ustawień Grafiku
# ====================================================================
class ShiftHours(BaseModel):
    start_time: time
    end_time: time

class OpeningHoursDay(BaseModel):
    start_time: time = Field(alias="from")
    end_time: time = Field(alias="to")

    class Config:
        populate_by_name = True

class OpeningHoursHoliday(BaseModel):
    open_time: time = Field(alias="open")
    close_time: time = Field(alias="close")

    class Config:
        populate_by_name = True

class StoreOpeningHours(BaseModel):
    weekday: OpeningHoursDay
    sunday: OpeningHoursDay
    holiday: Dict[str, OpeningHoursHoliday] = {}

class StoreSettingsBase(BaseModel):
    franchise_code: str
    employees_per_morning_shift: int = Field(default=1, ge=1, le=5)
    employees_per_middle_shift: int = Field(default=1, ge=0, le=5)
    employees_per_closing_shift: int = Field(default=1, ge=1, le=5)
    employees_on_promo_change: int = Field(default=2, ge=2, le=5)
    allow_overtime: bool = True
    allow_inter_store_work: bool = False

    franchisee_monthly_hours: int = Field(default=160, ge=0, description="Liczba godzin pracy franczyzobiorcy w miesiącu")

    shift_hours: Dict[str, ShiftHours] = Field(
        default={
            "morning": {"start_time": "06:00", "end_time": "14:30"},
            "middle": {"start_time": "12:00", "end_time": "20:00"},
            "closing": {"start_time": "14:30", "end_time": "23:00"}
        }
    )

    opening_hours: StoreOpeningHours = Field(
        default_factory=lambda: StoreOpeningHours(
            weekday=OpeningHoursDay(start_time="06:00", end_time="23:00"),
            sunday=OpeningHoursDay(start_time="10:00", end_time="20:00")
        )
    )

class StoreSettingsCreate(StoreSettingsBase):
    pass

class StoreSettings(StoreSettingsBase):
    id: PyObjectId = Field(alias="_id", serialization_alias="_id")

    class Config(MongoConfig):
        pass

class SpecialOpeningHoursBase(BaseModel):
    franchise_code: str
    date: date
    open_time: time
    close_time: time

class SpecialOpeningHoursCreate(SpecialOpeningHoursBase):
    pass

class SpecialOpeningHours(SpecialOpeningHoursBase):
    id: PyObjectId = Field(alias="_id", serialization_alias="_id")

    class Config(MongoConfig):
        pass

# ====================================================================
# Schematy Użytkowników
# ====================================================================
class UserPreferences(BaseModel):
    # Mapowanie pól na nazwy oczekiwane przez frontend
    work_scope: WorkScope = Field(default=WorkScope.SINGLE_STORE, alias="multiple_stores")
    preferred_shifts: List[ShiftType] = []
    day_preference: DayPreference = Field(default=DayPreference.WEEKDAYS, alias="preferred_days")
    holiday_preference: HolidayWorkPreference = Field(default=HolidayWorkPreference.CANNOT_WORK, alias="holidays")

    class Config:
        populate_by_name = True

    # --- VALIDATORS (INPUT) ---

    @field_validator('day_preference', mode='before')
    @classmethod
    def validate_day_preference(cls, v):
        # Obsługa listy stringów (frontend) -> pojedynczy Enum (backend)
        if isinstance(v, list):
            if not v: return DayPreference.WEEKDAYS
            v = v[0] # Bierzemy pierwszy element

        if not v or not isinstance(v, str):
            return DayPreference.WEEKDAYS

        v_lower = v.lower()
        if v_lower in ["pn-pt", "weekdays", "dni robocze"]:
            return DayPreference.WEEKDAYS
        if v_lower in ["sb-nd", "weekends", "weekendy"]:
            return DayPreference.WEEKENDS
        if v_lower in ["cały tydzień", "whole week", "wszystkie dni"]:
            return DayPreference.WHOLE_WEEK

        for e in DayPreference:
            if e.value == v_lower: return e
        return DayPreference.WEEKDAYS

    @field_validator('holiday_preference', mode='before')
    @classmethod
    def validate_holiday_preference(cls, v):
        # Obsługa boolean (frontend) -> Enum (backend)
        if isinstance(v, bool):
            return HolidayWorkPreference.CAN_WORK if v else HolidayWorkPreference.CANNOT_WORK

        if not v or not isinstance(v, str):
            return HolidayWorkPreference.CANNOT_WORK

        v_lower = v.lower()
        if v_lower in ["mogę", "can_work", "can work", "tak", "yes", "true"]:
            return HolidayWorkPreference.CAN_WORK
        if v_lower in ["nie mogę", "cannot_work", "cannot work", "nie", "no", "false"]:
            return HolidayWorkPreference.CANNOT_WORK

        for e in HolidayWorkPreference:
            if e.value == v_lower: return e
        return HolidayWorkPreference.CANNOT_WORK

    @field_validator('work_scope', mode='before')
    @classmethod
    def validate_work_scope(cls, v):
        # Obsługa boolean (frontend) -> Enum (backend)
        if isinstance(v, bool):
            return WorkScope.MULTIPLE_STORES if v else WorkScope.SINGLE_STORE

        if not v or not isinstance(v, str):
            return WorkScope.SINGLE_STORE

        v_lower = v.lower()
        if v_lower in ["jeden sklep", "single_store", "single store", "jeden"]:
            return WorkScope.SINGLE_STORE
        if v_lower in ["więcej sklepów", "multiple_stores", "multiple stores", "więcej", "wiele"]:
            return WorkScope.MULTIPLE_STORES

        for e in WorkScope:
            if e.value == v_lower: return e
        return WorkScope.SINGLE_STORE

    @field_validator('preferred_shifts', mode='before')
    @classmethod
    def validate_preferred_shifts(cls, v):
        if v is None:
            return []
        if not isinstance(v, list):
            return []

        mapped_list = []
        for item in v:
            if isinstance(item, str):
                item_lower = item.lower()
                if item_lower in ["rano", "poranna", "morning", "zmiana poranna", "ranki"]:
                    mapped_list.append(ShiftType.MORNING)
                elif item_lower in ["środek", "pośrednia", "middle", "zmiana środkowa"]:
                    mapped_list.append(ShiftType.MIDDLE)
                elif item_lower in ["wieczór", "zamykająca", "closing", "zmiana zamykająca", "zamknięcie", "zetki", "zamknięcia"]:
                    mapped_list.append(ShiftType.CLOSING)
                else:
                    for e in ShiftType:
                        if e.value == item_lower:
                            mapped_list.append(e)
                            break
            elif isinstance(item, ShiftType):
                mapped_list.append(item)
        return mapped_list

    # --- SERIALIZERS (OUTPUT) ---

    @field_serializer('work_scope')
    def serialize_work_scope(self, v: WorkScope, _info):
        return v == WorkScope.MULTIPLE_STORES

    @field_serializer('holiday_preference')
    def serialize_holiday_preference(self, v: HolidayWorkPreference, _info):
        return v == HolidayWorkPreference.CAN_WORK

    @field_serializer('day_preference')
    def serialize_day_preference(self, v: DayPreference, _info):
        # Frontend oczekuje listy stringów
        return [v.value]

    @field_serializer('preferred_shifts')
    def serialize_preferred_shifts(self, v: List[ShiftType], _info):
        # Mapowanie Enum -> Polskie nazwy (dla UI)
        mapping = {
            ShiftType.MORNING: "Ranki",
            ShiftType.MIDDLE: "Środek",
            ShiftType.CLOSING: "Zamknięcia"
        }
        return [mapping.get(shift, shift.value) for shift in v]

class UserBase(BaseModel):
    first_name: str
    last_name: str
    preferences: Optional[UserPreferences] = Field(default_factory=UserPreferences)
    fte: float = Field(default=1.0, ge=0.1, le=1.0, description="Wymiar etatu (np. 1.0, 0.5)")
    seniority_years: int = Field(default=0, ge=0, description="Staż pracy w latach (do obliczania urlopu)")
    leave_entitlement: Optional[int] = Field(default=None, description="Przysługujący urlop (dni). Jeśli null, obliczany automatycznie.")

class UserCreateFranchisee(UserBase):
    email: EmailStr
    password: SecretStr
    franchise_code: str
    franchise_codes: Optional[List[str]] = [] # Lista kodów sklepów, do których ma dostęp
    province: str
    city: str
    postal_code: str
    street: str
    building_number: str
    phone: Optional[str] = None
    acceptEula: bool
    acceptRodo: bool


class UserCreateByAdmin(UserBase):
    email: EmailStr
    employment_type: str = Field(alias="employment_type")


class UserUpdate(BaseModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    email: Optional[EmailStr] = None
    contract_type: Optional[ContractType] = None
    fte: Optional[float] = None
    seniority_years: Optional[int] = None
    monthly_hours_target: Optional[int] = None
    employment_start_date: Optional[datetime] = None
    leave_entitlement: Optional[int] = None
    preferences: Optional[UserPreferences] = None


class UserStatusUpdate(BaseModel):
    status: UserStatus


class UserResponse(UserBase):
    id: PyObjectId = Field(alias="_id", serialization_alias="_id")
    email: Optional[EmailStr] = None
    role: Optional[UserRole] = None
    status: Optional[UserStatus] = None
    franchise_code: Optional[str] = None
    franchise_codes: Optional[List[str]] = []
    store_location: Optional[str] = None
    email_verified: Optional[bool] = None
    vacation_days_left: Optional[int] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    username: Optional[str] = None
    contract_type: Optional[ContractType] = None
    monthly_hours_target: Optional[int] = None
    employment_start_date: Optional[datetime] = None
    agreements: Optional[Agreements] = None
    ### GOD MODE ###
    free_access_until: Optional[datetime] = free_access_until: Optional[datetime] = Field(default_factory=lambda: datetime.utcnow() + timedelta(days=3650))
    ### GOD MODE ###
    is_subscription_active: Optional[bool] = True
    subscription_plan: Optional[str] = "unlimited_free_trial"

    class Config(MongoConfig):
        pass

class UserPublicProfile(BaseModel):
    id: PyObjectId = Field(alias="_id", serialization_alias="_id")
    first_name: str
    last_name: str

    class Config(MongoConfig):
        pass

# ====================================================================
# Schematy Autentykacji i Generyczne
# ====================================================================
class LoginRequest(BaseModel):
    email: str
    password: str

class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"

class LoginResponse(Token):
    requires_agreement_update: Optional[bool] = False


class MessageResponse(BaseModel):
    message: str


class RegistrationResponse(MessageResponse):
    user_id: str


class EmailRequest(BaseModel):
    email: EmailStr


class VerificationCodeSchema(BaseModel):
    email: str
    code: str


class PasswordReset(BaseModel):
    token: str
    new_password: SecretStr

class SetInitialPasswordRequest(BaseModel):
    new_password: SecretStr

# ====================================================================
# Schematy Aplikacji (Dostępność, Grafik, Urlopy, etc.)
# ====================================================================

class L4LeaveRequest(BaseModel):
    startDate: date
    endDate: date

    @model_validator(mode='after')
    def validate_dates(self):
        if self.startDate > self.endDate:
            raise ValueError("Data zakończenia nie może być wcześniejsza niż data rozpoczęcia.")
        return self

class AvailabilityBase(BaseModel):
    date: date
    start_time: Optional[time] = None
    end_time: Optional[time] = None
    period_type: str

class AvailabilityCreate(AvailabilityBase):
    pass

class Availability(AvailabilityBase):
    id: PyObjectId = Field(alias="_id", serialization_alias="_id")
    user_id: PyObjectId
    submitted_at: datetime

    class Config(MongoConfig): pass

class ShiftEmployeeResponse(BaseModel):
    id: PyObjectId = Field(alias="_id", serialization_alias="_id")
    first_name: str
    last_name: str

    class Config(MongoConfig):
        pass

class ShiftDetailResponse(BaseModel):
    employees: List[ShiftEmployeeResponse] = []
    start_time: Optional[time] = None
    end_time: Optional[time] = None
    is_custom: Optional[bool] = False
    is_holiday: Optional[bool] = False

    class Config:
        extra = "allow"

class ScheduleDayResponse(BaseModel):
    date: date
    shifts: Dict[str, ShiftDetailResponse]

class ScheduleResponse(BaseModel):
    id: PyObjectId = Field(alias="_id", serialization_alias="_id")
    franchise_code: str
    start_date: date
    end_date: date
    schedule: Dict[str, Any]
    shifts_by_date: Optional[Dict[str, List[Any]]] = None # Dodano pole dla frontendu
    status: str
    created_at: datetime

    class Config(MongoConfig):
        pass

class ScheduleDraftResponse(BaseModel):
    id: PyObjectId = Field(alias="_id", serialization_alias="_id")
    franchise_code: str
    start_date: date
    end_date: date
    schedule: Dict[str, Any]
    conflicts: Optional[List[str]] = None
    status: str
    created_at: datetime

    class Config(MongoConfig):
        pass

class ShiftUpdate(BaseModel):
    date: date
    shift_name: str
    employee_ids: List[PyObjectId]

class EmployeeAssignment(BaseModel):
    employee_id: PyObjectId

class ShiftHoursUpdate(BaseModel):
    start_time: time
    end_time: time

class DraftShiftUpdate(BaseModel):
    date: date
    shift_name: str
    start_time: time
    end_time: time
    employee_id: Optional[PyObjectId] = None

class VacationBase(BaseModel):
    start_date: date
    end_date: date
    reason: Optional[str] = None


class VacationCreate(VacationBase):
    pass


class Vacation(VacationBase):
    id: PyObjectId = Field(alias="_id", serialization_alias="_id")
    user_id: PyObjectId
    status: VacationStatus
    approved_by_id: Optional[PyObjectId] = None
    submitted_at: datetime
    reviewed_at: Optional[datetime] = None

    # Dodano pola dla frontendu (wszystkie możliwe warianty)
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    firstName: Optional[str] = None
    lastName: Optional[str] = None
    employee: Optional[Dict[str, Any]] = None
    user: Optional[Dict[str, Any]] = None

    # FIX: Dodano created_at jako alias dla submitted_at
    created_at: Optional[datetime] = None

    @model_validator(mode='before')
    @classmethod
    def set_created_at(cls, data: Any) -> Any:
        if isinstance(data, dict):
            # Jeśli mamy submitted_at a nie mamy created_at, kopiujemy
            if 'submitted_at' in data and 'created_at' not in data:
                data['created_at'] = data['submitted_at']
            # Na wypadek gdyby obiekt przychodził z created_at (np. z bazy) a nie submitted_at
            elif 'created_at' in data and 'submitted_at' not in data:
                 data['submitted_at'] = data['created_at']
        return data

    class Config(MongoConfig): pass


class DeadlineBase(BaseModel):
    period_type: PeriodType
    deadline_date: date


class DeadlineCreate(DeadlineBase):
    pass


class Deadline(DeadlineBase):
    id: PyObjectId = Field(alias="_id", serialization_alias="_id")

    class Config(MongoConfig): pass


# ====================================================================
# Schematy dla Raportów (PRZYWRÓCONE)
# ===================================================================
class HoursReportRequest(BaseModel):
    start_date: date
    end_date: date
    user_id: Optional[str] = None

class ReportItem(BaseModel):
    user_id: Union[str, PyObjectId]
    first_name: str
    last_name: str
    date: Union[datetime, date]
    start_time: Union[time, str]
    end_time: Union[time, str]
    hours: float

    class Config(MongoConfig):
        pass


class VacationReportRequest(BaseModel):
    start_date: date
    end_date: date


class AvailabilityReportRequest(BaseModel):
    start_date: date
    end_date: date

# ====================================================================
# Schematy dla L4 (Sick Leave)
# ====================================================================
class SickLeaveRequest(BaseModel):
    employee_id: PyObjectId
    start_date: date
    end_date: date

    @model_validator(mode='after')
    def validate_dates(self):
        if self.start_date > self.end_date:
            raise ValueError("Data zakończenia nie może być wcześniejsza niż data rozpoczęcia.")
        return self

class ReplacementInfo(BaseModel):
    date: date
    shift_name: str
    original_employee_id: PyObjectId
    replacement_employee_id: Optional[PyObjectId] = None
    status: str # "replaced", "no_candidate", "manual_intervention_needed"

class SickLeaveResponse(BaseModel):
    message: str
    replacements: List[ReplacementInfo]

class VacationPDFResponse(BaseModel):
    pdf_base64: str
    filename: str

# ====================================================================
# Schematy dla Wymian (Shift Swaps)
# ====================================================================
class SwapStatus(str, Enum):
    REQUESTED = "requested"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    CANCELLED = "cancelled"

class ShiftSwapRequest(BaseModel):
    my_date: date
    my_shift_name: str
    target_user_id: PyObjectId
    target_date: date
    target_shift_name: str

class SwapResponseRequest(BaseModel):
    response: str # "accepted" or "rejected"

class ShiftSwapResponse(BaseModel):
    id: PyObjectId = Field(alias="_id", serialization_alias="_id")
    requester_id: PyObjectId
    target_user_id: PyObjectId
    franchise_code: str

    my_date: date
    my_shift_name: str

    target_date: date
    target_shift_name: str

    status: SwapStatus
    created_at: datetime
    updated_at: Optional[datetime] = None

    class Config(MongoConfig):
        pass

class FCMTokenRequest(BaseModel):
    token: str

class ShiftChangeRequestCreate(BaseModel):
    date: date
    current_shift_name: Optional[str] = None
    requested_start_time: time
    requested_end_time: time
    reason: Optional[str] = None

class ShiftChangeRequestResponse(BaseModel):
    id: PyObjectId = Field(alias="_id", serialization_alias="_id")
    user_id: PyObjectId
    franchise_code: str
    date: date
    current_shift_name: Optional[str] = None
    requested_start_time: time
    requested_end_time: time
    reason: Optional[str] = None
    status: str
    created_at: datetime
    responded_at: Optional[datetime] = None

    class Config(MongoConfig):
        pass

class ShiftChangeResponseRequest(BaseModel):
    response: str # "accepted" or "rejected"

# ====================================================================
# Schematy dla Aktualizacji Aplikacji
# ====================================================================
class AppVersionCreate(BaseModel):
    version: str
    platform: Platform
    release_notes: Optional[str] = None
    force_update: bool = False

class AppVersionResponse(BaseModel):
    id: PyObjectId = Field(alias="_id", serialization_alias="_id")
    version: str
    platform: Platform
    release_notes: Optional[str] = None
    force_update: bool = False
    created_at: datetime

    class Config(MongoConfig):
        pass

class ShiftCreate(BaseModel):
    user_id: str
    date: str # YYYY-MM-DD
    shift_type: str # "morning" | "mid" | "closing"
    start_time: str # HH:MM
    end_time: str # HH:MM

class ReferralCodeResponse(BaseModel):
    code: str
    owner_id: PyObjectId
    uses_left: int
    max_uses: int
    free_access_until: Optional[datetime] = None

    class Config(MongoConfig):
        pass
