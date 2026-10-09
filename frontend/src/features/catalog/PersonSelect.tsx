import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, post } from '../../api/client'
import { usePermission } from '../../app/session'
import { ErrorState, Field, Modal, Notice, Pagination } from '../../components/ui'

export type GovernancePerson = { id: string; name: string; active: boolean; version: number; reference?: string | null; email?: string | null; user_id?: string | null; identity_kind?: string }
type PeoplePage = { items: GovernancePerson[]; total: number }
type Account = { id: string; name: string; email: string; person_id?: string | null }
const pageSize = 25

export function PersonEditor({ person, onClose, onSaved }: { person?: GovernancePerson; onClose: () => void; onSaved: (person: GovernancePerson) => void }) {
  const cache = useQueryClient()
  const [form, setForm] = useState({ name: person?.name || '', reference: person?.reference || '', email: person?.email || '', user_id: person?.user_id || '', active: person?.active ?? true })
  const [linkUser, setLinkUser] = useState(!!person?.user_id), [search, setSearch] = useState(''), [offset, setOffset] = useState(0)
  const accounts = useQuery({ queryKey: ['governance-accounts', search, offset], queryFn: () => api<{ items: Account[]; total: number }>(`/governance/people/users?search=${encodeURIComponent(search)}&offset=${offset}&limit=${pageSize}`), enabled: linkUser })
  const save = useMutation({ mutationFn: () => {
    const values = { name: form.name.trim() || undefined, reference: form.reference || null, email: form.email || null, user_id: linkUser ? form.user_id || null : null }
    return person ? api<GovernancePerson>(`/governance/people/${person.id}`, { method: 'PATCH', body: JSON.stringify({ ...values, expected_version: person.version, active: form.active }) }) : post<GovernancePerson>('/governance/people', values)
  }, onSuccess: saved => { void cache.invalidateQueries({ queryKey: ['governance-people'] }); void cache.invalidateQueries({ queryKey: ['governance-accounts'] }); onSaved(saved) } })
  return <Modal open onOpenChange={open => { if (!open && !save.isPending) onClose() }} title={person ? 'Editar persona de gobierno' : 'Agregar persona de gobierno'} description="Una persona puede ejercer varios papeles. Su asignación no concede acceso ni envía correo.">
    <form className="form-stack" onSubmit={event => { event.preventDefault(); event.stopPropagation(); save.mutate() }}>
      <label className="checkbox-row"><input type="checkbox" checked={linkUser} disabled={save.isPending} onChange={event => setLinkUser(event.target.checked)}/>Vincular con una cuenta existente</label>
      {linkUser && <><Field label="Buscar cuenta existente"><input value={search} disabled={save.isPending} onChange={event => { setSearch(event.target.value); setOffset(0) }}/></Field><Field label="Cuenta existente"><select required value={form.user_id} disabled={save.isPending || accounts.isPending} onChange={event => setForm({ ...form, user_id: event.target.value })}><option value="">Selecciona una cuenta</option>{form.user_id && !accounts.data?.items.some(item => item.id === form.user_id) && <option value={form.user_id}>Vínculo conservado</option>}{accounts.data?.items.map(item => <option key={item.id} value={item.id}>{item.name} · {item.email}{item.person_id ? ' · Persona ya registrada' : ''}</option>)}</select></Field><Pagination offset={offset} total={accounts.data?.total || 0} limit={pageSize} onChange={setOffset}/>{accounts.error && <ErrorState error={accounts.error}/>}</>}
      <Field label="Nombre de la persona" hint={linkUser && !person ? 'Puedes dejarlo vacío para reutilizar el nombre de la cuenta.' : undefined}><input autoFocus required={!linkUser || !!person} maxLength={200} value={form.name} disabled={save.isPending} onChange={event => setForm({ ...form, name: event.target.value })}/></Field>
      <Field label="Referencia (opcional)"><input maxLength={320} value={form.reference} disabled={save.isPending} onChange={event => setForm({ ...form, reference: event.target.value })}/></Field>
      <Field label="Correo (opcional)"><input type="email" maxLength={200} value={form.email} disabled={save.isPending} onChange={event => setForm({ ...form, email: event.target.value })}/></Field>
      {person && <label className="checkbox-row"><input type="checkbox" checked={form.active} disabled={save.isPending} onChange={event => setForm({ ...form, active: event.target.checked })}/>Persona activa</label>}
      {person && !form.active && <Notice>Las asignaciones históricas conservan esta persona. No estará disponible para nuevas asignaciones.</Notice>}
      {save.error && <ErrorState error={save.error}/>}<div className="modal-footer"><button className="button secondary" type="button" disabled={save.isPending} onClick={onClose}>Cancelar</button><button className="button primary" disabled={save.isPending || (!form.name.trim() && !linkUser) || (linkUser && !form.user_id)}>{save.isPending ? 'Guardando…' : person ? 'Guardar persona' : 'Agregar persona'}</button></div>
    </form>
  </Modal>
}

/** Shared by Reportes publication and Catalog governance; retains form state on creation. */
export function PersonSelect({ label, value, onChange, knownPerson, disabled = false }: { label: string; value: string | null; onChange: (id: string | null) => void; knownPerson?: GovernancePerson | null; disabled?: boolean }) {
  const canManage = usePermission('people:manage'), canRead = usePermission('people:read')
  const [search, setSearch] = useState(''), [offset, setOffset] = useState(0), [creating, setCreating] = useState(false), [created, setCreated] = useState<GovernancePerson | null>(null)
  const people = useQuery({ queryKey: ['governance-people', 'active', search, offset], queryFn: () => api<PeoplePage>(`/governance/people?active=true&search=${encodeURIComponent(search)}&offset=${offset}&limit=${pageSize}`), enabled: canRead })
  const selected = created?.id === value ? created : knownPerson?.id === value ? knownPerson : undefined
  const detail = useQuery({ queryKey: ['governance-people', 'person', value], queryFn: () => api<GovernancePerson>(`/governance/people/${value}`), enabled: !!value && !selected && canRead })
  const current = selected || detail.data
  return <div className="person-selection">
    <Field label={`Buscar ${label.toLowerCase()}`}><input value={search} disabled={disabled || !canRead} onChange={event => { setSearch(event.target.value); setOffset(0) }} placeholder="Nombre, referencia o correo"/></Field>
    <Field label={label} hint="La asignación no concede permisos. Una misma persona puede ejercer varios papeles."><select value={value || ''} disabled={disabled || !canRead} onChange={event => onChange(event.target.value || null)}><option value="">Sin asignar</option>{value && !people.data?.items.some(item => item.id === value) && <option value={value}>{current?.name || 'Persona conservada'}{current?.active === false ? ' (inactiva; asignación conservada)' : ''}</option>}{people.data?.items.map(item => <option key={item.id} value={item.id}>{item.name}{item.reference || item.email ? ` · ${item.reference || item.email}` : ''}</option>)}</select></Field>
    {canRead && <Pagination offset={offset} total={people.data?.total || 0} limit={pageSize} onChange={setOffset}/>} {people.error && <ErrorState error={people.error}/>} {detail.error && <ErrorState error={detail.error}/>}
    {canManage && <button className="text-button" type="button" disabled={disabled} onClick={() => setCreating(true)}>Agregar nuevo · {label}</button>}
    {creating && <PersonEditor onClose={() => setCreating(false)} onSaved={person => { setCreated(person); if (person.active) onChange(person.id); setCreating(false) }}/>} {!canRead && <small>Tu rol no permite consultar personas de gobierno.</small>}
  </div>
}
