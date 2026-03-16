"""
Router /api/feedback — recibe feedback de mensajes del frontend-next.
"""
from __future__ import annotations

from typing import Annotated, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from interfaces.api.auth import get_current_user

router = APIRouter(prefix="/api", tags=["feedback"])


class FeedbackRequest(BaseModel):
    conversation_id: Optional[str] = None
    message_index:   Optional[int] = None
    sentiment:       str            # "positive" | "negative"


@router.post("/feedback", status_code=200)
def submit_feedback(
    body:    FeedbackRequest,
    user_id: Annotated[str, Depends(get_current_user)],
) -> dict:
    """Recibe feedback de mensajes. Actualmente no-op (acepta y descarta)."""
    return {"status": "ok"}
