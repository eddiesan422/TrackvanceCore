import { useState } from 'react'
import { Copy, Eye, EyeOff } from 'lucide-react'
import { date, Field, Modal, Notice } from '../../components/ui'
import type { UserCredentialIssue } from './types'

/** Mounted only while the authorized one-time response is being presented. */
export function CredentialIssueModal({ issue, regenerated, close }: { issue: UserCredentialIssue; regenerated: boolean; close: () => void }) {
  const [visible, setVisible] = useState(false), [message, setMessage] = useState('')
  const { user, temporary_credentials: credentials } = issue
  async function copy(value: string, confirmation: string) {
    try { await navigator.clipboard.writeText(value); setMessage(confirmation) }
    catch { setMessage('No se pudo copiar. Selecciona el contenido y cópialo manualmente.') }
  }
  return <Modal open onOpenChange={open => { if (!open) close() }} title={regenerated ? 'Credenciales regeneradas' : 'Usuario creado'} description="Entrega estas credenciales al usuario mediante un canal seguro.">
    <div className="form-stack credential-issue">
      <Notice>Guarda estas credenciales ahora. La contraseña temporal sólo se muestra una vez y Trackvance no podrá recuperarla posteriormente.</Notice>
      <dl className="credential-summary"><div><dt>Nombre</dt><dd>{user.name}</dd></div><div><dt>Rol</dt><dd>{user.role}</dd></div><div><dt>Válida hasta</dt><dd>{date(credentials.expires_at)}</dd></div></dl>
      <Field label="Username"><input readOnly autoComplete="off" value={credentials.username}/></Field>
      <Field label="Contraseña temporal" hint="Debe cambiarse en el primer acceso."><input readOnly type={visible ? 'text' : 'password'} autoComplete="off" spellCheck={false} value={credentials.temporary_password}/></Field>
      <button className="text-button credential-visibility" type="button" aria-pressed={visible} onClick={() => setVisible(!visible)}>{visible ? <EyeOff size={16}/> : <Eye size={16}/>} {visible ? 'Ocultar contraseña' : 'Mostrar contraseña'}</button>
      <div className="credential-copy-actions"><button className="button secondary" type="button" onClick={() => void copy(credentials.username, 'Username copiado.')}><Copy size={15}/> Copiar username</button><button className="button secondary" type="button" onClick={() => void copy(credentials.temporary_password, 'Contraseña copiada.')}><Copy size={15}/> Copiar contraseña</button><button className="button secondary" type="button" onClick={() => void copy(`Username: ${credentials.username}\nContraseña temporal: ${credentials.temporary_password}`, 'Credenciales copiadas.')}><Copy size={15}/> Copiar credenciales</button></div>
      {message && <p role="status">{message}</p>}
      <div className="modal-footer"><button className="button primary" type="button" onClick={close}>Entendido</button></div>
    </div>
  </Modal>
}
