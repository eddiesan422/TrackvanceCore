import { screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { renderApp } from '../../test/render'
import DocumentVerification from './DocumentVerification'

const reason = { code: 'INDEPENDENT_SOURCE_REQUIRED', path: 'headers.0.invoice_number' }
const verification = { policy_version: 'TRACKVANCE_AUTOMATIC_VERIFICATION_098_V1', status: 'VERIFYING',
  reasons_before: [reason], remaining_reasons: [reason], resolved_paths: [],
  scope: { kind: 'WHOLE_DOCUMENT', pages: [1, 2] }, primary_reused: true, human_reviewed: false }

describe('Automatic document verification visibility', () => {
  it('shows the additional reading reason, scope and reuse without demanding confirmation', () => {
    renderApp(<DocumentVerification verification={verification}/>)
    expect(screen.getByRole('region', { name: 'Verificación automática de la extracción' })).toBeVisible()
    expect(screen.getByText('Comprobando la extracción')).toBeVisible()
    expect(screen.getByText(/Se reutilizó el reconocimiento original/)).toBeVisible()
    expect(screen.getByText(/documento completo \(2 páginas\)/)).toBeVisible()
    expect(screen.getByText(/No necesitas confirmar campos/)).toBeVisible()
    expect(screen.queryByRole('checkbox')).not.toBeInTheDocument()
  })

  it('distinguishes resolved controls from remaining exceptions and leaves approval to the validator', () => {
    renderApp(<DocumentVerification verification={{ ...verification, status: 'REVIEW_REQUIRED',
      remaining_reasons: [{ code: 'INDEPENDENT_ROW_STRUCTURE_REQUIRED', table: 'detalle' }],
      resolved_paths: ['headers.0.invoice_number'] }}/>)
    expect(screen.getByText('Verificación terminada con excepciones')).toBeVisible()
    expect(screen.getByText('Controles resueltos (1)')).toBeVisible()
    expect(screen.getByText(/Falta comprobar la estructura y continuidad de la tabla: detalle/)).toBeVisible()
    expect(screen.getByText(/Los campos ya verificados no necesitan confirmación/)).toBeVisible()
    expect(screen.queryByText('Aprobación automática verificada')).not.toBeInTheDocument()
  })

  it('explains a frozen semantic opt-out instead of silently changing its configuration', () => {
    renderApp(<DocumentVerification verification={{ ...verification, status: 'BLOCKED',
      blocked_reason: 'QWEN_DISABLED_IN_FROZEN_CONFIGURATION' }}/>)
    expect(screen.getByText(/La configuración congelada no permite la lectura semántica/)).toBeVisible()
    expect(screen.getByText(/este resultado conserva su configuración/)).toBeVisible()
  })

  it('does not label completed controls as human acceptance', () => {
    renderApp(<DocumentVerification verification={{ ...verification, status: 'CONTROLS_COMPLETED', remaining_reasons: [] }}/>)
    expect(screen.getByText('Controles adicionales completados')).toBeVisible()
    expect(screen.getByText(/Esta comprobación no declara revisión humana/)).toBeVisible()
    expect(screen.queryByText(/Controles que siguen pendientes/)).not.toBeInTheDocument()
  })
})
