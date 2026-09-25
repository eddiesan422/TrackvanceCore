import { useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, Route, Routes, useNavigate } from 'react-router-dom'
import { ArrowRight, ArrowUpRight, Database, Play, Plus, Send, Server } from 'lucide-react'
import { api } from '../../api/client'
import type { RecordData } from '../../api/client'
import { usePermission } from '../../app/session'
import { Badge, Empty, ErrorState, Loading, number, PageHeading, SearchBox } from '../../components/ui'
import { DestinationDetail, DestinationsPage } from './Destinations'
import { DeliveryBuilder } from './DeliveryBuilder'
import { DeliveryNavigation } from './DeliveryNavigation'
import type { DeliveryConfiguration, DeliveryDestination } from './types'
import './delivery.css'

function configValue(configuration: DeliveryConfiguration, key: string) {
  return configuration.config?.[key]
}

export function DeliveriesPage() {
  const canConfigure = usePermission('configurations:write'), canExecute = usePermission('runs:execute')
  const [search, setSearch] = useState('')
  const navigate = useNavigate(), cache = useQueryClient(), keys = useRef(new Map<string, string>())
  const configurations = useQuery({ queryKey: ['delivery-configurations'], queryFn: () => api<{ items: DeliveryConfiguration[]; total: number }>('/delivery/configurations'), refetchInterval: 15000 })
  const destinations = useQuery({ queryKey: ['delivery-destinations'], queryFn: () => api<{ items: DeliveryDestination[]; total: number }>('/delivery/destinations') })
  const run = useMutation({
    mutationFn: async (configuration: DeliveryConfiguration) => {
      let key = keys.current.get(configuration.id)
      if (!key) { key = crypto.randomUUID(); keys.current.set(configuration.id, key) }
      return api<{ id: string }>('/delivery/runs', { method: 'POST', headers: { 'Idempotency-Key': key }, body: JSON.stringify({ configuration_id: configuration.id, dataset_version_id: configuration.dataset_version_id || configValue(configuration, 'dataset_version_id') }) })
    },
    onSuccess: (data, configuration) => { keys.current.delete(configuration.id); cache.invalidateQueries({ queryKey: ['runs'] }); cache.invalidateQueries({ queryKey: ['dashboard'] }); navigate(`/runs/${data.id}`) },
  })
  const destinationNames = new Map((destinations.data?.items || []).map(item => [item.id, item.name]))
  const superseded = new Set((configurations.data?.items || []).map(item => String(item.previous_version_id || '')).filter(Boolean))
  const items = (configurations.data?.items || []).filter(item => {
    const destinationId = String(item.destination_id || configValue(item, 'destination_id') || '')
    return `${item.name} ${item.dataset_name || ''} ${item.destination_name || destinationNames.get(destinationId) || ''} ${configValue(item, 'write_strategy') || ''}`.toLocaleLowerCase().includes(search.toLocaleLowerCase())
  })
  return <><DeliveryNavigation/><PageHeading eyebrow="04 / PUBLICACIÓN CONTROLADA" title="Data Delivery" description="Entrega versiones inmutables de tus datasets hacia PostgreSQL y SQL Server con preflight, evidencia y trazabilidad." action={<><Link className="button secondary" to="/delivery/destinations"><Server size={16}/> Administrar destinos</Link><Link className={`button primary ${canConfigure ? '' : 'disabled-link'}`} aria-disabled={!canConfigure} to={canConfigure ? '/delivery/new' : '#'}><Plus size={16}/> Nueva entrega</Link></>}/>
    <div className="module-intro delivery"><Send size={28}/><div><strong>De una DatasetVersion a un destino externo, sin transformar datos.</strong><p>Cada configuración fija artifact, destino, target, mapping técnico y estrategia. La escritura ocurre en un delivery-worker separado.</p></div><span className="version-pill">{number(configurations.data?.total || 0)} entregas</span></div>
    <section className="panel"><div className="tabs module-tabs"><button className="active">Configuraciones publicadas<span>{number(configurations.data?.total || 0)}</span></button></div><div className="table-toolbar"><SearchBox value={search} onChange={setSearch} placeholder="Buscar entrega, dataset o destino…"/><span className="muted">Snapshots inmutables</span></div>
      {configurations.isPending ? <Loading text="Cargando entregas…"/> : configurations.error ? <ErrorState error={configurations.error} retry={() => configurations.refetch()}/> : !items.length ? <Empty title={search ? 'Sin coincidencias' : 'Prepara tu primera entrega'} description={search ? 'Prueba otro nombre, dataset o destino.' : 'Selecciona una DatasetVersion, valida el target y publica una configuración antes de ejecutar.'} action={<Link className="button primary" to={search ? '/delivery' : '/delivery/new'}>{search ? 'Limpiar búsqueda' : 'Nueva entrega'}</Link>}/> : <div className="config-list delivery-config-list">{items.map(configuration => {
        const target = (configuration.config?.target || {}) as RecordData, strategy = String(configValue(configuration, 'write_strategy') || 'APPEND')
        const destinationId = String(configuration.destination_id || configValue(configuration, 'destination_id') || '')
        const destinationName = configuration.destination_name || destinationNames.get(destinationId) || (destinationId ? `Destino ${destinationId.slice(0, 8)}` : 'Destino')
        const latest = configuration.latest_run as RecordData | undefined
        return <article className="config-card" key={configuration.id}><div className="config-icon delivery"><Send size={23}/></div><div className="config-info"><div className="config-title"><h3>{configuration.name}</h3><span className="version-pill">v{configuration.version}</span><Badge value={configuration.status || 'PUBLISHED'}/></div><p>{configuration.description || 'Publicación versionada de una DatasetVersion.'}</p><div className="config-meta"><span><Database size={12}/> {configuration.dataset_name || 'Dataset'} · v{configuration.dataset_version || String(configuration.dataset_version_id || configValue(configuration, 'dataset_version_id') || '').slice(0, 8)}</span><ArrowRight size={12}/><span><Server size={12}/> {destinationName}</span><span>·</span><span className="mono">{String(target.schema_name || '')}.{String(target.table_name || '')}</span></div><div className="delivery-config-badges"><Badge value={target.mode}/><Badge value={strategy}/><span>{number(Array.isArray(configuration.config?.columns) ? configuration.config.columns.length : 0)} columnas</span></div></div><div className="config-actions">{superseded.has(configuration.id) ? <span className="muted">Versión supersedida</span> : <Link className="text-button" to={`/delivery/new?configuration=${configuration.id}`}>Nueva versión</Link>}{latest ? <Link to={`/runs/${latest.id}`} className="last-run"><small>Última entrega</small><Badge value={latest.delivery_status || latest.status}/></Link> : <span className="muted">Sin ejecuciones</span>}<button className="button secondary small" disabled={!canExecute || run.isPending} onClick={() => run.mutate(configuration)}><Play size={14}/> Ejecutar</button></div></article>
      })}</div>}
      {run.error && <ErrorState error={run.error}/>}
    </section>
  </>
}

export function DeliveryRoutes() {
  return <Routes><Route index element={<DeliveriesPage/>}/><Route path="new" element={<DeliveryBuilder/>}/><Route path="destinations" element={<DestinationsPage/>}/><Route path="destinations/:id" element={<DestinationDetail/>}/><Route path="*" element={<div className="empty-state"><h1>Sección de Data Delivery no encontrada</h1><Link className="button primary" to="/delivery">Volver a Entregas <ArrowUpRight size={15}/></Link></div>}/></Routes>
}

export { DeliveryNavigation }
