/**
 * Gemini Rate Limit — Hermes Desktop Status Bar & Popover Configuration UI
 * Plain ESM uncompiled module loaded into Hermes Desktop.
 */

import {
  Button,
  icons,
  PALETTE_AREA,
  Popover,
  PopoverContent,
  PopoverTrigger,
  Separator,
  STATUSBAR_AREAS
} from '@hermes/plugin-sdk'
import { useEffect, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const PLUGIN_ID = 'gemini-rate-limit'

// Universal helper for ctx.rest supporting both method styles
async function apiGet(ctx, path) {
  if (ctx && ctx.rest && typeof ctx.rest.get === 'function') {
    return ctx.rest.get(path)
  }
  return ctx.rest(path)
}

async function apiPost(ctx, path, payload) {
  if (ctx && ctx.rest && typeof ctx.rest.post === 'function') {
    return ctx.rest.post(path, payload)
  }
  return ctx.rest(path, { method: 'POST', body: payload })
}

// Preset configurations for common Google Gemini models
const PRESETS = [
  { name: 'Gemini 3.5 Flash Lite', rpm: 15, tpm: 250000 },
  { name: 'Gemini 3.8 Flash', rpm: 10, tpm: 250000 },
  { name: 'Gemini 2.5 Pro', rpm: 5, tpm: 125000 }
]

// Global opener ref for PALETTE_AREA shortcut
let _openPopoverRef = null

function GeminiQuotaStatusItem({ ctx }) {
  const [status, setStatus] = useState({
    enabled: true,
    rpm: 15,
    tpm: 250000,
    rpd: 1000,
    safety_factor: 0.92,
    effective_rpm: 13,
    effective_tpm: 230000,
    rpm_used: 0,
    tpm_used: 0,
    last_wait_seconds: 0.0,
    total_throttled_requests: 0,
    is_throttling: false
  })

  const [open, setOpen] = useState(false)
  const [rpmInput, setRpmInput] = useState('15')
  const [tpmInput, setTpmInput] = useState('250000')
  const [enabledInput, setEnabledInput] = useState(true)
  const [saving, setSaving] = useState(false)
  const [saveMessage, setSaveMessage] = useState('')

  // Expose opener for palette command
  useEffect(() => {
    _openPopoverRef = () => setOpen(prev => !prev)
    return () => {
      _openPopoverRef = null
    }
  }, [])

  // Sync inputs whenever popover opens or status changes
  useEffect(() => {
    if (open) {
      setRpmInput(String(status.rpm || 15))
      setTpmInput(String(status.tpm || 250000))
      setEnabledInput(status.enabled !== false)
      setSaveMessage('')
    }
  }, [open, status.rpm, status.tpm, status.enabled])

  // Poll /status every 2 seconds
  useEffect(() => {
    let cancelled = false
    const fetchStatus = async () => {
      try {
        const data = await apiGet(ctx, '/status')
        if (!cancelled && data && typeof data === 'object') {
          setStatus(prev => ({ ...prev, ...data }))
        }
      } catch (err) {
        // Silently tolerate transient gateway restarts or disconnected state
      }
    }

    fetchStatus()
    const timer = setInterval(fetchStatus, 2000)
    return () => {
      cancelled = true
      clearInterval(timer)
    }
  }, [ctx])

  // Save handler
  const handleSave = async () => {
    setSaving(true)
    setSaveMessage('')
    try {
      const parsedRpm = parseInt(rpmInput, 10)
      const parsedTpm = parseInt(tpmInput, 10)

      if (isNaN(parsedRpm) || parsedRpm < 1) {
        setSaveMessage('Error: RPM must be >= 1')
        setSaving(false)
        return
      }
      if (isNaN(parsedTpm) || parsedTpm < 1000) {
        setSaveMessage('Error: TPM must be >= 1000')
        setSaving(false)
        return
      }

      const updated = await apiPost(ctx, '/config', {
        rpm: parsedRpm,
        tpm: parsedTpm,
        enabled: enabledInput
      })

      if (updated && typeof updated === 'object') {
        setStatus(prev => ({ ...prev, ...updated }))
      }
      setSaveMessage('Saved successfully!')
      setTimeout(() => {
        setSaveMessage('')
        setOpen(false)
      }, 700)
    } catch (err) {
      setSaveMessage(`Save failed: ${err.message || 'unknown error'}`)
    } finally {
      setSaving(false)
    }
  }

  // Quota usage computations
  const rpmRatio = (status.effective_rpm > 0) ? (status.rpm_used / status.effective_rpm) : 0
  const tpmRatio = (status.effective_tpm > 0) ? (status.tpm_used / status.effective_tpm) : 0
  const isHigh = rpmRatio >= 0.75 || tpmRatio >= 0.75
  const isThrottling = Boolean(status.is_throttling)

  // Status color styles using CSS variables and fallback standard colors
  let dotColor = 'var(--ui-accent, #10b981)' // green / neutral
  let chipBg = 'transparent'
  let textColor = 'var(--ui-text-secondary, #94a3b8)'

  if (!status.enabled) {
    dotColor = 'var(--ui-text-quaternary, #64748b)'
    textColor = 'var(--ui-text-quaternary, #64748b)'
  } else if (isThrottling) {
    dotColor = '#ef4444' // red
    chipBg = 'rgba(239, 68, 68, 0.15)'
    textColor = '#ef4444'
  } else if (isHigh) {
    dotColor = '#f59e0b' // amber
    chipBg = 'rgba(245, 158, 11, 0.12)'
    textColor = '#f59e0b'
  }

  const rpmUsed = status.rpm_used || 0
  const rpmMax = status.rpm || 15
  const tpmUsedK = Math.round((status.tpm_used || 0) / 1000)
  const tpmMaxK = Math.round((status.tpm || 250000) / 1000)

  const labelText = `Gemini: ${rpmUsed}/${rpmMax} RPM · ${tpmUsedK}k/${tpmMaxK}k TPM`

  return jsx(Popover, {
    open,
    onOpenChange: setOpen,
    children: [
      jsx(PopoverTrigger, {
        asChild: true,
        children: jsx('button', {
          type: 'button',
          'aria-label': 'Configure Gemini Rate Limit',
          title: `Gemini Quota Status: ${rpmUsed}/${rpmMax} RPM, ${tpmUsedK}k/${tpmMaxK}k TPM (Effective: ${status.effective_rpm} RPM, ${Math.round(status.effective_tpm/1000)}k TPM)${isThrottling ? ' [THROTTLING]' : ''}`,
          style: {
            display: 'inline-flex',
            alignItems: 'center',
            gap: '6px',
            padding: '2px 7px',
            borderRadius: '4px',
            fontSize: '11px',
            lineHeight: '16px',
            fontFamily: 'inherit',
            background: chipBg,
            color: textColor,
            border: isThrottling ? '1px solid rgba(239, 68, 68, 0.4)' : '1px solid transparent',
            cursor: 'pointer'
          },
          children: [
            jsx('span', {
              style: {
                display: 'inline-block',
                width: '6px',
                height: '6px',
                borderRadius: '9999px',
                background: dotColor,
                flexShrink: 0
              }
            }),
            jsx('span', {
              style: {
                whiteSpace: 'nowrap',
                fontWeight: isThrottling ? '600' : '400'
              },
              children: labelText
            })
          ]
        })
      }),
      jsx(PopoverContent, {
        side: 'top',
        align: 'end',
        style: {
          width: '320px',
          padding: '14px',
          fontSize: '12px',
          background: 'var(--ui-background, #18181b)',
          color: 'var(--ui-text-primary, #f4f4f5)',
          border: '1px solid var(--ui-stroke-secondary, #27272a)',
          borderRadius: '8px',
          boxShadow: '0 10px 25px -5px rgba(0, 0, 0, 0.5)',
          zIndex: 100
        },
        children: jsxs('div', {
          style: { display: 'flex', flexDirection: 'column', gap: '12px' },
          children: [
            // Title & Enable Switch
            jsxs('div', {
              style: { display: 'flex', alignItems: 'center', justifyContent: 'space-between' },
              children: [
                jsx('span', {
                  style: { fontWeight: '600', fontSize: '13px' },
                  children: 'Gemini Rate Limiter'
                }),
                jsxs('label', {
                  style: {
                    display: 'flex',
                    alignItems: 'center',
                    gap: '6px',
                    fontSize: '11px',
                    cursor: 'pointer',
                    color: 'var(--ui-text-secondary, #a1a1aa)'
                  },
                  children: [
                    jsx('input', {
                      type: 'checkbox',
                      checked: enabledInput,
                      onChange: e => setEnabledInput(e.target.checked),
                      style: { cursor: 'pointer' }
                    }),
                    'Enabled'
                  ]
                })
              ]
            }),

            jsx(Separator, {}),

            // Live status stats summary
            jsxs('div', {
              style: {
                display: 'grid',
                gridTemplateColumns: '1fr 1fr',
                gap: '8px',
                padding: '8px',
                borderRadius: '6px',
                background: 'var(--ui-hover, rgba(255, 255, 255, 0.04))',
                fontSize: '11px'
              },
              children: [
                jsxs('div', {
                  children: [
                    jsx('div', { style: { color: 'var(--ui-text-tertiary, #71717a)' }, children: 'RPM in 60s' }),
                    jsx('div', {
                      style: { fontWeight: '600', color: isHigh ? '#f59e0b' : 'inherit' },
                      children: `${status.rpm_used} / ${status.effective_rpm} eff`
                    })
                  ]
                }),
                jsxs('div', {
                  children: [
                    jsx('div', { style: { color: 'var(--ui-text-tertiary, #71717a)' }, children: 'TPM in 60s' }),
                    jsx('div', {
                      style: { fontWeight: '600', color: isHigh ? '#f59e0b' : 'inherit' },
                      children: `${(status.tpm_used || 0).toLocaleString()} / ${(status.effective_tpm || 0).toLocaleString()} eff`
                    })
                  ]
                }),
                jsxs('div', {
                  children: [
                    jsx('div', { style: { color: 'var(--ui-text-tertiary, #71717a)' }, children: 'Throttled Calls' }),
                    jsx('div', { style: { fontWeight: '600' }, children: String(status.total_throttled_requests || 0) })
                  ]
                }),
                jsxs('div', {
                  children: [
                    jsx('div', { style: { color: 'var(--ui-text-tertiary, #71717a)' }, children: 'Last Wait' }),
                    jsx('div', {
                      style: { fontWeight: '600', color: status.last_wait_seconds > 0 ? '#ef4444' : 'inherit' },
                      children: `${status.last_wait_seconds || 0}s`
                    })
                  ]
                })
              ]
            }),

            // Inputs for RPM and TPM
            jsxs('div', {
              style: { display: 'flex', flexDirection: 'column', gap: '8px' },
              children: [
                jsxs('div', {
                  style: { display: 'flex', alignItems: 'center', justifyContent: 'space-between' },
                  children: [
                    jsx('label', {
                      style: { fontSize: '11px', color: 'var(--ui-text-secondary, #a1a1aa)' },
                      children: 'Peak RPM (Requests / min)'
                    }),
                    jsx('input', {
                      type: 'number',
                      min: '1',
                      step: '1',
                      value: rpmInput,
                      onChange: e => setRpmInput(e.target.value),
                      style: {
                        width: '80px',
                        padding: '4px 6px',
                        fontSize: '11px',
                        background: 'var(--ui-background, #18181b)',
                        border: '1px solid var(--ui-stroke-secondary, #3f3f46)',
                        borderRadius: '4px',
                        color: 'inherit',
                        textAlign: 'right'
                      }
                    })
                  ]
                }),
                jsxs('div', {
                  style: { display: 'flex', alignItems: 'center', justifyContent: 'space-between' },
                  children: [
                    jsx('label', {
                      style: { fontSize: '11px', color: 'var(--ui-text-secondary, #a1a1aa)' },
                      children: 'Peak TPM (Input tokens / min)'
                    }),
                    jsx('input', {
                      type: 'number',
                      min: '1000',
                      step: '5000',
                      value: tpmInput,
                      onChange: e => setTpmInput(e.target.value),
                      style: {
                        width: '80px',
                        padding: '4px 6px',
                        fontSize: '11px',
                        background: 'var(--ui-background, #18181b)',
                        border: '1px solid var(--ui-stroke-secondary, #3f3f46)',
                        borderRadius: '4px',
                        color: 'inherit',
                        textAlign: 'right'
                      }
                    })
                  ]
                }),
                // RPD Unused Reference Note
                jsx('div', {
                  style: {
                    fontSize: '10px',
                    color: 'var(--ui-text-quaternary, #71717a)',
                    marginTop: '2px'
                  },
                  children: `RPD: ${(status.rpd || 1000).toLocaleString()} ref — unenforced constant (no daily blocking)`
                })
              ]
            }),

            // Quick Presets
            jsxs('div', {
              style: { display: 'flex', flexDirection: 'column', gap: '4px' },
              children: [
                jsx('span', {
                  style: { fontSize: '10px', color: 'var(--ui-text-tertiary, #a1a1aa)', fontWeight: '500' },
                  children: 'Quick Presets (Free Tier):'
                }),
                jsxs('div', {
                  style: { display: 'flex', flexWrap: 'wrap', gap: '4px' },
                  children: PRESETS.map(preset =>
                    jsx('button', {
                      key: preset.name,
                      type: 'button',
                      onClick: () => {
                        setRpmInput(String(preset.rpm))
                        setTpmInput(String(preset.tpm))
                      },
                      style: {
                        fontSize: '10px',
                        padding: '2px 6px',
                        borderRadius: '4px',
                        border: '1px solid var(--ui-stroke-secondary, #3f3f46)',
                        background: 'transparent',
                        color: 'var(--ui-text-secondary, #a1a1aa)',
                        cursor: 'pointer'
                      },
                      children: `${preset.name} (${preset.rpm} RPM)`
                    })
                  )
                })
              ]
            }),

            // Message feedback
            saveMessage && jsx('div', {
              style: {
                fontSize: '11px',
                color: saveMessage.startsWith('Error') || saveMessage.startsWith('Save failed') ? '#ef4444' : '#10b981',
                textAlign: 'center'
              },
              children: saveMessage
            }),

            // Action buttons
            jsxs('div', {
              style: { display: 'flex', justifyContent: 'flex-end', gap: '8px', marginTop: '4px' },
              children: [
                jsx(Button, {
                  variant: 'ghost',
                  size: 'sm',
                  onClick: () => setOpen(false),
                  children: 'Cancel'
                }),
                jsx(Button, {
                  variant: 'primary',
                  size: 'sm',
                  disabled: saving,
                  onClick: handleSave,
                  children: saving ? 'Saving…' : 'Save'
                })
              ]
            })
          ]
        })
      })
    ]
  })
}

export default {
  id: PLUGIN_ID,
  name: 'Gemini Rate Limit',
  defaultEnabled: true,
  register(ctx) {
    // 1. Bottom status bar quota item
    ctx.register({
      id: 'gemini-quota',
      area: STATUSBAR_AREAS.right,
      order: 120,
      render: () => jsx(GeminiQuotaStatusItem, { ctx })
    })

    // 2. Command palette item (Ctrl+K)
    ctx.register({
      id: 'gemini-quota-palette',
      area: PALETTE_AREA,
      data: {
        id: 'gemini-quota.configure',
        label: 'Gemini Rate Limit: Configure Quotas',
        keywords: ['gemini', 'rate', 'limit', 'quota', 'rpm', 'tpm'],
        run: () => {
          if (typeof _openPopoverRef === 'function') {
            _openPopoverRef()
          } else if (typeof ctx.runAction === 'function') {
            ctx.runAction('view.toggleStatusbar')
          }
        }
      }
    })
  }
}
