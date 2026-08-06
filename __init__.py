"""Talaria phone bridge plugin for Hermes.

Phase 1 of the four-phase arc (Talaria-27 OPEN_ITEMS #251): tools + admin.
2A adds the webhook-mode platform adapter on the gateway's existing HTTP
listener; later slices migrate remote turns to the runs plane and
decommission the legacy relay/connector sidecars.

The platform import stays inside ``register`` (not at module level) so
the tools/CLI half never breaks if gateway modules are unavailable in a
bare CLI context — tools + CLI must survive even when platform
registration cannot proceed.
"""

from . import admin, tools


def register(ctx) -> None:
    tools.register_tools(ctx)
    admin.register_cli(ctx)
    try:
        from .platform_adapter import TalariaPlatformAdapter
        ctx.register_platform(
            name="talaria",
            label="Talaria",
            adapter_factory=lambda cfg: TalariaPlatformAdapter(cfg),
            check_fn=lambda: True,
        )
    except Exception as exc:  # pragma: no cover — CLI-context guard
        print(f"[talaria] platform registration skipped: {exc}")
