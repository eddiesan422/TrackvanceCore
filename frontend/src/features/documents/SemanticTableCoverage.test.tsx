import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { renderApp } from '../../test/render'
import SemanticTableCoverage from './SemanticTableCoverage'

describe('Explicit semantic collection source review', () => {
  it('shows automatic coverage with its evidence and only asks to confirm the pending collection', () => {
    renderApp(<SemanticTableCoverage definitions={[{ name: 'items' }, { name: 'actions' }]} tables={{ items: [{ amount: '10.00' }], actions: [] }} status={{ tables: [
      { name: 'items', coverage_state: 'AUTOMATICALLY_VERIFIED', review_required: false, proof_refs: [{ id: 'coverage-source', page: 1, stage: 'PDF_NATIVE' }] },
      { name: 'actions', coverage_state: 'PENDING', review_required: true },
    ] }} confirmed={[]} onChange={vi.fn()} canView busy={false}/> )
    expect(screen.getByText('Estado de cobertura guardado: verificada automáticamente')).toBeVisible()
    expect(screen.getByText(/coverage-source/)).toBeVisible()
    expect(screen.getAllByRole('checkbox')).toHaveLength(1)
    expect(screen.getByRole('checkbox', { name: 'Confirmé en el original la cobertura de actions' })).not.toBeChecked()
    expect(screen.queryByText(/confirmado mediante revisión/)).not.toBeInTheDocument()
  })

  it('requires confirmation of an empty collection without equating zero retained rows with documentary absence', async () => {
    const change = vi.fn(), user = userEvent.setup()
    renderApp(<SemanticTableCoverage definitions={[{ name: 'actions' }]} tables={{ actions: [] }} status={{ tables: [{ name: 'actions', review_required: true }] }} confirmed={[]} onChange={change} canView busy={false}/> )
    expect(screen.getByText('Filas conservadas: 0')).toBeVisible()
    expect(screen.getByText('Estado de cobertura guardado: requiere revisión')).toBeVisible()
    const checkbox = screen.getByRole('checkbox', { name: 'Confirmé en el original la cobertura de actions' })
    expect(checkbox).not.toBeChecked()
    await user.click(checkbox)
    expect(change).toHaveBeenCalledWith(['actions'])
  })

  it('keeps a previous receipt distinct from the server current coverage decision', () => {
    renderApp(<SemanticTableCoverage definitions={[{ name: 'items' }]} tables={{ items: [{ amount: '10.00' }] }} status={{ tables: [{ name: 'items', review_required: true, latest_confirmation: { created_at: '2026-11-01T12:00:00Z', reason: 'Revisión anterior' } }] }} confirmed={[]} onChange={vi.fn()} canView busy={false}/> )
    expect(screen.getByText(/Última confirmación:.*Revisión anterior/)).toBeVisible()
    expect(screen.getByText('Estado de cobertura guardado: requiere revisión')).toBeVisible()
    expect(screen.getByRole('checkbox')).not.toBeChecked()
  })

  it('does not allow source confirmation without original access permission', () => {
    renderApp(<SemanticTableCoverage definitions={[{ name: 'items' }]} tables={{ items: [] }} confirmed={[]} onChange={vi.fn()} canView={false} busy={false}/> )
    expect(screen.getByRole('checkbox')).toBeDisabled()
    expect(screen.getByText(/requiere permiso para consultar el original/)).toBeVisible()
  })
})
