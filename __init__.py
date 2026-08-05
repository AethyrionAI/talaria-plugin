"""Talaria phone bridge plugin for Hermes.

Phase 1 of the four-phase arc (Talaria-27 OPEN_ITEMS #251): tools + admin.
Phase 2 adds the webhook-mode platform adapter on the gateway's existing
HTTP listener; Phase 3 migrates remote turns to the runs plane; Phase 4
decommissions the legacy relay/connector sidecars.
"""

from . import admin, tools


def register(ctx) -> None:
    tools.register_tools(ctx)
    admin.register_cli(ctx)
