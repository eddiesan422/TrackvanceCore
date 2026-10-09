import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { describe, expect, it } from 'vitest'
import { renderApp } from '../../test/render'
import { PrimaryKeyPicker } from './PrimaryKeyPicker'

function Example() {
  const [mode, setMode] = useState<'DEFINE' | 'NONE'>('DEFINE')
  const [keys, setKeys] = useState<string[]>([])
  return <PrimaryKeyPicker mode={mode} columns={[{ source_name: 'client', target_name: 'customer_id' }, { source_name: 'transaction', target_name: 'transaction_id' }]} keys={keys} historical={false} onMode={setMode} onKeys={setKeys}/>
}

describe('R085-02 primary key selection', () => {
  it('defaults to defining an ordered NOT NULL key on final mapping names', async () => {
    const user = userEvent.setup()
    renderApp(<Example/>)
    expect(screen.getByRole('radio', { name: 'Definir clave primaria' })).toBeChecked()
    expect(screen.getByText(/Identificador conserva texto/)).toBeVisible()
    await user.click(screen.getByRole('checkbox', { name: 'Clave primaria customer_id' }))
    await user.click(screen.getByRole('checkbox', { name: 'Clave primaria transaction_id' }))
    const order = screen.getByRole('list', { name: 'Orden de la clave primaria' })
    expect(within(order).getAllByRole('listitem').map(item => item.textContent)).toEqual(['customer_id · NOT NULL ', 'transaction_id · NOT NULL '])
    await user.click(screen.getByRole('button', { name: 'Subir clave transaction_id' }))
    expect(within(order).getAllByRole('listitem')[0]).toHaveTextContent('transaction_id · NOT NULL')
    expect(screen.getByRole('button', { name: 'Subir clave transaction_id' })).toBeDisabled()
  })

  it('allows no PK only by explicit keyboard choice and displays its warning', async () => {
    const user = userEvent.setup()
    renderApp(<Example/>)
    const none = screen.getByRole('radio', { name: 'Crear sin clave primaria' })
    none.focus()
    await user.keyboard(' ')
    expect(none).toBeChecked()
    expect(screen.getByText(/podrá admitir filas repetidas/)).toBeVisible()
    expect(screen.queryByRole('checkbox', { name: /Clave primaria/ })).not.toBeInTheDocument()
  })
})
