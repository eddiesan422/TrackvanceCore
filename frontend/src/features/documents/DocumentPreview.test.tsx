import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import DocumentPreview from './DocumentPreview'

const first = '/api/v1/documents/originals/one/pages/1'
const second = '/api/v1/documents/originals/one/pages/2'
const success = () => new Response(new Blob(['image'], { type: 'image/jpeg' }), { status: 200 })
const busy = () => new Response(JSON.stringify({ error: { code: 'DOCUMENT_PREVIEW_BUSY' } }), { status: 429 })
const flush = async () => { await act(async () => { await Promise.resolve() }) }
let fetcher: ReturnType<typeof vi.fn<typeof fetch>>
let create: ReturnType<typeof vi.fn>
let revoke: ReturnType<typeof vi.fn>

beforeEach(() => {
  vi.useFakeTimers()
  fetcher = vi.fn<typeof fetch>()
  create = vi.fn().mockReturnValueOnce('blob:first').mockReturnValueOnce('blob:second')
  revoke = vi.fn()
  vi.stubGlobal('fetch', fetcher)
  vi.stubGlobal('URL', Object.assign(class extends URL {}, { createObjectURL: create, revokeObjectURL: revoke }))
})
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals() })

describe('Document preview admission and page ownership', () => {
  it('retries only a busy preview and retains loading until the image loads', async () => {
    fetcher.mockResolvedValueOnce(busy()).mockResolvedValueOnce(success())
    render(<DocumentPreview source={first} alt="Página 1" zoom={125}><span data-testid="region"/></DocumentPreview>)
    await flush()
    expect(screen.getByRole('status')).toHaveTextContent('Cargando página')
    expect(screen.queryByTestId('region')).not.toBeInTheDocument()
    await act(async () => { await vi.advanceTimersByTimeAsync(250) })
    expect(fetcher).toHaveBeenCalledTimes(2)
    expect(fetcher.mock.calls[0][1]).toMatchObject({ credentials: 'same-origin', signal: expect.any(AbortSignal) })
    const image = screen.getByRole('img', { name: 'Página 1' })
    expect(image).toHaveAttribute('src', 'blob:first')
    fireEvent.load(image)
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
    expect(screen.getByTestId('region')).toBeInTheDocument()
    expect(image.parentElement).toHaveStyle({ width: '125%' })
  })

  it('aborts the prior page and rejects a late body even if transport ignores abort', async () => {
    let finishOld!: (blob: Blob) => void
    const oldBody = new Promise<Blob>(resolve => { finishOld = resolve })
    fetcher.mockResolvedValueOnce({ ok: true, blob: () => oldBody } as Response).mockResolvedValueOnce(success())
    const view = render(<DocumentPreview source={first} alt="Página 1" zoom={100}/>)
    await flush()
    const oldSignal = fetcher.mock.calls[0][1]?.signal
    view.rerender(<DocumentPreview source={second} alt="Página 2" zoom={100}/>)
    await flush()
    expect(oldSignal?.aborted).toBe(true)
    expect(screen.getByRole('img', { name: 'Página 2' })).toHaveAttribute('src', 'blob:first')
    await act(async () => { finishOld(new Blob(['obsolete'])) })
    expect(create).toHaveBeenCalledTimes(1)
    expect(screen.getByRole('img', { name: 'Página 2' })).toHaveAttribute('src', 'blob:first')
    view.unmount()
    expect(revoke).toHaveBeenCalledExactlyOnceWith('blob:first')
  })

  it.each([403, 500, 429])('does not retry HTTP %s without the exact preview busy code', async status => {
    fetcher.mockResolvedValue(new Response(JSON.stringify({ error: { code: 'OTHER_ERROR' } }), { status }))
    render(<DocumentPreview source={first} alt="Página 1" zoom={100}/>)
    await flush()
    await act(async () => { await vi.advanceTimersByTimeAsync(60000) })
    expect(fetcher).toHaveBeenCalledTimes(1)
    expect(screen.getByRole('alert')).toHaveTextContent('No se pudo cargar')
    expect(screen.getByRole('button', { name: 'Reintentar vista previa' })).toBeEnabled()
  })

  it('stops after six busy responses and starts a fresh finite attempt only on explicit retry', async () => {
    fetcher.mockImplementation(async () => busy())
    render(<DocumentPreview source={first} alt="Página 1" zoom={100}/>)
    await flush()
    await act(async () => { await vi.advanceTimersByTimeAsync(60000) })
    expect(fetcher).toHaveBeenCalledTimes(6)
    expect(screen.getByRole('alert')).toHaveTextContent('sigue ocupada')
    await act(async () => { await vi.advanceTimersByTimeAsync(60000) })
    expect(fetcher).toHaveBeenCalledTimes(6)
    fetcher.mockResolvedValueOnce(success())
    fireEvent.click(screen.getByRole('button', { name: 'Reintentar vista previa' }))
    await flush()
    expect(fetcher).toHaveBeenCalledTimes(7)
    expect(screen.getByRole('img')).toHaveAttribute('src', 'blob:first')
  })

  it('cancels an obsolete busy wait and revokes each successful page URL on replacement', async () => {
    fetcher.mockResolvedValueOnce(busy()).mockResolvedValueOnce(success()).mockResolvedValueOnce(success())
    const view = render(<DocumentPreview source={first} alt="Página 1" zoom={100}/>)
    await flush()
    view.rerender(<DocumentPreview source={second} alt="Página 2" zoom={100}/>)
    await flush()
    await act(async () => { await vi.advanceTimersByTimeAsync(60000) })
    expect(fetcher).toHaveBeenCalledTimes(2)
    view.rerender(<DocumentPreview source={first} alt="Página 1" zoom={100}/>)
    await flush()
    expect(revoke).toHaveBeenCalledExactlyOnceWith('blob:first')
    expect(screen.getByRole('img')).toHaveAttribute('src', 'blob:second')
    view.unmount()
    expect(revoke.mock.calls).toEqual([['blob:first'], ['blob:second']])
  })
})
