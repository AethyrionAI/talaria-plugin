"""Adapter shell checks that don't need instantiation.

platform_adapter.py imports gateway modules (only importable under the
hermes venv, which is also this suite's runner — see tests/test_tools.py
and the plugin README). Instantiating TalariaPlatformAdapter needs a real
PlatformConfig, so this stays a signature check rather than a behavior
test — the import smoke command covers "does it load", this covers "is
send() actually callable by the gateway's real callers" (I1, coordinator
fix round: the prior send(self, chat_id, text, **kwargs) shape does not
match BasePlatformAdapter's abstract contract, and in-tree callers pass
content= as a keyword).
"""

import inspect

from ..platform_adapter import TalariaPlatformAdapter


def test_send_signature_matches_base_platform_adapter_contract():
    sig = inspect.signature(TalariaPlatformAdapter.send)
    assert list(sig.parameters) == ["self", "chat_id", "content", "reply_to", "metadata"]
