import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { post } from '../../api/client'
import { renderApp } from '../../test/render'
import DocumentPublication from './DocumentPublication'

vi.mock('../../api/client', async importOriginal => ({ ...await importOriginal<typeof import('../../api/client')>(), post: vi.fn() }))
const item = { id: 'original', family_id: 'family', template_id: 'template', status: 'REVIEWED', issues: [], extraction: { id: 'revision-2' }, publications: [] }
beforeEach(() => vi.clearAllMocks())

describe('Explicit versioned publication', () => {
  it('uses one stable idempotency key for repeated requests and displays the actual blocked outcome', async () => {
    const user = userEvent.setup(), refresh = vi.fn()
    vi.mocked(post).mockResolvedValue({ id: 'batch-1', status: 'BLOCKED' })
    renderApp(<DocumentPublication document={item} canPublish refresh={refresh}/>)
    const button = screen.getByRole('button', { name: 'Publicar esta revisión en datasets' })
    await user.click(button)
    expect(await screen.findByText(/Estado de publicación: BLOCKED/)).toBeVisible()
    await waitFor(() => expect(button).toBeEnabled())
    await user.click(button)
    const first = vi.mocked(post).mock.calls[0]
    expect(first[0]).toBe('/documents/families/family/batches')
    expect(first[1]).toEqual({ idempotency_key: expect.any(String), template_id: 'template', document_ids: ['original'] })
    expect(vi.mocked(post).mock.calls[1][1]).toEqual(first[1])
    expect(screen.queryByText(/publicación realizada/i)).not.toBeInTheDocument()
  })

  it('explains the partial and critical-exception blocks before making a request', () => {
    const { unmount } = renderApp(<DocumentPublication document={{ ...item, incomplete_inference: { status: 'PARTIAL' } }} canPublish refresh={vi.fn()}/>)
    expect(screen.getByRole('button', { name: 'Publicar esta revisión en datasets' })).toBeDisabled()
    expect(screen.getByText(/El reconocimiento está incompleto/)).toBeVisible()
    unmount()
    const critical = renderApp(<DocumentPublication document={{ ...item, issues: ['CRITICAL_CONFLICT'] }} canPublish refresh={vi.fn()}/>)
    expect(screen.getByText(/Hay 1 controles pendientes/)).toBeVisible()
    critical.unmount()
    renderApp(<DocumentPublication document={{ ...item, execution: { resources_released: false } }} canPublish refresh={vi.fn()}/>)
    expect(screen.getByRole('button', { name: 'Publicar esta revisión en datasets' })).toBeDisabled()
    expect(screen.getByText(/todavía no ha liberado sus recursos/)).toBeVisible()
    expect(post).not.toHaveBeenCalled()
  })
})
