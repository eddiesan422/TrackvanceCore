import { useState } from 'react'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, it } from 'vitest'
import { Modal } from './ui'

function Dialogs() {
  const [outer, setOuter] = useState(false), [inner, setInner] = useState(false)
  return <main tabIndex={-1}><button onClick={() => setOuter(true)}>Abrir configuración</button><Modal open={outer} onOpenChange={setOuter} title="Configuración"><button onClick={() => setInner(true)}>Agregar persona</button><Modal open={inner} onOpenChange={setInner} title="Persona"><input aria-label="Nombre"/></Modal></Modal></main>
}

it('returns keyboard focus through nested controlled dialogs and back to the original action', async () => {
  const user = userEvent.setup()
  render(<Dialogs/>)
  const opener = screen.getByRole('button', { name: 'Abrir configuración' })
  await user.click(opener)
  const outer = await screen.findByRole('dialog', { name: 'Configuración' })
  const nestedOpener = within(outer).getByRole('button', { name: 'Agregar persona' })
  await user.click(nestedOpener)
  const inner = await screen.findByRole('dialog', { name: 'Persona' })
  await user.tab()
  expect(inner.contains(document.activeElement)).toBe(true)
  await user.keyboard('{Escape}')
  expect(screen.queryByRole('dialog', { name: 'Persona' })).not.toBeInTheDocument()
  expect(nestedOpener).toHaveFocus()
  await user.keyboard('{Escape}')
  expect(screen.queryByRole('dialog', { name: 'Configuración' })).not.toBeInTheDocument()
  expect(opener).toHaveFocus()
})

it('returns a directly opened dialog to the main content when no trigger exists', async () => {
  const user = userEvent.setup()
  function Direct() { const [open, setOpen] = useState(true); return <main tabIndex={-1}><Modal open={open} onOpenChange={setOpen} title="Acceso directo"><input aria-label="Dato"/></Modal></main> }
  render(<Direct/>)
  await screen.findByRole('dialog', { name: 'Acceso directo' })
  await user.keyboard('{Escape}')
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  expect(screen.getByRole('main')).toHaveFocus()
})
