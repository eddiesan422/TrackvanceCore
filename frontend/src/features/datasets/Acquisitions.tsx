import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate } from 'react-router-dom'
import { Upload } from 'lucide-react'
import { api, uploadBinary } from '../../api/client'
import type { RecordData } from '../../api/client'
import { usePermission } from '../../app/session'
import { Badge, date, Empty, ErrorState, Field, Loading, Modal, Notice, number } from '../../components/ui'

export type Acquisition = {
  id: string; dataset_id: string; source_type: string; filename: string
  status: 'QUEUED' | 'RUNNING' | 'SUCCESS' | 'FAILED' | 'CANCELLED'; stage: string
  attempts: number; processed_rows: number; processed_bytes: number
  total_rows: number | null; total_bytes: number | null; duration_seconds: number | null
  cancel_requested: boolean; output_version_id: string | null
  error_code: string | null; error_message: string | null; initiated_by: string
  created_at: string; started_at: string | null; finished_at: string | null
}
type Inspection = {
  format: string; format_label: string; filename: string; row_count: number | null
  sampled_rows: number; sheets: string[]; selected_sheet: string | null; detected_delimiter: string | null
  reader_options: { sheet_name?: string; delimiter?: string }
  columns: { name: string; logical_type: string; semantic_tag?: string | null }[]
}
type Received = { upload: { id: string; filename: string; size_bytes: number; transfer_complete: boolean }; inspection: Inspection }
type Overrides = Record<string, { logical_type?: string; semantic_tag?: 'IDENTIFIER' }>
const terminal = (run?: Acquisition) => !!run && ['SUCCESS', 'FAILED', 'CANCELLED'].includes(run.status)
const stages: Record<string, string> = { QUEUED: 'En cola', READING: 'Leyendo fuente', MATERIALIZING: 'Materializando partes', PROFILING: 'Perfilando toda la población', PUBLISHING: 'Publicando versión', COMPLETED: 'Completada', FAILED: 'Fallida', CANCELLED: 'Cancelada' }
const bytes = (value: number) => value >= 1024 * 1024 ? `${number(value / (1024 * 1024))} MiB` : `${number(value / 1024)} KiB`

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
  return <div aria-live="polite"><p><Badge value={run.status}/> <strong>{stages[run.stage] || run.stage}</strong> · {number(run.processed_rows)} registros · {bytes(run.processed_bytes)} observados{run.duration_seconds != null && <> · {number(run.duration_seconds)} s</>}</p>
    {run.status === 'SUCCESS' && <Notice success>Versión publicada después de leer y perfilar la fuente completa.</Notice>}
    {run.error_message && <Notice>{run.error_message} {run.error_code && <span className="mono">({run.error_code})</span>}</Notice>}
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
    {runs.isPending ? <Loading/> : runs.error ? <ErrorState error={runs.error} retry={() => runs.refetch()}/> : !runs.data?.items.length ? <Empty title="Sin adquisiciones" description="Las nuevas cargas y consultas a fuentes aparecerán aquí."/> : <div className="table-scroll"><table><thead><tr><th>Fuente</th><th>Estado y progreso</th><th>Registrada</th><th>Usuario</th><th/></tr></thead><tbody>{runs.data.items.map(run => <tr key={run.id}><td>{run.filename}<span className="table-subtitle">{run.source_type} · intento {number(run.attempts)}</span></td><td>{terminal(run) ? <><Badge value={run.status}/><span className="table-subtitle">{number(run.processed_rows)} registros · {bytes(run.processed_bytes)}</span>{run.error_message && <span className="table-subtitle">{run.error_code}: {run.error_message}</span>}</> : <AcquisitionStatus id={run.id}/>}</td><td>{date(run.created_at)}</td><td>{run.initiated_by}</td><td>{run.output_version_id && <Link className="text-link" to={`/datasets/${run.dataset_id}?version=${run.output_version_id}`}>Ver versión</Link>}</td></tr>)}</tbody></table></div>}</section>
}

export function AcquisitionDialog({ open, onClose, onLegacy, datasetId, datasetName, existingDatasets = [] }: { open: boolean; onClose: () => void; onLegacy?: () => void; datasetId?: string; datasetName?: string; existingDatasets?: RecordData[] }) {
  const navigate = useNavigate(), cache = useQueryClient()
  const [file, setFile] = useState<File | null>(null), [received, setReceived] = useState<Received | null>(null)
  const [transferred, setTransferred] = useState(0), [name, setName] = useState(datasetName || ''), [domain, setDomain] = useState('Operaciones')
  const [options, setOptions] = useState<{ sheet_name?: string; delimiter?: string }>({}), [overrides, setOverrides] = useState<Overrides>({})
  const transfer = useRef<AbortController | null>(null), registeredDataset = useRef<string | undefined>(datasetId), requestKey = useRef(crypto.randomUUID())
  useEffect(() => () => transfer.current?.abort(), [])
  const stage = useMutation({ mutationFn: async (selection: File) => {
    transfer.current?.abort(); const controller = new AbortController(); transfer.current = controller
    return uploadBinary<Received>(`/datasets/uploads/stage?filename=${encodeURIComponent(selection.name)}`, selection, setTransferred, controller.signal)
  }, onSuccess: data => { setReceived(data); setOptions(data.inspection.reader_options); requestKey.current = crypto.randomUUID() } })
  const inspect = useMutation({ mutationFn: (value: typeof options) => api<Inspection>(`/datasets/uploads/${received!.upload.id}/inspect?reader_options=${encodeURIComponent(JSON.stringify(value))}`), onSuccess: data => { setReceived(value => value ? { ...value, inspection: data } : value); setOverrides({}) } })
  const register = useMutation({ mutationFn: async () => {
    let identifier = registeredDataset.current
    if (!identifier) {
      const match = existingDatasets.find(item => String(item.name).trim().replace(/\s+/g, ' ').toLocaleLowerCase('es') === name.trim().replace(/\s+/g, ' ').toLocaleLowerCase('es'))
      identifier = match?.id
      if (!identifier) identifier = (await api<{ id: string }>('/datasets', { method: 'POST', body: JSON.stringify({ name: name.trim(), domain: domain.trim() }) })).id
      registeredDataset.current = identifier
    }
    return api<Acquisition>(`/datasets/${identifier}/acquisitions`, { method: 'POST', headers: { 'Idempotency-Key': requestKey.current }, body: JSON.stringify({ upload_id: received!.upload.id, reader_options: options, column_overrides: overrides }) })
  }, onSuccess: run => { cache.invalidateQueries({ queryKey: ['acquisitions'] }); cache.invalidateQueries({ queryKey: ['datasets'] }); onClose(); navigate(`/datasets/${run.dataset_id}`) } })
  const inspection = received?.inspection
  const matching = !datasetId && existingDatasets.find(item => String(item.name).trim().replace(/\s+/g, ' ').toLocaleLowerCase('es') === name.trim().replace(/\s+/g, ' ').toLocaleLowerCase('es'))
  function changeOptions(value: typeof options) { setOptions(value); requestKey.current = crypto.randomUUID(); inspect.mutate(value) }
  return <Modal open={open} onOpenChange={value => { if (!value && !register.isPending) onClose() }} title={datasetId ? 'Adquirir nueva versión' : 'Adquirir dataset'} description="Primero recibe el archivo y confirma su lectura. El análisis completo continúa en segundo plano." wide>
    <form className="form-stack" onSubmit={event => { event.preventDefault(); register.mutate() }}>
      <Field label="Archivo"><input type="file" accept=".csv,.txt,.tsv,.json,.jsonl,.ndjson,.parquet,.pq,.xlsx" disabled={stage.isPending || register.isPending} onChange={event => { const selected = event.target.files?.[0]; if (!selected) return; setFile(selected); setReceived(null); setTransferred(0); setOverrides({}); registeredDataset.current = datasetId; if (!datasetId && !name.trim()) setName(selected.name.replace(/\.[^.]+$/, '').replaceAll('_', ' ')); stage.mutate(selected) }}/></Field>
      {stage.isPending && <><Loading text={transferred === file?.size ? 'Transferencia enviada; verificando recepción…' : 'Recibiendo archivo…'}/><p>{bytes(transferred)} de {bytes(file?.size || 0)} transferidos. La adquisición todavía no está registrada.</p><button className="button secondary" type="button" onClick={() => transfer.current?.abort()}>Cancelar transferencia</button></>}
      {stage.error && <ErrorState error={stage.error}/>}
      {received && <><Notice success>Transferencia completa · {bytes(received.upload.size_bytes)}. Confirma las opciones antes de registrar la adquisición.</Notice>
        <p>{inspection?.format_label} · muestra de {number(inspection?.sampled_rows)} registros{inspection?.row_count != null && <> · {number(inspection.row_count)} registros declarados</>}</p>
        {(inspection?.format === 'CSV' || inspection?.format === 'TXT') && <Field label="Delimitador"><select value={options.delimiter || inspection.detected_delimiter || ','} disabled={inspect.isPending || register.isPending} onChange={event => changeOptions({ ...options, delimiter: event.target.value })}><option value=",">Coma (,)</option><option value=";">Punto y coma (;)</option><option value={'\t'}>Tabulación</option><option value="|">Barra vertical (|)</option></select></Field>}
        {inspection?.format === 'XLSX' && <Field label="Hoja"><select value={options.sheet_name || inspection.selected_sheet || ''} disabled={inspect.isPending || register.isPending} onChange={event => changeOptions({ ...options, sheet_name: event.target.value })}>{inspection.sheets.map(sheet => <option key={sheet}>{sheet}</option>)}</select></Field>}
        {inspect.isPending && <Loading text="Inspeccionando opciones…"/>}{inspect.error && <ErrorState error={inspect.error}/>}
        <Notice>La vista previa es una muestra. El perfil final y los tipos confirmados se validan contra la fuente completa. CSV, TXT, Parquet y NDJSON permiten volumen; XLSX y JSON no lineal tienen límites menores.</Notice>
        <div className="table-scroll"><table><thead><tr><th>Columna</th><th>Tipo confirmado</th><th>Identificador</th></tr></thead><tbody>{inspection?.columns.map(column => <tr key={column.name}><td className="mono">{column.name}</td><td><select aria-label={`Tipo de ${column.name}`} disabled={register.isPending || overrides[column.name]?.semantic_tag === 'IDENTIFIER'} value={overrides[column.name]?.logical_type || ''} onChange={event => { const value = event.target.value; setOverrides(current => ({ ...current, [column.name]: value ? { ...current[column.name], logical_type: value } : {} })); requestKey.current = crypto.randomUUID() }}><option value="">Inferir completamente · muestra {column.logical_type}</option>{['STRING', 'DECIMAL', 'INT64', 'DATE', 'TIMESTAMP', 'BOOLEAN'].map(type => <option key={type}>{type}</option>)}</select></td><td><input type="checkbox" aria-label={`Identificador ${column.name}`} checked={overrides[column.name]?.semantic_tag === 'IDENTIFIER' || column.semantic_tag === 'IDENTIFIER'} disabled={register.isPending || column.semantic_tag === 'IDENTIFIER'} onChange={event => { setOverrides(current => ({ ...current, [column.name]: event.target.checked ? { logical_type: 'STRING', semantic_tag: 'IDENTIFIER' } : {} })); requestKey.current = crypto.randomUUID() }}/></td></tr>)}</tbody></table></div>
      </>}
      {!datasetId && <div className="form-grid"><Field label="Nombre del dataset"><input required maxLength={160} value={name} disabled={register.isPending} onChange={event => { setName(event.target.value); registeredDataset.current = undefined; requestKey.current = crypto.randomUUID() }}/></Field><Field label="Área de negocio"><input required maxLength={80} value={domain} disabled={register.isPending || !!matching} onChange={event => { setDomain(event.target.value); requestKey.current = crypto.randomUUID() }}/></Field></div>}
      {matching && <Notice>Ya existe “{matching.name}”. Este archivo se agregará como una nueva versión inmutable del dataset existente.</Notice>}
      {register.error && <ErrorState error={register.error}/>}
      {onLegacy && <button type="button" className="text-button" disabled={stage.isPending || register.isPending} onClick={() => { onClose(); onLegacy() }}>Usar carga rápida limitada</button>}
      <div className="modal-footer"><button type="button" className="button secondary" disabled={register.isPending} onClick={onClose}>Cerrar</button><button className="button primary" disabled={!received || stage.isPending || inspect.isPending || !!inspect.error || register.isPending || !name.trim() || !domain.trim()}><Upload size={16}/>{register.isPending ? 'Registrando adquisición…' : 'Registrar adquisición'}</button></div>
    </form>
  </Modal>
}
