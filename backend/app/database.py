import motor.motor_asyncio
from pymongo.errors import CollectionInvalid
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
            self.client = motor.motor_asyncio.AsyncIOMotorClient(
                self.db_url,
                tlsCAFile=certifi.where()
            )
            self.db = self.client[self.db_name]
            logger.info("Połączono z bazą danych MongoDB.")
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
        # POPRAWKA: Dodano nowe kolekcje
        required_collections = ["users", "vacations", "schedule", "availabilities", "stores", "storesettings", "specialopeninghours"]

        for col in required_collections:
            if col not in collections:
                try:
                    await self.db.create_collection(col)
                    logger.info(f"Utworzono kolekcję: {col}")
                except CollectionInvalid as e:
                    logger.error(f"Błąd podczas tworzenia kolekcji {col}: {e}")

    async def create_indexes(self):
        if not self.db is None:
            # Indeksy dla Użytkowników
            await self.db.users.create_index([("email", 1)], unique=True, sparse=True)
            await self.db.users.create_index([("username", 1)], unique=True, sparse=True)
            await self.db.users.create_index([("franchise_code", 1)])
            
            # Indeksy dla Sklepów
            await self.db.stores.create_index([("franchise_code", 1)], unique=True)

            # NOWE INDEKSY dla Ustawień i Specjalnych Godzin
            await self.db.storesettings.create_index([("franchise_code", 1)], unique=True)
            await self.db.specialopeninghours.create_index([("franchise_code", 1), ("date", 1)], unique=True)

            # Indeksy dla pozostałych kolekcji
            await self.db.vacations.create_index([("user_id", 1)])
            await self.db.vacations.create_index([("status", 1)])
            await self.db.schedule.create_index([("user_id", 1)])
            await self.db.schedule.create_index([("date", 1)])
            await self.db.availabilities.create_index([("user_id", 1)])
            await self.db.availabilities.create_index([("date", 1)])
            logger.info("Utworzono indeksy w bazie danych.")
        else:
            logger.error("Błąd: Nie można utworzyć indeksów, połączenie z bazą danych nie jest aktywne.")


db_manager = DatabaseManager(settings.MONGODB_URI, settings.MONGODB_DB_NAME)


async def get_db():
    if db_manager.db is None:
        raise HTTPException(status_code=500, detail="Baza danych nie jest połączona.")
    return db_manager.db
