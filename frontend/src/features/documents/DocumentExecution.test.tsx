import { screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { renderApp } from '../../test/render'
import DocumentExecution from './DocumentExecution'

describe('Observed document execution', () => {
  it('shows frozen engine settings and observed timing without inventing human review', () => {
    renderApp(<DocumentExecution document={{ status: 'RUNNING', phase: 'VALIDATION', progress: 75, quality: { status: 'AUTO_VERIFIED', validator_version: 'idp098-v1' }, engine_snapshot: { provider: 'PADDLE_QWEN_LOCAL', paddle_model: 'PaddleOCR-VL-1.6', llm_model: 'Qwen3.5 9B', use_llm: true, configuration_version: 4, snapshot_sha256: 'immutable-snapshot' }, extraction: { engine: { resources: { elapsed_seconds: 2.5 } } } }}/>)
    expect(screen.getByText('Fase: Validación independiente')).toBeVisible()
    expect(screen.getByRole('progressbar')).toHaveAttribute('value', '75')
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
})
