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
  { id: 'PADDLE_QWEN_LOCAL', name: 'Paddle + Qwen local', status: 'UNAVAILABLE', available: false, execution_enabled: false, model: 'PaddleOCR-VL-1.6 + Qwen3.5', reason: 'Servicios locales sin conexión' },
  { id: 'AWS_TEXTRACT', name: 'Amazon Textract', status: 'PREPARED', available: false, execution_enabled: false, model: 'AnalyzeExpense', reason: 'Habilitación real bloqueada' },
] }

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(api).mockResolvedValue(result)
})

describe('Document engine configuration', () => {
  it('requires an explicit new automatic policy and preserves the semantic opt-out', async () => {
    const user = userEvent.setup()
    vi.mocked(api).mockResolvedValue({ ...result, configuration: { ...configuration, provider: 'PADDLE_QWEN_LOCAL', extraction_strategy: 'PADDLE_STRUCTURED', use_llm: false }, providers: result.providers.map(provider => provider.id === 'PADDLE_QWEN_LOCAL' ? { ...provider, status: 'AVAILABLE', available: true, execution_enabled: true } : provider) })
    renderApp(<DocumentEngineSettings familyId="family-1"/>, { permissions: ['documents:read', 'documents:configure'] })
    const automatic = await screen.findByRole('checkbox', { name: 'Completar controles pendientes automáticamente' })
    expect(automatic).not.toBeChecked()
    await user.click(automatic)
    expect(screen.getByRole('checkbox', { name: 'Interpretación semántica cuando sea necesaria' })).not.toBeChecked()
    expect(screen.getByText(/como máximo una lectura de un mecanismo distinto/)).toBeVisible()
    await user.type(screen.getByLabelText('Motivo del cambio de motor'), 'Comprobaciones locales acotadas para documentos nuevos')
    await user.click(screen.getByRole('button', { name: 'Guardar configuración auditada' }))
    await waitFor(() => expect(api).toHaveBeenCalledWith('/documents/engines?family_id=family-1', expect.objectContaining({
      method: 'PUT', body: JSON.stringify({ expected_version: 3, configuration: { ...configuration, provider: 'PADDLE_QWEN_LOCAL', extraction_strategy: 'PADDLE_STRUCTURED', use_llm: false, automatic_verification: 'BOUNDED_LOCAL_V1' }, reason: 'Comprobaciones locales acotadas para documentos nuevos' }),
    })))
  })

  it('shows observed availability and keeps prepared AWS execution disabled', async () => {
    renderApp(<DocumentEngineSettings/>, { permissions: ['documents:read', 'documents:configure'] })
    expect(await screen.findByRole('heading', { name: 'Motor y disponibilidad' })).toBeVisible()
    expect(screen.getByText('Servicios locales sin conexión')).toBeVisible()
    expect(screen.getByRole('option', { name: /Amazon Textract/ })).toBeDisabled()
    expect(screen.getByText(/Las llamadas reales a AWS permanecen bloqueadas/)).toBeVisible()
    expect(screen.getByLabelText('Modelo semántico')).toHaveAttribute('readonly')
    for (const name of ['Tiempo máximo (segundos)', 'Contexto máximo (tokens)', 'Reintentos máximos', 'Trabajos simultáneos', 'Interpretación semántica cuando sea necesaria']) expect(screen.getByLabelText(name)).toBeDisabled()
    expect(screen.getByText(/Tesseract conserva su límite nativo de 120 segundos/)).toBeVisible()
    expect(screen.getByRole('button', { name: 'Guardar configuración auditada' })).toBeDisabled()
  })

  it('sends the exact optimistic version, configuration and audit reason for a family override', async () => {
    const user = userEvent.setup()
    vi.mocked(api).mockResolvedValue({ ...result, providers: result.providers.map(provider => provider.id === 'PADDLE_QWEN_LOCAL' ? { ...provider, status: 'AVAILABLE', available: true, execution_enabled: true } : provider) })
    renderApp(<DocumentEngineSettings familyId="family-1"/>, { permissions: ['documents:read', 'documents:configure'] })
    await screen.findByLabelText('Proveedor documental')
    await user.selectOptions(screen.getByLabelText('Proveedor documental'), 'PADDLE_QWEN_LOCAL')
    for (const name of ['Tiempo máximo (segundos)', 'Contexto máximo (tokens)', 'Reintentos máximos', 'Trabajos simultáneos', 'Interpretación semántica cuando sea necesaria']) expect(screen.getByLabelText(name)).toBeEnabled()
    await user.clear(screen.getByLabelText('Tiempo máximo (segundos)'))
    await user.type(screen.getByLabelText('Tiempo máximo (segundos)'), '180')
    await user.selectOptions(screen.getByLabelText('Proveedor documental'), 'TESSERACT_LEGACY')
    expect(screen.getByLabelText('Tiempo máximo (segundos)')).toBeDisabled()
    expect(screen.getByLabelText('Tiempo máximo (segundos)')).toHaveValue(180)
    await user.selectOptions(screen.getByLabelText('Proveedor documental'), 'PADDLE_QWEN_LOCAL')
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

  it('saves a new strategy only after an explicit selection and leaves historical work unchanged', async () => {
    const user = userEvent.setup()
    vi.mocked(api).mockResolvedValue({ ...result, configuration: { ...configuration, provider: 'PADDLE_QWEN_LOCAL' }, providers: result.providers.map(provider => provider.id === 'PADDLE_QWEN_LOCAL' ? { ...provider, status: 'AVAILABLE', available: true, execution_enabled: true } : provider) })
    renderApp(<DocumentEngineSettings familyId="family-1"/>, { permissions: ['documents:read', 'documents:configure'] })
    expect(await screen.findByLabelText('Estrategia de extracción')).toHaveValue('LEGACY')
    await user.selectOptions(screen.getByLabelText('Estrategia de extracción'), 'NATIVE_FIRST')
    await user.type(screen.getByLabelText('Motivo del cambio de motor'), 'Prueba aislada con ruta visual explícita')
    await user.click(screen.getByRole('button', { name: 'Guardar configuración auditada' }))
    await waitFor(() => expect(api).toHaveBeenCalledWith('/documents/engines?family_id=family-1', expect.objectContaining({ method: 'PUT', body: JSON.stringify({ expected_version: 3, configuration: { ...configuration, provider: 'PADDLE_QWEN_LOCAL', extraction_strategy: 'NATIVE_FIRST' }, reason: 'Prueba aislada con ruta visual explícita' }) })))
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

  it('prepares the visual context only from an untouched legacy default and preserves an explicit user context', async () => {
    const user = userEvent.setup()
    vi.mocked(api).mockResolvedValue({ ...result, configuration: { ...configuration, provider: 'PADDLE_QWEN_LOCAL', context_tokens: 8192 }, providers: result.providers.map(provider => provider.id === 'PADDLE_QWEN_LOCAL' ? { ...provider, status: 'AVAILABLE', available: true, execution_enabled: true } : provider) })
    renderApp(<DocumentEngineSettings/>, { permissions: ['documents:read', 'documents:configure'] })
    const context = await screen.findByLabelText('Contexto máximo (tokens)')
    expect(context).toHaveValue(8192)
    await user.selectOptions(screen.getByLabelText('Estrategia de extracción'), 'QWEN_VISUAL')
    expect(context).toHaveValue(32768)
    expect(screen.getByText(/Se preparó un contexto de 32768/)).toBeVisible()
    await user.selectOptions(screen.getByLabelText('Estrategia de extracción'), 'LEGACY')
    expect(context).toHaveValue(8192)
    await user.selectOptions(screen.getByLabelText('Estrategia de extracción'), 'SELECTIVE')
    expect(context).toHaveValue(32768)
    await user.clear(context); await user.type(context, '16000')
    await user.selectOptions(screen.getByLabelText('Estrategia de extracción'), 'NATIVE_FIRST')
    expect(context).toHaveValue(16000)
    expect(vi.mocked(api).mock.calls.some(([, options]) => options?.method === 'PUT')).toBe(false)
  })
})
