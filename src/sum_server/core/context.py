"""Per-request contextvars (actor) used by audit writes.

The current actor is set by the ``current_actor`` FastAPI dependency and read by
``write_audit`` so audit attribution does not require explicit threading of the
actor through every service signature.
"""
from __future__ import annotations

import uuid
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Literal

ActorKind = Literal["user", "agent", "system"]

@dataclass(frozen=True, slots=True)
class Actor:
    kind: ActorKind
    id: uuid.UUID | None # None only for the system actor

SYSTEM_ACTOR = Actor(kind="system", id=None)

actor_ctx: ContextVar[Actor | None] = ContextVar("actor_ctx", default=None)

def get_actor() -> Actor:
    actor = actor_ctx.get()
    return actor if actor is not None else SYSTEM_ACTOR

def set_actor(actor: Actor) -> None:
    actor_ctx.set(actor)