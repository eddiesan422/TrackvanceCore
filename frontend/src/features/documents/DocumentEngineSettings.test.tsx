import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../../api/client'
import { renderApp } from '../../test/render'
import DocumentEngineSettings from './DocumentEngineSettings'

vi.mock('../../api/client', async importOriginal => ({ ...await importOriginal<typeof import('../../api/client')>(), api: vi.fn() }))
const configuration = { provider: 'TESSERACT_LEGACY', paddle_model: 'PaddleOCR-VL-1.6', llm_model: 'qwen3.5:9b-q4_K_M', prompt_version: 'idp098-v1', timeout_seconds: 120, context_tokens: 4096, max_retries: 1, max_parallel_jobs: 1, use_llm: true }
const result = { configuration, version: 3, scope: 'ORGANIZATION', family_id: null, can_configure: true, providers: [
  { id: 'TESSERACT_LEGACY', name: 'Tesseract compatible', status: 'AVAILABLE', available: true, execution_enabled: true, model: 'Tesseract CPU' },
  { id: 'PADDLE_QWEN_LOCAL', name: 'Paddle + Qwen local', status: 'UNAVAILABLE', available: false, execution_enabled: true, model: 'PaddleOCR-VL-1.6 + Qwen3.5', reason: 'Servicios locales sin conexión' },
  { id: 'AWS_TEXTRACT', name: 'Amazon Textract', status: 'PREPARED', available: false, execution_enabled: false, model: 'AnalyzeExpense', reason: 'Habilitación real bloqueada' },
] }

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(api).mockResolvedValue(result)
})

describe('Document engine configuration', () => {
  it('shows observed availability and keeps prepared AWS execution disabled', async () => {
    renderApp(<DocumentEngineSettings/>, { permissions: ['documents:read', 'documents:configure'] })
    expect(await screen.findByRole('heading', { name: 'Motor y disponibilidad' })).toBeVisible()
    expect(screen.getByText('Servicios locales sin conexión')).toBeVisible()
    expect(screen.getByRole('option', { name: /Amazon Textract/ })).toBeDisabled()
    expect(screen.getByText(/Las llamadas reales a AWS permanecen bloqueadas/)).toBeVisible()
    expect(screen.getByLabelText('Modelo semántico')).toHaveAttribute('readonly')
    expect(screen.getByRole('button', { name: 'Guardar configuración auditada' })).toBeDisabled()
  })

  it('sends the exact optimistic version, configuration and audit reason for a family override', async () => {
    const user = userEvent.setup()
    renderApp(<DocumentEngineSettings familyId="family-1"/>, { permissions: ['documents:read', 'documents:configure'] })
    await screen.findByLabelText('Proveedor documental')
    await user.selectOptions(screen.getByLabelText('Proveedor documental'), 'PADDLE_QWEN_LOCAL')
    await user.clear(screen.getByLabelText('Tiempo máximo (segundos)'))
    await user.type(screen.getByLabelText('Tiempo máximo (segundos)'), '180')
    await user.type(screen.getByLabelText('Motivo del cambio de motor'), ' Validated local model selection ')
    await user.click(screen.getByRole('button', { name: 'Guardar configuración auditada' }))
    await waitFor(() => expect(api).toHaveBeenCalledWith('/documents/engines?family_id=family-1', {
      method: 'PUT', body: JSON.stringify({ expected_version: 3, configuration: { ...configuration, provider: 'PADDLE_QWEN_LOCAL', timeout_seconds: 180 }, reason: 'Validated local model selection' }),
    }))
  })

  it('uses server administrative authorization in addition to document permissions', async () => {
    vi.mocked(api).mockResolvedValue({ ...result, can_configure: false })
    renderApp(<DocumentEngineSettings/>, { permissions: ['documents:read', 'documents:configure'] })
    expect(await screen.findByLabelText('Proveedor documental')).toBeDisabled()
    expect(screen.queryByRole('button', { name: 'Guardar configuración auditada' })).not.toBeInTheDocument()
  })

  it('keeps a rejected save visible without reporting success or changing frozen work', async () => {
    const user = userEvent.setup()
    vi.mocked(api).mockImplementation(async (_path, options) => { if (options?.method === 'PUT') throw new Error('La configuración cambió; actualiza antes de guardar.'); return result })
    renderApp(<DocumentEngineSettings/>, { permissions: ['documents:read', 'documents:configure'] })
    await user.type(await screen.findByLabelText('Motivo del cambio de motor'), 'Concurrent update')
    await user.click(screen.getByRole('button', { name: 'Guardar configuración auditada' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('La configuración cambió')
    expect(screen.queryByText(/Configuración guardada/)).not.toBeInTheDocument()
  })
})
