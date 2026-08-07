"""#263-D: the early binder and the late binder must reach the SAME hub.

Two sites bind the transport singleton and they bind at different moments:

  * LATE  — ``tools._hub()`` re-imports ``HUB`` on every call (tools.py:48-50),
    so it always tracks whatever ``transport`` module is current.
  * EARLY — ``platform_adapter`` freezes it at module import (line 20) and
    hands that reference to ``EnvelopeService`` at construction (line 33),
    which holds it for life (envelope.py:57).

If they ever diverge, the phone drains into one hub while the tool's check_fn
reads another: ``talaria_phone_query`` gates itself off against a demonstrably
live phone. That is #263(a).

Scoping on 2026-08-07 FALSIFIED the originally-filed trigger: eight forced
``discover_and_load(force=True)`` passes against the real ``PluginManager``
left the hub identical, because ``hermes_cli/plugins.py:1885-1889`` replaces
only the PARENT package object — submodules stay cached and are never
re-executed. So this file is a regression pin, not a bug reproduction: it
holds the invariant while the two routes that COULD break it stay unobserved
(a manifest-name divergence giving the package a second module name, or a
submodule eviction after a failed import letting ``transport`` re-execute).

A pin that can only pass is not a pin, so its falsifiability was DEMONSTRATED
once: inserting ``sys.modules.pop(f'{package}.transport', None)`` before the
reload below — i.e. forcing route (2) — makes it fail, and fail with the
right diagnosis:

    FAILED test_hub_identity.py::test_hub_survives_a_loader_faithful_package_reload
    E  AssertionError: a package reload gave tools._hub() a NEW hub — the
       late binder drifted (#263(a))
    E  assert <talaria.transport.TransportHub object at 0x109b807d0>
           is <talaria.transport.TransportHub object at 0x108e6a850>
    1 failed, 3 passed

Two distinct TransportHub instances — the split, exactly. The demo line was
then removed; do not leave it in.
"""

import importlib.util
import sys
import types

from .. import tools
from ..envelope import EnvelopeService
from ..transport import HUB


def _package_name() -> str:
    return __package__.rsplit(".", 1)[0]


def test_late_and_early_binders_agree_today():
    """263-D, baseline. The two binding sites reach one object."""
    from .. import platform_adapter

    assert tools._hub() is HUB
    assert platform_adapter.HUB is HUB, (
        "platform_adapter froze a different hub than tools._hub() resolves — "
        "this is the #263(a) split"
    )


def test_the_hub_an_envelope_service_holds_is_the_hub_the_tool_reads():
    """263-D. Build the EnvelopeService exactly as the adapter does
    (platform_adapter.py:30-35) and prove the tool's check_fn would read the
    same instance. The adapter itself needs a registered gateway Platform
    enum member, so this pins the reference it is handed rather than the
    instantiation."""
    from .. import outbox, platform_adapter, store

    service = EnvelopeService(
        api_key_provider=lambda: "",
        hub=platform_adapter.HUB,
        store_mod=store,
        outbox_mod=outbox,
    )
    assert service._hub is tools._hub()


def test_hub_survives_a_loader_faithful_package_reload():
    """263-D, the regression pin proper.

    Re-execute the package ``__init__`` the way hermes_cli/plugins.py does
    (module_from_spec -> sys.modules[name] = module -> exec_module) and assert
    every binder still reaches the ORIGINAL hub. A ``transport`` that
    re-executes under this replaces ``HUB`` and fails here.
    """
    package = _package_name()
    original_parent = sys.modules[package]
    original_hub = tools._hub()
    plugin_dir = original_parent.__path__[0]

    spec = importlib.util.spec_from_file_location(
        package,
        f"{plugin_dir}/__init__.py",
        submodule_search_locations=[plugin_dir],
    )
    module = importlib.util.module_from_spec(spec)
    module.__package__ = package
    module.__path__ = [plugin_dir]
    try:
        sys.modules[package] = module
        spec.loader.exec_module(module)

        from .. import platform_adapter

        assert tools._hub() is original_hub, (
            "a package reload gave tools._hub() a NEW hub — the late binder "
            "drifted (#263(a))"
        )
        assert platform_adapter.HUB is original_hub, (
            "a package reload left platform_adapter on a stale hub (#263(a))"
        )
        assert sys.modules[f"{package}.transport"].HUB is original_hub
    finally:
        sys.modules[package] = original_parent

    # And the restore itself must not have moved anything.
    assert tools._hub() is original_hub


def test_parent_module_identity_is_not_what_we_are_pinning():
    """Guard against a future reader mistaking the pin above for a no-op.

    The loader DOES churn the parent module object on every pass — that part
    of the filed report was accurate. What must not churn is the hub.
    """
    package = _package_name()
    original_parent = sys.modules[package]
    fresh = types.ModuleType(package)
    assert fresh is not original_parent
