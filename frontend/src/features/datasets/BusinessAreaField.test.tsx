import { useState } from 'react'
import { fireEvent, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, it, vi } from 'vitest'
import { renderApp } from '../../test/render'
import { availableBusinessAreas, BusinessAreaField, businessAreaError } from './BusinessAreaField'

it('keeps initial and organization areas ordered and deduplicated without rewriting historical values', () => {
  const historical = [{ id: 'one', domain: 'Comercial' }, { id: 'two', domain: 'comercial' }, { id: 'three', domain: ' Finanzas ' }, { id: 'four', domain: ' Atención ' }]
  expect(availableBusinessAreas(historical)).toEqual([' Atención ', 'Comercial', 'Finanzas', 'Logística', 'Operaciones', 'Ventas'])
  expect(historical[2].domain).toBe(' Finanzas ')
  expect(businessAreaError('   ')).toMatch(/Indica un área/)
  expect(businessAreaError('a'.repeat(81))).toMatch(/80 caracteres/)
  expect(businessAreaError('😀'.repeat(80))).toBe('')
})

it('supports selecting known areas and creating a validated area up to 80 Unicode characters', async () => {
  function Form() {
    const [value, setValue] = useState('Operaciones')
    return <><BusinessAreaField value={value} onChange={setValue} datasets={[{ id: 'one', domain: 'Comercial' }]}/><output>{value}</output></>
  }
  const user = userEvent.setup()
  renderApp(<Form/>)
  await user.selectOptions(screen.getByLabelText('Área de negocio'), 'Comercial')
  expect(screen.getByRole('status')).toHaveTextContent('Comercial')
  await user.selectOptions(screen.getByLabelText('Área de negocio'), '__new_domain__')
  expect(screen.getByRole('alert')).toHaveTextContent('Indica un área')
  fireEvent.change(screen.getByLabelText('Nueva área de negocio'), { target: { value: 'a'.repeat(81) } })
  expect(screen.getByRole('alert')).toHaveTextContent('80 caracteres')
  fireEvent.change(screen.getByLabelText('Nueva área de negocio'), { target: { value: 'Riesgos' } })
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  expect(screen.getByRole('status')).toHaveTextContent('Riesgos')
})

it('shows the existing dataset area exactly and prevents any implicit area replacement', () => {
  const changed = vi.fn()
  renderApp(<BusinessAreaField value="Operaciones" onChange={changed} fixedValue=" Comercial histórico "/>)
  expect(screen.getByLabelText('Área de negocio')).toHaveValue(' Comercial histórico ')
  expect(screen.getByLabelText('Área de negocio')).toBeDisabled()
  expect(screen.queryByRole('option', { name: /Agregar nueva área/ })).not.toBeInTheDocument()
  expect(changed).not.toHaveBeenCalled()
})
