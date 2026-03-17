from datetime import datetime, timedelta, date, time
from bson import ObjectId
from typing import List, Optional, Union
import motor.motor_asyncio
import re
import secrets
import string
from passlib.context import CryptContext
from collections import defaultdict

from . import models, schemas
from .database import get_db # 🟢 Importujemy tylko get_db
import logging

logger = logging.getLogger(__name__)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)

def generate_unique_username(first_name: str, last_name: str, existing_usernames: Optional[List[str]] = None):
    base_username = f"{first_name[0].lower()}{last_name.lower()}"
    username = base_username
    counter = 1
    # Check if the username already exists. This will be more efficient if we can check against a set of existing usernames.
    while existing_usernames and username in existing_usernames:
        username = f"{base_username}{counter}"
        counter += 1
    return username

def validate_phone_number(phone: str) -> bool:
    return bool(re.match(r'^\+?[1-9]\d{1,14}$', phone))

def generate_secure_password() -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    return ''.join(secrets.choice(alphabet) for _ in range(12))

async def get_schedule_entry_by_id(db: motor.motor_asyncio.AsyncIOMotorDatabase, shift_id: ObjectId) -> Optional[dict]:
    """Pobiera pojedynczy wpis grafiku na podstawie jego ID."""
    return await db.schedule.find_one({"_id": shift_id})

async def create_leave(db: motor.motor_asyncio.AsyncIOMotorDatabase, user_id: ObjectId, leave_data: schemas.L4LeaveRequest) -> dict:
    """Tworzy nowy wpis o zwolnieniu L4 w bazie danych."""
    leave_doc = {
        "user_id": user_id,
        "type": models.LeaveType.L4.value,
        "start_date": datetime.combine(leave_data.startDate, time.min),
        "end_date": datetime.combine(leave_data.endDate, time.max),
        "submitted_at": datetime.utcnow()
    }
    result = await db.leaves.insert_one(leave_doc)
    return await db.leaves.find_one({"_id": result.inserted_id})

async def delete_schedule_entries_for_user(db: motor.motor_asyncio.AsyncIOMotorDatabase, user_id: ObjectId, start_date: date, end_date: date) -> int:
    """Usuwa wszystkie wpisy w grafiku dla danego użytkownika w podanym zakresie dat."""
    start_datetime = datetime.combine(start_date, time.min)
    end_datetime = datetime.combine(end_date, time.max)
    
    delete_result = await db.schedule.delete_many({
        "user_id": user_id,
        "date": {"$gte": start_datetime, "$lte": end_datetime}
    })
    return delete_result.deleted_count

async def get_schedule_for_date_range(db: motor.motor_asyncio.AsyncIOMotorDatabase, franchise_code: str, start_date: date, end_date: date) -> List[schemas.ScheduleDayResponse]:
    """Pobiera i agreguje grafik dla danego sklepu i zakresu dat."""
    start_datetime = datetime.combine(start_date, time.min)
    end_datetime = datetime.combine(end_date, time.max)

    shifts_cursor = db.schedule.find({
        "franchise_code": franchise_code,
        "date": {"$gte": start_datetime, "$lte": end_datetime}
    })

    user_ids = set()
    raw_shifts = await shifts_cursor.to_list(length=None)
    for shift in raw_shifts:
        user_ids.add(shift["user_id"])

    users_cursor = db.users.find({"_id": {"$in": list(user_ids)}})
    users_map = {user["_id"]: schemas.ShiftEmployeeResponse(**user) async for user in users_cursor}

    schedule_by_day = defaultdict(lambda: defaultdict(lambda: {"employees": []}))
    for shift in raw_shifts:
        shift_date = shift["date"].date()
        shift_name = shift.get("shift_name", "unknown")
        
        if not schedule_by_day[shift_date][shift_name].get("start_time"):
            schedule_by_day[shift_date][shift_name]["start_time"] = shift["start_time"]
            schedule_by_day[shift_date][shift_name]["end_time"] = shift["end_time"]
        
        employee_data = users_map.get(shift["user_id"])
        if employee_data:
            schedule_by_day[shift_date][shift_name]["employees"].append(employee_data)

    response = []
    current_date = start_date
    while current_date <= end_date:
        day_data = schedule_by_day.get(current_date, {})
        day_shifts = {}
        for shift_name, shift_details in day_data.items():
            day_shifts[shift_name] = schemas.ShiftDetailResponse(**shift_details)
        
        response.append(schemas.ScheduleDayResponse(date=current_date, shifts=day_shifts))
        current_date += timedelta(days=1)
        
    return response

# User CRUD Operations
class UserCRUD:
    @staticmethod
    async def get_user_by_username(db: motor.motor_asyncio.AsyncIOMotorClient, username: str):
        return await db.users.find_one({"username": username})

    @staticmethod
    async def get_user_by_email(db: motor.motor_asyncio.AsyncIOMotorClient, email: str):
        return await db.users.find_one({"email": email})

    @staticmethod
    async def get_user_by_phone(db: motor.motor_asyncio.AsyncIOMotorClient, phone: str):
        if not phone:
            return None
        return await db.users.find_one({"phone": phone})

    @staticmethod
    async def get_user_by_identifier(db: motor.motor_asyncio.AsyncIOMotorClient, identifier: str):
        return await db.users.find_one({
            "$or": [
                {"email": identifier},
                {"username": identifier},
                {"phone": identifier}
            ]
        })

    @staticmethod
    async def get_user_by_id(db: motor.motor_asyncio.AsyncIOMotorClient, user_id: Union[str, ObjectId]):
        if isinstance(user_id, str):
            user_id = ObjectId(user_id)
        return await db.users.find_one({"_id": user_id})

    @staticmethod
    async def create_user(db: motor.motor_asyncio.AsyncIOMotorClient, user_data: dict):
        result = await db.users.insert_one(user_data)
        return await db.users.find_one({"_id": result.inserted_id})

    @staticmethod
    async def get_all_users(db: motor.motor_asyncio.AsyncIOMotorClient, skip: int = 0, limit: int = 100):
        return await db.users.find().skip(skip).limit(limit).to_list(length=limit)

    @staticmethod
    async def update_user_status(db: motor.motor_asyncio.AsyncIOMotorClient, user_id: str, status: models.UserStatus):
        return await db.users.find_one_and_update(
            {"_id": ObjectId(user_id)},
            {"$set": {"status": status.value, "updated_at": datetime.utcnow()}},
            return_document=motor.motor_asyncio.ReturnDocument.AFTER
        )

    @staticmethod
    async def update_user_password(db: motor.motor_asyncio.AsyncIOMotorClient, user_id: str, new_password: str):
        hashed_password = get_password_hash(new_password)
        return await db.users.find_one_and_update(
            {"_id": ObjectId(user_id)},
            {"$set": {
                "password_hash": hashed_password,
                "updated_at": datetime.utcnow()
            }},
            return_document=motor.motor_asyncio.ReturnDocument.AFTER
        )

    @staticmethod
    async def update_user(db: motor.motor_asyncio.AsyncIOMotorClient, user_id: str, update_data: dict):
        update_data["updated_at"] = datetime.utcnow()
        if 'role' in update_data and hasattr(update_data['role'], 'value'):
            update_data['role'] = update_data['role'].value
        if 'status' in update_data and hasattr(update_data['status'], 'value'):
            update_data['status'] = update_data['status'].value
        if 'contract_type' in update_data and hasattr(update_data['contract_type'], 'value'):
            update_data['contract_type'] = update_data['contract_type'].value

        return await db.users.find_one_and_update(
            {"_id": ObjectId(user_id)},
            {"$set": update_data},
            return_document=motor.motor_asyncio.ReturnDocument.AFTER
        )
    @staticmethod
    async def create_verification_code(db: motor.motor_asyncio.AsyncIOMotorClient, email: str, code: str, expires_minutes=15):
        verification_data = {
            "email": email,
            "code": code,
            "expires_at": datetime.utcnow() + timedelta(minutes=expires_minutes),
            "created_at": datetime.utcnow(),
            "used": False
        }
        await db.verification_codes.insert_one(verification_data)

# Availability CRUD Operations
class AvailabilityCRUD:
    @staticmethod
    async def create_availability(db: motor.motor_asyncio.AsyncIOMotorClient, availability: schemas.AvailabilityCreate, user_id: str):
        availability_dict = {
            "user_id": ObjectId(user_id),
            "date": datetime.combine(availability.date, time.min),
            "start_time": availability.start_time,
            "end_time": availability.end_time,
            "period_type": availability.period_type.value,
            "submitted_at": datetime.utcnow()
        }

        result = await db.availability.insert_one(availability_dict)
        await db.users.update_one(
            {"_id": ObjectId(user_id)},
            {"$push": {"availability": result.inserted_id}}
        )
        return await db.availability.find_one({"_id": result.inserted_id})

    @staticmethod
    async def get_user_availability(db: motor.motor_asyncio.AsyncIOMotorClient, user_id: str, start_date: datetime, end_date: datetime):
        return await db.availability.find({
            "user_id": ObjectId(user_id),
            "date": {"$gte": start_date, "$lte": end_date}
        }).to_list(length=None)

    @staticmethod
    async def get_availability_by_id(db: motor.motor_asyncio.AsyncIOMotorClient, availability_id: str):
        return await db.availability.find_one({"_id": ObjectId(availability_id)})

    @staticmethod
    async def get_availability_by_user_and_date(db: motor.motor_asyncio.AsyncIOMotorClient, user_id: str, date: date):
        start_of_day = datetime.combine(date, time.min)
        end_of_day = datetime.combine(date, time.max)
        return await db.availability.find_one({
            "user_id": ObjectId(user_id),
            "date": {"$gte": start_of_day, "$lte": end_of_day}
        })

    @staticmethod
    async def delete_availability(db: motor.motor_asyncio.AsyncIOMotorClient, availability_id: str, user_id: str):
        result = await db.availability.delete_one({"_id": ObjectId(availability_id)})
        if result.deleted_count > 0:
            await db.users.update_one(
                {"_id": ObjectId(user_id)},
                {"$pull": {"availability": ObjectId(availability_id)}}
            )
        return result.deleted_count > 0

    @staticmethod
    async def update_availability(db: motor.motor_asyncio.AsyncIOMotorClient, availability_id: str, update_data: dict):
        if 'period_type' in update_data and hasattr(update_data['period_type'], 'value'):
            update_data['period_type'] = update_data['period_type'].value

        return await db.availability.find_one_and_update(
            {"_id": ObjectId(availability_id)},
            {"$set": update_data},
            return_document=motor.motor_asyncio.ReturnDocument.AFTER
        )

    @staticmethod
    async def get_user_availabilities(db: motor.motor_asyncio.AsyncIOMotorClient, user_id: str):
        return await db.availability.find({"user_id": ObjectId(user_id)}).to_list(length=None)


# Vacation CRUD Operations
class VacationCRUD:
    @staticmethod
    async def create_vacation_request(db: motor.motor_asyncio.AsyncIOMotorClient, vacation: schemas.VacationCreate, user_id: str):
        vacation_dict = {
            "user_id": ObjectId(user_id),
            "start_date": datetime.combine(vacation.start_date, time.min),
            "end_date": datetime.combine(vacation.end_date, time.max),
            "reason": vacation.reason,
            "status": models.VacationStatus.PENDING.value,
            "submitted_at": datetime.utcnow()
        }

        result = await db.vacations.insert_one(vacation_dict)
        await db.users.update_one(
            {"_id": ObjectId(user_id)},
            {"$push": {"vacations": result.inserted_id}}
        )
        return await db.vacations.find_one({"_id": result.inserted_id})

    @staticmethod
    async def get_user_vacation_requests(db: motor.motor_asyncio.AsyncIOMotorClient, user_id: str):
        return await db.vacations.find(
            {"user_id": ObjectId(user_id)}
        ).sort("start_date", -1).to_list(length=None)

    @staticmethod
    async def get_vacation_by_id(db: motor.motor_asyncio.AsyncIOMotorClient, vacation_id: str):
        return await db.vacations.find_one({"_id": ObjectId(vacation_id)})

    @staticmethod
    async def update_vacation_status(db: motor.motor_asyncio.AsyncIOMotorClient, vacation_id: str, status: models.VacationStatus, approved_by_id: Optional[str] = None):
        update_data = {
            "status": status.value,
            "reviewed_at": datetime.utcnow()
        }
        if approved_by_id:
            update_data["approved_by_id"] = ObjectId(approved_by_id)

        return await db.vacations.find_one_and_update(
            {"_id": ObjectId(vacation_id)},
            {"$set": update_data},
            return_document=motor.motor_asyncio.ReturnDocument.AFTER
        )

    @staticmethod
    async def get_pending_vacation_requests(db: motor.motor_asyncio.AsyncIOMotorClient):
        return await db.vacations.find(
            {"status": models.VacationStatus.PENDING.value}
        ).sort("submitted_at", 1).to_list(length=None)


# Deadline CRUD Operations
class DeadlineCRUD:
    @staticmethod
    async def create_deadline(db: motor.motor_asyncio.AsyncIOMotorClient, deadline: schemas.DeadlineCreate):
        deadline_dict = {
            "period_type": deadline.period_type.value,
            "deadline_date": datetime.combine(deadline.deadline_date, time.min),
            "created_at": datetime.utcnow()
        }
        result = await db.deadlines.insert_one(deadline_dict)
        return await db.deadlines.find_one({"_id": result.inserted_id})

    @staticmethod
    async def get_deadline_by_period_type(db: motor.motor_asyncio.AsyncIOMotorClient, period_type: models.PeriodType):
        return await db.deadlines.find_one({"period_type": period_type.value})

    @staticmethod
    async def update_deadline(db: motor.motor_asyncio.AsyncIOMotorClient, period_type: models.PeriodType, deadline_date: datetime):
        return await db.deadlines.find_one_and_update(
            {"period_type": period_type.value},
            {"$set": {"deadline_date": deadline_date, "created_at": datetime.utcnow()}},
            upsert=True,
            return_document=motor.motor_asyncio.ReturnDocument.AFTER
        )

    @staticmethod
    async def get_all_deadlines(db: motor.motor_asyncio.AsyncIOMotorClient):
        return await db.deadlines.find().sort("period_type", 1).to_list(length=None)


# Inicjalizacja CRUD operations
user_crud = UserCRUD()
availability_crud = AvailabilityCRUD()
vacation_crud = VacationCRUD()
deadline_crud = DeadlineCRUD()
