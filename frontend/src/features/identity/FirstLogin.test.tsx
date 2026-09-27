import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { post } from '../../api/client'
import { renderApp } from '../../test/render'
import { FirstLogin } from './FirstLogin'

vi.mock('../../api/client', async importOriginal => ({ ...await importOriginal<typeof import('../../api/client')>(), post: vi.fn() }))
describe('First login', () => {
  it('requires a matching strong password and accepts the rotated session', async () => {
    const user = userEvent.setup(), completed = vi.fn(), session = { csrf_token: 'rotated' }
    vi.mocked(post).mockResolvedValue(session)
    renderApp(<FirstLogin onComplete={completed} onLogout={vi.fn()}/>)
    await user.type(screen.getByLabelText('Nueva contraseña'), 'New local phrase 2048!')
    await user.type(screen.getByLabelText('Confirmar contraseña'), 'Mismatch')
    expect(screen.getByRole('button', { name: 'Guardar y continuar' })).toBeDisabled()
    await user.clear(screen.getByLabelText('Confirmar contraseña'))
    await user.type(screen.getByLabelText('Confirmar contraseña'), 'New local phrase 2048!')
    await user.click(screen.getByRole('button', { name: 'Guardar y continuar' }))
    await waitFor(() => expect(completed).toHaveBeenCalledWith(session))
    expect(post).toHaveBeenCalledWith('/auth/first-login/change-password', { new_password: 'New local phrase 2048!' })
    expect(screen.getByLabelText('Nueva contraseña')).toHaveValue('')
  })
})
