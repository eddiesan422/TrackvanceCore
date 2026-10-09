import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../../api/client'
import { usePermission } from '../../app/session'
import { Badge, ErrorState, Loading, Notice, Pagination, SearchBox } from '../../components/ui'
import { PersonEditor } from './PersonSelect'
import type { GovernancePerson } from './PersonSelect'

export function PeopleAdmin() {
  const canRead = usePermission('people:read'), canManage = usePermission('people:manage'), cache = useQueryClient()
  const [search, setSearch] = useState(''), [offset, setOffset] = useState(0), [editing, setEditing] = useState<GovernancePerson | null | undefined>(undefined)
  const people = useQuery({ queryKey: ['governance-people', 'admin', search, offset], queryFn: () => api<{ items: GovernancePerson[]; total: number }>(`/governance/people?search=${encodeURIComponent(search)}&offset=${offset}&limit=25`), enabled: canRead })
  return <><Notice>Catálogo compartido para los tres papeles de gobierno. Las personas y las cuentas de acceso tienen identidades distintas.</Notice><div className="table-toolbar"><SearchBox value={search} onChange={value => { setSearch(value); setOffset(0) }}/>{canManage && <button className="button primary small" onClick={() => setEditing(null)}>Agregar persona</button>}</div>
    {!canRead ? <Notice>Tu rol no permite consultar personas de gobierno.</Notice> : people.isPending ? <Loading/> : people.error ? <ErrorState error={people.error}/> : <div className="table-scroll"><table><thead><tr><th>Persona</th><th>Referencia / correo</th><th>Vínculo a cuenta</th><th>Estado</th><th/></tr></thead><tbody>{people.data?.items.map(person => <tr key={person.id}><td>{person.name}</td><td>{person.reference || 'Sin referencia'}<small className="table-subtitle">{person.email}</small></td><td>{person.user_id ? 'Cuenta verificada' : 'Sin cuenta de acceso'}</td><td><Badge value={person.active ? 'ACTIVE' : 'INACTIVE'}/></td><td>{canManage && <button className="text-button" onClick={() => setEditing(person)}>Editar persona</button>}</td></tr>)}</tbody></table></div>}
    <Pagination offset={offset} total={people.data?.total || 0} limit={25} onChange={setOffset}/>{editing !== undefined && <PersonEditor person={editing || undefined} onClose={() => setEditing(undefined)} onSaved={() => { setEditing(undefined); void cache.invalidateQueries({ queryKey: ['governance-people'] }) }}/>}</>
}
