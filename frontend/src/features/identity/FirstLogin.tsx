import { useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { KeyRound } from 'lucide-react'
import { post, type Session } from '../../api/client'
import { ErrorState, Field, Notice } from '../../components/ui'
import './identity.css'

export function FirstLogin({ onComplete, onLogout }: { onComplete: (session: Session) => void; onLogout: () => void }) {
  const [password, setPassword] = useState(''), [confirmation, setConfirmation] = useState('')
  const change = useMutation({ mutationFn: () => post<Session>('/auth/first-login/change-password', { new_password: password }), onSuccess: data => { setPassword(''); setConfirmation(''); onComplete(data) } })
  const logout = useMutation({ mutationFn: () => post('/auth/logout'), onSuccess: onLogout })
  return <div className="first-login-page"><div className="login-card"><div className="session-icon"><KeyRound size={26}/></div><h1>Cambia tu contraseña</h1><p>Define tu contraseña local para completar el primer acceso y entrar a Trackvance.</p><Notice>Usa al menos 12 caracteres. Debe ser diferente de la contraseña temporal recibida por correo. Este paso también se requiere si entraste con Microsoft o Google.</Notice><form className="form-stack" onSubmit={event => { event.preventDefault(); if (password === confirmation && password.length >= 12) change.mutate() }}><Field label="Nueva contraseña"><input required type="password" autoComplete="new-password" minLength={12} maxLength={1024} value={password} onChange={event => setPassword(event.target.value)}/></Field><Field label="Confirmar contraseña"><input required type="password" autoComplete="new-password" minLength={12} maxLength={1024} value={confirmation} onChange={event => setConfirmation(event.target.value)}/></Field>{confirmation && password !== confirmation && <p role="alert">Las contraseñas no coinciden.</p>}{change.error && <ErrorState error={change.error}/>}<button className="button primary full" disabled={change.isPending || password.length < 12 || password !== confirmation}>{change.isPending ? 'Guardando contraseña…' : 'Guardar y continuar'}</button></form><button className="text-button full" disabled={logout.isPending || change.isPending} onClick={() => logout.mutate()}>Cerrar sesión</button>{logout.error && <ErrorState error={logout.error}/>}</div></div>
}
