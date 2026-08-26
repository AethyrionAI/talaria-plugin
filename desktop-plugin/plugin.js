/**
 * Talaria desktop face v0 (tracker 270): the pane that answers "is it actually
 * installed?" — read-only, three machine-derived states (bar 270-C):
 *
 *   NOT INSTALLED        ← ctx.rest('/status') failed with the backend's 404
 *                          (no dashboard/ mounted — the productized confusion)
 *   INSTALLED · NOT LIVE ← 200, adapter.observation !== 'live'
 *   LIVE                 ← 200, adapter.observation === 'live'
 *
 * Devices are the LIVE state's CONTENT, never its verdict (270-C adaptation
 * #2). A non-404 transport failure renders an honest unknown — it licenses
 * no state. Vocabulary is shared with the iOS app's 269-A family.
 *
 * Install: copy this file to ~/.hermes/desktop-plugins/talaria/plugin.js
 * (folder name == plugin id). Plain ESM, loaded uncompiled — jsx() calls
 * only; imports limited to @hermes/plugin-sdk, react, react/jsx-runtime;
 * theme vars only, no hardcoded colors (270-E).
 */

import { useQuery } from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'talaria'

function looksLikeNotFound(error) {
  if (!error) return false
  if (error.status === 404 || error.code === 404) return true
  return /\b404\b/.test(String(error.message ?? error))
}

function stateOf(query) {
  if (query.isPending) return 'loading'
  if (query.isError) return looksLikeNotFound(query.error) ? 'notInstalled' : 'unknown'
  return query.data?.adapter?.observation === 'live' ? 'live' : 'installedNotLive'
}

const STATE_LABEL = {
  loading: '…',
  unknown: '—',
  notInstalled: 'NOT INSTALLED',
  installedNotLive: 'INSTALLED · NOT LIVE',
  live: 'LIVE'
}

function row(label, value, valueClass) {
  return jsxs('div', {
    className: 'flex items-baseline justify-between gap-2',
    children: [
      jsx('span', { className: 'text-(--ui-text-tertiary)', children: label }),
      jsx('span', { className: valueClass ?? 'text-(--ui-text-secondary)', children: value })
    ]
  })
}

function deviceRow(device) {
  const seen = device.last_seen ? device.last_seen.replace('T', ' ').slice(0, 16) : '—'
  return jsxs('div', {
    className: 'flex items-baseline justify-between gap-2',
    children: [
      jsx('span', {
        className: device.active ? 'text-(--ui-text-secondary)' : 'text-(--ui-text-tertiary) line-through',
        children: device.name || device.id
      }),
      jsx('span', { className: 'text-(--ui-text-tertiary)', children: seen })
    ]
  })
}

function TalariaPane(ctx) {
  const query = useQuery({
    queryKey: ['talaria', 'status'],
    queryFn: () => ctx.rest('/status'),
    refetchInterval: 5000,
    retry: false
  })
  const state = stateOf(query)

  const children = [
    row('State', STATE_LABEL[state],
      state === 'live' ? 'text-(--ui-accent)' : 'text-(--ui-text-secondary)')
  ]

  if (state === 'notInstalled') {
    children.push(jsx('div', {
      className: 'text-(--ui-text-tertiary)',
      children: "Talaria isn't set up on this machine yet. Ask Hermes to install it."
    }))
  }

  if (state === 'installedNotLive') {
    const adapter = query.data?.adapter ?? {}
    children.push(row('Adapter', adapter.observation ?? '—'))
    children.push(jsx('div', {
      className: 'text-(--ui-text-tertiary)',
      children: adapter.observation === 'absent'
        ? 'The plugin is on disk, but the gateway is not serving its adapter — restart the gateway to load it.'
        : 'The gateway did not answer the liveness probe.'
    }))
  }

  if (state === 'live') {
    children.push(row('Version', query.data?.plugin?.version ?? '—'))
    const devices = query.data?.devices ?? []
    children.push(jsx('div', {
      className: 'mt-1 text-(--ui-text-tertiary)',
      children: devices.length === 1 ? '1 paired device' : `${devices.length} paired devices`
    }))
    if (devices.length === 0) {
      children.push(jsx('div', {
        className: 'text-(--ui-text-tertiary)',
        children: 'No devices paired yet — run `hermes talaria pair-qr` and scan it from the app.'
      }))
    }
    for (const device of devices) children.push(deviceRow(device))
  }

  if (state === 'unknown') {
    children.push(jsx('div', {
      className: 'text-(--ui-text-tertiary)',
      children: 'The desktop backend did not answer — no state is claimed.'
    }))
  }

  return jsxs('div', {
    className: 'flex h-full flex-col gap-2 overflow-y-auto p-3 text-sm',
    children: [
      jsx('div', { className: 'font-medium', children: 'Talaria' }),
      ...children
    ]
  })
}

export default {
  id: ID, // must match the folder name AND plugin.yaml's name (tracker 263(a) discipline)
  name: 'Talaria',
  description: 'Is the Talaria phone bridge actually installed? Read-only status.',
  register(ctx) {
    ctx.register({
      id: 'pane',
      area: 'panes',
      title: 'talaria',
      data: { placement: 'right', width: '260px' },
      render: () => jsx(TalariaPane, ctx)
    })
  }
}
