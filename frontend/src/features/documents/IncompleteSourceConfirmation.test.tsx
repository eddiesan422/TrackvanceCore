import type { ComponentProps } from 'react'
import { fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import IncompleteSourceConfirmation, { IncompleteSourceNotice } from './IncompleteSourceConfirmation'

const binding = { source_sha256: 'opaque-source', expected_extraction_id: 'extraction-2', expected_payload_hash: 'opaque-payload', expected_marker_hash: 'opaque-marker', pages: [1, 2] }
function fixture(overrides: Partial<ComponentProps<typeof IncompleteSourceConfirmation>> = {}): ComponentProps<typeof IncompleteSourceConfirmation> {
  return { review: { status: 'PARTIAL', failed_stage: 'REFINE', error_code: 'DOCUMENT_TIMEOUT', automatic_approval_blocked: true,
      requires_full_source_review: true, can_confirm: true, confirmation_binding: binding },
    extractionId: binding.expected_extraction_id, payloadHash: binding.expected_payload_hash, sourceSha256: binding.source_sha256,
    cellsReviewed: true, canReview: true, canView: true, pendingChanges: false, busy: false,
    sourceUrl: page => `/actual-source/pages/${page}`, onConfirm: vi.fn().mockResolvedValue(undefined), ...overrides }
}
async function inspectPages(user: ReturnType<typeof userEvent.setup>) {
  for (const page of binding.pages) {
    await user.click(screen.getByRole('button', { name: `Ver página ${page} del original completo` }))
    fireEvent.load(screen.getByRole('img', { name: `Original completo, página ${page}` }))
  }
}
async function acknowledge(user: ReturnType<typeof userEvent.setup>) {
  await inspectPages(user)
  await user.click(screen.getByRole('checkbox', { name: 'He revisado completas todas las páginas del original.' }))
  await user.click(screen.getByRole('checkbox', { name: /He comprobado la totalidad de filas/ }))
  await user.type(screen.getByLabelText('Motivo de la confirmación de fuente completa'), '  Revisé todas las páginas, valores y filas contra la fuente completa.  ')
}

describe('Full-source manual resolution of a partial candidate', () => {
  it('keeps normal documents untouched and labels resolved partial history as manual', () => {
    const { rerender } = render(<IncompleteSourceConfirmation {...fixture({ review: undefined })}/>)
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
    rerender(<IncompleteSourceNotice review={{ ...fixture().review!, status: 'HUMAN_RESOLVED', can_confirm: false, requires_full_source_review: false }}/>)
    expect(screen.getByText(/Fuente completa confirmada mediante revisión manual/)).toBeVisible()
    expect(screen.queryByText(/Aprobación automática/)).not.toBeInTheDocument()
  })

  it('requires actual page loads, both acknowledgments and reason, then echoes only the server binding', async () => {
    const opaqueExtras = { ...binding, actor_id: 'untrusted-extra-actor', quality: 'AUTO_VERIFIED' }
    const user = userEvent.setup(), props = fixture({ review: { ...fixture().review!, confirmation_binding: opaqueExtras } })
    const before = JSON.stringify(props.review)
    render(<IncompleteSourceConfirmation {...props}/>)
    const submit = screen.getByRole('button', { name: 'Confirmar fuente completa mediante revisión manual' })
    expect(submit).toBeDisabled()
    await user.click(screen.getByRole('button', { name: 'Ver página 1 del original completo' }))
    expect(screen.getByRole('checkbox', { name: /He revisado completas todas/ })).toBeDisabled()
    await acknowledge(user)
    expect(submit).toBeEnabled()
    await user.click(submit)
    expect(props.onConfirm).toHaveBeenCalledWith({ ...binding, pages: [1, 2], all_pages_confirmed: true, all_rows_confirmed: true }, 'Revisé todas las páginas, valores y filas contra la fuente completa.')
    expect(JSON.stringify(props.review)).toBe(before)
    expect(submit).toBeDisabled()
  })

  it.each(['server', 'cells', 'edits', 'download', 'busy'] as const)('cannot confirm when %s prerequisites are unavailable', async condition => {
    const user = userEvent.setup(), props = fixture()
    if (condition === 'server') props.review = { ...props.review!, can_confirm: false }
    if (condition === 'cells') props.cellsReviewed = false
    if (condition === 'edits') props.pendingChanges = true
    if (condition === 'download') props.canView = false
    if (condition === 'busy') props.busy = true
    render(<IncompleteSourceConfirmation {...props}/>)
    expect(screen.getByRole('button', { name: 'Confirmar fuente completa mediante revisión manual' })).toBeDisabled()
    await user.click(screen.getByRole('checkbox', { name: /He revisado completas todas/ }))
    expect(props.onConfirm).not.toHaveBeenCalled()
  })

  it('requires review permission and rejects cross-document, stale payload or malformed page bindings', () => {
    const props = fixture({ canReview: false })
    const { rerender } = render(<IncompleteSourceConfirmation {...props}/>)
    expect(screen.queryByRole('checkbox')).not.toBeInTheDocument()
    for (const overrides of [{ sourceSha256: 'another-source' }, { payloadHash: 'changed-payload' }, { extractionId: 'changed-extraction' }]) {
      rerender(<IncompleteSourceConfirmation {...fixture(overrides)}/>)
      expect(screen.getByText(/La vinculación con la revisión actual/)).toBeVisible()
      expect(screen.queryByRole('button')).not.toBeInTheDocument()
    }
    rerender(<IncompleteSourceConfirmation {...fixture({ review: { ...fixture().review!, confirmation_binding: { ...binding, pages: [1, 1] } } })}/>)
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
  })

  it('resets every source acknowledgment and loaded page when the current revision binding changes', async () => {
    const user = userEvent.setup(), props = fixture()
    const { rerender } = render(<IncompleteSourceConfirmation {...props}/>)
    await acknowledge(user)
    const next = { ...binding, expected_extraction_id: 'extraction-3', expected_payload_hash: 'next-payload', expected_marker_hash: 'next-marker' }
    rerender(<IncompleteSourceConfirmation {...props} extractionId={next.expected_extraction_id} payloadHash={next.expected_payload_hash} review={{ ...props.review!, confirmation_binding: next }}/>)
    expect(screen.getByText('Páginas consultadas: 0 de 2.')).toBeVisible()
    expect(screen.getByLabelText('Motivo de la confirmación de fuente completa')).toHaveValue('')
    expect(screen.getByRole('button', { name: 'Confirmar fuente completa mediante revisión manual' })).toBeDisabled()
    expect(props.onConfirm).not.toHaveBeenCalled()
  })

  it('keeps an unsuccessful source image unavailable and clears consent on a server conflict', async () => {
    const user = userEvent.setup(), props = fixture({ onConfirm: vi.fn().mockRejectedValue(new Error('La revisión cambió; recarga el original.')) })
    render(<IncompleteSourceConfirmation {...props}/>)
    await user.click(screen.getByRole('button', { name: 'Ver página 1 del original completo' }))
    fireEvent.error(screen.getByRole('img', { name: 'Original completo, página 1' }))
    expect(screen.getByText('Páginas consultadas: 0 de 2.')).toBeVisible()
    await acknowledge(user)
    await user.click(screen.getByRole('button', { name: 'Confirmar fuente completa mediante revisión manual' }))
    expect(await screen.findByText(/La revisión cambió; recarga el original/)).toBeVisible()
    expect(screen.getByText('Páginas consultadas: 0 de 2.')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Confirmar fuente completa mediante revisión manual' })).toBeDisabled()
  })
})
