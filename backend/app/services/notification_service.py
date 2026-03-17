import logging
import firebase_admin
from firebase_admin import credentials, messaging
from typing import List, Optional
import motor.motor_asyncio
from bson import ObjectId
import os

logger = logging.getLogger(__name__)

# Inicjalizacja Firebase (Singleton)
_firebase_initialized = False

def initialize_firebase():
    global _firebase_initialized
    if _firebase_initialized:
        return

    try:
        # Szukamy pliku klucza w głównym katalogu lub w backend/
        cred_path = "serviceAccountKey.json"
        if not os.path.exists(cred_path):
            cred_path = "backend/serviceAccountKey.json"
            
        if os.path.exists(cred_path):
            cred = credentials.Certificate(cred_path)
            # Sprawdź czy aplikacja już istnieje, żeby uniknąć błędu przy reloadzie
            try:
                firebase_admin.get_app()
            except ValueError:
                firebase_admin.initialize_app(cred)
            
            _firebase_initialized = True
            logger.info("Firebase Admin SDK initialized successfully.")
        else:
            logger.warning(f"Firebase serviceAccountKey.json not found at {cred_path}. Push notifications will be disabled.")
    except Exception as e:
        logger.error(f"Failed to initialize Firebase: {e}")

# Wywołaj przy imporcie (bezpieczne, bo sprawdza flagę)
initialize_firebase()

async def send_push_to_user(
    db: motor.motor_asyncio.AsyncIOMotorDatabase,
    user_id: ObjectId,
    title: str,
    body: str,
    data: Optional[dict] = None
):
    """
    Wysyła powiadomienie push do wszystkich urządzeń użytkownika.
    """
    if not _firebase_initialized:
        logger.warning(f"Skipping push notification to {user_id} (Firebase not initialized). Title: {title}")
        return

    try:
        user = await db.users.find_one({"_id": user_id})
        if not user:
            logger.warning(f"User {user_id} not found for push notification.")
            return

        tokens = user.get("fcm_tokens", [])
        if not tokens:
            logger.info(f"User {user_id} has no registered FCM tokens.")
            return

        # Oczyszczanie danych (wszystkie wartości muszą być stringami)
        safe_data = {}
        if data:
            for k, v in data.items():
                safe_data[k] = str(v)

        message = messaging.MulticastMessage(
            notification=messaging.Notification(
                title=title,
                body=body,
            ),
            data=safe_data,
            tokens=tokens,
        )

        # Próba wysłania nową metodą (v5+), a jak nie to starą, a jak nie to pętlą
        try:
            # Dla nowszych wersji firebase-admin
            if hasattr(messaging, 'send_multicast'):
                response = messaging.send_multicast(message)
            elif hasattr(messaging, 'send_each_for_multicast'):
                response = messaging.send_each_for_multicast(message)
            else:
                # Fallback dla bardzo starych lub dziwnych wersji - pętla
                success_count = 0
                failure_count = 0
                failed_tokens = []
                for token in tokens:
                    try:
                        single_msg = messaging.Message(
                            notification=messaging.Notification(title=title, body=body),
                            data=safe_data,
                            token=token
                        )
                        messaging.send(single_msg)
                        success_count += 1
                    except Exception:
                        failure_count += 1
                        failed_tokens.append(token)
                
                logger.info(f"Sent push (loop fallback) to user {user_id}: {success_count} success, {failure_count} failed.")
                return # Koniec dla fallbacku

        except AttributeError:
             # Jeśli MulticastMessage nie istnieje w tej wersji, użyj pętli
             success_count = 0
             for token in tokens:
                try:
                    single_msg = messaging.Message(
                        notification=messaging.Notification(title=title, body=body),
                        data=safe_data,
                        token=token
                    )
                    messaging.send(single_msg)
                    success_count += 1
                except Exception as e:
                    logger.error(f"Failed to send to token {token}: {e}")
             logger.info(f"Sent push (loop fallback v2) to user {user_id}: {success_count} sent.")
             return

        # Obsługa odpowiedzi z send_multicast / send_each_for_multicast
        if response.failure_count > 0:
            responses = response.responses
            failed_tokens = []
            for idx, resp in enumerate(responses):
                if not resp.success:
                    # Usuń nieaktywne tokeny
                    failed_tokens.append(tokens[idx])
            
            if failed_tokens:
                logger.info(f"Removing {len(failed_tokens)} invalid FCM tokens for user {user_id}")
                await db.users.update_one(
                    {"_id": user_id},
                    {"$pull": {"fcm_tokens": {"$in": failed_tokens}}}
                )

        logger.info(f"Sent push to user {user_id}: {response.success_count} success, {response.failure_count} failed.")

    except Exception as e:
        logger.error(f"Error sending push notification to {user_id}: {e}")

async def send_push_to_admins(
    db: motor.motor_asyncio.AsyncIOMotorDatabase,
    franchise_code: str,
    title: str,
    body: str,
    data: Optional[dict] = None
):
    """
    Wysyła powiadomienie do wszystkich adminów/franczyzobiorców danego sklepu.
    """
    admins = await db.users.find({
        "franchise_code": franchise_code,
        "role": {"$in": ["admin", "franchisee"]}
    }).to_list(length=None)

    for admin in admins:
        await send_push_to_user(db, admin["_id"], title, body, data)
