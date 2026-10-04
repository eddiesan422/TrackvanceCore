import { render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { PreflightChecks } from './PreflightChecks'

describe('Preflight outcomes', () => {
  it('preserves each API message and outcome, including repeated column codes', () => {
    const checks = [
      { code: 'PERMISSIONS', status: 'FAIL', message: 'No se verificaron todos los permisos necesarios para escribir en la tabla seleccionada.' },
      { code: 'LENGTH_COMPATIBLE', status: 'PASS', message: 'La longitud de nombre cabe en el target.' },
      { code: 'LENGTH_COMPATIBLE', status: 'FAIL', message: 'La longitud de descripción excede la capacidad del target.' },
    ]
    render(<PreflightChecks checks={checks}/>)
    const rows = within(screen.getByRole('list', { name: 'Comprobaciones de preflight' })).getAllByRole('listitem')
    checks.forEach((check, index) => {
      expect(rows[index]).toHaveAttribute('data-outcome', check.status)
      expect(within(rows[index]).getByText(check.message)).toBeVisible()
    })
  })
})
