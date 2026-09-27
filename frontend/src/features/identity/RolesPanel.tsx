import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { LockKeyhole, Plus, RefreshCw } from 'lucide-react'
import { api } from '../../api/client'
import { usePermission, useSession } from '../../app/session'
import { Badge, Empty, ErrorState, Field, Loading, Modal, Notice, SearchBox } from '../../components/ui'
import type { Items, Permission, Role } from './types'
import './identity.css'

function withDependencies(codes: string[], catalogue: Permission[]): string[] {
  const selected = new Set(codes)
  function add(code: string) { for (const dependency of catalogue.find(item => item.code === code)?.dependencies || []) { if (!selected.has(dependency)) { selected.add(dependency); add(dependency) } } }
  codes.forEach(add)
  return [...selected]
}

function RoleEditor({ item, close }: { item: Role | null; close: () => void }) {
  const cache = useQueryClient(), session = useSession(), canManage = usePermission('roles:manage')
  const [name, setName] = useState(item?.name || ''), [description, setDescription] = useState(item?.description || '')
  const [active, setActive] = useState(item?.active ?? true), [permissions, setPermissions] = useState(item?.permissions || [])
  const catalogue = useQuery({ queryKey: ['permission-catalogue'], queryFn: () => api<Items<Permission>>('/roles/permissions') })
  const save = useMutation({
    mutationFn: () => api(item ? `/roles/${item.id}` : '/roles', { method: item ? 'PATCH' : 'POST', body: JSON.stringify({ name: name.trim(), description: description.trim(), active, permissions, ...(item ? { version: item.version } : {}) }) }),
    onSuccess: async () => { await Promise.all([cache.invalidateQueries({ queryKey: ['roles'] }), cache.invalidateQueries({ queryKey: ['users'] }), cache.invalidateQueries({ queryKey: ['session'] }), cache.invalidateQueries({ queryKey: ['audit'] })]); close() },
  })
  const catalogueItems = catalogue.data?.items || []
  const groups = [...new Set(catalogueItems.map(permission => permission.group))]
  const protectedRole = Boolean(item?.protected)
  const readOnly = protectedRole || !canManage
  function toggle(code: string, checked: boolean) {
    if (checked) setPermissions(current => withDependencies([...current, code], catalogueItems))
    else setPermissions(current => current.filter(selected => selected !== code && !withDependencies([selected], catalogueItems).includes(code)))
  }
  return <form className="form-stack" onSubmit={event => { event.preventDefault(); if (!readOnly) save.mutate() }}>
    {protectedRole && <Notice>Administrator es un rol protegido del sistema. Recibe automáticamente todos los permisos actuales y futuros.</Notice>}
    <Field label="Nombre del rol"><input required maxLength={120} disabled={readOnly} value={name} onChange={event => setName(event.target.value)}/></Field>
    <Field label="Descripción del rol"><textarea rows={2} maxLength={2000} disabled={readOnly} value={description} onChange={event => setDescription(event.target.value)}/></Field>
    <label className="checkbox-label"><input type="checkbox" checked={active} disabled={readOnly || (active && Boolean(item?.user_count))} onChange={event => setActive(event.target.checked)}/> Rol activo</label>
    {!!item?.user_count && <Notice>Este rol tiene {item.user_count} usuarios no eliminados. Reasígnalos antes de desactivar o eliminar el rol; los usuarios inactivos también cuentan.</Notice>}
    <div className="identity-permission-heading"><h3>Permisos del producto</h3><p>Al seleccionar un permiso se incluyen sus dependencias. Los cambios se aplican a los usuarios actuales y futuros.</p></div>
    {catalogue.isPending ? <Loading/> : catalogue.error ? <ErrorState error={catalogue.error} retry={() => catalogue.refetch()}/> : <div className="permission-groups">{groups.map(group => <fieldset key={group}><legend>{group}</legend>{catalogueItems.filter(permission => permission.group === group).map(permission => <label className="permission-choice" key={permission.code}><input type="checkbox" checked={protectedRole || permissions.includes(permission.code)} disabled={readOnly || !permission.delegable || !session?.user.permissions.includes(permission.code)} onChange={event => toggle(permission.code, event.target.checked)}/><span><strong>{permission.label}</strong><code>{permission.code}</code>{permission.dependencies.length > 0 && <small>Requiere: {permission.dependencies.join(', ')}</small>}{!permission.delegable && <small>Administración protegida</small>}</span></label>)}</fieldset>)}</div>}
    {save.error && <ErrorState error={save.error}/>}
    <div className="modal-footer"><button type="button" className="button secondary" onClick={close}>{readOnly ? 'Cerrar' : 'Cancelar'}</button>{!readOnly && <button className="button primary" disabled={!catalogue.data || save.isPending}>{save.isPending ? 'Guardando…' : item ? 'Guardar rol' : 'Crear rol'}</button>}</div>
  </form>
}

export function RolesPanel() {
  const cache = useQueryClient(), canManage = usePermission('roles:manage')
  const [search, setSearch] = useState(''), [editor, setEditor] = useState<Role | null | undefined>(), [removing, setRemoving] = useState<Role | null>(null)
  const roles = useQuery({ queryKey: ['roles'], queryFn: () => api<Items<Role>>('/roles') })
  const remove = useMutation({ mutationFn: (item: Role) => api(`/roles/${item.id}`, { method: 'DELETE', body: JSON.stringify({ version: item.version }) }), onSuccess: () => { cache.invalidateQueries({ queryKey: ['roles'] }); cache.invalidateQueries({ queryKey: ['audit'] }); setRemoving(null) } })
  const rows = (roles.data?.items || []).filter(item => `${item.name} ${item.description}`.toLowerCase().includes(search.toLowerCase()))
  return <><section className="panel"><div className="panel-heading"><div><h2>Roles y permisos</h2><p>Define el acceso de tu equipo desde el catálogo de permisos de Trackvance.</p></div><div className="toolbar-right"><button className="button secondary" disabled={roles.isFetching} onClick={() => roles.refetch()}><RefreshCw size={16}/> Actualizar roles</button>{canManage && <button className="button primary" onClick={() => setEditor(null)}><Plus size={16}/> Nuevo rol</button>}</div></div>
    <div className="table-toolbar"><SearchBox value={search} onChange={setSearch} placeholder="Buscar rol o descripción…"/></div>
    {roles.isPending ? <Loading/> : roles.error ? <ErrorState error={roles.error} retry={() => roles.refetch()}/> : !rows.length ? <Empty title="Sin roles en esta vista" description="Ajusta la búsqueda o crea un rol."/> : <div className="table-scroll"><table><thead><tr><th>Rol</th><th>Estado</th><th>Usuarios</th><th>Permisos</th><th>Acciones</th></tr></thead><tbody>{rows.map(item => <tr key={item.id}><td><strong>{item.name}</strong>{item.protected && <small className="table-subtitle"><LockKeyhole size={13}/> Protegido del sistema</small>}<small className="table-subtitle">{item.description}</small></td><td><Badge value={item.deleted ? 'DELETED' : item.active ? 'ACTIVE' : 'INACTIVE'}/></td><td>{item.user_count}</td><td>{item.protected ? 'Todos, incluidos futuros' : item.permissions.length}</td><td>{canManage && !item.deleted ? <div className="toolbar-right"><button className="text-button" onClick={() => setEditor(item)}>{item.protected ? 'Ver permisos' : `Editar ${item.name}`}</button>{!item.protected && <button className="text-button" disabled={item.user_count > 0} title={item.user_count ? 'El rol tiene usuarios asociados, incluidos inactivos.' : undefined} onClick={() => { remove.reset(); setRemoving(item) }}>Eliminar {item.name}</button>}</div> : <button className="text-button" onClick={() => setEditor(item)}>Ver permisos de {item.name}</button>}</td></tr>)}</tbody></table></div>}
  </section><Modal open={editor !== undefined} onOpenChange={open => { if (!open) setEditor(undefined) }} title={editor ? `Rol · ${editor.name}` : 'Nuevo rol'} description="Los permisos efectivos se resuelven con la definición vigente del rol." wide>{editor !== undefined && <RoleEditor key={editor?.id || 'new'} item={editor} close={() => setEditor(undefined)}/>}</Modal>
  <Modal open={!!removing} onOpenChange={open => { if (!open) setRemoving(null) }} title="Eliminar rol" description="La baja lógica conserva su identidad para auditoría.">{removing && <><Notice>Se eliminará {removing.name}. Su nombre y su historia se conservarán.</Notice>{remove.error && <ErrorState error={remove.error}/>}<div className="modal-footer"><button className="button secondary" onClick={() => setRemoving(null)}>Cancelar</button><button className="button primary" disabled={remove.isPending} onClick={() => remove.mutate(removing)}>Confirmar baja del rol</button></div></>}</Modal></>
}
