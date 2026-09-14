import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { api } from '../../api/client'
import { renderApp } from '../../test/render'
import { UploadDialog } from './Datasets'

vi.mock('../../api/client', async importOriginal => ({ ...await importOriginal<typeof import('../../api/client')>(), api: vi.fn(), post: vi.fn() }))

describe('Identifier override during upload', () => {
  it('sends explicit identifier tags with the untouched CSV bytes', async () => {
    vi.mocked(api).mockResolvedValue({ id: 'version' })
    const user = userEvent.setup()
    renderApp(<UploadDialog open onClose={vi.fn()} datasetId="customers" datasetName="Clientes"/>)
    const file = new File(['document_id,name\n001234567,Cliente\n1234567,Otro\n'], 'customers.csv', { type: 'text/csv' })
    await user.upload(screen.getByLabelText('Seleccionar archivo CSV'), file)
    await user.type(screen.getByLabelText('Columnas identificadoras (opcional)'), 'document_id, codigo')
    await user.click(screen.getByRole('button', { name: 'Cargar y analizar' }))
    await waitFor(() => expect(api).toHaveBeenCalled())
    const [path, options] = vi.mocked(api).mock.calls[0]
    expect(path).toBe('/datasets/customers/versions/upload')
    const body = options?.body as FormData
    expect(body.get('file')).toBe(file)
    expect(JSON.parse(String(body.get('column_overrides')))).toEqual({ document_id: { logical_type: 'STRING', semantic_tag: 'IDENTIFIER' }, codigo: { logical_type: 'STRING', semantic_tag: 'IDENTIFIER' } })
  })
})
