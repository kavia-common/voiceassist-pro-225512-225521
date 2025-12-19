import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import Body, FastAPI, HTTPException, Path, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr, Field
from starlette.websockets import WebSocketState

# PUBLIC_INTERFACE
def get_env(name: str, default: Optional[str] = None) -> Optional[str]:
    """Get environment variable with optional default."""
    return os.getenv(name, default)


# MongoDB (Motor) integration
try:
    from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
except Exception:  # pragma: no cover - dependency resolved by requirements.txt
    AsyncIOMotorClient = None  # type: ignore
    AsyncIOMotorDatabase = None  # type: ignore


logger = logging.getLogger("core_backend_api")
logging.basicConfig(level=logging.INFO)


# Centralized database client holder
class MongoClientManager:
    """Singleton-like manager for Motor client and db reference."""
    _client: Optional[AsyncIOMotorClient] = None
    _db: Optional["AsyncIOMotorDatabase"] = None

    # PUBLIC_INTERFACE
    @classmethod
    def init(cls) -> None:
        """Initialize the Mongo client and database using environment variables."""
        mongo_url = get_env("MONGODB_URL")
        mongo_db = get_env("MONGODB_DB")

        # Allow falling back to db_connection.txt if env is not set (per domain instruction)
        if not mongo_url or not mongo_db:
            try:
                # Attempt to read connection info from adjacent data_store repo if available
                # Format expected in db_connection.txt: "mongosh <connection-string>"
                base_dir = "/home/kavia/workspace/code-generation/voiceassist-pro-225512-225523/data_store"
                connection_path = os.path.join(base_dir, "db_connection.txt")
                if os.path.exists(connection_path):
                    with open(connection_path, "r") as f:
                        content = f.read().strip()
                    # naive parse to get the mongodb url
                    if content.startswith("mongosh"):
                        parts = content.split()
                        if len(parts) >= 2:
                            mongo_url = parts[1]
                            # If DB name is not part of URL, try to read from env or default
                            # We avoid guessing DB name: require MONGODB_DB if not encoded in URL path
                            # But per requirement, default to settings from db_connection.txt: if path contains a db at end, use it.
                            try:
                                from urllib.parse import urlparse
                                parsed = urlparse(mongo_url)
                                if parsed.path and parsed.path != "/":
                                    mongo_db = parsed.path.strip("/").split("/")[0]
                            except Exception:
                                pass
                # If still missing, set a safe default db name
                if not mongo_db:
                    mongo_db = "voiceassist"
            except Exception as e:
                logger.warning(f"Failed to parse db_connection.txt: {e}")

        if not mongo_url or not mongo_db:
            raise RuntimeError(
                "MongoDB configuration missing. Ensure MONGODB_URL and MONGODB_DB are set "
                "or db_connection.txt is available with a 'mongosh <connection-string>' line."
            )

        # Create Motor client (async)
        cls._client = AsyncIOMotorClient(mongo_url)
        cls._db = cls._client[mongo_db]
        logger.info("Mongo client initialized.")

    # PUBLIC_INTERFACE
    @classmethod
    def db(cls) -> "AsyncIOMotorDatabase":
        """Return the active Motor database, initializing if needed."""
        if cls._db is None:
            cls.init()
        assert cls._db is not None
        return cls._db

    # PUBLIC_INTERFACE
    @classmethod
    def client(cls) -> "AsyncIOMotorClient":
        """Return the active Motor client, initializing if needed."""
        if cls._client is None:
            cls.init()
        assert cls._client is not None
        return cls._client


# Pydantic Models
class HealthResponse(BaseModel):
    status: str = Field(..., description="Overall service status")
    timestamp: datetime = Field(..., description="Server time in UTC")


class UserCreate(BaseModel):
    email: EmailStr = Field(..., description="User email address")
    name: str = Field(..., description="Full name of the user")
    role: str = Field("agent", description="Role of the user (e.g., admin, supervisor, agent)")
    active: bool = Field(True, description="Whether the user account is active")


class User(BaseModel):
    id: str = Field(..., description="User ID (stringified ObjectId)")
    email: EmailStr
    name: str
    role: str
    active: bool
    created_at: datetime


class CallCreate(BaseModel):
    user_id: str = Field(..., description="User ID initiating the call")
    customer_number: str = Field(..., description="Customer phone number")
    direction: str = Field(..., description="inbound or outbound")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Arbitrary call metadata")


class Call(BaseModel):
    id: str
    user_id: str
    customer_number: str
    direction: str
    status: str
    started_at: datetime
    ended_at: Optional[datetime] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class CallStatusPatch(BaseModel):
    status: str = Field(..., description="New status for the call")
    ended_at: Optional[datetime] = Field(None, description="Optional end time if call ended")
    metadata: Optional[Dict[str, Any]] = Field(None, description="Optional metadata patch")


class PromptConfig(BaseModel):
    """Represents current prompt templates/settings (singleton doc)."""
    id: str = Field(..., description="Identifier for the config (e.g., 'current')")
    templates: Dict[str, str] = Field(default_factory=dict, description="Prompt templates by key")
    updated_at: datetime


class PromptConfigUpdate(BaseModel):
    templates: Dict[str, str] = Field(default_factory=dict, description="Prompt templates by key")


class AppConfig(BaseModel):
    """Represents global application config (singleton doc)."""
    id: str = Field(..., description="Identifier for the config (e.g., 'global')")
    flags: Dict[str, Any] = Field(default_factory=dict, description="Feature flags and settings")
    integrations: Dict[str, Any] = Field(default_factory=dict, description="Integration settings")
    updated_at: datetime


class AppConfigUpdate(BaseModel):
    flags: Dict[str, Any] = Field(default_factory=dict)
    integrations: Dict[str, Any] = Field(default_factory=dict)


class AnalyticsSnapshot(BaseModel):
    total_users: int
    active_calls: int
    total_calls_today: int
    avg_call_duration_sec: float
    generated_at: datetime


# WebSocket Connection Manager
class ConnectionManager:
    """Manages WebSocket clients and broadcasting of events."""
    def __init__(self) -> None:
        self.active_connections: List[WebSocket] = []
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self.active_connections.append(websocket)
        await self._send_safe(
            websocket,
            {"type": "system", "event": "connected", "timestamp": datetime.now(timezone.utc).isoformat()},
        )

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._lock:
            if websocket in self.active_connections:
                self.active_connections.remove(websocket)

    async def _send_safe(self, websocket: WebSocket, message: Dict[str, Any]) -> None:
        try:
            if websocket.application_state == WebSocketState.CONNECTED:
                await websocket.send_text(json.dumps(message))
        except Exception as e:
            logger.debug(f"WebSocket send failed: {e}")

    # PUBLIC_INTERFACE
    async def broadcast(self, message: Dict[str, Any]) -> None:
        """Broadcast a JSON message to all connected clients."""
        async with self._lock:
            websockets = list(self.active_connections)
        for ws in websockets:
            await self._send_safe(ws, message)


manager = ConnectionManager()

# FastAPI application with OpenAPI metadata and tags
app = FastAPI(
    title="VoiceAssist Core Backend API",
    description="Backend for orchestration, call flow logic, conversation processing, and integrations. Provides REST endpoints and WebSocket for real-time updates.",
    version="0.1.0",
    openapi_tags=[
        {"name": "system", "description": "System and health endpoints"},
        {"name": "users", "description": "User management"},
        {"name": "calls", "description": "Call lifecycle management"},
        {"name": "prompts", "description": "Prompt templates and versions"},
        {"name": "configs", "description": "Global app configuration"},
        {"name": "analytics", "description": "Analytics and metrics"},
        {"name": "events", "description": "WebSocket events for real-time updates"},
    ],
)

# CORS: allow localhost:3000 and preview origin if present
preview_origin = get_env("PREVIEW_ORIGIN")
allow_origins = ["http://localhost:3000"]
if preview_origin:
    allow_origins.append(preview_origin)

app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Utilities for Mongo ObjectId conversion
from bson import ObjectId  # type: ignore


def oid_str(oid: Any) -> str:
    try:
        return str(oid)
    except Exception:
        return str(oid)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


# PUBLIC_INTERFACE
@app.on_event("startup")
async def on_startup() -> None:
    """Initialize Mongo client and create indexes if needed."""
    MongoClientManager.init()
    db = MongoClientManager.db()

    # Ensure indexes (idempotent)
    await db["users"].create_index("email", unique=True)
    await db["calls"].create_index([("status", 1), ("started_at", -1)])
    await db["calls"].create_index("user_id")
    await db["prompts"].create_index("_id", unique=True)
    await db["configs"].create_index("_id", unique=True)


# PUBLIC_INTERFACE
@app.get("/health", response_model=HealthResponse, tags=["system"], summary="Health Check")
async def health_check() -> HealthResponse:
    """Return basic health status and server time."""
    return HealthResponse(status="ok", timestamp=now_utc())


# USERS
# PUBLIC_INTERFACE
@app.get("/users", response_model=List[User], tags=["users"], summary="List users")
async def list_users() -> List[User]:
    """List all users in the system."""
    db = MongoClientManager.db()
    users: List[User] = []
    async for doc in db["users"].find({}).sort("created_at", -1):
        users.append(
            User(
                id=oid_str(doc.get("_id")),
                email=doc["email"],
                name=doc["name"],
                role=doc.get("role", "agent"),
                active=bool(doc.get("active", True)),
                created_at=doc.get("created_at") or now_utc(),
            )
        )
    return users


# PUBLIC_INTERFACE
@app.post("/users", response_model=User, tags=["users"], summary="Create user")
async def create_user(payload: UserCreate = Body(...)) -> User:
    """Create a new user with unique email."""
    db = MongoClientManager.db()
    doc = {
        "email": payload.email,
        "name": payload.name,
        "role": payload.role,
        "active": payload.active,
        "created_at": now_utc(),
    }
    try:
        res = await db["users"].insert_one(doc)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Unable to create user: {e}")
    created = await db["users"].find_one({"_id": res.inserted_id})
    assert created is not None
    return User(
        id=oid_str(created["_id"]),
        email=created["email"],
        name=created["name"],
        role=created.get("role", "agent"),
        active=bool(created.get("active", True)),
        created_at=created.get("created_at") or now_utc(),
    )


# CALLS
# PUBLIC_INTERFACE
@app.get("/calls", response_model=List[Call], tags=["calls"], summary="List calls")
async def list_calls(
    status: Optional[str] = Query(None, description="Filter by status"),
    user_id: Optional[str] = Query(None, description="Filter by user_id"),
    limit: int = Query(50, ge=1, le=200, description="Max number of calls to return"),
) -> List[Call]:
    """List calls with optional filters."""
    db = MongoClientManager.db()
    q: Dict[str, Any] = {}
    if status:
        q["status"] = status
    if user_id:
        q["user_id"] = user_id

    calls: List[Call] = []
    cursor = db["calls"].find(q).sort("started_at", -1).limit(limit)
    async for doc in cursor:
        calls.append(
            Call(
                id=oid_str(doc["_id"]),
                user_id=doc["user_id"],
                customer_number=doc["customer_number"],
                direction=doc["direction"],
                status=doc["status"],
                started_at=doc.get("started_at") or now_utc(),
                ended_at=doc.get("ended_at"),
                metadata=doc.get("metadata", {}),
            )
        )
    return calls


# PUBLIC_INTERFACE
@app.post("/calls", response_model=Call, tags=["calls"], summary="Create call")
async def create_call(payload: CallCreate = Body(...)) -> Call:
    """Create a new call in 'initiated' status and broadcast event."""
    db = MongoClientManager.db()
    doc = {
        "user_id": payload.user_id,
        "customer_number": payload.customer_number,
        "direction": payload.direction,
        "status": "initiated",
        "started_at": now_utc(),
        "metadata": payload.metadata or {},
    }
    res = await db["calls"].insert_one(doc)
    created = await db["calls"].find_one({"_id": res.inserted_id})
    assert created is not None

    call = Call(
        id=oid_str(created["_id"]),
        user_id=created["user_id"],
        customer_number=created["customer_number"],
        direction=created["direction"],
        status=created["status"],
        started_at=created.get("started_at") or now_utc(),
        ended_at=created.get("ended_at"),
        metadata=created.get("metadata", {}),
    )

    await manager.broadcast(
        {
            "type": "call",
            "event": "created",
            "call": call.model_dump(mode="json"),
            "timestamp": now_utc().isoformat(),
        }
    )
    return call


# PUBLIC_INTERFACE
@app.patch(
    "/calls/{call_id}",
    response_model=Call,
    tags=["calls"],
    summary="Update call status",
)
async def update_call_status(
    call_id: str = Path(..., description="Call ID"),
    payload: CallStatusPatch = Body(...),
) -> Call:
    """Update call status and optionally set ended_at and metadata. Broadcast status updates to WebSocket."""
    db = MongoClientManager.db()
    updates: Dict[str, Any] = {"status": payload.status}
    if payload.ended_at is not None:
        updates["ended_at"] = payload.ended_at
    if payload.metadata is not None:
        # Merge metadata shallowly
        updates["metadata"] = payload.metadata

    result = await db["calls"].find_one_and_update(
        {"_id": ObjectId(call_id)} if ObjectId.is_valid(call_id) else {"_id": call_id},
        {"$set": updates},
        return_document=True,  # type: ignore
    )

    if not result:
        raise HTTPException(status_code=404, detail="Call not found")

    call = Call(
        id=oid_str(result["_id"]),
        user_id=result["user_id"],
        customer_number=result["customer_number"],
        direction=result["direction"],
        status=result["status"],
        started_at=result.get("started_at") or now_utc(),
        ended_at=result.get("ended_at"),
        metadata=result.get("metadata", {}),
    )

    await manager.broadcast(
        {
            "type": "call",
            "event": "status_updated",
            "call": call.model_dump(mode="json"),
            "timestamp": now_utc().isoformat(),
        }
    )

    return call


# PROMPTS (singleton doc with _id="current")
# PUBLIC_INTERFACE
@app.get("/prompts", response_model=PromptConfig, tags=["prompts"], summary="Get current prompt configuration")
async def get_prompts() -> PromptConfig:
    """Get current prompt templates/settings (singleton: _id='current')."""
    db = MongoClientManager.db()
    doc = await db["prompts"].find_one({"_id": "current"}) or {}
    return PromptConfig(
        id="current",
        templates=doc.get("templates", {}),
        updated_at=doc.get("updated_at") or now_utc(),
    )


# PUBLIC_INTERFACE
@app.put("/prompts", response_model=PromptConfig, tags=["prompts"], summary="Update current prompt configuration")
async def put_prompts(payload: PromptConfigUpdate = Body(...)) -> PromptConfig:
    """Replace current prompt templates/settings and update timestamp."""
    db = MongoClientManager.db()
    replacement = {
        "_id": "current",
        "templates": payload.templates,
        "updated_at": now_utc(),
    }
    await db["prompts"].update_one({"_id": "current"}, {"$set": replacement}, upsert=True)
    doc = await db["prompts"].find_one({"_id": "current"})
    assert doc is not None
    return PromptConfig(id="current", templates=doc.get("templates", {}), updated_at=doc.get("updated_at") or now_utc())


# CONFIGS (singleton doc with _id="global")
# PUBLIC_INTERFACE
@app.get("/configs", response_model=AppConfig, tags=["configs"], summary="Get app configuration")
async def get_configs() -> AppConfig:
    """Get global application configuration (singleton: _id='global')."""
    db = MongoClientManager.db()
    doc = await db["configs"].find_one({"_id": "global"}) or {}
    return AppConfig(
        id="global",
        flags=doc.get("flags", {}),
        integrations=doc.get("integrations", {}),
        updated_at=doc.get("updated_at") or now_utc(),
    )


# PUBLIC_INTERFACE
@app.put("/configs", response_model=AppConfig, tags=["configs"], summary="Update app configuration")
async def put_configs(payload: AppConfigUpdate = Body(...)) -> AppConfig:
    """Replace global application configuration and update timestamp."""
    db = MongoClientManager.db()
    replacement = {
        "_id": "global",
        "flags": payload.flags,
        "integrations": payload.integrations,
        "updated_at": now_utc(),
    }
    await db["configs"].update_one({"_id": "global"}, {"$set": replacement}, upsert=True)
    doc = await db["configs"].find_one({"_id": "global"})
    assert doc is not None
    return AppConfig(
        id="global",
        flags=doc.get("flags", {}),
        integrations=doc.get("integrations", {}),
        updated_at=doc.get("updated_at") or now_utc(),
    )


# ANALYTICS
# PUBLIC_INTERFACE
@app.get(
    "/analytics/snapshot",
    response_model=AnalyticsSnapshot,
    tags=["analytics"],
    summary="Get analytics snapshot",
    description="Returns high-level analytics including counts and averages.",
)
async def analytics_snapshot() -> AnalyticsSnapshot:
    """Compute a simple analytics snapshot."""
    db = MongoClientManager.db()
    total_users = await db["users"].count_documents({})
    active_calls = await db["calls"].count_documents({"status": {"$in": ["initiated", "ringing", "live"]}})

    # Today's calls and average duration
    today = datetime.now(timezone.utc).date()
    start_of_day = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)
    total_calls_today = await db["calls"].count_documents({"started_at": {"$gte": start_of_day}})

    durations: List[float] = []
    async for doc in db["calls"].find({"ended_at": {"$ne": None}, "started_at": {"$gte": start_of_day}}):
        try:
            s: datetime = doc.get("started_at") or now_utc()
            e: datetime = doc.get("ended_at") or s
            durations.append(max(0.0, (e - s).total_seconds()))
        except Exception:
            continue
    avg_call_duration = (sum(durations) / len(durations)) if durations else 0.0

    return AnalyticsSnapshot(
        total_users=total_users,
        active_calls=active_calls,
        total_calls_today=total_calls_today,
        avg_call_duration_sec=avg_call_duration,
        generated_at=now_utc(),
    )


# WEBSOCKET for events
# PUBLIC_INTERFACE
@app.websocket("/ws/events")
async def ws_events(websocket: WebSocket) -> None:
    """
    WebSocket endpoint for broadcasting call status updates and system events.

    Usage:
    - Connect to ws://<host>:3001/ws/events
    - Receive JSON messages:
      { "type": "system" | "call", "event": "<event_name>", ... }

    Events:
    - system.connected
    - call.created
    - call.status_updated
    """
    await manager.connect(websocket)
    try:
        while True:
            # Keep the connection alive; echo pings if needed
            msg = await websocket.receive_text()
            # Optionally process client pings/commands; here we no-op and send ack
            await manager._send_safe(websocket, {"type": "system", "event": "ack", "message": msg})
    except WebSocketDisconnect:
        await manager.disconnect(websocket)
    except Exception as e:
        logger.debug(f"WebSocket error: {e}")
        await manager.disconnect(websocket)


# Root helper endpoint for WebSocket usage (docs)
# PUBLIC_INTERFACE
@app.get(
    "/docs/websocket",
    tags=["events"],
    summary="WebSocket usage",
    description="Explains how to use the /ws/events WebSocket endpoint.",
)
async def websocket_usage() -> Dict[str, Any]:
    """Return usage instructions for WebSocket clients."""
    return {
        "endpoint": "/ws/events",
        "operation_id": "events_ws",
        "summary": "Subscribe to call status updates and system events.",
        "notes": [
            "Connect from frontend (Angular) to receive real-time updates.",
            "Messages are JSON encoded and may include 'type', 'event', and additional fields.",
        ],
        "examples": {
            "connect": "new WebSocket('ws://localhost:3001/ws/events')",
            "message": {"type": "call", "event": "status_updated", "call": {"id": "..."}, "timestamp": "..."},
        },
    }
