import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate } from 'react-router-dom'
import { Upload } from 'lucide-react'
import { api, ApiError, uploadBinary } from '../../api/client'
import type { RecordData } from '../../api/client'
import { usePermission } from '../../app/session'
import { Badge, date, Empty, ErrorState, Field, Loading, Modal, Notice, number } from '../../components/ui'
import { BusinessAreaField, businessAreaError } from './BusinessAreaField'
import { AcquisitionLimits, getAcquisitionLimits, limitFormat, limitsKey, limitValue, useAcquisitionLimits } from './AcquisitionLimits'
import type { EffectiveLimits } from './AcquisitionLimits'

export type Acquisition = {
  id: string; dataset_id: string; source_type: string; filename: string
  status: 'QUEUED' | 'RUNNING' | 'SUCCESS' | 'FAILED' | 'CANCELLED'; stage: string
  attempts: number; processed_rows: number; processed_bytes: number
  total_rows: number | null; total_bytes: number | null; duration_seconds: number | null
  cancel_requested: boolean; output_version_id: string | null
  error_code: string | null; error_message: string | null; initiated_by: string
  created_at: string; started_at: string | null; finished_at: string | null
  received_bytes?: number | null; materialized_rows?: number; materialized_bytes?: number; published_rows?: number | null
  route_limits?: EffectiveLimits | null; error?: { code: string; message: string; details: Record<string, unknown> | null; reference: string | null } | null
}
type Inspection = {
  format: string; format_label: string; filename: string; row_count: number | null
  sampled_rows: number; sheets: string[]; selected_sheet: string | null; detected_delimiter: string | null
  reader_options: { sheet_name?: string; delimiter?: string }
  columns: { name: string; logical_type: string; semantic_tag?: string | null }[]
  effective_limits?: EffectiveLimits
}
type Received = { upload: { id: string; filename: string; size_bytes: number; transfer_complete: boolean }; inspection: Inspection }
type Overrides = Record<string, { logical_type?: string; semantic_tag?: 'IDENTIFIER' }>
const terminal = (run?: Acquisition) => !!run && ['SUCCESS', 'FAILED', 'CANCELLED'].includes(run.status)
const stages: Record<string, string> = { QUEUED: 'En cola', READING: 'Leyendo fuente', MATERIALIZING: 'Materializando partes', PROFILING: 'Perfilando toda la población', PUBLISHING: 'Publicando versión', COMPLETED: 'Completada', FAILED: 'Fallida', CANCELLED: 'Cancelada' }
const bytes = (value: number) => value >= 1024 * 1024 ? `${number(value / (1024 * 1024))} MiB` : `${number(value / 1024)} KiB`

function AcquisitionDiagnostic({ run }: { run: Acquisition }) {
  const error = run.error || (run.error_message ? { code: run.error_code, message: run.error_message, reference: null } : null)
  return <>{error && <Notice>{error.message} {error.code && <span className="mono">({error.code})</span>}{error.reference && <small> Referencia: {error.reference}</small>}</Notice>}
    {run.status === 'FAILED' && <p className="muted">No se publicó una versión parcial. Los contadores indican lo materializado antes de detenerse; no determinan el total de registros de la fuente.</p>}</>
}

function AcquisitionCounters({ run }: { run: Acquisition }) {
  return <>{run.received_bytes != null && <>{bytes(run.received_bytes)} recibidos · </>}{number(run.materialized_rows ?? run.processed_rows)} registros materializados · {bytes(run.materialized_bytes ?? run.processed_bytes)} materializados{run.status === 'SUCCESS' && run.published_rows != null && <> · {number(run.published_rows)} registros publicados</>}</>
}

export function AcquisitionStatus({ id, onCompleted, onFinished }: { id: string; onCompleted?: (versionId: string) => void; onFinished?: () => void }) {
  const cache = useQueryClient(), canWrite = usePermission('datasets:write')
  const notified = useRef<string | null>(null)
  const state = useQuery({ queryKey: ['acquisition', id], queryFn: () => api<Acquisition>(`/acquisitions/${id}`), refetchInterval: query => terminal(query.state.data) ? false : 2000 })
  const cancel = useMutation({ mutationFn: () => api<Acquisition>(`/acquisitions/${id}/cancel`, { method: 'POST' }), onSuccess: data => { cache.setQueryData(['acquisition', id], data); cache.invalidateQueries({ queryKey: ['acquisitions'] }) } })
  const run = state.data
  useEffect(() => {
    if (run && terminal(run) && notified.current !== `${run.id}:${run.status}`) {
      notified.current = `${run.id}:${run.status}`
      onFinished?.()
      if (run.status === 'SUCCESS' && run.output_version_id) {
        cache.invalidateQueries({ queryKey: ['dataset', run.dataset_id] })
        cache.invalidateQueries({ queryKey: ['datasets'] })
        cache.invalidateQueries({ queryKey: ['dashboard'] })
        onCompleted?.(run.output_version_id)
      }
    }
  }, [run, cache, onCompleted, onFinished])
  if (state.error) return <ErrorState error={state.error} retry={() => state.refetch()}/>
  if (!run) return <Loading text="Consultando adquisición…"/>
  return <div aria-live="polite"><p><Badge value={run.status}/> <strong>{stages[run.stage] || run.stage}</strong> · <AcquisitionCounters run={run}/>{run.duration_seconds != null && <> · {number(run.duration_seconds)} s</>}</p>
    {run.status === 'SUCCESS' && <Notice success>Versión publicada después de leer y perfilar la fuente completa.</Notice>}
    <AcquisitionDiagnostic run={run}/>
    {terminal(run) && run.route_limits && <details><summary>Límites de este intento</summary><AcquisitionLimits limits={run.route_limits}/></details>}
    {terminal(run) && run.route_limits === null && <p className="muted">No hay un descriptor de límites histórico disponible para este intento. Se conservan su resultado y su diagnóstico originales.</p>}
    {run.status === 'CANCELLED' && <Notice>La adquisición se canceló. Las versiones anteriores siguen disponibles.</Notice>}
    {!terminal(run) && <><p className="muted">Puedes salir de esta pantalla; el trabajo continúa en el servidor.</p><button className="button secondary small" disabled={!canWrite || run.cancel_requested || cancel.isPending} onClick={() => cancel.mutate()}>{run.cancel_requested ? 'Cancelación solicitada…' : 'Cancelar adquisición'}</button></>}
    {cancel.error && <ErrorState error={cancel.error}/>}</div>
}

export function AcquisitionHistory({ datasetId, onCompleted }: { datasetId?: string; onCompleted?: (versionId: string) => void }) {
  const canRead = usePermission('datasets:read'), cache = useQueryClient()
  const observed = useRef(new Map<string, string>())
  const runs = useQuery({ queryKey: ['acquisitions', datasetId || 'all'], queryFn: () => api<{ items: Acquisition[]; total: number }>(`/acquisitions?limit=100${datasetId ? `&dataset_id=${encodeURIComponent(datasetId)}` : ''}`), enabled: canRead,
    refetchInterval: query => query.state.data?.items.some(run => !terminal(run)) ? 2000 : 10000 })
  useEffect(() => {
    for (const run of runs.data?.items || []) {
      const previous = observed.current.get(run.id)
      observed.current.set(run.id, run.status)
      if (run.status === 'SUCCESS' && run.output_version_id && previous && previous !== 'SUCCESS') {
        cache.invalidateQueries({ queryKey: ['dataset', run.dataset_id] })
        cache.invalidateQueries({ queryKey: ['datasets'] })
        if (datasetId) onCompleted?.(run.output_version_id)
      }
    }
  }, [runs.data, cache, datasetId, onCompleted])
  if (!canRead) return null
  return <section className="panel"><div className="panel-heading"><div><h2>Historial de adquisiciones</h2><p>Recepción, lectura y publicación de nuevas versiones.</p></div><button className="button secondary small" onClick={() => runs.refetch()}>Actualizar</button></div>
    {runs.isPending ? <Loading/> : runs.error ? <ErrorState error={runs.error} retry={() => runs.refetch()}/> : !runs.data?.items.length ? <Empty title="Sin adquisiciones" description="Las nuevas cargas y consultas a fuentes aparecerán aquí."/> : <div className="table-scroll"><table><thead><tr><th>Fuente</th><th>Estado y progreso</th><th>Registrada</th><th>Usuario</th><th/></tr></thead><tbody>{runs.data.items.map(run => <tr key={run.id}><td>{run.filename}<span className="table-subtitle">{run.source_type} · intento {number(run.attempts)}</span></td><td>{terminal(run) ? <><Badge value={run.status}/><span className="table-subtitle"><AcquisitionCounters run={run}/></span><AcquisitionDiagnostic run={run}/>{run.route_limits && <details><summary>Límites del intento</summary><AcquisitionLimits limits={run.route_limits}/></details>}{run.route_limits === null && <p className="muted">No hay un descriptor de límites histórico disponible para este intento. Se conservan su resultado y su diagnóstico originales.</p>}</> : <AcquisitionStatus id={run.id}/>}</td><td>{date(run.created_at)}</td><td>{run.initiated_by}</td><td>{run.output_version_id && <Link className="text-link" to={`/datasets/${run.dataset_id}?version=${run.output_version_id}`}>Ver versión</Link>}</td></tr>)}</tbody></table></div>}</section>
}

export function AcquisitionDialog({ open, onClose, onLegacy, datasetId, datasetName, datasetDomain, existingDatasets = [] }: { open: boolean; onClose: () => void; onLegacy?: () => void; datasetId?: string; datasetName?: string; datasetDomain?: string; existingDatasets?: RecordData[] }) {
  const navigate = useNavigate(), cache = useQueryClient()
  const [file, setFile] = useState<File | null>(null), [received, setReceived] = useState<Received | null>(null)
  const [transferred, setTransferred] = useState(0), [name, setName] = useState(datasetName || ''), [domain, setDomain] = useState('Operaciones')
  const [options, setOptions] = useState<{ sheet_name?: string; delimiter?: string }>({}), [overrides, setOverrides] = useState<Overrides>({})
  const format = received?.inspection.format || limitFormat(file?.name)
  const limits = useAcquisitionLimits(format, 'ASYNC_ACQUISITION', open)
  const transfer = useRef<AbortController | null>(null), registeredDataset = useRef<string | undefined>(datasetId), requestKey = useRef(crypto.randomUUID())
  const [createdMetadata, setCreatedMetadata] = useState<{ name: string; domain: string } | null>(null)
  useEffect(() => () => transfer.current?.abort(), [])
  const stage = useMutation({ mutationFn: async (selection: File) => {
    transfer.current?.abort(); const controller = new AbortController(); transfer.current = controller
    const descriptor = await cache.fetchQuery({ queryKey: limitsKey(limitFormat(selection.name), 'ASYNC_ACQUISITION'), queryFn: () => getAcquisitionLimits(limitFormat(selection.name), 'ASYNC_ACQUISITION') })
    if (controller.signal.aborted) throw new ApiError('Transferencia cancelada antes de registrar la adquisición.', 0)
    // .json may contain JSON Lines. Its tabular 10 MiB cap becomes reliable
    // only after bounded content inspection; staging still has a byte cap.
    const transferLimits = limitFormat(selection.name) === 'JSON'
      ? await cache.fetchQuery({ queryKey: limitsKey('JSON_LINES', 'ASYNC_ACQUISITION'), queryFn: () => getAcquisitionLimits('JSON_LINES', 'ASYNC_ACQUISITION') })
      : descriptor
    if (controller.signal.aborted) throw new ApiError('Transferencia cancelada antes de registrar la adquisición.', 0)
    const maximum = transferLimits.limits.compressed_bytes || transferLimits.limits.max_upload_bytes
    if (maximum && selection.size > maximum.max) throw new ApiError(`El archivo supera el máximo configurado para adquisición: ${limitValue(maximum.max, maximum.unit)}.`, 422, undefined, 'UPLOAD_TOO_LARGE')
    return uploadBinary<Received>(`/datasets/uploads/stage?filename=${encodeURIComponent(selection.name)}`, selection, setTransferred, controller.signal)
  }, onSuccess: data => { setReceived(data); setOptions(data.inspection.reader_options); requestKey.current = crypto.randomUUID() } })
  const inspect = useMutation({ mutationFn: (value: typeof options) => api<Inspection>(`/datasets/uploads/${received!.upload.id}/inspect?reader_options=${encodeURIComponent(JSON.stringify(value))}`), onSuccess: data => { setReceived(value => value ? { ...value, inspection: data } : value); setOverrides({}) } })
  const register = useMutation({ mutationFn: async () => {
    let identifier = registeredDataset.current
    if (!identifier) {
      const match = existingDatasets.find(item => String(item.name).trim().replace(/\s+/g, ' ').toLocaleLowerCase('es') === name.trim().replace(/\s+/g, ' ').toLocaleLowerCase('es'))
      identifier = match?.id
      if (!identifier) {
        identifier = (await api<{ id: string }>('/datasets', { method: 'POST', body: JSON.stringify({ name: name.trim(), domain: domain.trim() }) })).id
        setCreatedMetadata({ name: name.trim(), domain: domain.trim() })
      }
      registeredDataset.current = identifier
    }
    return api<Acquisition>(`/datasets/${identifier}/acquisitions`, { method: 'POST', headers: { 'Idempotency-Key': requestKey.current }, body: JSON.stringify({ upload_id: received!.upload.id, reader_options: options, column_overrides: overrides }) })
  }, onSuccess: run => { cache.invalidateQueries({ queryKey: ['acquisitions'] }); cache.invalidateQueries({ queryKey: ['datasets'] }); onClose(); navigate(`/datasets/${run.dataset_id}`) } })
  const inspection = received?.inspection
  const effectiveLimits = inspection?.effective_limits || limits.data
  const matching = !datasetId && existingDatasets.find(item => String(item.name).trim().replace(/\s+/g, ' ').toLocaleLowerCase('es') === name.trim().replace(/\s+/g, ' ').toLocaleLowerCase('es'))
  const fixedDomain = datasetId ? datasetDomain ?? '' : createdMetadata ? createdMetadata.domain : matching ? String(matching.domain || '') : undefined
  const areaValid = fixedDomain !== undefined || !businessAreaError(domain)
  const canRegister = !!received && !!effectiveLimits && !stage.isPending && !inspect.isPending && !inspect.error && !register.isPending && !!name.trim() && areaValid
  function changeOptions(value: typeof options) { setOptions(value); requestKey.current = crypto.randomUUID(); inspect.mutate(value) }
  return <Modal open={open} onOpenChange={value => { if (!value && !register.isPending) onClose() }} title={datasetId ? 'Adquirir nueva versión' : 'Adquirir dataset'} description="Primero recibe el archivo y confirma su lectura. El análisis completo continúa en segundo plano." wide>
    <form className="form-stack" onSubmit={event => { event.preventDefault(); if (canRegister) register.mutate() }}>
      <Field label="Archivo"><input type="file" accept=".csv,.txt,.tsv,.json,.jsonl,.ndjson,.parquet,.pq,.xlsx" disabled={stage.isPending || register.isPending} onChange={event => { const selected = event.target.files?.[0]; if (!selected) return; setFile(selected); setReceived(null); setTransferred(0); setOverrides({}); if (!createdMetadata) registeredDataset.current = datasetId; if (!datasetId && !name.trim()) setName(selected.name.replace(/\.[^.]+$/, '').replaceAll('_', ' ')); stage.mutate(selected) }}/></Field>
      {stage.isPending && <><Loading text={transferred === file?.size ? 'Transferencia enviada; verificando recepción…' : 'Recibiendo archivo…'}/><p>{bytes(transferred)} de {bytes(file?.size || 0)} transferidos. La adquisición todavía no está registrada.</p><button className="button secondary" type="button" onClick={() => transfer.current?.abort()}>Cancelar transferencia</button></>}
      {stage.error && <ErrorState error={stage.error}/>}
      <AcquisitionLimits limits={effectiveLimits} loading={limits.isPending} error={limits.error} retry={() => { void limits.refetch() }}/>
      {received && <><Notice success>Transferencia completa · {bytes(received.upload.size_bytes)}. Confirma las opciones antes de registrar la adquisición.</Notice>
        <p>{inspection?.format_label} · {inspection?.sampled_rows ? <>muestra de {number(inspection.sampled_rows)} registros</> : 'inspección de metadata disponible'}{inspection?.row_count != null && <> · {number(inspection.row_count)} registros declarados</>}</p>
        {(inspection?.format === 'CSV' || inspection?.format === 'TXT') && <Field label="Delimitador"><select value={options.delimiter || inspection.detected_delimiter || ','} disabled={inspect.isPending || register.isPending} onChange={event => changeOptions({ ...options, delimiter: event.target.value })}><option value=",">Coma (,)</option><option value=";">Punto y coma (;)</option><option value={'\t'}>Tabulación</option><option value="|">Barra vertical (|)</option></select></Field>}
        {inspection?.format === 'XLSX' && <Field label="Hoja"><select value={options.sheet_name || inspection.selected_sheet || ''} disabled={inspect.isPending || register.isPending} onChange={event => changeOptions({ ...options, sheet_name: event.target.value })}>{inspection.sheets.map(sheet => <option key={sheet}>{sheet}</option>)}</select></Field>}
        {inspect.isPending && <Loading text="Inspeccionando opciones…"/>}{inspect.error && <ErrorState error={inspect.error}/>}
        <Notice>{effectiveLimits?.inspection_limited ? 'La inspección preliminar está limitada. Puedes seleccionar la hoja y registrar sin overrides; sus columnas y tipos se comprobarán durante la adquisición. ' : 'La vista previa es una muestra. '}El perfil final y los tipos confirmados se validan contra la fuente completa, con los límites efectivos mostrados para esta ruta.</Notice>
        <div className="table-scroll"><table><thead><tr><th>Columna</th><th>Tipo confirmado</th><th>Identificador</th></tr></thead><tbody>{inspection?.columns.map(column => <tr key={column.name}><td className="mono">{column.name}</td><td><select aria-label={`Tipo de ${column.name}`} disabled={register.isPending || overrides[column.name]?.semantic_tag === 'IDENTIFIER'} value={overrides[column.name]?.logical_type || ''} onChange={event => { const value = event.target.value; setOverrides(current => ({ ...current, [column.name]: value ? { ...current[column.name], logical_type: value } : {} })); requestKey.current = crypto.randomUUID() }}><option value="">Inferir completamente · muestra {column.logical_type}</option>{['STRING', 'DECIMAL', 'INT64', 'DATE', 'TIMESTAMP', 'BOOLEAN'].map(type => <option key={type}>{type}</option>)}</select></td><td><input type="checkbox" aria-label={`Identificador ${column.name}`} checked={overrides[column.name]?.semantic_tag === 'IDENTIFIER' || column.semantic_tag === 'IDENTIFIER'} disabled={register.isPending || column.semantic_tag === 'IDENTIFIER'} onChange={event => { setOverrides(current => ({ ...current, [column.name]: event.target.checked ? { logical_type: 'STRING', semantic_tag: 'IDENTIFIER' } : {} })); requestKey.current = crypto.randomUUID() }}/></td></tr>)}</tbody></table></div>
      </>}
      <div className="form-grid">{!datasetId && <Field label="Nombre del dataset"><input required maxLength={160} value={name} disabled={register.isPending || !!createdMetadata} onChange={event => { setName(event.target.value); registeredDataset.current = undefined; requestKey.current = crypto.randomUUID() }}/></Field>}<BusinessAreaField value={domain} datasets={existingDatasets} fixedValue={fixedDomain} disabled={register.isPending} onChange={value => { setDomain(value); requestKey.current = crypto.randomUUID() }}/></div>
      {matching && <Notice>Ya existe “{matching.name}”. Este archivo se agregará como una nueva versión inmutable del dataset existente.</Notice>}
      {register.error && <ErrorState error={register.error}/>}
      {onLegacy && <button type="button" className="text-button" disabled={stage.isPending || register.isPending} onClick={() => { onClose(); onLegacy() }}>Usar carga rápida limitada</button>}
      <div className="modal-footer"><button type="button" className="button secondary" disabled={register.isPending} onClick={onClose}>Cerrar</button><button className="button primary" disabled={!canRegister}><Upload size={16}/>{register.isPending ? 'Registrando adquisición…' : 'Registrar adquisición'}</button></div>
    </form>
  </Modal>
}
