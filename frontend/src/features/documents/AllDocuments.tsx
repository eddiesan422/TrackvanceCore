import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { api } from '../../api/client'
import type { Collection, RecordData } from '../../api/client'
import { usePermission } from '../../app/session'
import { Badge, date, Empty, ErrorState, Field, Loading, Pagination, SearchBox } from '../../components/ui'

function boundary(value: string, nextDay = false) {
  const result = new Date(`${value}T00:00:00`)
  if (nextDay) result.setDate(result.getDate() + 1)
  return result.toISOString()
}

export default function AllDocuments() {
  const [purpose, setPurpose] = useState('OPERATIONAL')
  const [search, setSearch] = useState('')
  const [familySearch, setFamilySearch] = useState('')
  const [family, setFamily] = useState('')
  const [status, setStatus] = useState('')
  const [from, setFrom] = useState('')
  const [to, setTo] = useState('')
  const [mode, setMode] = useState('')
  const [publication, setPublication] = useState('')
  const [order, setOrder] = useState('received_at:desc')
  const [offset, setOffset] = useState(0)
  const [pageSize, setPageSize] = useState('25')
  const download = usePermission('documents:download')
  const review = usePermission('documents:review')
  const change = (setter: (value: string) => void, value: string) => { setter(value); setOffset(0) }
  const families = useQuery({ queryKey: ['document-global-family-options', familySearch], queryFn: () => api<Collection>(`/documents/families?search=${encodeURIComponent(familySearch)}&limit=100`) })
  const [orderBy, direction] = order.split(':')
  const params = new URLSearchParams({ search, offset: String(offset), limit: pageSize, purpose, order_by: orderBy, direction })
  if (family) params.set('family_id', family)
  if (status) params.set('status', status)
  if (mode) params.set('mode', mode)
  if (publication) params.set('publication', publication)
  if (from) params.set('received_from', boundary(from))
  if (to) params.set('received_to', boundary(to, true))
  const documents = useQuery({ queryKey: ['documents', 'global', params.toString()], queryFn: () => api<Collection>(`/documents/originals?${params}`), refetchInterval: 5000 })
  return <section className="document-global" aria-labelledby="all-documents-heading">
    <div className="document-global-heading"><div><h2 id="all-documents-heading">Todos los documentos</h2><p>Consulta recepciones operativas y ejemplos históricos por separado. Las nuevas referencias y sus borradores se consultan en la configuración de cada familia.</p></div><span className="document-scope-note">Familias autorizadas · página de {pageSize} documentos</span></div>
    <div className="document-global-filters panel"><Field label="Tipo de documento"><select value={purpose} onChange={event => change(setPurpose, event.target.value)}><option value="OPERATIONAL">Documentos operativos</option><option value="LEGACY_REFERENCE">Ejemplos históricos de plantilla</option></select></Field>
      <div className="document-global-search"><SearchBox value={search} onChange={value => change(setSearch, value)} placeholder="Buscar nombre, referencia, identificador extraído o familia"/></div>
      <Field label="Buscar familia para filtrar"><input value={familySearch} maxLength={140} placeholder="Nombre de familia" onChange={event => setFamilySearch(event.target.value)}/></Field>
      <Field label="Familia documental"><select value={family} onChange={event => change(setFamily, event.target.value)}><option value="">Todas las familias autorizadas</option>{family && !families.data?.items.some(item => item.id === family) && <option value={family}>Familia seleccionada</option>}{families.data?.items.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></Field>
      <Field label="Estado del documento"><select value={status} onChange={event => change(setStatus, event.target.value)}><option value="">Todos los estados</option>{['QUEUED', 'RUNNING', 'REVIEW_REQUIRED', 'REVIEWED', 'PUBLISHED', 'FAILED', 'CANCELLED', 'DUPLICATE_FILE'].map(value => <option key={value} value={value}>{({ QUEUED: 'En cola', RUNNING: 'Extrayendo', REVIEW_REQUIRED: 'Requiere revisión', REVIEWED: 'Revisado', PUBLISHED: 'Publicado', FAILED: 'Fallido', CANCELLED: 'Cancelado', DUPLICATE_FILE: 'Archivo duplicado' } as Record<string, string>)[value]}</option>)}</select></Field>
      <Field label="Recepción desde"><input type="date" value={from} max={to || undefined} onChange={event => change(setFrom, event.target.value)}/></Field>
      <Field label="Recepción hasta" hint="Incluye el día completo en tu zona horaria."><input type="date" value={to} min={from || undefined} onChange={event => change(setTo, event.target.value)}/></Field>
      <Field label="Modalidad de recepción"><select value={mode} onChange={event => change(setMode, event.target.value)}><option value="">Todas las modalidades</option><option value="INDIVIDUAL">Por documento</option><option value="SCHEDULED">Lotes programados</option></select></Field>
      <Field label="Publicación del documento"><select value={publication} onChange={event => change(setPublication, event.target.value)}><option value="">Todas las publicaciones</option><option value="PUBLISHED">Con publicación</option><option value="UNPUBLISHED">Sin publicación</option><option value="BATCHED">Incluido en un lote</option></select></Field>
      <Field label="Ordenar documentos"><select value={order} onChange={event => change(setOrder, event.target.value)}><option value="received_at:desc">Recepción: más reciente</option><option value="received_at:asc">Recepción: más antigua</option><option value="filename:asc">Nombre: A a Z</option><option value="filename:desc">Nombre: Z a A</option><option value="family:asc">Familia: A a Z</option><option value="status:asc">Estado: A a Z</option></select></Field>
      <Field label="Documentos por página"><select value={pageSize} onChange={event => change(setPageSize, event.target.value)}>{['10', '25', '50', '100'].map(value => <option key={value} value={value}>{value}</option>)}</select></Field>
    </div>
    <p>{family ? <Link to={`/documents/families/${family}?view=references`}>Abrir referencias y borradores de esta familia</Link> : <span>Elige una familia para abrir su espacio de referencias y borradores.</span>}</p>
    {families.error && <ErrorState error={families.error}/>}{documents.error && <ErrorState error={documents.error}/>}{documents.isLoading && <Loading/>}
    <div className="table-scroll"><table className="document-global-table"><caption className="sr-only">Documentos de las familias autorizadas, con extracción, revisión y resultados</caption><thead><tr><th>Documento y familia</th><th>Recepción</th><th>Extracción y revisión</th><th>Lote y publicación</th><th>Dataset generado</th><th>Acciones</th></tr></thead><tbody>{documents.data?.items.map(item => <DocumentRow key={item.id} item={item} download={download} review={review}/>)}</tbody></table></div>
    {documents.data?.total === 0 && <Empty title="No hay documentos con estos filtros" description="Ajusta la búsqueda, las fechas o la familia para consultar otras recepciones autorizadas."/>}
    <Pagination offset={offset} total={documents.data?.total || 0} limit={Number(pageSize)} onChange={setOffset}/>
  </section>
}

function DocumentRow({ item, download, review }: { item: RecordData; download: boolean; review: boolean }) {
  return <tr><td><Link to={item.links.document}><strong>{item.filename}</strong></Link><small><Link to={item.links.family}>{item.family_name}</Link></small><small>Referencia: {item.id}</small><small>{item.is_example ? 'Referencia histórica de plantilla · sin efecto operativo' : 'Documento operativo'}</small></td>
    <td>{date(item.received_at)}<small>{item.mode === 'SCHEDULED' ? 'Lotes programados' : 'Por documento'}</small><Badge value={item.status}/></td>
    <td>{item.extraction ? <><Link to={item.links.extraction}>Extracción · revisión {item.extraction.revision}</Link><small>{item.extraction.reviewed ? 'Revisión humana guardada' : 'Sin revisión humana'}</small><small>{date(item.extraction.created_at)}</small></> : <span>Extracción pendiente</span>}</td>
    <td>{item.batch ? <><Link to={item.batch.href}>Lote {item.batch.id.slice(0, 8)}</Link><small><Badge value={item.batch.status}/></small></> : <span>Sin lote</span>}<small>{item.publication === 'PUBLISHED' ? 'Con publicación' : 'Sin publicación'}</small></td>
    <td>{item.outputs?.length ? item.outputs.map((output: RecordData) => <div key={`${output.structure}:${output.version_id}`}><Link to={output.href}>{output.dataset_name}</Link><small>{output.structure} · versión {output.version}</small></div>) : <span>Sin resultado disponible</span>}</td>
    <td><div className="document-global-actions"><Link className="button secondary small" to={item.links.review}>{review ? 'Revisar y corregir' : 'Ver documento'}</Link>{download && <a className="document-original-link" href={item.links.original} target="_blank" rel="noreferrer">Abrir original</a>}</div></td></tr>
}
