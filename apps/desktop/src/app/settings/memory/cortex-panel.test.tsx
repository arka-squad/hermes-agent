import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { CortexStatusResponse } from '@/types/hermes'

const getCortexStatus = vi.fn()
const startCortexActivation = vi.fn()
const getCortexActivation = vi.fn()
const verifyCortex = vi.fn()

vi.mock('@/hermes', () => ({
  getCortexStatus: () => getCortexStatus(),
  startCortexActivation: () => startCortexActivation(),
  getCortexActivation: (id: string) => getCortexActivation(id),
  verifyCortex: () => verifyCortex()
}))

vi.mock('@/store/notifications', () => ({
  notify: vi.fn(),
  notifyError: vi.fn()
}))

import { CortexProviderPanel, cortexStateLabel } from './cortex-panel'

function status(overrides: Partial<CortexStatusResponse> = {}): CortexStatusResponse {
  return {
    cortex: 'installed',
    plugin: { installed: false, version: null },
    binding: { present: false },
    policy: { provider: '', provider_required: false, native_context: 'builtin' },
    governed: false,
    runtime: null,
    remote: null,
    outbox: null,
    action: 'activate',
    ...overrides
  }
}

const bound = (): CortexStatusResponse =>
  status({
    plugin: { installed: true, version: '0.2.0' },
    binding: { present: true, bindingId: 'hb-1', revision: 1 },
    policy: { provider: 'cortex', provider_required: true, native_context: 'provider' },
    governed: true,
    remote: { desiredState: 'active', revision: 2, paused: false },
    action: 'none'
  })

describe('cortexStateLabel', () => {
  it('never says connected on a config file alone', () => {
    expect(cortexStateLabel(status({ cortex: 'missing', action: 'install_cortex' }))).toMatch(/not installed/)
    expect(cortexStateLabel(status())).toMatch(/not connected/)
    expect(cortexStateLabel(bound())).toMatch(/start a new session/)
    expect(cortexStateLabel({ ...bound(), runtime: { state: 'active', lastObservedAt: 'now' } })).toMatch(
      /waiting for the first exchange/
    )
    expect(cortexStateLabel({ ...bound(), remote: null, outbox: { queued: 3 } })).toMatch(/3 exchanges waiting/)
    expect(cortexStateLabel({ ...bound(), remote: { desiredState: 'paused', revision: 3, paused: true } })).toBe(
      'Paused'
    )
  })
})

describe('CortexProviderPanel', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    getCortexStatus.mockReset()
    startCortexActivation.mockReset()
    getCortexActivation.mockReset()
    verifyCortex.mockReset()
    Object.defineProperty(window, 'hermesDesktop', {
      configurable: true,
      value: { openExternal: vi.fn(async () => undefined) }
    })
  })

  afterEach(() => {
    cleanup()
    vi.useRealTimers()
  })

  it('opens the Cortex deep link, waits for the applied operation, then verifies the runtime', async () => {
    getCortexStatus
      .mockResolvedValueOnce(status())
      .mockResolvedValue({ ...bound(), runtime: { state: 'active', lastObservedAt: 'now' } })
    startCortexActivation.mockResolvedValue({
      operationId: 'hop-0123456789abcdef0123456789abcdef',
      deepLink: 'cortex://hermes/activate?op=hop-0123456789abcdef0123456789abcdef',
      expiresAt: '2099-01-01T00:00:00Z'
    })
    getCortexActivation.mockResolvedValueOnce({ state: 'pending' }).mockResolvedValue({ state: 'applied' })
    verifyCortex.mockResolvedValue({ ok: true })

    render(<CortexProviderPanel />)
    await vi.waitFor(() => expect(screen.getByRole('button', { name: /Activate Cortex/ })).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: /Activate Cortex/ }))
    await vi.waitFor(() =>
      expect(window.hermesDesktop.openExternal).toHaveBeenCalledWith(
        'cortex://hermes/activate?op=hop-0123456789abcdef0123456789abcdef'
      )
    )
    expect(screen.getByText(/Waiting for your authorization in Cortex/)).toBeTruthy()
    await vi.advanceTimersByTimeAsync(1600)
    expect(getCortexActivation).toHaveBeenCalledWith('hop-0123456789abcdef0123456789abcdef')
    await vi.advanceTimersByTimeAsync(1600)
    await vi.waitFor(() => expect(verifyCortex).toHaveBeenCalledTimes(1))
    await vi.waitFor(() => expect(screen.getByText(/waiting for the first exchange/)).toBeTruthy())
    expect(screen.queryByRole('button', { name: /Activate Cortex/ })).toBeNull()
  })

  it('without Cortex installed, offers to get it and never starts an operation', async () => {
    getCortexStatus.mockResolvedValue(status({ cortex: 'missing', action: 'install_cortex' }))
    render(<CortexProviderPanel />)
    await vi.waitFor(() => expect(screen.getByRole('button', { name: /Get Cortex/ })).toBeTruthy())
    expect(screen.queryByRole('button', { name: /Activate Cortex/ })).toBeNull()
    expect(startCortexActivation).not.toHaveBeenCalled()
  })

  it('a cancelled operation keeps the previous state and says so', async () => {
    getCortexStatus.mockResolvedValue(status())
    startCortexActivation.mockResolvedValue({
      operationId: 'hop-0123456789abcdef0123456789abcdef',
      deepLink: 'cortex://x',
      expiresAt: ''
    })
    getCortexActivation.mockResolvedValue({ state: 'cancelled' })
    render(<CortexProviderPanel />)
    await vi.waitFor(() => expect(screen.getByRole('button', { name: /Activate Cortex/ })).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: /Activate Cortex/ }))
    await vi.advanceTimersByTimeAsync(1600)
    await vi.waitFor(() => expect(screen.getByText(/Activation cancelled in Cortex/)).toBeTruthy())
    expect(verifyCortex).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: /Activate Cortex/ })).toBeTruthy()
  })
})
