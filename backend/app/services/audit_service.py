import logging
from datetime import datetime
from typing import Optional, Dict, Any
from fastapi import Request
import motor.motor_asyncio

logger = logging.getLogger(__name__)

SENSITIVE_KEYS = {"password", "hashed_password", "token", "access_token", "refresh_token", "pesel", "secret"}


def _sanitize_details(details: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not details or not isinstance(details, dict):
        return {}
    clean: Dict[str, Any] = {}
    for k, v in details.items():
        if str(k).lower() in SENSITIVE_KEYS:
            clean[k] = "[REDACTED]"
        elif isinstance(v, dict):
            clean[k] = _sanitize_details(v)
        else:
            clean[k] = v
    return clean


def extract_client_ip(request: Optional[Request]) -> str:
    if not request:
        return "unknown"
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


async def log_audit_event(
    db: Optional[motor.motor_asyncio.AsyncIOMotorDatabase],
    action: str,
    user_id: Optional[str] = None,
    franchise_code: Optional[str] = None,
    ip_address: Optional[str] = None,
    resource_id: Optional[str] = None,
    details: Optional[Dict[str, Any]] = None,
):
    """Zapisuje niezmienny wpis audytowy w kolekcji audit_logs."""
    if db is None or not hasattr(db, "audit_logs"):
        return
    entry = {
        "timestamp": datetime.utcnow(),
        "action": action,
        "user_id": str(user_id) if user_id is not None else None,
        "franchise_code": franchise_code,
        "ip_address": ip_address or "unknown",
        "resource_id": str(resource_id) if resource_id is not None else None,
        "details": _sanitize_details(details),
    }
    try:
        await db.audit_logs.insert_one(entry)
    except Exception as exc:
        logger.error(f"Nie udało się zapisać wpisu audytowego ({action}): {exc}")

