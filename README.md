# talaria-plugin

Talaria phone bridge for [Hermes](https://github.com/NousResearch) — the host-side
half of the [Talaria iOS app](https://github.com/AethyrionAI/Talaria-27). One
directory plugin that will, phase by phase, replace the legacy relay + connector +
MCP server + venv CLI sidecars (Talaria-27 `OPEN_ITEMS.md` #251).

## Install

```bash
git clone <this repo> ~/.hermes/plugins/talaria
```

Then enable it in `config.yaml`:

```yaml
plugins:
  enabled:
    - talaria
```

Survives `hermes update` by construction — nothing here touches Hermes core.

## Phase arc

1. **Tools + admin (this phase).** `talaria_phone_query` tool scaffold — gated
   *unavailable* by `check_fn` until a live phone transport exists, with an
   honest "phone unreachable" handler if invoked anyway — plus
   `hermes talaria pair|status|unpair`.
2. **Webhook platform adapter.** Inbound rides
   `POST /api/platforms/talaria/events` on the gateway's existing HTTP
   listener (no socket of its own): pairing handshake, inbox acks, durable
   outbox drain. This phase makes `pair` tokens consumable by the app and
   flips the tool's `check_fn` via last-seen heartbeats.
3. **Runs-transport migration.** Remote turns move to `/v1/runs` + events —
   in-chat approvals (proven e2e 2026-08-05) and pollable-by-id recovery.
4. **Relay decommission.** The legacy sidecars stop; this plugin is the bridge.

## Admin

```bash
hermes talaria pair     # prints a one-time pairing token (only its hash is stored)
hermes talaria status   # devices + phase state
hermes talaria unpair   # deactivates (records kept for rollback, never deleted)
```

Device store: `<HERMES_HOME>/talaria/devices.json` (0600, tokens hashed,
profile-aware).
