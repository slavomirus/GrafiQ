import asyncio
from motor.motor_asyncio import AsyncIOMotorClient
import os
from dotenv import load_dotenv

# Load env variables
load_dotenv(".env")

async def list_last_users():
    uri = os.getenv("MONGODB_URI")
    client = AsyncIOMotorClient(uri)
    db = client[os.getenv("MONGODB_DB_NAME")]
    
    print("Connecting to DB...")
    cursor = db.users.find().sort("created_at", -1).limit(5)
    
    print("Last 5 users:")
    async for user in cursor:
        print(f"ID: {user.get('_id')}, Email: {user.get('email')}, Status: {user.get('status')}, Created: {user.get('created_at')}")

if __name__ == "__main__":
    asyncio.run(list_last_users())
