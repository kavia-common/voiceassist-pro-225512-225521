# VoiceAssist Core Backend API

FastAPI backend for orchestration, call lifecycle, prompt/config management, analytics, and real-time events via WebSocket.

Ports and URLs
- REST base: http://localhost:3001
- OpenAPI docs: http://localhost:3001/docs
- Health: http://localhost:3001/health
- WebSocket: ws://localhost:3001/ws/events

Environment configuration
- MONGODB_URL: MongoDB connection string (e.g., mongodb+srv://user:pwd@cluster/test?retryWrites=true&w=majority)
- MONGODB_DB: Database name (e.g., voiceassist)

Fallback behavior
If MONGODB_URL or MONGODB_DB is not set, the service will attempt to parse:
  /home/kavia/workspace/code-generation/voiceassist-pro-225512-225523/data_store/db_connection.txt
This file should include a single line starting with:
  mongosh <connection-string>
If the URL contains a path segment (e.g., mongodb://.../voiceassist), that path will be used as the db name. If the db name cannot be derived, it defaults to "voiceassist".

CORS
- Allowed origins: http://localhost:3000 (Angular admin_dashboard_frontend) and PREVIEW_ORIGIN if set.

Start order (local dev)
1) Start MongoDB (data_store container or local instance) and ensure db_connection.txt exists if not using env vars.
2) Start backend (this container) on port 3001:
   - Install deps: pip install -r requirements.txt
   - Run: uvicorn src.api.main:app --host 0.0.0.0 --port 3001 --reload
3) Start frontend (admin_dashboard_frontend) on port 3000 with environment pointing to:
   - REST: http://localhost:3001
   - WebSocket: ws://localhost:3001/ws/events

Validation checklist (E2E)
- Backend health: curl http://localhost:3001/health -> { "status": "ok", ... }
- DB health (implicit): hitting list endpoints should return arrays; failures indicate DB misconfig.
- Seeded data: /users and /calls should return seeded users/calls if data_store seeding ran.
- Live updates: 
  - Open frontend Live Calls page (should open a WebSocket to ws://localhost:3001/ws/events).
  - In another terminal, update a call status:
    curl -X PATCH http://localhost:3001/calls/<call_id> -H "Content-Type: application/json" -d '{"status":"live"}'
  - The Live Calls page should update in real-time without refresh.

Notes
- OpenAPI schema can be (re)generated via: python -m src.api.generate_openapi
- For non-local previews, set PREVIEW_ORIGIN in backend and update frontend env to match the backend origin.
