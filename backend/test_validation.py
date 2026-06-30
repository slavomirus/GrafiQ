import asyncio
from app.schemas import UserUpdate, StoreSettingsCreate, ContractType
import pydantic

def test_user_update():
    payload = {
        "first_name": "Test",
        "last_name": "Test",
        "contract_type": "umowa zlecenie",
        "fte": None,
        "monthly_hours_target": 120,
        "seniority_years": None
    }
    try:
        user = UserUpdate(**payload)
        print("UserUpdate is valid:", user)
    except pydantic.ValidationError as e:
        print("UserUpdate Validation Error:", e)

def test_store_settings():
    payload = {
        "franchise_code": "123",
        "employees_per_morning_shift": 1,
        "employees_per_closing_shift": 1,
        "employees_on_promo_change": 2,
        "allow_overtime": False,
        "allow_inter_store_work": False,
        "franchisee_monthly_hours": 160,
        "shift_hours": {
            "morning": {"start_time": "06:00", "end_time": "14:30"},
            "middle": {"start_time": "12:00", "end_time": "20:00"},
            "closing": {"start_time": "14:30", "end_time": "23:00"}
        },
        "opening_hours": {
            "weekday": {"start_time": "06:00", "end_time": "23:00"},
            "sunday": {"start_time": "10:00", "end_time": "20:00"},
            "holiday": {}
        },
        "vacation_deadline_day": 20
    }
    try:
        settings = StoreSettingsCreate(**payload)
        print("StoreSettingsCreate is valid:", settings)
    except pydantic.ValidationError as e:
        print("StoreSettingsCreate Validation Error:", e)

if __name__ == "__main__":
    test_user_update()
    test_store_settings()
