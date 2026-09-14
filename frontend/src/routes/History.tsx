import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client'
import type { Collection } from '../api/client'
import { Empty, ErrorState, label, Loading, PageHeading, SearchBox } from '../components/ui'
import { RunsTable } from './Dashboard'

export default function HistoryPage() {
  const [search, setSearch] = useState(''), [module, setModule] = useState(''), [status, setStatus] = useState('')
  const runs = useQuery({ queryKey: ['runs', 'all'], queryFn: () => api<Collection>('/runs'), refetchInterval: 5000 })
  const items = (runs.data?.items || []).filter(item => `${item.name} ${item.id} ${item.dataset_name}`.toLowerCase().includes(search.toLowerCase()) && (!module || item.module === module) && (!status || item.status === status))
  return <><PageHeading back="/" eyebrow="TRAZABILIDAD COMPARTIDA" title="Historial de ejecuciones" description="Consulta los controles de Intake, ReconOps y Sentinel, con sus resultados y evidencia."/>
    <section className="panel"><div className="table-toolbar"><SearchBox value={search} onChange={setSearch} placeholder="Buscar ejecución o dataset…"/><div className="toolbar-right"><select aria-label="Filtrar módulo de ejecución" value={module} onChange={event => setModule(event.target.value)}><option value="">Todos los módulos</option>{['intake', 'recon', 'sentinel'].map(value => <option key={value} value={value}>{label(value)}</option>)}</select><select aria-label="Filtrar estado de ejecución" value={status} onChange={event => setStatus(event.target.value)}><option value="">Todos los estados</option>{['QUEUED', 'RUNNING', 'SUCCESS', 'FAILED', 'CANCELLED'].map(value => <option key={value} value={value}>{label(value)}</option>)}</select></div></div>
    {runs.isPending ? <Loading/> : runs.error ? <ErrorState error={runs.error} retry={() => runs.refetch()}/> : !items.length ? <Empty title="Sin ejecuciones en esta vista" description="Ajusta los filtros o inicia un control desde cualquiera de los tres módulos."/> : <RunsTable runs={items}/>}
    </section></>
}
