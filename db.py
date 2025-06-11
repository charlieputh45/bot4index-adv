from motor.motor_asyncio import AsyncIOMotorClient
from config import MONGO_URI

# MongoDB setup with Motor (async)
mongo = AsyncIOMotorClient(MONGO_URI)
db = mongo["bot4index"]
files_col = db["files"]
n_files_col = db["n_files"]
tokens_col = db["tokens"]
auth_users_col = db["auth_users"]
allowed_channels_col = db["allowed_channels"]
users_col = db["users"]
