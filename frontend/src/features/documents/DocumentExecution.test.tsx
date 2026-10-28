import { screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { renderApp } from '../../test/render'
import DocumentExecution from './DocumentExecution'

describe('Observed document execution', () => {
  it('shows the pending verification snapshot and current journal instead of an old completed result', () => {
    renderApp(<DocumentExecution document={{ status: 'QUEUED',
      engine_snapshot: { provider: 'PADDLE_QWEN_LOCAL', configuration_version: 1, snapshot_sha256: 'original-snapshot' },
      verification_configuration: { provider: 'PADDLE_QWEN_LOCAL', configuration_version: 2, snapshot_sha256: 'verification-snapshot', base_snapshot_sha256: 'original-snapshot' },
      extraction: { engine: { verification: { status: 'CONTROLS_COMPLETED', remaining_reasons: [] } } },
      execution: { stage: 'VERIFICATION_REQUEST', verification: { status: 'QUEUED', reasons_before: [], remaining_reasons: [], primary_reused: true, scope: { kind: 'WHOLE_DOCUMENT', pages: [1] } } },
    }}/>)
    expect(screen.getByText('Verificación en cola')).toBeVisible()
    expect(screen.queryByText('Controles adicionales completados')).not.toBeInTheDocument()
    expect(screen.getByText(/Configuración congelada · versión 2 · verification-snapshot/)).toBeVisible()
    expect(screen.getByText(/el motor original conserva su snapshot anterior/)).toBeVisible()
  })

  it('shows frozen engine settings and observed timing without inventing human review', () => {
    renderApp(<DocumentExecution document={{ status: 'RUNNING', phase: 'VALIDATION', progress: 75, quality: { status: 'AUTO_VERIFIED', validator_version: 'idp098-v1' }, engine_snapshot: { provider: 'PADDLE_QWEN_LOCAL', paddle_model: 'PaddleOCR-VL-1.6', llm_model: 'Qwen3.5 9B', use_llm: true, configuration_version: 4, snapshot_sha256: 'immutable-snapshot' }, extraction: { engine: { resources: { elapsed_seconds: 2.5 } } } }}/>)
    expect(screen.getByText('Fase: Validación independiente')).toBeVisible()
    expect(screen.queryByRole('progressbar')).not.toBeInTheDocument()
    expect(screen.getByText(/Configuración congelada · versión 4 · immutable-snapshot/)).toBeVisible()
    expect(screen.getByText(/Tiempo de extracción observado/)).toBeVisible()
    expect(screen.queryByText('Revisión humana verificada')).not.toBeInTheDocument()
  })

  it('does not infer approval or performance when observations are unavailable', () => {
    renderApp(<DocumentExecution document={{ status: 'RUNNING' }}/>)
    expect(screen.queryByLabelText('Ejecución documental')).not.toBeInTheDocument()
    expect(screen.queryByRole('progressbar')).not.toBeInTheDocument()
  })

  it('distinguishes independently validated human revisions from automatic approval', () => {
    renderApp(<DocumentExecution document={{ quality: { status: 'HUMAN_VERIFIED' } }}/>)
    expect(screen.getByText('Revisión humana verificada')).toBeVisible()
    expect(screen.queryByText('Aprobación automática verificada')).not.toBeInTheDocument()
  })

  it('preserves the failed stage and blocks automatic presentation for an incomplete candidate', () => {
    renderApp(<DocumentExecution document={{ quality: { status: 'AUTO_VERIFIED' }, incomplete_inference: { status: 'PARTIAL', failed_stage: 'INTERPRET', error_code: 'DOCUMENT_TIMEOUT', automatic_approval_blocked: true, requires_full_source_review: true, can_confirm: false } }}/>)
    expect(screen.getByText(/tiempo máximo en interpretación semántica/)).toBeVisible()
    expect(screen.getByText(/La publicación está bloqueada/)).toBeVisible()
    expect(screen.queryByText('Aprobación automática verificada')).not.toBeInTheDocument()
  })

  it('shows emitted and pending pages independently from validation and publication', () => {
    renderApp(<DocumentExecution document={{ status: 'RUNNING', progress: 95, phase: 'OCR', execution: { attempt_id: 'attempt-2', stage: 'QWEN_VISUAL', expected_pages: [1, 2, 3], emitted_pages: [1], pending_pages: [2, 3], recognition_complete: false, validation_complete: false, publication_complete: false, resources_released: false } }}/>)
    expect(screen.getByText('Fase: Reconocimiento Qwen visual')).toBeVisible()
    expect(screen.getByText('Páginas conservadas: 1/3 · pendientes: 2, 3')).toBeVisible()
    expect(screen.getByText('Reconocimiento: incompleto o pendiente')).toBeVisible()
    expect(screen.getByText('Publicación: no realizada')).toBeVisible()
    expect(screen.getByText('Recursos de ejecución: pendientes de liberar')).toBeVisible()
    expect(screen.queryByRole('progressbar')).not.toBeInTheDocument()
  })

  it('cannot present an old automatic approval while the current recognition is incomplete', () => {
    renderApp(<DocumentExecution document={{ status: 'RUNNING', quality: { status: 'AUTO_VERIFIED' }, execution: { stage: 'PADDLE', recognition_complete: false, expected_pages: [1, 2], emitted_pages: [1], pending_pages: [2] } }}/>)
    expect(screen.getByText('Requiere revisión')).toBeVisible()
    expect(screen.queryByText('Aprobación automática verificada')).not.toBeInTheDocument()
  })
})
