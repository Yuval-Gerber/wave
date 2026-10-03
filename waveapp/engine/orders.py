"""Idempotent order identity (SPEC.md §3).

Every order Wave sends carries a client order ID derived from
(position_uuid, action, attempt). On reconnect the engine reconciles broker
state against the DB before doing anything — these IDs are what make an order
recognizable as ours, tie it to its owning PositionActor, and make a re-send
after a network error safe (same id = broker rejects the duplicate).
"""

from __future__ import annotations

import uuid as uuidlib
from dataclasses import dataclass

PREFIX = "wave"


class OrderAction:
    ENTRY = "entry"
    EXIT = "exit"
    SCALE = "scale"
    KILL = "kill"
    STOP = "stop"  # standalone protective stop (auction entries can't bracket)


def new_position_uuid() -> str:
    return uuidlib.uuid4().hex


def make_client_order_id(position_uuid: str, action: str, attempt: int) -> str:
    """`wave-<uuid12>-<action>-<attempt>` — deterministic per (position, action,
    attempt), unique across positions, parseable by the router."""
    return f"{PREFIX}-{position_uuid[:12]}-{action}-{attempt}"


@dataclass(frozen=True)
class ParsedOrderId:
    position_key: str  # first 12 chars of the position uuid
    action: str
    attempt: int


def parse_client_order_id(client_order_id: str) -> ParsedOrderId | None:
    """Parse a Wave client order id; None for foreign/manual orders."""
    parts = client_order_id.split("-")
    if len(parts) != 4 or parts[0] != PREFIX:
        return None
    try:
        return ParsedOrderId(position_key=parts[1], action=parts[2], attempt=int(parts[3]))
    except ValueError:
        return None
