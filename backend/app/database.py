import motor.motor_asyncio
from pymongo.errors import CollectionInvalid, OperationFailure
from typing import Optional
from fastapi import Depends, HTTPException
import logging
import certifi

from .config import settings

logger = logging.getLogger(__name__)


class DatabaseManager:
    def __init__(self, db_url: str, db_name: str):
        self.client: Optional[motor.motor_asyncio.AsyncIOMotorClient] = None
        self.db: Optional[motor.motor_asyncio.AsyncIOMotorDatabase] = None
        self.db_url = db_url
        self.db_name = db_name

    async def connect_to_db(self):
        try:
            logger.info("Łączenie z bazą danych MongoDB...")
            self.client = motor.motor_asyncio.AsyncIOMotorClient(
                self.db_url,
                tls=True,
                tlsCAFile=certifi.where()
            )
            # Weryfikacja połączenia
            await self.client.admin.command('ping')
            self.db = self.client[self.db_name]
            logger.info("Połączono z bazą danych MongoDB i zweryfikowano połączenie.")
            await self.create_collections()
            await self.create_indexes()
        except Exception as e:
            logger.error(f"Błąd podczas łączenia z bazą danych: {e}")
            raise HTTPException(status_code=500, detail="Błąd połączenia z bazą danych.")

    async def close_db_connection(self):
        if self.client:
            self.client.close()
            logger.info("Zamknięto połączenie z bazą danych MongoDB.")

    async def create_collections(self):
        collections = await self.db.list_collection_names()
        required_collections = [
            "users", "vacations", "schedule", "schedules", "schedule_drafts", 
            "availabilities", "stores", "storesettings", "specialopeninghours", "sick_leaves"
        ]

        for col in required_collections:
            if col not in collections:
                try:
                    await self.db.create_collection(col)
                    logger.info(f"Utworzono kolekcję: {col}")
                except CollectionInvalid:
                    pass
                except Exception as e:
                    logger.error(f"Błąd podczas tworzenia kolekcji {col}: {e}")

    async def _create_ttl_index(self, collection_name: str, field_name: str, expire_seconds: int):
        """Tworzy indeks TTL, usuwając stary w razie konfliktu."""
        try:
            await self.db[collection_name].create_index([(field_name, 1)], expireAfterSeconds=expire_seconds)
        except OperationFailure as e:
            if e.code == 85: # IndexOptionsConflict
                logger.warning(f"Konflikt indeksu TTL dla {collection_name}.{field_name}. Usuwanie starego indeksu...")
                # Znajdź nazwę indeksu
                indexes = await self.db[collection_name].index_information()
                for name, info in indexes.items():
                    if info['key'] == [(field_name, 1)]:
                        await self.db[collection_name].drop_index(name)
                        logger.info(f"Usunięto stary indeks {name}. Tworzenie nowego...")
                        await self.db[collection_name].create_index([(field_name, 1)], expireAfterSeconds=expire_seconds)
                        return
            else:
                raise e

    async def create_indexes(self):
        if not self.db is None:
            # Indeksy unikalne i wyszukiwania
            await self.db.users.create_index([("email", 1)], unique=True, sparse=True)
            await self.db.users.create_index([("username", 1)], unique=True, sparse=True)
            await self.db.users.create_index([("franchise_code", 1)])
            await self.db.stores.create_index([("franchise_code", 1)], unique=True)
            await self.db.storesettings.create_index([("franchise_code", 1)], unique=True)
            await self.db.specialopeninghours.create_index([("franchise_code", 1), ("date", 1)], unique=True)
            await self.db.vacations.create_index([("user_id", 1)])
            await self.db.vacations.create_index([("status", 1)])
            await self.db.schedule.create_index([("user_id", 1)])
            # await self.db.schedule.create_index([("date", 1)]) # USUNIĘTE - teraz to TTL
            await self.db.availabilities.create_index([("user_id", 1)])
            # await self.db.availabilities.create_index([("date", 1)]) # USUNIĘTE - teraz to TTL

            # --- INDEKSY TTL (Automatyczne usuwanie po 180 dniach) ---
            expire_seconds = 180 * 24 * 60 * 60 # 180 dni
            
            await self._create_ttl_index("schedule", "date", expire_seconds)
            await self._create_ttl_index("schedules", "end_date", expire_seconds)
            await self._create_ttl_index("schedule_drafts", "start_date", expire_seconds)
            await self._create_ttl_index("vacations", "end_date", expire_seconds)
            await self._create_ttl_index("sick_leaves", "end_date", expire_seconds)
            await self._create_ttl_index("availabilities", "date", expire_seconds)

            logger.info("Utworzono indeksy (w tym TTL) w bazie danych.")
        else:
            logger.error("Błąd: Nie można utworzyć indeksów, połączenie z bazą danych nie jest aktywne.")


db_manager = DatabaseManager(settings.MONGODB_URI, settings.MONGODB_DB_NAME)


async def get_db():
    if db_manager.db is None:
        raise HTTPException(status_code=500, detail="Baza danych nie jest połączona.")
    return db_manager.db
