import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../../api/client'
import { renderApp } from '../../test/render'
import AllDocuments from './AllDocuments'

vi.mock('../../api/client', () => ({ api: vi.fn(), post: vi.fn() }))
const item = { id: 'document-1', filename: 'invoice.pdf', family_name: 'Invoices', received_at: '2026-10-17T10:00:00Z', status: 'PUBLISHED', mode: 'INDIVIDUAL', publication: 'PUBLISHED',
  extraction: { revision: 2, reviewed: true, created_at: '2026-10-17T10:01:00Z' }, batch: { id: 'batch-001', status: 'PUBLISHED', href: '/documents/families/family-1?batch=batch-001' },
  outputs: [{ structure: 'headers', dataset_name: 'Invoice headers', version_id: 'version-1', version: 1, href: '/datasets/dataset-1?version=version-1' }],
  links: { document: '/documents/families/family-1?document=document-1', family: '/documents/families/family-1', extraction: '/documents/families/family-1?document=document-1', review: '/documents/families/family-1?document=document-1', original: '/api/v1/documents/originals/document-1/file' } }

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(api).mockImplementation(async path => path.startsWith('/documents/families?') ? { items: [{ id: 'family-1', name: 'Invoices' }], total: 1 } : { items: [item], total: 51 })
})

describe('All authorized documents', () => {
  it('shows exact document, family, extraction, batch and version links with independent original permission', async () => {
    renderApp(<AllDocuments/>, { permissions: ['documents:read'] })
    expect(await screen.findByRole('link', { name: 'invoice.pdf' })).toHaveAttribute('href', item.links.document)
    expect(screen.getByRole('link', { name: 'Invoices' })).toHaveAttribute('href', item.links.family)
    expect(screen.getByRole('link', { name: 'Lote batch-00' })).toHaveAttribute('href', item.batch.href)
    expect(screen.getByRole('link', { name: 'Invoice headers' })).toHaveAttribute('href', item.outputs[0].href)
    expect(screen.getByText('Revisión humana guardada')).toBeVisible()
    expect(screen.queryByRole('link', { name: 'Abrir original' })).not.toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'Revisar y corregir' })).not.toBeInTheDocument()
  })

  it('sends search, family, filters, order and page to the backend and resets the page on a filter change', async () => {
    const user = userEvent.setup()
    renderApp(<AllDocuments/>, { permissions: ['documents:read', 'documents:review', 'documents:download'] })
    await screen.findByRole('link', { name: 'invoice.pdf' })
    expect(screen.getByRole('link', { name: 'Abrir original' })).toHaveAttribute('href', item.links.original)
    await user.click(screen.getByRole('button', { name: 'Página siguiente' }))
    await waitFor(() => expect(vi.mocked(api).mock.calls.some(([path]) => path.includes('offset=25'))).toBe(true))
    await user.type(screen.getByRole('textbox', { name: 'Buscar nombre, referencia, identificador extraído o familia' }), 'INV-12')
    await user.selectOptions(screen.getByLabelText('Familia documental'), 'family-1')
    await user.selectOptions(screen.getByLabelText('Estado del documento'), 'REVIEW_REQUIRED')
    await user.selectOptions(screen.getByLabelText('Modalidad de recepción'), 'SCHEDULED')
    await user.selectOptions(screen.getByLabelText('Publicación del documento'), 'UNPUBLISHED')
    await user.selectOptions(screen.getByLabelText('Ordenar documentos'), 'filename:asc')
    await waitFor(() => {
      const path = [...vi.mocked(api).mock.calls].reverse().find(([value]) => value.startsWith('/documents/originals?'))?.[0]
      const query = new URLSearchParams(path?.split('?')[1])
      expect(Object.fromEntries(query)).toEqual({ search: 'INV-12', offset: '0', limit: '25', order_by: 'filename', direction: 'asc', family_id: 'family-1', status: 'REVIEW_REQUIRED', mode: 'SCHEDULED', publication: 'UNPUBLISHED' })
    })
  })

  it('does not invent generated dataset links when the backend withholds their metadata', async () => {
    vi.mocked(api).mockImplementation(async path => path.startsWith('/documents/families?') ? { items: [], total: 0 } : { items: [{ ...item, outputs: [] }], total: 1 })
    renderApp(<AllDocuments/>, { permissions: ['documents:read'] })
    expect(await screen.findByText('Sin resultado disponible')).toBeVisible()
    expect(screen.queryByRole('link', { name: 'Invoice headers' })).not.toBeInTheDocument()
  })
})
