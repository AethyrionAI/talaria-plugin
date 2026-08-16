# talaria-plugin

Talaria phone bridge for [Hermes Agent](https://github.com/NousResearch/hermes-agent),
providing the host side of the private
[Talaria iOS app](https://github.com/AethyrionAI/Talaria-27).

The plugin currently ships:

- the `talaria_phone_query` tool;
- pairing, status, targeted-send, and unpair administration;
- a webhook platform adapter on Hermes's existing HTTP listener;
- authenticated drain, acknowledgement, query-result, and unpair envelopes;
- transactional, profile-aware SQLite persistence;
- explicit routing and independent acknowledgements for multiple devices.

It does not modify Hermes core and does not open a listener of its own. Inbound
phone events use `POST /api/platforms/talaria/events` through the gateway's
existing webhook platform route.

## Internal installation

Clone the private repository into the active Hermes profile's plugin directory
under the package name `talaria`, then enable it in `config.yaml`:

```yaml
plugins:
  enabled:
    - talaria
```

The `talaria` directory/package name remains required by the current plugin and
test import layout. Do not rename the checkout unless conventional packaging is
added later.

## Administration

```bash
hermes talaria pair
hermes talaria status
hermes talaria send "hello"
hermes talaria send --device <device-id> "hello phone"
hermes talaria send --all "hello every device"
hermes talaria unpair [device-id]
```

Delivery selection is deliberately fail-closed:

- With one active device, `send <text>` targets that device.
- With multiple active devices, `send <text>` refuses to guess.
- `--device <id>` targets exactly one active device.
- `--all` creates one targeted outbox row per active device. Each row has an
  independent acknowledgement lifecycle.
- Unknown or inactive targets fail without queuing an item.

The platform adapter follows the same contract: its `chat_id` must be the ID of
one active paired device. Missing targets are never treated as broadcast.

## Durable state and migration

The plugin owns this profile-aware database:

```text
<HERMES_HOME>/talaria/talaria.db
```

It uses Python's standard-library `sqlite3`, WAL mode, foreign keys, a busy
timeout, and write transactions. Pairing tokens remain SHA-256 hashes at rest;
plaintext tokens are returned only for one-time pairing. Devices and delivered
items are retained rather than deleted.

On first use, if legacy `devices.json` and/or `outbox.json` files exist beside
the database, the plugin imports both documents in one transaction and validates
the migrated rows before recording completion. The original JSON files remain
untouched as recovery evidence. A malformed legacy document raises a migration
error and is not renamed, truncated, or silently converted to empty state.
Retrying after repair is idempotent.

Legacy pending outbox rows did not carry an authoritative device target. They
migrate as `legacy_any` compatibility rows. The first authenticated active
device to drain atomically claims each such row; other devices cannot drain or
acknowledge it. New adapter and CLI sends always create explicit targeted rows.

## Authentication and delivery properties

- Pairing requires the gateway API key.
- Device operations require that device's active token and matching device ID.
- Tokens are bound to their device ID and hashed at rest.
- Deactivation never deletes the historical row.
- A device drains only its targeted rows or legacy rows it atomically claimed.
- A device can acknowledge only its own targeted or claimed rows.
- Repeated acknowledgements settle a row at most once.

## Running tests

The repository is not yet conventionally packaged. Expose the checkout under a
temporary parent using the package name `talaria`, then invoke pytest directly
from that parent:

```bash
mkdir -p /tmp/talaria-plugin-test-parent
ln -s "$PWD" /tmp/talaria-plugin-test-parent/talaria
cd /tmp/talaria-plugin-test-parent
~/.hermes/hermes-agent/venv/bin/pytest talaria/tests/ -q
```

Do not run `python -m pytest` from the plugin root. That inserts the plugin root
at `sys.path[0]`, where this repository's `tools.py` shadows Hermes's top-level
`tools` package when `gateway.*` imports. Production plugin loading is immune:
Hermes loads plugins under a package namespace without inserting the plugin
root into `sys.path`.

All persistence tests use temporary directories. They must never point at a
real `<HERMES_HOME>/talaria` directory or contact a real phone.

## Remaining phase arc

1. **Shipped:** phone-query tools and administration.
2. **Shipped:** webhook platform adapter, authenticated envelopes, transactional
   persistence, and multi-device routing.
3. **Future:** move remote turns to Hermes Runs transport and events.
4. **Future:** retire the legacy relay/connector sidecars after separate live
   deployment approval and rollback planning.

Repository publication remains intentionally deferred while the Talaria app is
private.
