import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { expect, it } from 'vitest'
import { FilterBuilder } from './FilterBuilder'
import type { FilterGroup } from './types'

function Harness() {
  const [group, setGroup] = useState<FilterGroup>({ operator: 'AND', conditions: [{ source_alias: 'a', column: 'code', operator: 'EQ', value: '' }] })
  return <><FilterBuilder value={group} onChange={setGroup} title="Filtro" columns={[{ source_alias: 'a', name: 'code', logical_type: 'STRING', semantic_tag: 'IDENTIFIER' }, { source_alias: 'a', name: 'amount', logical_type: 'DECIMAL' }, { source_alias: 'a', name: 'flag', logical_type: 'BOOLEAN' }]}/><output>{JSON.stringify(group)}</output></>
}

it('R080-03 offers literal text operators only for STRING and preserves special characters', async () => {
  render(<Harness/>); const user = userEvent.setup()
  const operator = screen.getByRole('combobox', { name: 'Filtro, condición 1: operador' })
  expect(within(operator).getByRole('option', { name: 'Contiene' })).toBeInTheDocument()
  await user.selectOptions(operator, 'CONTAINS')
  await user.type(screen.getByRole('textbox', { name: 'Filtro, condición 1: valor' }), "%_'\\A")
  expect(screen.getByRole('status')).toHaveTextContent('CONTAINS')
  expect(screen.getByRole('textbox')).toHaveValue("%_'\\A")
  await user.selectOptions(screen.getByRole('combobox', { name: 'Filtro, condición 1: columna' }), 'a:amount')
  expect(within(operator).queryByRole('option', { name: 'Contiene' })).not.toBeInTheDocument()
  expect(operator).toHaveValue('EQ')
  await user.selectOptions(screen.getByRole('combobox', { name: 'Filtro, condición 1: columna' }), 'a:flag')
  expect(within(operator).queryByRole('option', { name: 'Mayor' })).not.toBeInTheDocument()
})
