from __future__ import annotations

from datetime import date as Date
from typing import Literal

from pydantic import BaseModel, ConfigDict

CLASSIFICATIONS = ("bet_proposal", "reply_to_pending", "other")
# Draft fields kept by the agent. Only `team` is required; stake/side are chosen later on the betting side.
DRAFT_FIELDS = ("team", "opponent", "date")


def _nullable(schema: dict) -> dict:
    return {"anyOf": [schema, {"type": "null"}]}


# Strict tool: every key required, values nullable. The date window is checked in code.
PROPOSE_BET_TOOL: dict = {
    "name": "propose_bet",
    "description": (
        "Record which sports game the user's message wants a bet on. Fill only what the message states or "
        "clearly implies; use null for anything not stated. Always classify the message."
    ),
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "classification": {
                "type": "string",
                "enum": list(CLASSIFICATIONS),
                "description": (
                    "bet_proposal: the message asks to create a bet (even with no game named). "
                    "reply_to_pending: the message answers the pending question about a bet request already in "
                    "progress. other: anything else."
                ),
            },
            "team": _nullable({"type": "string", "description": "First team named for the game."}),
            "opponent": _nullable({"type": "string", "description": "The other team, if named."}),
            "date": _nullable({"type": "string", "format": "date", "description": "Game date as YYYY-MM-DD, if stated."}),
        },
        "required": ["classification", "team", "opponent", "date"],
        "additionalProperties": False,
    },
}


class Extraction(BaseModel):
    """Validated tool input. Code re-validates even though `strict` guarantees the schema."""

    model_config = ConfigDict(extra="ignore")

    classification: Literal["bet_proposal", "reply_to_pending", "other"]
    team: str | None = None
    opponent: str | None = None
    date: Date | None = None

    def fields(self) -> dict:
        """Draft fields only (no classification), JSON-friendly, blanks as None."""
        out: dict = {}
        for k in DRAFT_FIELDS:
            v = getattr(self, k)
            if isinstance(v, str):
                v = v.strip() or None
            if k == "date" and v is not None:
                v = v.isoformat()
            out[k] = v
        return out

    def present(self) -> dict[str, bool]:
        return {k: v is not None for k, v in self.fields().items()}
