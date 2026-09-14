import { useCallback, useEffect, useRef, useState } from 'react'

import { Button } from '@/components/ui/button'
import { getCortexActivation, getCortexStatus, startCortexActivation, verifyCortex } from '@/hermes'
import { Check, ExternalLink, Loader2 } from '@/lib/icons'
import { notify, notifyError } from '@/store/notifications'
import type { CortexActivationState, CortexStatusResponse } from '@/types/hermes'

import { Pill } from '../primitives'

const POLL_MS = 1500
// Cortex's activation operation expires after 15 minutes; poll no longer.
const POLL_TIMEOUT_MS = 15 * 60_000
const CORTEX_SITE = 'https://arkalabs.app/cortex'

type Phase = 'error' | 'idle' | 'pending' | 'verifying'

/** The human-readable connection state, decided by facts (spec §5.3):
 *  never "connected" on a config file alone. */
export function cortexStateLabel(status: CortexStatusResponse): string {
  if (status.cortex === 'missing') {
    return 'Cortex is not installed on this computer'
  }

  if (!status.binding?.present) {
    return 'Cortex is not connected to this profile'
  }

  if (status.remote === null) {
    const queued = status.outbox?.queued ?? 0

    return queued > 0
      ? `Cortex unavailable — ${queued} exchange${queued > 1 ? 's' : ''} waiting`
      : 'Cortex unavailable — exchanges will wait'
  }

  if (status.remote.paused) {
    return 'Paused'
  }

  if (!status.governed) {
    return 'Activation saved — Cortex is not yet the required provider'
  }

  if (status.runtime?.state === 'active') {
    return 'Connected — waiting for the first exchange'
  }

  return 'Activation saved — start a new session to load the provider'
}

export function CortexProviderPanel({ profile }: { profile?: string }) {
  const [status, setStatus] = useState<CortexStatusResponse | null>(null)
  const [phase, setPhase] = useState<Phase>('idle')
  const [detail, setDetail] = useState('')
  const timer = useRef<ReturnType<typeof setInterval> | null>(null)
  const deadline = useRef(0)

  const stop = useCallback(() => {
    if (timer.current !== null) {
      clearInterval(timer.current)
      timer.current = null
    }
  }, [])

  const refresh = useCallback(async () => {
    try {
      setStatus(await getCortexStatus(profile))
    } catch (err) {
      notifyError(err, 'Cortex status failed to load')
      setStatus(null)
    }
  }, [profile])

  useEffect(() => {
    void refresh()

    return () => stop()
  }, [refresh, stop])

  const verify = useCallback(async () => {
    setPhase('verifying')

    try {
      await verifyCortex(profile)
      notify({
        kind: 'success',
        title: 'Cortex provider loaded',
        message: 'A Hermes runtime loaded the Cortex provider for this profile.'
      })
    } catch (err) {
      setDetail('The runtime could not confirm the provider. Start a new session, then check again.')
      notifyError(err, 'Cortex verification failed')
    } finally {
      await refresh()
      setPhase('idle')
    }
  }, [profile, refresh])

  const finish = useCallback(
    async (state: CortexActivationState) => {
      stop()

      if (state === 'applied') {
        // Cortex configured the profile; a fresh diagnostic runtime prepares
        // the proof. A running session keeps its provider until restarted.
        await verify()
      } else {
        setPhase('idle')
        setDetail(state === 'expired' ? 'The activation expired — try again.' : 'Activation cancelled in Cortex.')
        await refresh()
      }
    },
    [refresh, stop, verify]
  )

  const activate = useCallback(async () => {
    setPhase('pending')
    setDetail('')
    let operationId: string

    try {
      const started = await startCortexActivation(profile)
      operationId = started.operationId
      await window.hermesDesktop.openExternal(started.deepLink)
    } catch (err) {
      setPhase('error')
      setDetail('Could not start the activation.')
      notifyError(err, 'Failed to start Cortex activation')

      return
    }

    deadline.current = Date.now() + POLL_TIMEOUT_MS
    stop()
    timer.current = setInterval(() => {
      void (async () => {
        try {
          const next = await getCortexActivation(operationId, profile)

          if (next.state === 'pending') {
            if (Date.now() > deadline.current) {
              await finish('expired')
            }

            return
          }

          await finish(next.state)
        } catch {
          // Transient poll failure — keep trying until the deadline.
        }
      })()
    }, POLL_MS)
  }, [finish, profile, stop])

  const cancel = useCallback(() => {
    stop()
    setPhase('idle')
    setDetail('')
  }, [stop])

  if (!status) {
    return null
  }

  const label = cortexStateLabel(status)
  const connected =
    Boolean(status.binding?.present) && status.governed && status.remote !== null && !status.remote.paused

  return (
    <section className="py-3" data-cortex-panel={status.action}>
      <div className="grid gap-3 rounded-xl bg-background/60 p-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <span className="text-[length:var(--conversation-text-font-size)] font-medium text-foreground">
            Cortex Cognitive Provider
          </span>
          <Pill tone={connected && status.runtime?.state === 'active' ? 'primary' : undefined}>
            {connected && status.runtime?.state === 'active' && <Check className="size-3" />}
            {label}
          </Pill>
        </div>
        <span className="text-xs text-muted-foreground">
          Cortex becomes the memory provider of this profile: what Hermes writes to memory and does with its tools is
          captured and governed there, and the admissible context comes back into Hermes requests. Hermes keeps running
          the actions.
        </span>
        {status.binding?.present && (
          <span className="text-xs text-muted-foreground">
            Plugin {status.plugin.version ?? 'unknown'} · policy{' '}
            {status.governed ? 'cortex, required, native context mediated' : 'not governed'}
            {status.runtime?.lastObservedAt ? ` · last runtime ${status.runtime.lastObservedAt}` : ''}
          </span>
        )}
        <div className="flex flex-wrap items-center gap-2">
          {status.action === 'install_cortex' && (
            <Button
              onClick={() => void window.hermesDesktop.openExternal(CORTEX_SITE)}
              size="sm"
              type="button"
              variant="secondary"
            >
              <ExternalLink className="size-3.5" />
              Get Cortex
            </Button>
          )}
          {(status.action === 'activate' || status.action === 'reactivate') && phase !== 'pending' && (
            <Button disabled={phase === 'verifying'} onClick={() => void activate()} size="sm" type="button">
              <ExternalLink className="size-3.5" />
              Activate Cortex
            </Button>
          )}
          {phase === 'pending' && (
            <>
              <span className="inline-flex items-center gap-1.5 text-xs text-muted-foreground">
                <Loader2 className="size-3 animate-spin" />
                Waiting for your authorization in Cortex…
              </span>
              <Button className="h-auto p-0 text-xs" onClick={cancel} size="sm" type="button" variant="link">
                Cancel
              </Button>
            </>
          )}
          {status.binding?.present && phase !== 'pending' && (
            <Button
              disabled={phase === 'verifying'}
              onClick={() => void verify()}
              size="sm"
              type="button"
              variant="secondary"
            >
              {phase === 'verifying' ? <Loader2 className="size-3.5 animate-spin" /> : <Check className="size-3.5" />}
              Check the provider
            </Button>
          )}
          {status.cortex === 'installed' && (
            <Button
              className="h-auto p-0 text-xs"
              onClick={() => void window.hermesDesktop.openExternal('cortex://outils')}
              size="sm"
              type="button"
              variant="link"
            >
              Open Cortex
            </Button>
          )}
        </div>
        {detail && <span className="text-xs text-destructive">{detail}</span>}
        {status.binding?.present && status.remote && (
          <span className="text-xs text-muted-foreground">
            Pause, resume and disconnect are available in Cortex → Configuration → Connections; they apply here on the
            next session.
          </span>
        )}
      </div>
    </section>
  )
}
