import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { post } from '../../api/client'
import { renderApp } from '../../test/render'
import DocumentRecognition from './DocumentRecognition'

vi.mock('../../api/client', async importOriginal => ({ ...await importOriginal<typeof import('../../api/client')>(), post: vi.fn() }))
const item = { id: 'source-1', status: 'REVIEW_REQUIRED', extraction: { id: 'extraction-1' }, recognition_artifacts: [{ artifact_id: 'raw-1', attempt_id: 'attempt-1', stage: 'PADDLE', page: 1, kind: 'TABLE_HTML', sha256: 'immutable-raw', media_type: 'text/html', bytes: 120 }] }
beforeEach(() => vi.clearAllMocks())

describe('Durable recognition results and replay', () => {
  it('queues native verification with both extraction and settings guards without declaring human review', async () => {
    const user = userEvent.setup(), refresh = vi.fn()
    vi.mocked(post).mockResolvedValue({ status: 'QUEUED', extraction: item.extraction })
    renderApp(<DocumentRecognition document={{ ...item, verification_action: { available: true, configuration_version: 4 } }} canDownload canReplay refresh={refresh}/>)
    await user.type(screen.getByLabelText('Motivo de la verificación automática'), 'Completar controles con evidencia independiente')
    await user.click(screen.getByRole('button', { name: 'Completar comprobaciones pendientes' }))
    await waitFor(() => expect(post).toHaveBeenCalledWith('/documents/originals/source-1/verify', {
      expected_extraction_id: 'extraction-1', expected_settings_version: 4, reason: 'Completar controles con evidencia independiente',
    }))
    expect(await screen.findByText(/Estado: QUEUED/)).toBeVisible()
    expect(screen.getByText(/no se declara una revisión humana/)).toBeVisible()
    expect(refresh).toHaveBeenCalledOnce()
  })

  it('shows a concrete configuration block and cannot queue a control while unavailable', () => {
    renderApp(<DocumentRecognition document={{ ...item, verification_action: { available: false, configuration_version: 1, message: 'Guarda una configuración nueva para completar los controles.' } }} canDownload canReplay refresh={vi.fn()}/>)
    expect(screen.getByRole('button', { name: 'Completar comprobaciones pendientes' })).toBeDisabled()
    expect(screen.getByText('Guarda una configuración nueva para completar los controles.')).toBeVisible()
    expect(post).not.toHaveBeenCalled()
  })

  it('requests a new adaptation from saved results and retains the original result link', async () => {
    const user = userEvent.setup(), refresh = vi.fn()
    vi.mocked(post).mockResolvedValue({ status: 'REVIEW_REQUIRED', extraction: { revision: 2 } })
    renderApp(<DocumentRecognition document={item} canDownload canReplay refresh={refresh}/>)
    await user.click(screen.getByText('Resultados por página y etapa (1)'))
    expect(screen.getByRole('link', { name: 'Descargar resultado original de página 1' })).toHaveAttribute('href', '/api/v1/documents/originals/source-1/recognition-artifacts/raw-1/content')
    await user.type(screen.getByLabelText('Motivo de la nueva adaptación'), 'Reaplicar la revisión de reglas sin nuevo reconocimiento')
    await user.click(screen.getByRole('button', { name: 'Adaptar resultados guardados sin nueva inferencia' }))
    await waitFor(() => expect(post).toHaveBeenCalledWith('/documents/originals/source-1/replay', { expected_extraction_id: 'extraction-1', reason: 'Reaplicar la revisión de reglas sin nuevo reconocimiento' }))
    expect(await screen.findByText(/Adaptación guardada como revisión 2/)).toBeVisible()
    expect(refresh).toHaveBeenCalledOnce()
    expect(screen.queryByRole('iframe')).not.toBeInTheDocument()
  })

  it('keeps active recognition fenced and honors download permission separately', async () => {
    const user = userEvent.setup()
    renderApp(<DocumentRecognition document={{ ...item, status: 'CANCELLED', execution: { resources_released: false } }} canDownload={false} canReplay refresh={vi.fn()}/>)
    await user.click(screen.getByText('Resultados por página y etapa (1)'))
    expect(screen.queryByRole('link')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Adaptar resultados guardados sin nueva inferencia' })).toBeDisabled()
    expect(screen.getByText(/Espera el cierre de la ejecución activa/)).toBeVisible()
    expect(post).not.toHaveBeenCalled()
  })
})
