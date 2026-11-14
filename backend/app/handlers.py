from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from .gateway_client import GatewayClient
from .logging import Logger, err_detail
from .repository import Repository

if TYPE_CHECKING:
    from .drafts import BetFlow

UNKNOWN_SENDER_REPLY = "I do not have you on the demo roster yet."


@dataclass(frozen=True)
class Inbound:
    message_id: str
    handle: str
    text: str
    received_at: datetime


def reply_key(message_id: str) -> str:
    return f"reply:{message_id}"


async def handle_inbound(
    msg: Inbound, repo: Repository, gateway: GatewayClient, logger: Logger, flow: "BetFlow"
) -> None:
    """Background handler. Never raises: logs and continues."""
    try:
        logger.debug("handler.text", handle=msg.handle, messageId=msg.message_id, text=msg.text)
        entry = await repo.get_roster_by_handle(msg.handle)
        logger.info("handler.received", handle=msg.handle, messageId=msg.message_id, known=entry is not None)
        if entry:
            await flow.handle(entry, msg.text, msg.message_id)
        else:
            result = await gateway.send_text(msg.handle, UNKNOWN_SENDER_REPLY, reply_key(msg.message_id))
            if not result.ok:
                logger.warn("handler.reply_failed", handle=msg.handle, messageId=msg.message_id, code=result.code)
    except Exception as e:
        logger.error("handler.error", handle=msg.handle, messageId=msg.message_id, detail=err_detail(e))
    try:
        await repo.mark_processed(msg.message_id)
    except Exception as e:
        logger.error("handler.mark_processed_failed", messageId=msg.message_id, detail=err_detail(e))
