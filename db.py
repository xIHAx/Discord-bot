"""
MongoDB access for the bot: file storage (GridFS) plus plain JSON documents.

Uses Motor (the async MongoDB driver) so calls don't block the bot's event
loop, and GridFS so files aren't limited by MongoDB's 16MB document cap.

Call init_db() once, early in startup (see main.py's setup_hook), before
using any of the other functions here.
"""

import os
from typing import Any, Dict, List, Optional

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase, AsyncIOMotorGridFSBucket

_client: Optional[AsyncIOMotorClient] = None
_db: Optional[AsyncIOMotorDatabase] = None
_fs: Optional[AsyncIOMotorGridFSBucket] = None


def init_db() -> AsyncIOMotorClient:
    """Connect to MongoDB using MONGO_URI from the environment (.env)."""
    global _client, _db, _fs

    uri = os.getenv("MONGO_URI")
    if not uri:
        raise RuntimeError("MONGO_URI is not set - add it to your .env file")

    db_name = os.getenv("MONGO_DB_NAME", "discord_bot")

    _client = AsyncIOMotorClient(uri)
    _db = _client[db_name]
    _fs = AsyncIOMotorGridFSBucket(_db)
    return _client


def close_db():
    if _client is not None:
        _client.close()


async def upload_file(local_path: str, filename: Optional[str] = None) -> str:
    """Upload a local file into MongoDB. Returns the new file's id as a string."""
    filename = filename or os.path.basename(local_path)
    with open(local_path, "rb") as f:
        data = f.read()
    file_id = await _fs.upload_from_stream(filename, data)
    return str(file_id)


async def download_file(filename: str, save_path: str) -> bool:
    """
    Fetch the most recently uploaded version of `filename` from MongoDB and
    write it to save_path. Returns False if no file with that name exists.
    """
    cursor = _fs.find({"filename": filename}).sort("uploadDate", -1).limit(1)
    docs = await cursor.to_list(length=1)
    if not docs:
        return False

    stream = await _fs.open_download_stream(docs[0]._id)
    data = await stream.read()

    with open(save_path, "wb") as f:
        f.write(data)
    return True


async def list_files() -> List[str]:
    """Return the filenames of everything currently stored."""
    cursor = _fs.find()
    docs = await cursor.to_list(length=None)
    return [doc.filename for doc in docs]


async def delete_file(filename: str) -> bool:
    """Delete every stored version of `filename`. Returns False if none existed."""
    cursor = _fs.find({"filename": filename})
    docs = await cursor.to_list(length=None)
    if not docs:
        return False
    for doc in docs:
        await _fs.delete(doc._id)
    return True


# --- Plain JSON documents (not files) -------------------------------------
# Use these when you want to store structured data - dicts of strings,
# numbers, etc. - as their own MongoDB documents in a normal collection,
# rather than as a GridFS file.

async def insert_document(collection_name: str, document: Dict[str, Any]) -> str:
    """Insert a dict as a new document in `collection_name`. Returns its _id as a string."""
    result = await _db[collection_name].insert_one(document)
    return str(result.inserted_id)


async def find_documents(collection_name: str, query: Optional[Dict[str, Any]] = None, limit: int = 100) -> List[Dict[str, Any]]:
    """Return up to `limit` documents from `collection_name` matching `query` (default: all)."""
    cursor = _db[collection_name].find(query or {}).limit(limit)
    return await cursor.to_list(length=limit)


async def find_one(collection_name: str, query: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return the first document matching `query`, or None."""
    return await _db[collection_name].find_one(query)


async def update_document(collection_name: str, query: Dict[str, Any], update: Dict[str, Any]) -> None:
    """Set fields on the (first) document matching `query`."""
    await _db[collection_name].update_one(query, {"$set": update})


async def upsert_document(collection_name: str, query: Dict[str, Any], document: Dict[str, Any]) -> None:
    """Replace the fields of the document matching `query` with `document`, creating it if absent."""
    await _db[collection_name].update_one(query, {"$set": document}, upsert=True)


async def ensure_index(collection_name: str, keys) -> None:
    """Create an index if it doesn't already exist. `keys` is whatever create_index() accepts,
    e.g. a list of (field, direction) tuples."""
    await _db[collection_name].create_index(keys)


def iter_documents(collection_name: str, query: Dict[str, Any], batch_size: int = 500):
    """
    Return a cursor over every document matching `query`, fetched from Mongo
    in batches of `batch_size` rather than being loaded all at once. Use this
    instead of find_documents() whenever the result set could be large (e.g.
    "everything due right now" across many users) - iterate it with
    `async for doc in cursor`.
    """
    return _db[collection_name].find(query).batch_size(batch_size)


async def bulk_set(collection_name: str, ids: List[Any], update: Dict[str, Any]) -> None:
    """Set the same fields on many documents (by _id) in a single round trip,
    instead of one update_one() call per document."""
    if not ids:
        return
    await _db[collection_name].update_many({"_id": {"$in": ids}}, {"$set": update})
