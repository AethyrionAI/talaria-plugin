"""Webhook-mode platform adapter (spec §1.1).

Thin shell: BasePlatformAdapter obligations + delegation to
EnvelopeService. No socket — inbound rides the api_server's existing
POST /api/platforms/talaria/events (verified route, api_server.py
~:1808). Platform("talaria") resolves via the enum's _missing_()
pseudo-member exactly as plugins/platforms/google_chat does.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional, Tuple

from gateway.config import Platform
from gateway.platforms.base import BasePlatformAdapter, SendResult

from . import outbox, store
from .envelope import EnvelopeService
from .transport import HUB

logger = logging.getLogger("talaria")


def _api_key() -> str:
    return os.environ.get("API_SERVER_KEY", "")


class TalariaPlatformAdapter(BasePlatformAdapter):
    def __init__(self, config):
        super().__init__(config, Platform("talaria"))
        self._envelope = EnvelopeService(
            api_key_provider=_api_key,
            hub=HUB,
            store_mod=store,
            outbox_mod=outbox,
        )
        # #263-E: this is the EARLY binder — HUB was frozen at module import
        # (line 20) and the EnvelopeService holds it for life. Compare this
        # id against tools.py's check_fn stamp; a mismatch is the #263(a)
        # split hub, and the two are otherwise indistinguishable in the log.
        logger.info("adapter attach hub=%s", id(HUB))

    # -- BasePlatformAdapter obligations ---------------------------------
    async def connect(self, *, is_reconnect: bool = False) -> bool:
        return True  # webhook mode: being registered IS being connected

    async def disconnect(self) -> None:
        return None

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        item = outbox.append(content, meta={"chat_id": chat_id})
        HUB.wake()
        return SendResult(success=True, message_id=item["id"])

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {"name": "Talaria", "type": "device"}

    # -- HTTP events (the whole transport) --------------------------------
    def verify_http_event_request(self, auth_header: str) -> Tuple[bool, str]:
        return self._envelope.verify(auth_header)

    async def dispatch_http_event(self, envelope: Dict[str, Any]) -> Dict[str, Any]:
        return await self._envelope.dispatch(envelope)
