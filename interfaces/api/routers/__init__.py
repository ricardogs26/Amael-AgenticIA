"""
interfaces.api.routers — Routers FastAPI de Amael-AgenticIA.

  chat          → /api/chat
  conversations → /api/conversations
  identity      → /api/identity
  planner       → /api/planner
  sre           → /api/sre
"""
from interfaces.api.routers.chat          import router as chat_router
from interfaces.api.routers.conversations import router as conversations_router
from interfaces.api.routers.identity      import router as identity_router
from interfaces.api.routers.planner       import router as planner_router
from interfaces.api.routers.sre           import router as sre_router

__all__ = [
    "chat_router",
    "conversations_router",
    "identity_router",
    "planner_router",
    "sre_router",
]
