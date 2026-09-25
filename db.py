"""
MongoDB access for the bot, storing plain JSON documents.

Uses Motor (the async MongoDB driver) so calls don't block the bot's event
loop.

Call init_db() once, early in startup (see main.py's setup_hook), before
using any of the other functions here.
"""

import os
from typing import Any, Dict, List, Optional, Tuple

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from pymongo import UpdateOne

_client: Optional[AsyncIOMotorClient] = None
_db: Optional[AsyncIOMotorDatabase] = None


def init_db() -> AsyncIOMotorClient:
    """Connect to MongoDB using MONGO_URI from the environment (.env)."""
    global _client, _db

    uri = os.getenv("MONGO_URI")
    if not uri:
        raise RuntimeError("MONGO_URI is not set - add it to your .env file")

    db_name = os.getenv("MONGO_DB_NAME", "discord_bot")

    _client = AsyncIOMotorClient(uri)
    _db = _client[db_name]
    return _client


def close_db():
    if _client is not None:
        _client.close()


async def insert_document(collection_name: str, document: Dict[str, Any]) -> str:
    """Insert a dict as a new document in `collection_name`. Returns its _id as a string."""
    result = await _db[collection_name].insert_one(document)
    return str(result.inserted_id)


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
    in batches of `batch_size` rather than being loaded all at once, so large
    result sets (e.g. "everything due right now" across many users) don't
    spike memory - iterate it with `async for doc in cursor`.
    """
    return _db[collection_name].find(query).batch_size(batch_size)


async def bulk_set(collection_name: str, ids: List[Any], update: Dict[str, Any]) -> None:
    """Set the same fields on many documents (by _id) in a single round trip,
    instead of one update_one() call per document."""
    if not ids:
        return
    await _db[collection_name].update_many({"_id": {"$in": ids}}, {"$set": update})


async def bulk_update(collection_name: str, updates: List[Tuple[Any, Dict[str, Any]]]) -> None:
    """Set (possibly different) fields on many documents in a single round
    trip. `updates` is a list of (_id, fields_to_set) pairs."""
    if not updates:
        return
    await _db[collection_name].bulk_write(
        [UpdateOne({"_id": _id}, {"$set": fields}) for _id, fields in updates],
        ordered=False,
    )
