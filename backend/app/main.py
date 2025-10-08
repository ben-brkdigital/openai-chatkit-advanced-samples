"""FastAPI entrypoint wiring the ChatKit server and REST endpoints."""
from __future__ import annotations

import os
from fastapi.middleware.cors import CORSMiddleware
from typing import Any

from chatkit.server import StreamingResult, ChatKitServer
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import Response, StreamingResponse
from starlette.responses import JSONResponse

from .chat import create_chatkit_server
from .facts import fact_store

app = FastAPI(title="ChatKit API")

# --- CORS (reads comma-separated origins from env; "*" allows all) ---
origins_env = os.getenv("CORS_ORIGINS", "").strip()
if origins_env in ("", "*"):
    allow_origins = ["*"]
else:
    allow_origins = [o.strip() for o in origins_env.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# --------------------------------------------------------------------

_chatkit_server: ChatKitServer | None = create_chatkit_server()

def get_chatkit_server() -> ChatKitServer:
    if _chatkit_server is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "ChatKit dependencies are missing. Install the ChatKit Python "
                "package to enable the conversational endpoint."
            ),
        )
    return _chatkit_server

@app.post("/chatkit")
async def chatkit_endpoint(
    request: Request, server: ChatKitServer = Depends(get_chatkit_server)
) -> Response:
    payload = await request.body()
    result = await server.process(payload, {"request": request})
    if isinstance(result, StreamingResult):
        return StreamingResponse(result, media_type="text/event-stream")
    if hasattr(result, "json"):
        return Response(content=result.json, media_type="application/json")
    return JSONResponse(result)

@app.get("/facts")
async def list_facts() -> dict[str, Any]:
    facts = await fact_store.list_saved()
    return {"facts": [fact.as_dict() for fact in facts]}

@app.post("/facts/{fact_id}/save")
async def save_fact(fact_id: str) -> dict[str, Any]:
    fact = await fact_store.mark_saved(fact_id)
    if fact is None:
        raise HTTPException(status_code=404, detail="Fact not found")
    return {"fact": fact.as_dict()}

@app.post("/facts/{fact_id}/discard")
async def discard_fact(fact_id: str) -> dict[str, Any]:
    fact = await fact_store.discard(fact_id)
    if fact is None:
        raise HTTPException(status_code=404, detail="Fact not found")
    return {"fact": fact.as_dict()}

@app.get("/health")
async def health_check() -> dict[str, str]:
    return {"status": "healthy"}
