import { fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import HeaderSourceConfirmation from './HeaderSourceConfirmation'

const review = { payload_hash: 'c'.repeat(64), eligible: true, observations: [{ observation_id: 'a'.repeat(64),
  observation_hash: 'b'.repeat(64), table: 'detail', page: 2, region: [.1, .2, .9, .3], reason: 'HEADER_UNVERIFIED', text: 'Description Qxx Price Amount', resolved: false }] }

describe('Exact source header confirmation', () => {
  it('requires the actual source image, explicit statement and note before sending only exact hashes', async () => {
    const user = userEvent.setup(), onConfirm = vi.fn().mockResolvedValue(undefined)
    render(<HeaderSourceConfirmation review={review} sourceUrl={page => `/source/pages/${page}`} busy={false} pendingChanges={false} onConfirm={onConfirm}/>)
    expect(screen.queryByRole('button', { name: 'Confirmar encabezado contra original' })).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Abrir región del original' }))
    const button = screen.getByRole('button', { name: 'Confirmar encabezado contra original' })
    expect(button).toBeDisabled()
    expect(screen.getByAltText('Original para comprobar encabezado, página 2')).toHaveAttribute('src', '/source/pages/2')
    fireEvent.load(screen.getByRole('img'))
    await user.type(screen.getByLabelText('Comprobación del encabezado en la fuente'), 'Sólo encabezado en la imagen original.')
    expect(button).toBeDisabled()
    await user.click(screen.getByRole('checkbox'))
    await user.click(button)
    expect(onConfirm).toHaveBeenCalledOnce()
    expect(onConfirm).toHaveBeenCalledWith({ observation_id: 'a'.repeat(64), expected_observation_hash: 'b'.repeat(64),
      expected_payload_hash: 'c'.repeat(64), decision: 'SOURCE_HEADER_ONLY_NO_PRODUCTS_CONFIRMED', source_note: 'Sólo encabezado en la imagen original.' })
    expect(screen.getByText(/Texto observado: Description Qxx/)).toBeInTheDocument()
  })

  it.each([{ eligible: false, pendingChanges: false }, { eligible: true, pendingChanges: true }])('keeps source confirmation blocked until reviewed values are saved: %j', async state => {
    const user = userEvent.setup(), onConfirm = vi.fn()
    render(<HeaderSourceConfirmation review={{ ...review, eligible: state.eligible }} sourceUrl={() => '/source'} busy={false} pendingChanges={state.pendingChanges} onConfirm={onConfirm}/>)
    await user.click(screen.getByRole('button', { name: 'Abrir región del original' }))
    fireEvent.load(screen.getByRole('img'))
    expect(screen.getByRole('checkbox')).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Confirmar encabezado contra original' })).toBeDisabled()
    expect(onConfirm).not.toHaveBeenCalled()
  })

  it('does not confirm a source that failed to load or lacks download permission', async () => {
    const user = userEvent.setup(), onConfirm = vi.fn()
    const view = render(<HeaderSourceConfirmation review={review} sourceUrl={() => '/source'} canView={false} busy={false} pendingChanges={false} onConfirm={onConfirm}/>)
    expect(screen.getByRole('button', { name: 'Abrir región del original' })).toBeDisabled()
    view.rerender(<HeaderSourceConfirmation review={review} sourceUrl={() => '/source'} canView busy={false} pendingChanges={false} onConfirm={onConfirm}/>)
    await user.click(screen.getByRole('button', { name: 'Abrir región del original' }))
    fireEvent.error(screen.getByRole('img'))
    expect(screen.getByRole('checkbox')).toBeDisabled()
    expect(onConfirm).not.toHaveBeenCalled()
  })

  it('keeps source failures visible and never turns a rejected confirmation into a resolved observation', async () => {
    const user = userEvent.setup(), onConfirm = vi.fn().mockRejectedValue(new Error('La revisión cambió.'))
    render(<HeaderSourceConfirmation review={review} sourceUrl={() => '/source'} busy={false} pendingChanges={false} onConfirm={onConfirm}/>)
    await user.click(screen.getByRole('button', { name: 'Abrir región del original' }))
    fireEvent.load(screen.getByRole('img'))
    await user.type(screen.getByLabelText('Comprobación del encabezado en la fuente'), 'Fuente examinada')
    await user.click(screen.getByRole('checkbox'))
    await user.click(screen.getByRole('button', { name: 'Confirmar encabezado contra original' }))
    expect(await screen.findByText('La revisión cambió.')).toBeInTheDocument()
    expect(screen.getByText(/Sin confirmar/)).toBeInTheDocument()
  })

  it('requires opening and inspecting source again after a changed payload instead of carrying consent forward', async () => {
    const user = userEvent.setup(), onConfirm = vi.fn()
    const view = render(<HeaderSourceConfirmation review={review} sourceUrl={() => '/source'} busy={false} pendingChanges={false} onConfirm={onConfirm}/>)
    await user.click(screen.getByRole('button', { name: 'Abrir región del original' }))
    fireEvent.load(screen.getByRole('img'))
    await user.type(screen.getByLabelText('Comprobación del encabezado en la fuente'), 'Inspección anterior')
    await user.click(screen.getByRole('checkbox'))
    expect(screen.getByRole('button', { name: 'Confirmar encabezado contra original' })).toBeEnabled()
    view.rerender(<HeaderSourceConfirmation review={{ ...review, payload_hash: 'd'.repeat(64) }} sourceUrl={() => '/source'} busy={false} pendingChanges={false} onConfirm={onConfirm}/>)
    expect(screen.queryByRole('button', { name: 'Confirmar encabezado contra original' })).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Abrir región del original' }))
    expect(screen.getByRole('checkbox')).not.toBeChecked()
    expect(screen.getByLabelText('Comprobación del encabezado en la fuente')).toHaveValue('')
    expect(screen.getByRole('button', { name: 'Confirmar encabezado contra original' })).toBeDisabled()
    expect(onConfirm).not.toHaveBeenCalled()
  })
})
