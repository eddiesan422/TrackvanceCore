import { useEffect, useRef, useState } from 'react'
import { flushSync } from 'react-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { KeyRound, Plus, RefreshCw } from 'lucide-react'
import { api } from '../../api/client'
import { usePermission } from '../../app/session'
import { Badge, date, Empty, ErrorState, Field, Loading, Modal, Notice, SearchBox } from '../../components/ui'
import type { Items, Role, User, UserCredentialIssue } from './types'
import { CredentialIssueModal } from './CredentialIssueModal'
import './identity.css'

function CredentialStatus({ user }: { user: User }) {
  const expired = user.must_change_password && !!user.temporary_password_expires_at && Date.parse(user.temporary_password_expires_at) <= Date.now()
  return <><span>{!user.must_change_password ? 'Contraseña definida' : expired ? 'Contraseña temporal vencida' : 'Primer acceso pendiente'}</span>{user.must_change_password && user.temporary_password_expires_at && <small className="table-subtitle">{expired ? 'Venció' : 'Vence'} {date(user.temporary_password_expires_at)}</small>}</>
}

function useLiveView() {
  const live = useRef(true)
  useEffect(() => { live.current = true; return () => { live.current = false } }, [])
  return live
}

function UserEditor({ item, close, issued }: { item: User | null; close: (saved?: boolean) => void; issued: (issue: UserCredentialIssue) => void }) {
  const cache = useQueryClient(), live = useLiveView()
  const [pending, setPending] = useState(false), [error, setError] = useState<Error | null>(null)
  const [firstName, setFirstName] = useState(item?.first_name || ''), [lastName, setLastName] = useState(item?.last_name || '')
  const [username, setUsername] = useState(item?.username || ''), [email, setEmail] = useState(item?.email || '')
  const [roleId, setRoleId] = useState(item?.role_id || ''), [active, setActive] = useState(item?.active ?? true)
  const roles = useQuery({ queryKey: ['roles', 'assignable'], queryFn: () => api<Items<Role>>('/users/roles') })
  // The issuance response must never enter React Query's query or mutation caches.
  async function save() {
    if (pending) return
    setPending(true); setError(null)
    try {
      const body = JSON.stringify({ first_name: firstName.trim(), last_name: lastName.trim(), username: username.trim(), email: email.trim(), role_id: roleId, active, ...(item ? { version: item.version } : {}) })
      if (item) {
        await api(`/users/${item.id}`, { method: 'PATCH', body })
        await Promise.all(['users', 'roles', 'audit', 'session'].map(key => cache.invalidateQueries({ queryKey: [key] })))
        if (live.current) close(true)
      } else {
        const result = await api<UserCredentialIssue>('/users', { method: 'POST', body, cache: 'no-store' })
        await Promise.all(['users', 'roles', 'audit'].map(key => cache.invalidateQueries({ queryKey: [key] })))
        if (live.current) issued(result)
      }
    } catch (failure) { if (live.current) setError(failure instanceof Error ? failure : new Error('No se pudo guardar el usuario.')) }
    finally { if (live.current) setPending(false) }
  }
  const effective = roles.data?.items.find(entry => entry.id === roleId)
  return <form className="form-stack" onSubmit={event => { event.preventDefault(); void save() }}>
    {item && !item.first_name && !item.last_name && <Notice>Cuenta histórica: {item.name}. Completa los nombres y apellidos reales al editarla.</Notice>}
    <div className="form-grid"><Field label="Nombres"><input required maxLength={100} autoComplete="given-name" value={firstName} onChange={event => setFirstName(event.target.value)}/></Field><Field label="Apellidos"><input required maxLength={100} autoComplete="family-name" value={lastName} onChange={event => setLastName(event.target.value)}/></Field></div>
    <Field label="Username" hint="De 3 a 80 caracteres: letras, números, punto, guion y guion bajo."><input required minLength={3} maxLength={80} autoComplete="off" value={username} onChange={event => setUsername(event.target.value)}/></Field>
    <Field label="Correo electrónico"><input required type="email" maxLength={200} autoComplete="email" value={email} onChange={event => setEmail(event.target.value)}/></Field>
    <Field label="Rol del usuario"><select required value={roleId} onChange={event => setRoleId(event.target.value)} disabled={!roles.data}><option value="">Selecciona un rol activo</option>{(roles.data?.items || []).filter(entry => !entry.deleted && (entry.active || entry.id === item?.role_id)).map(entry => <option key={entry.id} value={entry.id}>{entry.name}</option>)}</select></Field>
    {roles.isPending && <Loading/>}{roles.error && <ErrorState error={roles.error} retry={() => roles.refetch()}/>}
    {effective && <details><summary>Permisos efectivos del rol</summary><ul>{effective.permissions.map(permission => <li key={permission}><code>{permission}</code></li>)}</ul></details>}
    <label className="checkbox-label"><input type="checkbox" checked={active} onChange={event => setActive(event.target.checked)}/> Usuario activo</label>
    <Notice>{item ? 'Cambiar el rol, correo, username o estado revoca las sesiones abiertas. Siempre debe quedar un administrador activo.' : 'Trackvance genera una contraseña temporal y la muestra una sola vez al crear el usuario. Vence en 24 horas y debe cambiarse en el primer acceso.'}</Notice>
    {error && <ErrorState error={error}/>}
    <div className="modal-footer"><button className="button secondary" type="button" onClick={() => close()}>Cancelar</button><button className="button primary" disabled={pending || !roles.data || !roleId}>{pending ? 'Guardando…' : item ? 'Guardar usuario' : 'Crear usuario'}</button></div>
  </form>
}

function AccessMethods({ user }: { user: User }) {
  const cache = useQueryClient(), canManage = usePermission('users:manage')
  const unlink = useMutation({ mutationFn: (identityId: string) => api(`/users/${user.id}/external-identities/${identityId}`, { method: 'DELETE' }), onSuccess: () => { cache.invalidateQueries({ queryKey: ['users'] }); cache.invalidateQueries({ queryKey: ['audit'] }) } })
  return <section className="identity-access"><h3>Métodos de acceso</h3><p>Microsoft y Google autentican. Los permisos se administran mediante el rol de Trackvance.</p><div className="identity-method"><strong>Local</strong><span>{user.must_change_password ? 'Cambio de contraseña pendiente' : 'Disponible'}</span></div>{['Microsoft', 'Google'].map(provider => {
    const linked = (user.external_identities || []).find(identity => identity.provider.toLowerCase().includes(provider.toLowerCase()))
    return <div className="identity-method" key={provider}><div><strong>{provider}</strong><small>{linked ? `Vinculado · Último acceso ${date(linked.last_login_at)}` : 'No vinculado'}</small></div>{linked && canManage && !user.deleted && <button className="text-button" disabled={unlink.isPending} onClick={() => unlink.mutate(linked.id)}>Desvincular {provider}</button>}</div>
  })}{unlink.error && <ErrorState error={unlink.error}/>}</section>
}

function UserAction({ action, close, issued }: { action: { user: User; type: 'regenerate' | 'delete' }; close: (saved?: boolean) => void; issued: (issue: UserCredentialIssue) => void }) {
  const cache = useQueryClient(), live = useLiveView()
  const [pending, setPending] = useState(false), [error, setError] = useState<Error | null>(null)
  async function execute() {
    if (pending) return
    setPending(true); setError(null)
    try {
      const options = { method: action.type === 'regenerate' ? 'POST' : 'DELETE', body: JSON.stringify({ version: action.user.version }), cache: 'no-store' as const }
      if (action.type === 'regenerate') {
        const result = await api<UserCredentialIssue>(`/users/${action.user.id}/regenerate-credentials`, options)
        await Promise.all(['users', 'roles', 'audit'].map(key => cache.invalidateQueries({ queryKey: [key] })))
        if (live.current) issued(result)
      } else {
        await api(`/users/${action.user.id}`, options)
        await Promise.all(['users', 'roles', 'audit'].map(key => cache.invalidateQueries({ queryKey: [key] })))
        if (live.current) close(true)
      }
    } catch (failure) { if (live.current) setError(failure instanceof Error ? failure : new Error('No se pudo completar la acción.')) }
    finally { if (live.current) setPending(false) }
  }
  return <><Notice>{action.type === 'regenerate' ? 'Se generará una contraseña temporal nueva, se invalidará la anterior y se cerrarán todas sus sesiones. La nueva contraseña será válida durante 24 horas y deberá cambiarse en el primer acceso. Se mostrará una sola vez.' : `Se desactivará y eliminará lógicamente a ${action.user.name}. Se cerrarán sus sesiones y se conservará su identidad histórica.`}</Notice>{error && <ErrorState error={error}/>}<div className="modal-footer"><button className="button secondary" onClick={() => close()}>Cancelar</button><button className="button primary" disabled={pending} onClick={() => void execute()}>{pending ? 'Procesando…' : action.type === 'regenerate' ? 'Regenerar credenciales' : 'Confirmar baja del usuario'}</button></div></>
}

export function UsersPanel() {
  const canManage = usePermission('users:manage')
  const [issued, setIssued] = useState<{ issue: UserCredentialIssue; regenerated: boolean } | null>(null)
  const [search, setSearch] = useState(''), [status, setStatus] = useState(''), [editor, setEditor] = useState<User | null | undefined>()
  const [selectedId, setSelectedId] = useState(''), [action, setAction] = useState<{ user: User; type: 'regenerate' | 'delete' } | null>(null), [saved, setSaved] = useState(false)
  useEffect(() => {
    // Clear before the browser can freeze this document in its back/forward cache.
    const discard = () => flushSync(() => { setIssued(null); setEditor(undefined); setAction(null) })
    window.addEventListener('pagehide', discard)
    return () => window.removeEventListener('pagehide', discard)
  }, [])
  const users = useQuery({ queryKey: ['users', status === 'deleted'], queryFn: () => api<Items<User>>(status === 'deleted' ? '/users?include_deleted=true' : '/users') })
  const rows = (users.data?.items || []).filter(item => `${item.name} ${item.username} ${item.email} ${item.role}`.toLowerCase().includes(search.toLowerCase()) && (!status || (status === 'deleted' ? item.deleted : !item.deleted && String(item.active) === status)))
  const selected = users.data?.items.find(item => item.id === selectedId)
  return <><section className="panel"><div className="panel-heading"><div><h2>Cuentas de esta organización</h2><p>Roles, acceso local e identidades externas.</p></div><div className="toolbar-right"><button className="button secondary" onClick={() => users.refetch()} disabled={users.isFetching}><RefreshCw size={16}/> Actualizar usuarios</button>{canManage && <button className="button primary" onClick={() => { setSaved(false); setEditor(null) }}><Plus size={16}/> Nuevo usuario</button>}</div></div>
    {saved && <Notice success>El cambio del usuario quedó registrado.</Notice>}
    <div className="table-toolbar"><SearchBox value={search} onChange={setSearch} placeholder="Buscar usuario, correo o rol…"/><select aria-label="Filtrar usuarios por estado" value={status} onChange={event => setStatus(event.target.value)}><option value="">Todos los estados</option><option value="true">Activos</option><option value="false">Inactivos</option><option value="deleted">Eliminados</option></select></div>
    {users.isPending ? <Loading/> : users.error ? <ErrorState error={users.error} retry={() => users.refetch()}/> : !rows.length ? <Empty title="Sin usuarios en esta vista" description="Ajusta los filtros de búsqueda."/> : <div className="table-scroll"><table><thead><tr><th>Nombre / username</th><th>Correo</th><th>Rol</th><th>Estado</th><th>Credenciales</th><th>Último acceso</th><th>Acciones</th></tr></thead><tbody>{rows.map(item => <tr key={item.id}>
      <td><strong>{item.name}</strong><small className="table-subtitle mono">{item.username}</small></td><td>{item.email}</td><td>{item.role}</td><td><Badge value={item.deleted ? 'DELETED' : item.active ? 'ACTIVE' : 'INACTIVE'}/></td><td><CredentialStatus user={item}/></td><td>{item.last_login_at ? date(item.last_login_at) : 'Sin registro'}</td>
      <td><div className="toolbar-right"><button className="text-button" onClick={() => setSelectedId(item.id)} aria-label={`Métodos de acceso de ${item.name}`}>Acceso</button>{canManage && !item.deleted && <><button className="text-button" onClick={() => { setSaved(false); setEditor(item) }} aria-label={`Editar ${item.name}`}>Editar</button><button className="text-button" disabled={!item.active} onClick={() => { setAction({ user: item, type: 'regenerate' }) }} aria-label={`Regenerar credenciales de ${item.name}`}><KeyRound size={15}/> Regenerar</button><button className="text-button" onClick={() => { setAction({ user: item, type: 'delete' }) }} aria-label={`Eliminar ${item.name}`}>Eliminar</button></>}</div></td>
    </tr>)}</tbody></table></div>}
  </section><Modal open={editor !== undefined} onOpenChange={open => { if (!open) setEditor(undefined) }} title={editor ? 'Editar usuario' : 'Nuevo usuario'} description="Los roles y los permisos vigentes se validan en el servidor.">{editor !== undefined && <UserEditor key={editor?.id || 'new'} item={editor} issued={issue => { setEditor(undefined); setIssued({ issue, regenerated: false }) }} close={didSave => { setEditor(undefined); setSaved(Boolean(didSave)) }}/>}</Modal>
  <Modal open={!!selected} onOpenChange={open => { if (!open) setSelectedId('') }} title={selected?.name || 'Métodos de acceso'} description={selected?.email || ''}>{selected && <><AccessMethods user={selected}/><details><summary>Permisos efectivos del rol</summary><ul>{selected.permissions.map(permission => <li key={permission}><code>{permission}</code></li>)}</ul></details><div className="modal-footer"><button className="button secondary" onClick={() => setSelectedId('')}>Cerrar</button></div></>}</Modal>
  <Modal open={!!action} onOpenChange={open => { if (!open) setAction(null) }} title={action?.type === 'regenerate' ? 'Regenerar credenciales' : 'Eliminar usuario'} description={action?.user.email || ''}>{action && <UserAction action={action} close={didSave => { setAction(null); setSaved(Boolean(didSave)) }} issued={issue => { setAction(null); setIssued({ issue, regenerated: true }) }}/>}</Modal>
  {issued && <CredentialIssueModal issue={issued.issue} regenerated={issued.regenerated} close={() => { setIssued(null); setSaved(true) }}/>}</>
}
