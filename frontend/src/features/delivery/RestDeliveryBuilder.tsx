import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { api, post } from '../../api/client'
import { usePermission } from '../../app/session'
import { Badge, ErrorState, Field, Notice, PageHeading } from '../../components/ui'
import type { DatasetColumn, DatasetRecord, DeliveryConfiguration, DeliveryDestination, DeliveryPreview } from './types'
import { destinationVersionId } from './types'

type Mapping = { source_name: string; target_name: string; target_type: string; ordinal: number; nullable: boolean; precision?: number; scale?: number }
type Validation = { id: string; status: string; result?: { status: string }; error?: string }

export function RestDeliveryBuilder() {
  const canConfigure = usePermission('delivery:configure'), canExecute = usePermission('delivery:execute')
  const [params] = useSearchParams(), navigate = useNavigate(), idempotency = useRef(crypto.randomUUID())
  const [name, setName] = useState(''), [datasetId, setDatasetId] = useState(''), [versionId, setVersionId] = useState('')
  const [destinationId, setDestinationId] = useState(params.get('destination') || ''), [mappings, setMappings] = useState<Mapping[]>([])
  const [batchRows, setBatchRows] = useState(1000), [rowsKey, setRowsKey] = useState('records'), [staticBody, setStaticBody] = useState('{}')
  const [confirmationPath, setConfirmationPath] = useState('accepted'), [confirmationValue, setConfirmationValue] = useState('true')
  const [rowsPath, setRowsPath] = useState('rows'), [batchPath, setBatchPath] = useState('batch_id'), [hashPath, setHashPath] = useState('payload_sha256')
  const [validationId, setValidationId] = useState(''), [validatedHash, setValidatedHash] = useState(''), [published, setPublished] = useState<DeliveryConfiguration | null>(null)
  const existingId = params.get('configuration') || ''
  const configs = useQuery({ queryKey: ['delivery-configurations'], queryFn: () => api<{ items: DeliveryConfiguration[] }>('/delivery/configurations'), enabled: !!existingId })
  const existing = configs.data?.items.find(item => item.id === existingId)
  const applied = useRef(false)
  useEffect(() => {
    if (!existing || applied.current) return
    applied.current = true
    const config = existing.config || {}, target = config.target as Record<string, unknown>
    setName(existing.name); setDatasetId(existing.dataset_id || ''); setVersionId(String(config.dataset_version_id || ''))
    setDestinationId(String(config.destination_id || '')); setMappings(config.columns as Mapping[] || [])
    setBatchRows(Number(target.batch_rows || 1000)); setRowsKey(String(target.rows_key || 'records')); setStaticBody(JSON.stringify(target.static_body || {}, null, 2))
    setConfirmationPath(String(target.confirmation_path || 'accepted')); setConfirmationValue(JSON.stringify(target.confirmation_value ?? true))
    setRowsPath(String(target.rows_path || 'rows')); setBatchPath(String(target.batch_id_path || 'batch_id')); setHashPath(String(target.payload_hash_path || ''))
  }, [existing])
  const datasets = useQuery({ queryKey: ['datasets'], queryFn: () => api<{ items: DatasetRecord[] }>('/datasets') })
  const dataset = useQuery({ queryKey: ['dataset', datasetId], queryFn: () => api<DatasetRecord>(`/datasets/${datasetId}`), enabled: !!datasetId })
  const destinations = useQuery({ queryKey: ['delivery-destinations'], queryFn: () => api<{ items: DeliveryDestination[] }>('/delivery/destinations') })
  const destination = destinations.data?.items.find(item => item.id === destinationId && item.sink_type === 'REST')
  const profile = useQuery({ queryKey: ['profile', versionId], queryFn: () => api<{ schema?: DatasetColumn[]; profile?: { columns?: DatasetColumn[] } }>(`/dataset-versions/${versionId}/profile`), enabled: !!versionId })
  useEffect(() => {
    const columns = profile.data?.schema || profile.data?.profile?.columns || []
    if (!columns.length) return
    setMappings(current => columns.map((column, ordinal) => ({ source_name: column.name, target_name: current.find(item => item.source_name === column.name)?.target_name || column.name,
      target_type: column.logical_type, ordinal, nullable: column.nullable ?? true,
      ...(column.logical_type === 'DECIMAL' ? { precision: column.precision || 38, scale: column.scale ?? 18 } : {}) })))
  }, [profile.data])
  let body: Record<string, unknown> = {}, parseError = ''
  try {
    const staticValue: unknown = JSON.parse(staticBody), accepted: unknown = JSON.parse(confirmationValue)
    if (!staticValue || typeof staticValue !== 'object' || Array.isArray(staticValue) || !['string', 'number', 'boolean'].includes(typeof accepted)) throw new Error('JSON no válido')
    body = { dataset_version_id: versionId, destination_id: destinationId, destination_version_id: destinationVersionId(destination), columns: mappings,
      target: { mode: 'REST', rows_key: rowsKey, static_body: staticValue, batch_rows: batchRows, confirmation_path: confirmationPath,
        confirmation_value: accepted, rows_path: rowsPath, batch_id_path: batchPath, ...(hashPath ? { payload_hash_path: hashPath } : {}) } }
  } catch { parseError = 'El cuerpo fijo debe ser un objeto JSON y la confirmación un valor JSON simple.' }
  const hash = JSON.stringify(body), complete = !!(name.trim() && versionId && destination?.enabled && mappings.length && !parseError && confirmationPath && rowsPath && batchPath)
  const preview = useMutation({ mutationFn: () => post<DeliveryPreview>('/delivery/preview', body) })
  const validate = useMutation({ mutationFn: (snapshot: { body: Record<string, unknown>; hash: string }) => post<Validation>('/delivery/validations', snapshot.body), onSuccess: (result, snapshot) => { setValidationId(result.id); setValidatedHash(snapshot.hash) } })
  const validation = useQuery({ queryKey: ['delivery-validation', validationId], queryFn: () => api<Validation>(`/delivery/validations/${validationId}`), enabled: !!validationId,
    refetchInterval: query => ['QUEUED', 'RUNNING'].includes(query.state.data?.status || '') ? 1500 : false })
  const approved = validatedHash === hash && validation.data?.status === 'SUCCESS' && validation.data.result?.status === 'PASS'
  const publish = useMutation({ mutationFn: () => post<DeliveryConfiguration>(existing ? `/delivery/configurations/${existing.id}/versions?validation_run_id=${validationId}` : `/delivery/configurations?validation_run_id=${validationId}`,
    { ...body, ...(existing ? {} : { name }) }), onSuccess: setPublished })
  const execute = useMutation({ mutationFn: () => api<{ id: string }>('/delivery/runs', { method: 'POST', headers: { 'Idempotency-Key': idempotency.current }, body: JSON.stringify({ configuration_id: published?.id, dataset_version_id: versionId }) }), onSuccess: result => navigate(`/runs/${result.id}`) })
  if (!canConfigure) return <Notice>No tienes permiso para configurar entregas.</Notice>
  return <><PageHeading back="/delivery" title={existing ? `Nueva versión de ${existing.name}` : 'Nueva entrega REST'} description="POST síncrono por lotes independientes con confirmación explícita del receptor."/>
    <section className="panel"><form className="form-stack" onSubmit={event => { event.preventDefault(); if (approved) publish.mutate() }}>
      <fieldset disabled={!!published || publish.isPending}><div className="form-grid">
        <Field label="Nombre de la entrega"><input required disabled={!!existing} value={name} onChange={event => setName(event.target.value)}/></Field>
        <Field label="Dataset"><select value={datasetId} onChange={event => { setDatasetId(event.target.value); setVersionId(''); setMappings([]) }}><option value="">Selecciona</option>{datasets.data?.items.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></Field>
        <Field label="Versión exacta"><select value={versionId} onChange={event => setVersionId(event.target.value)}><option value="">Selecciona</option>{dataset.data?.versions?.map(item => <option key={item.id} value={item.id}>v{item.version} · {item.row_count} filas</option>)}</select></Field>
        <Field label="Destino REST"><select value={destinationId} onChange={event => setDestinationId(event.target.value)}><option value="">Selecciona</option>{destinations.data?.items.filter(item => item.sink_type === 'REST' && item.enabled).map(item => <option key={item.id} value={item.id}>{item.name} · revisión {item.version}</option>)}</select></Field>
        <Field label="Clave JSON de filas"><input required value={rowsKey} onChange={event => setRowsKey(event.target.value)}/></Field>
        <Field label="Filas por lote"><input type="number" min={1} max={10000} value={batchRows} onChange={event => setBatchRows(Number(event.target.value))}/></Field>
        <Field label="Ruta de confirmación"><input required value={confirmationPath} onChange={event => setConfirmationPath(event.target.value)}/></Field>
        <Field label="Valor de confirmación (JSON)"><input required value={confirmationValue} onChange={event => setConfirmationValue(event.target.value)}/></Field>
        <Field label="Ruta de filas confirmadas"><input required value={rowsPath} onChange={event => setRowsPath(event.target.value)}/></Field>
        <Field label="Ruta de identidad del lote"><input required value={batchPath} onChange={event => setBatchPath(event.target.value)}/></Field>
        <Field label="Ruta de hash del payload (opcional)"><input value={hashPath} onChange={event => setHashPath(event.target.value)}/></Field>
      </div><Field label="Cuerpo JSON fijo adicional"><textarea rows={3} value={staticBody} onChange={event => setStaticBody(event.target.value)}/></Field>
      <h3>Mapping técnico</h3><table><thead><tr><th>Campo de origen</th><th>Clave JSON de salida</th><th>Tipo conservado</th></tr></thead><tbody>{mappings.map((column, index) => <tr key={column.source_name}><td>{column.source_name}</td><td><input aria-label={`Clave de salida ${column.source_name}`} value={column.target_name} onChange={event => setMappings(current => current.map((item, i) => i === index ? { ...item, target_name: event.target.value } : item))}/></td><td>{column.target_type}{column.target_type === 'DECIMAL' && ' · string decimal exacto'}</td></tr>)}</tbody></table></fieldset>
      <Notice>Preview y preflight verifican localmente la población completa. Cada POST contiene batch_id y el encabezado X-Trackvance-Payload-SHA256. El receptor debe confirmar identidad, filas y resultado. No hay transacción global; HTTP 202 permanece incierto y no se reenvía.</Notice>
      {parseError && <Notice>{parseError}</Notice>}{preview.data && <details open><summary>Muestra del payload</summary><pre>{JSON.stringify(preview.data.destination_rows, null, 2)}</pre></details>}
      {validation.data && <Notice success={approved}>Preflight: <Badge value={validation.data.status}/>{validation.data.error}</Notice>}
      {[datasets.error, dataset.error, destinations.error, profile.error, preview.error, validate.error, validation.error, publish.error, execute.error].filter(Boolean).map((error, index) => <ErrorState key={index} error={error}/>)}
      <div className="modal-footer"><Link className="button secondary" to="/delivery">Volver</Link><button type="button" className="button secondary" disabled={!complete || preview.isPending || !!published} onClick={() => preview.mutate()}>Preview local</button><button type="button" className="button secondary" disabled={!complete || validate.isPending || !!published} onClick={() => validate.mutate({ body, hash })}>Preflight completo</button><button className="button primary" disabled={!approved || publish.isPending || !!published}>Publicar configuración</button>{published && <button type="button" className="button primary" disabled={!canExecute || execute.isPending} onClick={() => execute.mutate()}>Ejecutar entrega</button>}</div>
    </form></section>
  </>
}
