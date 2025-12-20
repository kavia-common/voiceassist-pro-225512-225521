# Backend Smoke Tests

This suite provides minimal end-to-end smoke validation for the FastAPI backend.

What is covered:
- GET /health returns 200 with expected shape.
- Database connectivity checks against MongoDB (requires MONGODB_URL and MONGODB_DB).
- Calls API minimal CRUD: create a call, list calls, and verify presence (requires DB).
- WebSocket /ws/events connects and receives initial system \"connected\" message and an \"ack\" on ping.
  This WS test runs even without a database by bypassing Mongo initialization in test using monkeypatch.

Running tests:
- Activate virtualenv and install dependencies:
  - python -m venv venv && . venv/bin/activate
  - pip install -r requirements.txt
- Run pytest (non-interactive/CI mode):
  - pytest -q

Environment:
- To enable DB-backed tests, export:
  - MONGODB_URL
  - MONGODB_DB
- If these are not set, DB tests will be skipped and reported as such.
