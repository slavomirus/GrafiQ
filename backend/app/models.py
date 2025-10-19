from datetime import datetime, date, time
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
        return core_schema.no_info_after_validator_function(
            cls.validate,
            core_schema.str_schema(),
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
    NEEDS_PASSWORD_CHANGE = "needs_password_change" # <-- POPRAWKA: Dodano brakujący status

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

# Base model z konfiguracją dla MongoDB
class MongoDBModel(BaseModel):
    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        json_encoders={ObjectId: str},
        use_enum_values=True,
        populate_by_name=True
    )

# Schematy Pydantic dla dokumentów MongoDB
class AvailabilityBase(MongoDBModel):
    date: date
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
    start_date: date
    end_date: date
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
    date: date
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
    deadline_date: date

class DeadlineCreate(DeadlineBase):
    pass

class Deadline(DeadlineBase):
    id: PyObjectId = Field(default_factory=PyObjectId, alias="_id")
    created_at: datetime = Field(default_factory=datetime.utcnow)

class UserBase(MongoDBModel):
    email: EmailStr
    phone: str
    first_name: str
    last_name: str
    franchise_code: Optional[str] = None
    store_location: Optional[str] = None

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

    # Relacje jako listy ObjectId
    availability: List[PyObjectId] = []
    vacations: List[PyObjectId] = []
    assigned_schedules: List[PyObjectId] = []
    assigned_schedules_admin: List[PyObjectId] = []

    @validator('updated_at', pre=True, always=True)
    def set_updated_at(cls, v):
        return v or datetime.utcnow()

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


class VerificationCode(BaseModel):
    email: str
    code: str
    expires_at: datetime
    created_at: datetime = Field(default_factory=datetime.utcnow)

    class Config:
        arbitrary_types_allowed = True
        json_encoders = {ObjectId: str}
