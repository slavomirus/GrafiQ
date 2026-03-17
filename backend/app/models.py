from datetime import datetime, date, time, timedelta
from enum import Enum
from typing import Optional, List, Any
from pydantic import BaseModel, EmailStr, Field, validator, ConfigDict
from bson import ObjectId
from pymongo import IndexModel, ASCENDING, DESCENDING
from pydantic_core import core_schema

# Custom ObjectId handling dla Pydantic v2
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
            raise ValueError("Invalid ObjectId")
        return ObjectId(v)

    @classmethod
    def __get_pydantic_json_schema__(cls, core_schema, handler):
        json_schema = handler(core_schema)
        json_schema.update(type="string", format="objectid")
        return json_schema

# Enums
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

class LeaveType(str, Enum):
    L4 = "l4"

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

class ShiftChangeStatus(str, Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"

class SwapStatus(str, Enum):
    REQUESTED = "requested"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    CANCELLED = "cancelled"

class Platform(str, Enum):
    ANDROID = "android"
    IOS = "ios"

# Base model z konfiguracją dla MongoDB
class MongoDBModel(BaseModel):
    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        json_encoders={ObjectId: str},
        use_enum_values=True,
        populate_by_name=True
    )

class UserPreferences(MongoDBModel):
    work_scope: WorkScope = WorkScope.SINGLE_STORE
    preferred_shifts: List[ShiftType] = []
    day_preference: DayPreference = DayPreference.WEEKDAYS
    holiday_preference: HolidayWorkPreference = HolidayWorkPreference.CANNOT_WORK

# Schematy Pydantic dla dokumentów MongoDB
class Leave(MongoDBModel):
    id: PyObjectId = Field(default_factory=PyObjectId, alias="_id")
    user_id: PyObjectId
    type: LeaveType
    start_date: datetime
    end_date: datetime
    submitted_at: datetime = Field(default_factory=datetime.utcnow)

class AvailabilityBase(MongoDBModel):
    date: datetime
    start_time: time
    end_time: time
    period_type: PeriodType

class AvailabilityCreate(AvailabilityBase):
    user_id: PyObjectId

class Availability(AvailabilityBase):
    id: PyObjectId = Field(default_factory=PyObjectId, alias="_id")
    user_id: PyObjectId
    submitted_at: datetime = Field(default_factory=datetime.utcnow)

class VacationBase(MongoDBModel):
    start_date: datetime
    end_date: datetime
    reason: Optional[str] = None
    status: VacationStatus = VacationStatus.PENDING

class VacationCreate(VacationBase):
    user_id: PyObjectId

class Vacation(VacationBase):
    id: PyObjectId = Field(default_factory=PyObjectId, alias="_id")
    user_id: PyObjectId
    approved_by_id: Optional[PyObjectId] = None
    submitted_at: datetime = Field(default_factory=datetime.utcnow)
    reviewed_at: Optional[datetime] = None

class ScheduleBase(MongoDBModel):
    date: datetime
    start_time: time
    end_time: time

class ScheduleCreate(ScheduleBase):
    user_id: PyObjectId
    assigned_by_id: PyObjectId

class Schedule(ScheduleBase):
    id: PyObjectId = Field(default_factory=PyObjectId, alias="_id")
    user_id: PyObjectId
    assigned_by_id: PyObjectId
    created_at: datetime = Field(default_factory=datetime.utcnow)

class DeadlineBase(MongoDBModel):
    period_type: PeriodType
    deadline_date: datetime

class DeadlineCreate(DeadlineBase):
    pass

class Deadline(DeadlineBase):
    id: PyObjectId = Field(default_factory=PyObjectId, alias="_id")
    created_at: datetime = Field(default_factory=datetime.utcnow)

def default_free_access():
    return datetime.utcnow() + timedelta(days=14)

class UserBase(MongoDBModel):
    email: EmailStr
    phone: str
    first_name: str
    last_name: str
    franchise_code: Optional[str] = None
    store_location: Optional[str] = None
    preferences: Optional[UserPreferences] = Field(default_factory=UserPreferences)

class UserCreate(UserBase):
    password: str
    username: Optional[str] = None
    contract_type: ContractType = ContractType.UOP
    role: UserRole = UserRole.EMPLOYEE

class UserUpdate(MongoDBModel):
    phone: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    franchise_code: Optional[str] = None
    store_location: Optional[str] = None
    contract_type: Optional[ContractType] = None
    vacation_days_left: Optional[int] = None
    preferences: Optional[UserPreferences] = None
    fte: Optional[float] = None
    seniority_years: Optional[int] = None
    monthly_hours_target: Optional[int] = None
    employment_start_date: Optional[datetime] = None

class User(UserBase):
    id: PyObjectId = Field(default_factory=PyObjectId, alias="_id")
    username: Optional[str] = None
    password_hash: str
    contract_type: ContractType = ContractType.UOP
    role: UserRole = UserRole.EMPLOYEE
    status: UserStatus = UserStatus.PENDING
    email_verified: bool = False
    verification_token: Optional[str] = None
    reset_token: Optional[str] = None
    vacation_days_left: int = 20
    is_active: bool = True
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    
    # Nowe pola
    fte: float = 1.0 # Wymiar etatu (1.0, 0.75, 0.5, 0.25)
    seniority_years: int = 0 # Staż pracy w latach
    monthly_hours_target: Optional[int] = None # Dla umów zlecenie
    employment_start_date: Optional[datetime] = None # Data rozpoczęcia pracy (do automatycznego stażu)

    # Subskrypcje i Płatności
    free_access_until: Optional[datetime] = Field(default_factory=default_free_access)
    is_subscription_active: bool = False
    subscription_plan: Optional[str] = None

    # Relacje jako listy ObjectId
    availability: List[PyObjectId] = []
    vacations: List[PyObjectId] = []
    assigned_schedules: List[PyObjectId] = []
    assigned_schedules_admin: List[PyObjectId] = []

    @validator('updated_at', pre=True, always=True)
    def set_updated_at(cls, v):
        return v or datetime.utcnow()

class ShiftChangeRequest(MongoDBModel):
    id: PyObjectId = Field(default_factory=PyObjectId, alias="_id")
    user_id: PyObjectId
    franchise_code: str
    date: datetime
    current_shift_name: Optional[str] = None
    requested_start_time: time
    requested_end_time: time
    reason: Optional[str] = None
    status: ShiftChangeStatus = ShiftChangeStatus.PENDING
    created_at: datetime = Field(default_factory=datetime.utcnow)
    responded_at: Optional[datetime] = None
    responded_by_id: Optional[PyObjectId] = None

class ShiftSwapRequest(MongoDBModel):
    id: PyObjectId = Field(default_factory=PyObjectId, alias="_id")
    requester_id: PyObjectId
    target_user_id: PyObjectId
    franchise_code: str
    my_date: datetime
    my_shift_name: str
    target_date: datetime
    target_shift_name: str
    status: SwapStatus = SwapStatus.REQUESTED
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: Optional[datetime] = None

class AppVersion(MongoDBModel):
    id: PyObjectId = Field(default_factory=PyObjectId, alias="_id")
    version: str
    platform: Platform
    release_notes: Optional[str] = None
    force_update: bool = False
    created_at: datetime = Field(default_factory=datetime.utcnow)

class ReferralCode(MongoDBModel):
    id: PyObjectId = Field(default_factory=PyObjectId, alias="_id")
    code: str
    owner_id: PyObjectId
    uses_left: int = 5
    max_uses: int = 5
    used_by: List[PyObjectId] = []
    created_at: datetime = Field(default_factory=datetime.utcnow)

# Indexy dla MongoDB
USER_INDEXES = [
    IndexModel([("email", ASCENDING)], unique=True),
    IndexModel([("phone", ASCENDING)], unique=True),
    IndexModel([("username", ASCENDING)], unique=True, sparse=True),
    IndexModel([("franchise_code", ASCENDING)]),
    IndexModel([("status", ASCENDING)]),
]

AVAILABILITY_INDEXES = [
    IndexModel([("user_id", ASCENDING), ("date", ASCENDING)]),
    IndexModel([("period_type", ASCENDING)]),
]

SCHEDULE_INDEXES = [
    IndexModel([("user_id", ASCENDING), ("date", ASCENDING)]),
    IndexModel([("assigned_by_id", ASCENDING)]),
]

VACATION_INDEXES = [
    IndexModel([("user_id", ASCENDING), ("status", ASCENDING)]),
    IndexModel([("start_date", ASCENDING), ("end_date", ASCENDING)]),
]

DEADLINE_INDEXES = [
    IndexModel([("period_type", ASCENDING)], unique=True),
]

LEAVE_INDEXES = [
    IndexModel([("user_id", ASCENDING), ("start_date", ASCENDING), ("end_date", ASCENDING)]),
]

SHIFT_CHANGE_REQUEST_INDEXES = [
    IndexModel([("franchise_code", ASCENDING), ("status", ASCENDING)]),
    IndexModel([("user_id", ASCENDING), ("date", ASCENDING)]),
]

SHIFT_SWAP_REQUEST_INDEXES = [
    IndexModel([("franchise_code", ASCENDING)]),
    IndexModel([("requester_id", ASCENDING)]),
    IndexModel([("target_user_id", ASCENDING)]),
]

APP_VERSION_INDEXES = [
    IndexModel([("platform", ASCENDING), ("created_at", DESCENDING)]),
]

REFERRAL_CODE_INDEXES = [
    IndexModel([("code", ASCENDING)], unique=True),
    IndexModel([("owner_id", ASCENDING)]),
]

class VerificationCode(BaseModel):
    email: str
    code: str
    expires_at: datetime
    created_at: datetime = Field(default_factory=datetime.utcnow)

    class Config:
        arbitrary_types_allowed = True
        json_encoders = {ObjectId: str}
