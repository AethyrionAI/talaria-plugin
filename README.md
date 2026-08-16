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

Install an immutable commit through Hermes's native plugin installer, then enable
the plugin. Private-repository access uses the operator's existing GitHub
credentials:

```bash
hermes plugins install AethyrionAI/talaria-plugin \
  --ref <full-40-character-commit-sha> \
  --no-enable
hermes plugins enable talaria
```

Manual directory installs remain supported: place the checkout at
`plugins/<name>/` or one category level deep (`plugins/<category>/<name>/`) —
Hermes's plugin scanner caps discovery at two path segments — and enable the
manifest name:

```yaml
plugins:
  enabled:
    - talaria
```

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

The platform adapter follows the same contract: its `chat_id` must name one
active paired device — either the device ID, or the device's `install_id`
(the rotation-proof address: device IDs change on every re-pair, install IDs
do not, so persisted chat_ids should prefer the install ID). Missing targets
are never treated as broadcast.

## Durable state and migration

The plugin owns this profile-aware database:

```text
<HERMES_HOME>/talaria/talaria.db
```

It uses Python's standard-library `sqlite3`, WAL mode, foreign keys, a busy
timeout, and write transactions. Pairing tokens remain SHA-256 hashes at rest;
plaintext tokens are returned only for one-time pairing. Devices and delivered
items are retained rather than deleted.

Initialization runs once per process, at plugin registration (and lazily as a
fallback). If legacy `devices.json` and/or `outbox.json` files exist beside
the database, both documents import in one transaction with per-row read-back
validation, and a completion marker records that a migration happened. A fresh
install with no legacy files writes no marker, so legacy JSON appearing later
(an old gateway process still writing, or a restore from backup) imports at
the next initialization.

Migration is fail-soft: a malformed legacy file is quarantined — renamed to
`<file>.rejected` with its bytes preserved — with a logged warning, and the
plugin keeps serving; the other file still imports. An unreadable
`talaria.db` is itself quarantined to `talaria.db.corrupt-<stamp>` and
rebuilt from the untouched JSON. Valid legacy files are never renamed or
modified.

**Migration recovery:** after repairing a quarantined file, rename it back
(drop the `.rejected` suffix), delete the `legacy_json_migration` row from
`schema_metadata` in `talaria.db`, and restart the gateway; re-import is
idempotent (already-present rows are skipped and verified).

Each legacy outbox row migrates to the device its `meta.chat_id` names when
that device was imported active; otherwise to the only active device when
exactly one exists. Only genuinely ambiguous rows migrate as `legacy_any`
compatibility rows, which the first authenticated active device to drain
atomically claims. Re-pairing re-targets a device's pending rows to its new
identity and releases claims held by deactivated devices, so rotation never
strands queued messages. New adapter and CLI sends always create explicit
targeted rows.

## Authentication and delivery properties

- Wire pairing (the app's `pair` event) requires the gateway API key;
  `hermes talaria pair` is a local, credential-free fallback for an operator
  already on the host, and such manual rows never auto-rotate.
- Device operations require that device's active token and matching device ID.
- Tokens are bound to their device ID and hashed at rest.
- Deactivation never deletes the historical row.
- A device drains only its targeted rows or legacy rows it atomically claimed.
- A device can acknowledge only its own targeted or claimed rows.
- Repeated acknowledgements settle a row at most once.

## Running tests

The implementation lives in the conventional `talaria/` package. From the
repository root, run:

```bash
pytest tests/ -q
python -m compileall -q .
hermes plugins doctor . --ci
```

Both `pytest` and `python -m pytest` work from the repository root
(`pytest.ini` carries `pythonpath = .`).

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
