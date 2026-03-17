from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect, Query
from typing import List, Optional
from .. import models, schemas, dependencies
from ..database import get_db
import motor.motor_asyncio
from datetime import datetime

router = APIRouter()

# --- WebSocket Connection Manager ---

class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except Exception:
                # Handle potential errors (e.g., connection closed)
                pass

manager = ConnectionManager()

# --- Endpoints ---

@router.get("/check", response_model=schemas.AppVersionResponse)
async def check_for_updates(
    platform: schemas.Platform,
    current_version: Optional[str] = None,
    db: motor.motor_asyncio.AsyncIOMotorClient = Depends(get_db)
):
    """
    Sprawdza dostępność nowej wersji aplikacji dla danej platformy.
    Zwraca najnowszą wersję.
    """
    # Pobierz najnowszą wersję dla danej platformy
    latest_version = await db.app_versions.find_one(
        {"platform": platform.value},
        sort=[("created_at", -1)]
    )

    if not latest_version:
        raise HTTPException(status_code=404, detail="No version information found")

    return latest_version

@router.post("/", response_model=schemas.AppVersionResponse)
async def create_app_version(
    version_data: schemas.AppVersionCreate,
    current_user: dict = Depends(dependencies.get_current_admin_user),
    db: motor.motor_asyncio.AsyncIOMotorClient = Depends(get_db)
):
    """
    Tworzy nową wersję aplikacji (tylko dla admina/franczyzobiorcy).
    Wysyła powiadomienie przez WebSocket do podłączonych klientów.
    """
    new_version = models.AppVersion(**version_data.dict())
    result = await db.app_versions.insert_one(new_version.model_dump(by_alias=True))
    
    created_version = await db.app_versions.find_one({"_id": result.inserted_id})
    
    # Broadcast update notification
    await manager.broadcast({
        "type": "UPDATE_AVAILABLE",
        "platform": version_data.platform.value,
        "version": version_data.version,
        "force_update": version_data.force_update,
        "release_notes": version_data.release_notes
    })
    
    return created_version

@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            # Keep the connection alive and listen for any client messages (optional)
            # For now, we just wait. The server pushes updates.
            await websocket.receive_text() 
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception:
        manager.disconnect(websocket)
