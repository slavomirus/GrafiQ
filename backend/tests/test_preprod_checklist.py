import os
import sys
import unittest
from datetime import date, datetime, time, timedelta
from bson import ObjectId
from fastapi import HTTPException

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import models, schemas, security
from app.dependencies import get_current_user, get_current_admin_user
from app.endpoints.auth import (
    login_for_access_token,
    refresh_access_token,
    revoke_all_sessions,
    _LOGIN_FAILURES,
)
from app.endpoints.users import get_user, anonymize_user_rodo, get_audit_logs
from app.services.audit_service import log_audit_event
from app.services.encryption_service import encrypt_field, decrypt_field
from app.services.schedule_service import resolve_shift_hours
from app.services.validator_service import ScheduleValidator


def _match(doc: dict, query: dict) -> bool:
    for k, v in query.items():
        if k == "$or":
            if not any(_match(doc, sub) for sub in v):
                return False
            continue
        val = doc.get(k)
        if isinstance(v, dict):
            if "$in" in v and val not in v["$in"]:
                return False
            if "$nin" in v and val in v["$nin"]:
                return False
            if "$ne" in v and val == v["$ne"]:
                return False
            if "$gte" in v and (val is None or val < v["$gte"]):
                return False
            if "$lte" in v and (val is None or val > v["$lte"]):
                return False
            if "$lt" in v and (val is None or val >= v["$lt"]):
                return False
        else:
            if val != v:
                return False
    return True


class InMemCursor:
    def __init__(self, items):
        self.items = list(items)

    def sort(self, field, direction):
        self.items.sort(key=lambda x: x.get(field) or datetime.min, reverse=(direction < 0))
        return self

    def limit(self, n):
        self.items = self.items[:n]
        return self

    async def to_list(self, length=None):
        return self.items


class InMemColl:
    def __init__(self, docs=None):
        self.docs = list(docs or [])

    async def find_one(self, query):
        for d in self.docs:
            if _match(d, query):
                return dict(d)
        return None

    def find(self, query):
        return InMemCursor([dict(d) for d in self.docs if _match(d, query)])

    async def insert_one(self, doc):
        d = dict(doc)
        if "_id" not in d:
            d["_id"] = ObjectId()
        self.docs.append(d)

        class Res:
            inserted_id = d["_id"]

        return Res()

    async def update_one(self, query, upd, upsert=False):
        for i, d in enumerate(self.docs):
            if _match(d, query):
                nd = dict(d)
                if "$set" in upd:
                    nd.update(upd["$set"])
                if "$inc" in upd:
                    for k, v in upd["$inc"].items():
                        nd[k] = nd.get(k, 0) + v
                if "$unset" in upd:
                    for k in upd["$unset"]:
                        nd.pop(k, None)
                self.docs[i] = nd
                return


class MockDB:
    def __init__(self):
        self.users = InMemColl()
        self.schedule = InMemColl()
        self.vacations = InMemColl()
        self.sick_leaves = InMemColl()
        self.audit_logs = InMemColl()


def split_night_shift_hours_across_month(shift_start: datetime, shift_end: datetime) -> dict:
    """Pomocniczy podział godzin zmiany nocnej na przełomie miesiąca."""
    totals = {}
    cur = shift_start
    while cur < shift_end:
        next_midnight = datetime.combine(cur.date() + timedelta(days=1), time.min)
        seg_end = min(shift_end, next_midnight)
        ym = cur.strftime("%Y-%m")
        totals[ym] = totals.get(ym, 0.0) + (seg_end - cur).total_seconds() / 3600.0
        cur = seg_end
    return totals


class PreProdChecklistTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _LOGIN_FAILURES.clear()

    async def test_1_kodeks_pracy_rest_and_leaves(self):
        db = MockDB()
        emp_id = ObjectId()
        day1 = date(2026, 11, 10)
        day2 = date(2026, 11, 11)
        await db.schedule.insert_one({
            "franchise_code": "ZAB1",
            "user_id": emp_id,
            "date": datetime.combine(day1, time.min),
            "shift_name": "closing",
            "start_time": "14:00",
            "end_time": "23:00",
        })
        validator = ScheduleValidator(db)
        # 23:00 -> 06:00 = 7h (< 11h) -> MUST fail
        ok, reason = await validator.validate_shift_assignment(emp_id, day2, time(6, 0), time(14, 0), "ZAB1")
        self.assertFalse(ok)
        self.assertIn("11h", reason)

        # 23:00 -> 10:00 = 11h (>= 11h) -> MUST pass
        ok2, _ = await validator.validate_shift_assignment(emp_id, day2, time(10, 0), time(18, 0), "ZAB1")
        self.assertTrue(ok2)

        # L4 block
        l4_day = date(2026, 11, 15)
        await db.sick_leaves.insert_one({
            "user_id": emp_id,
            "start_date": datetime.combine(l4_day, time.min),
            "end_date": datetime.combine(l4_day + timedelta(days=6), time.max),
        })
        ok_l4, reason_l4 = await validator.validate_shift_assignment(emp_id, l4_day + timedelta(days=2), time(6, 0), time(14, 0), "ZAB1")
        self.assertFalse(ok_l4)
        self.assertIn("L4", reason_l4)

    def test_2_month_boundary_and_closed_holiday(self):
        split = split_night_shift_hours_across_month(datetime(2026, 11, 30, 22, 0), datetime(2026, 12, 1, 6, 0))
        self.assertEqual(split, {"2026-11": 2.0, "2026-12": 6.0})
        self.assertEqual(resolve_shift_hours(date(2026, 12, 25), "morning", {}, {"2026-12-25": {"is_closed": True}}), ("00:00", "00:00"))

    async def test_3_idor_and_rbac_multi_tenancy(self):
        db = MockDB()
        franchisee_a = {
            "_id": ObjectId(), "email": "a@zabka.pl", "role": models.UserRole.FRANCHISEE.value,
            "status": models.UserStatus.ACTIVE.value, "franchise_code": "ZAB_A", "franchise_codes": ["ZAB_A"],
            "first_name": "A", "last_name": "A",
        }
        emp_b = {
            "_id": ObjectId(), "email": "b@zabka.pl", "role": models.UserRole.EMPLOYEE.value,
            "status": models.UserStatus.ACTIVE.value, "franchise_code": "ZAB_B",
            "first_name": "B", "last_name": "B", "email_verified": True, "vacation_days_left": 20, "created_at": datetime.utcnow(),
        }
        await db.users.insert_one(franchisee_a)
        await db.users.insert_one(emp_b)

        # Franchisee A trying to read Franchisee B's employee -> 403 Forbidden
        with self.assertRaises(HTTPException) as ctx:
            await get_user(str(emp_b["_id"]), franchisee_a, db)
        self.assertEqual(ctx.exception.status_code, 403)

        # RBAC: employee cannot pass get_current_admin_user
        with self.assertRaises(HTTPException) as ctx_rbac:
            await get_current_admin_user(emp_b)
        self.assertEqual(ctx_rbac.exception.status_code, 403)

    async def test_4_iam_bruteforce_refresh_rotation_and_revocation(self):
        db = MockDB()
        uid = ObjectId()
        await db.users.insert_one({
            "_id": uid,
            "email": "pracownik@zabka.pl",
            "hashed_password": security.get_password_hash("StrongPass123!"),
            "role": models.UserRole.EMPLOYEE.value,
            "status": models.UserStatus.ACTIVE.value,
            "franchise_code": "ZAB1",
            "token_version": 0,
        })

        # 5 failed logins -> lockout (429)
        for _ in range(5):
            with self.assertRaises(HTTPException) as err:
                await login_for_access_token(schemas.LoginRequest(email="pracownik@zabka.pl", password="wrong"), None, db)
            self.assertEqual(err.exception.status_code, 401)

        with self.assertRaises(HTTPException) as lock_err:
            await login_for_access_token(schemas.LoginRequest(email="pracownik@zabka.pl", password="StrongPass123!"), None, db)
        self.assertEqual(lock_err.exception.status_code, 429)

        # Clear lockout and log in cleanly
        _LOGIN_FAILURES.clear()
        tokens = await login_for_access_token(schemas.LoginRequest(email="pracownik@zabka.pl", password="StrongPass123!"), None, db)
        access_1 = tokens["access_token"]
        refresh_1 = tokens["refresh_token"]

        # Refresh token rotation
        rotated = await refresh_access_token(schemas.RefreshTokenRequest(refresh_token=refresh_1), None, db)
        self.assertNotEqual(rotated["refresh_token"], refresh_1)

        # Replay of old refresh_1 -> detected & revokes all sessions (increments token_version)
        with self.assertRaises(HTTPException) as replay_err:
            await refresh_access_token(schemas.RefreshTokenRequest(refresh_token=refresh_1), None, db)
        self.assertEqual(replay_err.exception.status_code, 401)

        # access_1 is now invalid because token_version incremented
        with self.assertRaises(HTTPException) as revoked_err:
            await get_current_user(token=access_1, x_franchise_code=None, db=db)
        self.assertEqual(revoked_err.exception.status_code, 401)

        # Explicit revoke_all_sessions works too
        user_doc = await db.users.find_one({"_id": uid})
        tv_before = user_doc["token_version"]
        await revoke_all_sessions(None, user_doc, db)
        user_after = await db.users.find_one({"_id": uid})
        self.assertEqual(user_after["token_version"], tv_before + 1)

    async def test_5_encryption_audit_and_rodo_anonymization(self):
        db = MockDB()
        encrypted = encrypt_field("90010112345")
        self.assertTrue(encrypted.startswith("enc::"))
        self.assertNotIn("90010112345", encrypted)
        self.assertEqual(decrypt_field(encrypted), "90010112345")
        self.assertEqual(decrypt_field("legacy_plain"), "legacy_plain")

        await log_audit_event(db, "REPORT_EXPORTED", user_id="u1", franchise_code="ZAB1", ip_address="127.0.0.1", details={"pesel": "90010112345", "month": "2026-11"})
        self.assertEqual(db.audit_logs.docs[0]["details"]["pesel"], "[REDACTED]")

        admin_u = {"_id": ObjectId(), "role": models.UserRole.FRANCHISEE.value, "franchise_code": "ZAB1"}
        emp_u = {"_id": ObjectId(), "first_name": "Jan", "last_name": "Kowalski", "email": "jan@x.pl", "phone": "500600700", "franchise_code": "ZAB1", "status": models.UserStatus.ACTIVE.value, "token_version": 0}
        await db.users.insert_one(emp_u)
        await anonymize_user_rodo(str(emp_u["_id"]), None, admin_u, db)
        anon_doc = await db.users.find_one({"_id": emp_u["_id"]})
        self.assertEqual(anon_doc["first_name"], "Anonimowy")
        self.assertEqual(anon_doc["status"], models.UserStatus.SUSPENDED.value)
        self.assertEqual(anon_doc["token_version"], 1)

        logs = await get_audit_logs(50, admin_u, db)
        self.assertTrue(any(l["action"] == "USER_ANONYMIZED" for l in logs))


if __name__ == "__main__":
    unittest.main()

