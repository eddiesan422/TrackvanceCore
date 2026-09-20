import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { KeyRound, Plus, RefreshCw } from 'lucide-react'
import { api, type Collection, type RecordData } from '../../api/client'
import { usePermission } from '../../app/session'
import { Badge, Empty, ErrorState, Field, Loading, Modal, Notice, SearchBox } from '../../components/ui'

function UserEditor({ item, close }: { item: RecordData | null; close: (saved?: boolean) => void }) {
  const cache = useQueryClient()
  const [name, setName] = useState(item?.name || ''), [email, setEmail] = useState(item?.email || '')
  const [role, setRole] = useState(item?.role || 'Data Analyst'), [active, setActive] = useState(item?.active ?? true)
  const [password, setPassword] = useState('')
  const roles = useQuery({ queryKey: ['user-roles'], queryFn: () => api<Collection>('/users/roles') })
  const save = useMutation({
    mutationFn: () => api(item ? `/users/${item.id}` : '/users', {
      method: item ? 'PATCH' : 'POST', body: JSON.stringify({ name, email, role, active, ...(item ? { version: item.version } : { password }) }),
    }),
    onSuccess: () => { setPassword(''); cache.invalidateQueries({ queryKey: ['users'] }); cache.invalidateQueries({ queryKey: ['audit'] }); close(true) },
  })
  const effective = roles.data?.items.find(entry => entry.name === role || (role === 'Data Owner' && entry.name === 'Data Owner / Lead'))
  return <form className="form-stack" onSubmit={event => { event.preventDefault(); save.mutate() }}>
    <Field label="Nombre del usuario"><input required maxLength={200} value={name} onChange={event => setName(event.target.value)}/></Field>
    <Field label="Correo electrónico"><input required type="email" maxLength={200} value={email} onChange={event => setEmail(event.target.value)}/></Field>
    <Field label="Rol del usuario"><select value={role} onChange={event => setRole(event.target.value)} disabled={!roles.data}>
      {role === 'Data Owner' && <option value="Data Owner">Data Owner (histórico)</option>}
      {(roles.data?.items || []).map(entry => <option key={entry.name}>{entry.name}</option>)}
    </select></Field>
    {roles.isPending && <Loading/>}{roles.error && <ErrorState error={roles.error} retry={() => roles.refetch()}/>}
    {effective && <details><summary>Permisos efectivos del rol</summary><ul>{effective.permissions.map((permission: string) => <li key={permission}><code>{permission}</code></li>)}</ul></details>}
    <label className="checkbox-label"><input type="checkbox" checked={active} onChange={event => setActive(event.target.checked)}/> Usuario activo</label>
    {!item && <Field label="Contraseña inicial" hint="Al menos 12 caracteres. Compártela directamente con la persona por un canal seguro."><input required type="password" minLength={12} maxLength={1024} autoComplete="new-password" value={password} onChange={event => setPassword(event.target.value)}/></Field>}
    {item && <Notice>Cambiar correo, rol o estado revoca las sesiones abiertas. Siempre debe quedar un administrador activo.</Notice>}
    {save.error && <ErrorState error={save.error}/>}
    <div className="modal-footer"><button className="button secondary" type="button" onClick={() => close()}>Cancelar</button><button className="button primary" disabled={save.isPending || !roles.data}>{save.isPending ? 'Guardando…' : item ? 'Guardar usuario' : 'Crear usuario'}</button></div>
  </form>
}

function PasswordEditor({ item, close }: { item: RecordData; close: (saved?: boolean) => void }) {
  const cache = useQueryClient()
  const [password, setPassword] = useState(''), [confirmation, setConfirmation] = useState('')
  const save = useMutation({
    mutationFn: () => api(`/users/${item.id}/reset-password`, { method: 'POST', body: JSON.stringify({ version: item.version, password }) }),
    onSuccess: () => { setPassword(''); setConfirmation(''); cache.invalidateQueries({ queryKey: ['users'] }); cache.invalidateQueries({ queryKey: ['audit'] }); close(true) },
  })
  return <form className="form-stack" onSubmit={event => { event.preventDefault(); if (password === confirmation) save.mutate() }}>
    <Notice>Se cerrarán todas las sesiones de {item.name}. La contraseña no se mostrará después de guardarla.</Notice>
    <Field label="Nueva contraseña" hint="Al menos 12 caracteres."><input type="password" required minLength={12} maxLength={1024} autoComplete="new-password" value={password} onChange={event => setPassword(event.target.value)}/></Field>
    <Field label="Confirmar contraseña"><input type="password" required autoComplete="new-password" value={confirmation} onChange={event => setConfirmation(event.target.value)}/></Field>
    {confirmation && password !== confirmation && <p role="alert">Las contraseñas no coinciden.</p>}
    {save.error && <ErrorState error={save.error}/>}
    <div className="modal-footer"><button type="button" className="button secondary" onClick={() => close()}>Cancelar</button><button className="button primary" disabled={save.isPending || password.length < 12 || password !== confirmation}>{save.isPending ? 'Restableciendo…' : 'Restablecer contraseña'}</button></div>
  </form>
}

export function UsersPanel() {
  const canManage = usePermission('users:write')
  const [search, setSearch] = useState(''), [status, setStatus] = useState(''), [editor, setEditor] = useState<RecordData | null | undefined>()
  const [reset, setReset] = useState<RecordData | null>(null), [saved, setSaved] = useState(false)
  const users = useQuery({ queryKey: ['users'], queryFn: () => api<Collection>('/users') })
  const rows = (users.data?.items || []).filter(item => `${item.name} ${item.email} ${item.role}`.toLowerCase().includes(search.toLowerCase()) && (!status || String(item.active) === status))
  return <><section className="panel"><div className="panel-heading"><div><h2>Cuentas de esta organización</h2><p>Roles, acceso local y permisos efectivos.</p></div><div className="toolbar-right"><button className="button secondary" onClick={() => users.refetch()} disabled={users.isFetching}><RefreshCw size={16}/> Actualizar usuarios</button>{canManage && <button className="button primary" onClick={() => { setSaved(false); setEditor(null) }}><Plus size={16}/> Nuevo usuario</button>}</div></div>
    {saved && <Notice success>El cambio del usuario quedó registrado.</Notice>}
    <div className="table-toolbar"><SearchBox value={search} onChange={setSearch} placeholder="Buscar usuario, correo o rol…"/><select aria-label="Filtrar usuarios por estado" value={status} onChange={event => setStatus(event.target.value)}><option value="">Todos los estados</option><option value="true">Activos</option><option value="false">Inactivos</option></select></div>
    {users.isPending ? <Loading/> : users.error ? <ErrorState error={users.error} retry={() => users.refetch()}/> : !rows.length ? <Empty title="Sin usuarios en esta vista" description="Ajusta los filtros de búsqueda."/> : <div className="table-scroll"><table><thead><tr><th>Nombre</th><th>Correo</th><th>Rol</th><th>Estado</th><th>Permisos</th><th>Acciones</th></tr></thead><tbody>{rows.map(item => <tr key={item.id}>
      <td>{item.name}</td><td>{item.email}</td><td>{item.role}</td><td><Badge value={item.active ? 'ACTIVE' : 'INACTIVE'}/></td>
      <td><details><summary>Ver permisos</summary><ul>{(item.permissions || []).map((permission: string) => <li key={permission}><code>{permission}</code></li>)}</ul></details></td>
      <td>{canManage ? <div className="toolbar-right"><button className="text-button" onClick={() => { setSaved(false); setEditor(item) }} aria-label={`Editar ${item.name}`}>Editar</button><button className="text-button" onClick={() => { setSaved(false); setReset(item) }} aria-label={`Restablecer contraseña de ${item.name}`}><KeyRound size={15}/> Contraseña</button></div> : <span className="muted">Solo consulta</span>}</td>
    </tr>)}</tbody></table></div>}
  </section><Modal open={editor !== undefined} onOpenChange={open => { if (!open) setEditor(undefined) }} title={editor ? 'Editar usuario' : 'Nuevo usuario'} description="Administración local con permisos validados por el servidor.">{editor !== undefined && <UserEditor key={editor?.id || 'new'} item={editor} close={didSave => { setEditor(undefined); setSaved(Boolean(didSave)) }}/>}</Modal>
  <Modal open={!!reset} onOpenChange={open => { if (!open) setReset(null) }} title="Restablecer contraseña local" description={reset?.email || ''}>{reset && <PasswordEditor item={reset} close={didSave => { setReset(null); setSaved(Boolean(didSave)) }}/>}</Modal></>
}
