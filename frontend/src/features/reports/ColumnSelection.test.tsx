import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, it } from 'vitest'
import { renderApp } from '../../test/render'
import { ReportBuilder } from './Reports'
import { emptyDraft, selectColumns } from './types'

it('preserves edited order and aliases and avoids collisions without silently clipping', () => {
  const current = [{ source_alias: 'b', column: 'id', alias: 'a_id' }]
  const sources = [{ alias: 'a', columns: [{ name: 'id' }, { name: 'other' }] }, { alias: 'b', columns: [{ name: 'id' }] }]
  const selected = selectColumns(current, sources, true)
  expect(selected).toEqual([current[0], { source_alias: 'a', column: 'id', alias: 'a_id_2' }, { source_alias: 'a', column: 'other', alias: 'a_other' }])
  expect(selectColumns(selected, sources, true)).toEqual(selected)
  expect(selectColumns(selected, [sources[0]], false)).toEqual(current)
  expect(selectColumns([], [{ alias: 'wide', columns: Array.from({ length: 101 }, (_, i) => ({ name: `c${i}` })) }], true)).toHaveLength(101)
})

it('supports keyboard selection with mixed state and all columns hidden by a visual filter', async () => {
  const initial = { ...emptyDraft(), sources: [{ alias: 'a', input_dataset_id: 'in', contract_id: 'contract', contract_revision_ids: ['r'], policy: 'LATEST_APPROVED' as const }], columns: [{ source_alias: 'a', column: 'id', alias: 'edited' }], expected_schemas: { a: [{ name: 'id' }, { name: 'other' }] } }
  renderApp(<ReportBuilder initial={initial}/>, { permissions: ['reports:read'] })
  const user = userEvent.setup()
  await user.click(screen.getByRole('button', { name: '3. Columnas y filtros' }))
  const checkbox = screen.getByRole('checkbox', { name: 'Selección de todas las fuentes' })
  expect(checkbox).toHaveAttribute('aria-checked', 'mixed')
  await user.type(screen.getByPlaceholderText('Filtrar columnas visibles…'), 'id')
  checkbox.focus(); await user.keyboard(' ')
  expect(screen.getByText('todas las fuentes: 2 / 2 seleccionadas')).toBeInTheDocument()
  expect(screen.getByDisplayValue('edited')).toBeInTheDocument()
})
