# Plik: backend/app/schemas.py

from pydantic import BaseModel, EmailStr, SecretStr, Field, field_validator
from enum import Enum
from datetime import date, time, datetime
from typing import Optional, List, Dict, Any
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

class WorkScope(str, Enum):
    SINGLE_STORE = "jeden sklep"
    MULTIPLE_STORES = "więcej sklepów"

class HolidayWorkPreference(str, Enum):
    CAN_WORK = "mogę"
    CANNOT_WORK = "nie mogę"

class ScheduleGenerationPeriod(str, Enum):
    WEEKLY = "tygodniowo"
    MONTHLY = "miesięcznie"

# ====================================================================
# Wspólna konfiguracja dla modeli MongoDB
# ====================================================================
class MongoConfig:
    from_attributes = True
    populate_by_name = True
    arbitrary_types_allowed = True
    json_encoders = {ObjectId: str}


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
    id: PyObjectId = Field(alias="_id")
    owner_id: PyObjectId

    class Config(MongoConfig):
        pass

# ====================================================================
# Schematy Ustawień Grafiku
# ====================================================================
class StoreSettingsBase(BaseModel):
    franchise_code: str
    employees_per_morning_shift: int = Field(default=1, ge=1, le=5)
    employees_per_middle_shift: int = Field(default=1, ge=0, le=5) # POPRAWKA: Zmiana wartości domyślnej
    employees_per_closing_shift: int = Field(default=1, ge=1, le=5)
    employees_on_promo_change: int = Field(default=2, ge=2, le=5)
    allow_overtime: bool = True
    allow_inter_store_work: bool = False
    schedule_generation_period: ScheduleGenerationPeriod = ScheduleGenerationPeriod.WEEKLY

class StoreSettingsCreate(StoreSettingsBase):
    pass

class StoreSettings(StoreSettingsBase):
    id: PyObjectId = Field(alias="_id")

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
    id: PyObjectId = Field(alias="_id")

    class Config(MongoConfig):
        pass

# ====================================================================
# Schematy Użytkowników
# ====================================================================
class UserPreferences(BaseModel):
    work_scope: WorkScope = WorkScope.SINGLE_STORE
    preferred_shifts: List[ShiftType] = []
    day_preference: DayPreference = DayPreference.WEEKDAYS
    holiday_preference: HolidayWorkPreference = HolidayWorkPreference.CANNOT_WORK

class UserBase(BaseModel):
    first_name: str
    last_name: str
    preferences: UserPreferences = Field(default_factory=UserPreferences)

class UserCreateFranchisee(UserBase):
    email: EmailStr
    password: SecretStr
    franchise_code: str
    province: str
    city: str
    postal_code: str
    street: str
    building_number: str
    phone: Optional[str] = None


class UserCreateByAdmin(UserBase):
    email: EmailStr
    employment_type: str = Field(alias="employment_type")


class UserUpdate(BaseModel):
    pass


class UserStatusUpdate(BaseModel):
    status: UserStatus


class UserResponse(UserBase):
    id: PyObjectId = Field(alias="_id")
    email: EmailStr
    role: UserRole
    status: UserStatus
    franchise_code: Optional[str] = None
    store_location: Optional[str] = None
    email_verified: bool
    vacation_days_left: int
    created_at: datetime
    updated_at: Optional[datetime] = None
    username: Optional[str] = None
    contract_type: Optional[ContractType] = None

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

class AvailabilityBase(BaseModel):
    date: date
    start_time: Optional[time] = None
    end_time: Optional[time] = None
    period_type: str

class AvailabilityCreate(AvailabilityBase):
    pass

class Availability(AvailabilityBase):
    id: PyObjectId = Field(alias="_id")
    user_id: PyObjectId
    submitted_at: datetime

    class Config(MongoConfig): pass

class ScheduleDraftResponse(BaseModel):
    id: PyObjectId = Field(alias="_id")
    franchise_code: str
    start_date: date
    end_date: date
    schedule: Dict[str, Any]
    conflicts: List[str]
    status: str
    created_at: datetime

    class Config(MongoConfig):
        pass

class ShiftUpdate(BaseModel):
    date: date
    shift_name: str
    employee_ids: List[PyObjectId]

class VacationBase(BaseModel):
    start_date: date
    end_date: date
    reason: Optional[str] = None


class VacationCreate(VacationBase):
    pass


class Vacation(VacationBase):
    id: PyObjectId = Field(alias="_id")
    user_id: PyObjectId
    status: VacationStatus
    approved_by_id: Optional[PyObjectId] = None
    submitted_at: datetime
    reviewed_at: Optional[datetime] = None

    class Config(MongoConfig): pass


class DeadlineBase(BaseModel):
    period_type: PeriodType
    deadline_date: date


class DeadlineCreate(DeadlineBase):
    pass


class Deadline(DeadlineBase):
    id: PyObjectId = Field(alias="_id")

    class Config(MongoConfig): pass


# ====================================================================
# Schematy dla Raportów (PRZYWRÓCONE)
# ====================================================================
class HoursReportRequest(BaseModel):
    start_date: date
    end_date: date
    user_id: Optional[str] = None


class VacationReportRequest(BaseModel):
    start_date: date
    end_date: date


class AvailabilityReportRequest(BaseModel):
    start_date: date
    end_date: date
