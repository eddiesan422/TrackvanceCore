import { useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { ArrowDown, ArrowLeft, ArrowRight, ArrowUp, Check, Database, Eye, Play, Plus, RefreshCw, Send, ShieldCheck } from 'lucide-react'
import { api, post } from '../../api/client'
import type { Collection, RecordData } from '../../api/client'
import { usePermission } from '../../app/session'
import { Badge, date, ErrorState, Field, Loading, Notice, number, PageHeading } from '../../components/ui'
import type { ColumnMapping, DatasetColumn, DatasetRecord, DeliveryColumnType, DeliveryConfiguration, DeliveryDestination, DeliveryDraft, DeliveryPreflight, DeliveryPreview, TableMetadata, TargetMode, WriteStrategy } from './types'
import { collectionItems, destinationVersionId } from './types'
import './delivery.css'

const steps = ['Dataset y versión', 'Destino', 'Target', 'Columnas', 'Estrategia', 'Preview y preflight', 'Publicar y ejecutar']

const typeOptions: DeliveryColumnType[] = ['STRING', 'INT64', 'DECIMAL', 'DATE', 'TIMESTAMP', 'BOOLEAN']

function deliveryColumnType(logicalType: string): DeliveryColumnType | null {
  const logical = logicalType.toUpperCase()
  return typeOptions.includes(logical as DeliveryColumnType) ? logical as DeliveryColumnType : null
}

function defaultTargetType(logicalType: string): DeliveryColumnType {
  return deliveryColumnType(logicalType) || 'STRING'
}

function typeParameters(type: DeliveryColumnType, value: { precision?: number | null; scale?: number | null; length?: number | null }) {
  if (type === 'DECIMAL') {
    const precision = value.precision == null ? undefined : Number(value.precision)
    const scale = value.scale == null ? undefined : Number(value.scale)
    const validPrecision = precision == null || (Number.isInteger(precision) && precision >= 1 && precision <= 38)
    const validScale = scale == null || (Number.isInteger(scale) && scale >= 0 && scale <= 38)
    if (!validPrecision || !validScale || (scale ?? 10) > (precision ?? 38)) return {}
    return { ...(precision != null ? { precision } : {}), ...(scale != null ? { scale } : {}) }
  }
  if (type === 'STRING' && value.length != null) {
    const length = Number(value.length)
    return Number.isInteger(length) && length >= 1 && length <= 1_000_000 ? { length } : {}
  }
  return {}
}

function validTypeParameters(mapping: ColumnMapping) {
  if (mapping.target_type === 'DECIMAL') {
    const precision = mapping.precision ?? 38, scale = mapping.scale ?? 10
    return Number.isInteger(precision) && Number.isInteger(scale) && precision >= 1 && precision <= 38 && scale >= 0 && scale <= precision
  }
  if (mapping.target_type === 'STRING') return mapping.length == null || (Number.isInteger(mapping.length) && mapping.length >= 1 && mapping.length <= 1_000_000)
  return mapping.precision == null && mapping.scale == null && mapping.length == null
}

function namesFrom(value: unknown) {
  return collectionItems<string | { name: string }>(value).map(item => typeof item === 'string' ? item : item.name).filter(Boolean)
}

function profileColumns(value: { profile?: { columns?: DatasetColumn[] }; schema?: DatasetColumn[] } | undefined, fallback: DatasetColumn[] = []) {
  return value?.profile?.columns || value?.schema || fallback
}

function configuredColumns(value: RecordData | undefined): ColumnMapping[] {
  if (!Array.isArray(value?.columns)) return []
  return value.columns.map((entry: RecordData, index: number) => {
    const sourceType = String(entry.source_type || '')
    const targetType = defaultTargetType(sourceType || String(entry.target_type || 'STRING'))
    return {
      source_name: String(entry.source_name || ''),
      source_type: sourceType,
      target_name: String(entry.target_name || entry.source_name || ''),
      target_type: targetType,
      ordinal: Number(entry.ordinal ?? index),
      nullable: entry.nullable !== false,
      selected: true,
      ...typeParameters(targetType, {
        precision: entry.precision == null ? null : Number(entry.precision),
        scale: entry.scale == null ? null : Number(entry.scale),
        length: entry.length == null ? null : Number(entry.length),
      }),
    }
  })
}

function jsonValue(value: unknown) {
  if (value == null) return 'null'
  if (typeof value === 'string') return value
  return JSON.stringify(value)
}

export function DeliveryBuilder() {
  const [params] = useSearchParams(), navigate = useNavigate(), cache = useQueryClient()
  const initialId = params.get('configuration') || '', destinationParam = params.get('destination') || ''
  const canConfigure = usePermission('configurations:write'), canExecute = usePermission('runs:execute')
  const [step, setStep] = useState(1), [name, setName] = useState(''), [owner, setOwner] = useState('Equipo de datos'), [description, setDescription] = useState('')
  const [datasetId, setDatasetId] = useState(''), [versionId, setVersionId] = useState(''), [destinationId, setDestinationId] = useState(destinationParam)
  const [targetMode, setTargetMode] = useState<TargetMode>('EXISTING_TABLE'), [schemaMode, setSchemaMode] = useState<'EXISTING' | 'NEW'>('EXISTING')
  const [schemaName, setSchemaName] = useState(''), [newSchemaName, setNewSchemaName] = useState(''), [tableName, setTableName] = useState('')
  const [mappings, setMappings] = useState<ColumnMapping[]>([]), [strategy, setStrategy] = useState<WriteStrategy>('APPEND'), [upsertKeys, setUpsertKeys] = useState<string[]>([])
  const [previewData, setPreviewData] = useState<DeliveryPreview | null>(null), [preflightData, setPreflightData] = useState<DeliveryPreflight | null>(null)
  const [published, setPublished] = useState<DeliveryConfiguration | null>(null)
  const initialApplied = useRef(false), mappingKey = useRef(''), metadataKey = useRef(''), idempotencyKey = useRef('')

  const datasets = useQuery({ queryKey: ['datasets'], queryFn: () => api<Collection>('/datasets') })
  const configurations = useQuery({ queryKey: ['delivery-configurations'], queryFn: () => api<{ items: DeliveryConfiguration[]; total: number }>('/delivery/configurations'), enabled: !!initialId })
  const destinations = useQuery({ queryKey: ['delivery-destinations'], queryFn: () => api<{ items: DeliveryDestination[]; total: number }>('/delivery/destinations') })
  const dataset = useQuery({ queryKey: ['dataset', datasetId], queryFn: () => api<DatasetRecord>(`/datasets/${datasetId}`), enabled: !!datasetId })
  const destination = useQuery({ queryKey: ['delivery-destination', destinationId], queryFn: () => api<DeliveryDestination>(`/delivery/destinations/${destinationId}`), enabled: !!destinationId })
  const versions = dataset.data?.versions || [], selectedVersion = versions.find(version => version.id === versionId)
  const profile = useQuery({ queryKey: ['profile', versionId], queryFn: () => api<{ profile?: { columns?: DatasetColumn[] }; schema?: DatasetColumn[]; sample?: Record<string, unknown>[] }>(`/dataset-versions/${versionId}/profile`), enabled: !!versionId })
  const sourceColumns = useMemo(() => profileColumns(profile.data, selectedVersion?.schema), [profile.data, selectedVersion?.schema])
  const schemas = useQuery({ queryKey: ['delivery-destination-schemas', destinationId, destinationVersionId(destination.data)], queryFn: () => api<unknown>(`/delivery/destinations/${destinationId}/schemas`), enabled: !!destinationId && !!destination.data?.enabled })
  const effectiveSchema = targetMode === 'CREATE_TABLE' && schemaMode === 'NEW' ? newSchemaName.trim() : schemaName
  const tables = useQuery({ queryKey: ['delivery-destination-tables', destinationId, destinationVersionId(destination.data), schemaName], queryFn: () => api<unknown>(`/delivery/destinations/${destinationId}/tables?${new URLSearchParams({ schema_name: schemaName })}`), enabled: !!destinationId && !!schemaName && targetMode === 'EXISTING_TABLE' })
  const metadata = useQuery({ queryKey: ['delivery-table-metadata', destinationId, destinationVersionId(destination.data), schemaName, tableName], queryFn: () => api<TableMetadata>(`/delivery/destinations/${destinationId}/table-metadata?${new URLSearchParams({ schema_name: schemaName, table_name: tableName })}`), enabled: !!destinationId && !!schemaName && !!tableName && targetMode === 'EXISTING_TABLE' })
  const initial = configurations.data?.items.find(item => item.id === initialId)

  useEffect(() => {
    if (!initial || initialApplied.current) return
    initialApplied.current = true
    const config = initial.config || {}
    const target = (config.target || {}) as RecordData
    setName(initial.name)
    setOwner(String(initial.owner || 'Equipo de datos'))
    setDescription(String(initial.description || ''))
    setDatasetId(initial.dataset_id || String(config.dataset_id || ''))
    setVersionId(initial.dataset_version_id || String(config.dataset_version_id || ''))
    setDestinationId(initial.destination_id || String(config.destination_id || ''))
    setTargetMode((target.mode || 'EXISTING_TABLE') as TargetMode)
    setSchemaMode(target.create_schema ? 'NEW' : 'EXISTING')
    if (target.create_schema) setNewSchemaName(String(target.schema_name || ''))
    else setSchemaName(String(target.schema_name || ''))
    setTableName(String(target.table_name || ''))
    setMappings(configuredColumns(config))
    setStrategy((config.write_strategy || 'APPEND') as WriteStrategy)
    setUpsertKeys(Array.isArray(config.upsert_keys) ? config.upsert_keys.map(String) : [])
  }, [initial])

  useEffect(() => {
    if (!sourceColumns.length || !versionId || !destination.data) return
    const key = `${versionId}:${destination.data.sink_type}`
    if (mappingKey.current === key) return
    mappingKey.current = key
    setMappings(current => sourceColumns.map((column, index) => {
      const saved = current.find(item => item.source_name === column.name)
      const targetType = defaultTargetType(column.logical_type)
      if (saved) {
        const savedType = deliveryColumnType(saved.source_type) || saved.target_type
        const parameters = savedType === targetType ? saved : column
        return {
          ...saved,
          source_type: column.logical_type,
          target_type: targetType,
          ordinal: saved.ordinal ?? index,
          precision: undefined,
          scale: undefined,
          length: undefined,
          ...typeParameters(targetType, parameters),
        }
      }
      return {
        source_name: column.name,
        source_type: column.logical_type,
        target_name: column.name,
        target_type: targetType,
        ordinal: index,
        nullable: column.nullable !== false,
        selected: true,
        ...typeParameters(targetType, column),
      }
    }))
  }, [destination.data, sourceColumns, versionId])

  useEffect(() => {
    if (!metadata.data || targetMode !== 'EXISTING_TABLE') return
    const key = `${destinationId}:${schemaName}:${tableName}`
    if (metadataKey.current === key) return
    metadataKey.current = key
    setMappings(current => current.map(mapping => {
      const target = metadata.data!.columns.find(column => column.name === mapping.target_name || column.name === mapping.source_name)
      if (!target) return mapping
      const sourceType = defaultTargetType(mapping.source_type)
      const targetType = deliveryColumnType(target.logical_type)
      const parameters = targetType === sourceType ? target : mapping
      return {
        ...mapping,
        target_name: target.name,
        target_type: sourceType,
        nullable: target.nullable,
        precision: undefined,
        scale: undefined,
        length: undefined,
        ...typeParameters(sourceType, parameters),
      }
    }))
  }, [destinationId, metadata.data, schemaName, tableName, targetMode])

  const selectedMappings = useMemo(() => mappings.filter(item => item.selected).sort((left, right) => left.ordinal - right.ordinal), [mappings])
  const duplicateTargets = useMemo(() => {
    const names = selectedMappings.map(item => item.target_name.trim().toLocaleLowerCase())
    return new Set(names.filter((name, index) => !!name && names.indexOf(name) !== index))
  }, [selectedMappings])
  const deliveryColumns = useMemo<DeliveryDraft['columns']>(() => selectedMappings.map((item, index) => {
    const targetType = defaultTargetType(item.source_type)
    return {
      source_name: item.source_name,
      target_name: item.target_name.trim(),
      target_type: targetType,
      ordinal: index,
      nullable: item.nullable,
      ...typeParameters(targetType, item),
    }
  }), [selectedMappings])
  const draft = useMemo<DeliveryDraft>(() => ({
    schema_version: 1,
    dataset_version_id: versionId,
    destination_id: destinationId,
    destination_version_id: destinationVersionId(destination.data),
    target: { mode: targetMode, schema_name: effectiveSchema, table_name: tableName.trim(), create_schema: targetMode === 'CREATE_TABLE' && schemaMode === 'NEW' },
    columns: deliveryColumns,
    write_strategy: targetMode === 'CREATE_TABLE' ? 'CREATE_AND_LOAD' : strategy,
    upsert_keys: targetMode === 'EXISTING_TABLE' && strategy === 'UPSERT' ? upsertKeys.map(key => key.trim()) : [],
  }), [deliveryColumns, destination.data, destinationId, effectiveSchema, schemaMode, strategy, tableName, targetMode, upsertKeys, versionId])
  const draftFingerprint = JSON.stringify(draft), currentFingerprint = useRef(draftFingerprint)
  currentFingerprint.current = draftFingerprint
  const previousFingerprint = useRef('')
  useEffect(() => {
    if (previousFingerprint.current && previousFingerprint.current !== draftFingerprint) { setPreviewData(null); setPreflightData(null); setPublished(null) }
    previousFingerprint.current = draftFingerprint
  }, [draftFingerprint])

  const preview = useMutation({
    mutationFn: ({ value }: { value: DeliveryDraft; fingerprint: string }) => post<DeliveryPreview>('/delivery/preview', value),
    onMutate: () => { setPreviewData(null) },
    onSuccess: (data, variables) => { if (variables.fingerprint === currentFingerprint.current) setPreviewData(data) },
  })
  const preflight = useMutation({
    mutationFn: ({ value }: { value: DeliveryDraft; fingerprint: string }) => post<DeliveryPreflight>('/delivery/preflight', value),
    onMutate: () => { setPreflightData(null) },
    onSuccess: (data, variables) => { if (variables.fingerprint === currentFingerprint.current) setPreflightData(data) },
  })
  const publish = useMutation({
    mutationFn: ({ value, publishedName, publishedOwner, publishedDescription }: { value: DeliveryDraft; fingerprint: string; publishedName: string; publishedOwner: string; publishedDescription: string }) => {
      return initial
        ? post<DeliveryConfiguration>(`/delivery/configurations/${initial.id}/versions`, { ...value, description: publishedDescription })
        : post<DeliveryConfiguration>('/delivery/configurations', { ...value, name: publishedName, owner: publishedOwner, description: publishedDescription })
    },
    onSuccess: (data, variables) => {
      if (variables.fingerprint !== currentFingerprint.current) return
      setPublished(data)
      cache.invalidateQueries({ queryKey: ['delivery-configurations'] })
    },
  })
  const execute = useMutation({
    mutationFn: async () => {
      if (!idempotencyKey.current) idempotencyKey.current = crypto.randomUUID()
      return api<{ id: string }>('/delivery/runs', { method: 'POST', headers: { 'Idempotency-Key': idempotencyKey.current }, body: JSON.stringify({ configuration_id: published!.id, dataset_version_id: versionId }) })
    },
    onSuccess: data => { idempotencyKey.current = ''; cache.invalidateQueries({ queryKey: ['runs'] }); cache.invalidateQueries({ queryKey: ['dashboard'] }); navigate(`/runs/${data.id}`) },
  })

  const schemaNames = namesFrom(schemas.data), tableNames = namesFrom(tables.data)
  const versionValid = !!name.trim() && !!owner.trim() && !!datasetId && !!versionId && !!selectedVersion?.canonical_artifact_id
  const destinationValid = !!destination.data?.enabled && !!destinationVersionId(destination.data)
  const targetValid = !!effectiveSchema && !!tableName.trim() && (targetMode === 'CREATE_TABLE' || !!metadata.data)
  const invalidParameters = selectedMappings.some(item => !validTypeParameters(item))
  const unsupportedTypes = selectedMappings.filter(item => !deliveryColumnType(item.source_type))
  const mappingValid = !!selectedMappings.length && selectedMappings.length <= 100 && selectedMappings.every(item => item.target_name.trim() && deliveryColumnType(item.source_type) === item.target_type) && !invalidParameters && !duplicateTargets.size
  const strategyValid = targetMode === 'CREATE_TABLE' || strategy !== 'UPSERT' || !!upsertKeys.length
  const validSteps = [versionValid, destinationValid, targetValid, mappingValid, strategyValid, preflightData?.status === 'PASS', !!published]
  const canContinue = validSteps[step - 1]

  function changeDataset(value: string) { setDatasetId(value); setVersionId(''); setMappings([]); mappingKey.current = ''; setPublished(null) }
  function changeVersion(value: string) { setVersionId(value); setMappings([]); mappingKey.current = ''; setPublished(null) }
  function changeDestination(value: string) { setDestinationId(value); setSchemaName(''); setNewSchemaName(''); setTableName(''); setMappings([]); mappingKey.current = ''; metadataKey.current = ''; setPublished(null) }
  function updateMapping(source: string, changes: Partial<ColumnMapping>) { setMappings(current => current.map(item => item.source_name === source ? { ...item, ...changes } : item)) }
  function moveMapping(source: string, direction: -1 | 1) {
    setMappings(current => {
      const ordered = [...current].sort((left, right) => left.ordinal - right.ordinal)
      const index = ordered.findIndex(item => item.source_name === source), next = index + direction
      if (index < 0 || next < 0 || next >= ordered.length) return current
      const temporary = ordered[index].ordinal
      ordered[index] = { ...ordered[index], ordinal: ordered[next].ordinal }
      ordered[next] = { ...ordered[next], ordinal: temporary }
      return ordered
    })
  }

  if (!canConfigure) return <Notice>No tienes permiso para publicar configuraciones de entrega.</Notice>
  return <><PageHeading back="/delivery" eyebrow="DATA DELIVERY / CONSTRUCTOR" title={initial ? `Nueva versión de ${initial.name}` : 'Nueva entrega'} description="Publica una DatasetVersion inmutable hacia un destino externo, sin transformar sus datos."/>
    <ol className="delivery-stepper" aria-label="Pasos del constructor">{steps.map((title, index) => <li key={title} className={`${step === index + 1 ? 'active' : ''} ${step > index + 1 ? 'complete' : ''}`}><button type="button" disabled={index + 1 > step || !!published || publish.isPending || execute.isPending} onClick={() => setStep(index + 1)}><span>{step > index + 1 ? <Check size={13}/> : index + 1}</span>{title}</button></li>)}</ol>
    <section className="panel delivery-builder">
      <div className="delivery-builder-heading"><div><span>PASO {step} DE 7</span><h2>{steps[step - 1]}</h2></div>{published && <Badge value="PUBLISHED"/>}</div>

      {step === 1 && <div className="delivery-step-content form-stack"><div className="form-grid"><Field label="Nombre de la entrega"><input required maxLength={120} disabled={!!initial} value={name} onChange={event => setName(event.target.value)} placeholder="Ej. Publicar ventas mensuales"/></Field><Field label="Responsable"><input required maxLength={120} disabled={!!initial} value={owner} onChange={event => setOwner(event.target.value)}/></Field><Field label="Dataset"><select required disabled={!!initial || datasets.isPending} value={datasetId} onChange={event => changeDataset(event.target.value)}><option value="">Selecciona un dataset</option>{datasets.data?.items.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></Field></div>
        {datasets.error && <ErrorState error={datasets.error} retry={() => datasets.refetch()}/>} {dataset.isPending && <Loading text="Cargando versiones inmutables…"/>} {dataset.error && <ErrorState error={dataset.error} retry={() => dataset.refetch()}/>} {datasetId && dataset.data && <Field label="DatasetVersion exacta" hint="La configuración y el Run conservarán este identificador."><select required value={versionId} onChange={event => changeVersion(event.target.value)}><option value="">Selecciona una versión</option>{versions.map(version => <option key={version.id} value={version.id}>v{version.version} · {version.filename || version.id.slice(0, 8)} · {number(version.row_count)} filas</option>)}</select></Field>}
        {selectedVersion && <div className="delivery-version-card"><Database size={22}/><div><strong>{dataset.data?.name} · versión {selectedVersion.version}</strong><span>{number(selectedVersion.row_count)} filas · {selectedVersion.source_type || 'DatasetVersion'} · {date(selectedVersion.created_at)}</span><code>{selectedVersion.id}</code></div><Badge value={selectedVersion.canonical_artifact_id ? 'READY' : 'FAILED_PRECONDITION'}>{selectedVersion.canonical_artifact_id ? 'Parquet canónico disponible' : 'Sin artifact canónico'}</Badge></div>}
        <Field label="Descripción (opcional)"><textarea rows={2} maxLength={2000} value={description} onChange={event => setDescription(event.target.value)}/></Field>
      </div>}

      {step === 2 && <div className="delivery-step-content form-stack"><Field label="Destino de publicación"><select required value={destinationId} onChange={event => changeDestination(event.target.value)}><option value="">Selecciona un destino activo</option>{(destinations.data?.items || []).filter(item => item.enabled && !item.deleted).map(item => <option key={item.id} value={item.id}>{item.name} · {item.sink_type === 'POSTGRESQL' ? 'PostgreSQL' : 'SQL Server'}</option>)}</select></Field>
        {destinations.isPending && <Loading text="Cargando destinos…"/>} {destinations.error && <ErrorState error={destinations.error} retry={() => destinations.refetch()}/>} {!destinations.isPending && !destinations.data?.items.some(item => item.enabled && !item.deleted) && <Notice>Primero <Link to="/delivery/destinations">crea y prueba un destino</Link>.</Notice>}
        {destination.isPending && <Loading text="Fijando la revisión del destino…"/>} {destination.error && <ErrorState error={destination.error} retry={() => destination.refetch()}/>} {destination.data && <div className="delivery-destination-card"><Send size={22}/><div><strong>{destination.data.name}</strong><span>{destination.data.sink_type === 'POSTGRESQL' ? 'PostgreSQL' : 'SQL Server'} · revisión {destination.data.version}</span><code>{destinationVersionId(destination.data) || 'Revisión no disponible'}</code></div><Badge value={destination.data.enabled ? 'ACTIVE' : 'INACTIVE'}/></div>}
      </div>}

      {step === 3 && <div className="delivery-step-content form-stack"><div className="delivery-choice-grid"><button type="button" className={targetMode === 'EXISTING_TABLE' ? 'selected' : ''} onClick={() => { setTargetMode('EXISTING_TABLE'); setSchemaMode('EXISTING'); setTableName(''); metadataKey.current = '' }}><Database size={21}/><strong>Usar tabla existente</strong><span>Inspecciona columnas, nulabilidad y claves reales.</span></button><button type="button" className={targetMode === 'CREATE_TABLE' ? 'selected' : ''} onClick={() => { setTargetMode('CREATE_TABLE'); setTableName(''); metadataKey.current = '' }}><Plus size={21}/><strong>Crear tabla nueva</strong><span>La creación ocurre únicamente al ejecutar el Run.</span></button></div>
        {schemas.isPending && <Loading text="Consultando schemas disponibles…"/>} {schemas.error && <ErrorState error={schemas.error} retry={() => schemas.refetch()}/>} {targetMode === 'CREATE_TABLE' && <Field label="Ubicación del schema"><select value={schemaMode} onChange={event => { setSchemaMode(event.target.value as 'EXISTING' | 'NEW'); setSchemaName(''); setNewSchemaName('') }}><option value="EXISTING">Usar schema existente</option><option value="NEW">Crear schema nuevo al ejecutar</option></select></Field>}
        {targetMode === 'CREATE_TABLE' && schemaMode === 'NEW' ? <Field label="Nuevo schema" hint="El preflight valida el identificador y los permisos sin crearlo."><input required maxLength={128} value={newSchemaName} onChange={event => setNewSchemaName(event.target.value)} placeholder="publicacion"/></Field> : <Field label="Schema"><select required value={schemaName} onChange={event => { setSchemaName(event.target.value); setTableName(''); metadataKey.current = '' }}><option value="">Selecciona un schema</option>{schemaNames.map(item => <option key={item} value={item}>{item}</option>)}</select></Field>}
        {targetMode === 'EXISTING_TABLE' ? <><Field label="Tabla existente"><select required disabled={!schemaName || tables.isPending} value={tableName} onChange={event => { setTableName(event.target.value); metadataKey.current = '' }}><option value="">Selecciona una tabla</option>{tableNames.map(item => <option key={item} value={item}>{item}</option>)}</select></Field>{tables.isPending && <Loading text="Consultando tablas…"/>}{tables.error && <ErrorState error={tables.error} retry={() => tables.refetch()}/>} {metadata.isPending && <Loading text="Inspeccionando estructura y claves…"/>} {metadata.error && <ErrorState error={metadata.error} retry={() => metadata.refetch()}/>} {metadata.data && <div className="delivery-metadata"><strong>{metadata.data.schema_name}.{metadata.data.table_name}</strong><span>{number(metadata.data.columns.length)} columnas · PK: {metadata.data.constraints.find(item => item.type === 'PRIMARY_KEY')?.columns.join(', ') || 'sin clave primaria'}</span><div>{metadata.data.columns.map(column => <code key={column.name}>{column.name} <small>{column.native_type}{column.nullable ? ' · NULL' : ' · NOT NULL'}</small></code>)}</div></div>}</> : <Field label="Nueva tabla" hint="No se crea durante preview ni preflight."><input required maxLength={128} value={tableName} onChange={event => setTableName(event.target.value)} placeholder="ventas_publicadas"/></Field>}
      </div>}

      {step === 4 && <div className="delivery-step-content"><div className="delivery-section-copy"><div><h3>Mapping de salida</h3><p>Selecciona, ordena y adapta técnicamente cada columna. No se aplican reglas de negocio ni transformaciones funcionales.</p></div><span>{number(selectedMappings.length)} de {number(mappings.length)} columnas</span></div>{profile.isPending ? <Loading text="Leyendo el esquema canónico…"/> : profile.error ? <ErrorState error={profile.error} retry={() => profile.refetch()}/> : <div className="table-scroll delivery-mapping"><table><thead><tr><th>Incluir</th><th>Orden</th><th>Origen</th><th>Nombre destino</th><th>Tipo destino</th><th>Parámetros</th><th>Nulos</th></tr></thead><tbody>{[...mappings].sort((left, right) => left.ordinal - right.ordinal).map((mapping, index) => {
          const fixedType = defaultTargetType(mapping.source_type)
          const decimal = fixedType === 'DECIMAL', sized = fixedType === 'STRING'
          const choices = [fixedType]
          return <tr key={mapping.source_name} className={mapping.selected ? '' : 'excluded'}><td><input aria-label={`Incluir ${mapping.source_name}`} type="checkbox" checked={mapping.selected} onChange={event => { updateMapping(mapping.source_name, { selected: event.target.checked }); if (!event.target.checked) setUpsertKeys(keys => keys.filter(key => key !== mapping.target_name)) }}/></td><td><div className="mapping-order"><button type="button" className="icon-button" aria-label={`Subir ${mapping.source_name}`} disabled={index === 0} onClick={() => moveMapping(mapping.source_name, -1)}><ArrowUp size={14}/></button><button type="button" className="icon-button" aria-label={`Bajar ${mapping.source_name}`} disabled={index === mappings.length - 1} onClick={() => moveMapping(mapping.source_name, 1)}><ArrowDown size={14}/></button></div></td><td><strong className="mono">{mapping.source_name}</strong><small className="table-subtitle">{mapping.source_type}</small></td><td><input aria-label={`Nombre destino de ${mapping.source_name}`} maxLength={128} disabled={!mapping.selected} value={mapping.target_name} onChange={event => { const previous = mapping.target_name; updateMapping(mapping.source_name, { target_name: event.target.value }); setUpsertKeys(keys => keys.map(key => key === previous ? event.target.value : key)) }}/>{duplicateTargets.has(mapping.target_name.trim().toLocaleLowerCase()) && <small className="mapping-error">Nombre duplicado</small>}</td><td><select aria-label={`Tipo destino de ${mapping.source_name}`} disabled value={fixedType}>{choices.map(type => <option key={type} value={type}>{type}</option>)}</select></td><td>{decimal ? <div className="mapping-params"><input aria-label={`Precisión de ${mapping.source_name}`} type="number" min={1} max={38} placeholder="Precisión" value={mapping.precision ?? ''} onChange={event => updateMapping(mapping.source_name, { precision: event.target.value ? Number(event.target.value) : undefined })}/><input aria-label={`Escala de ${mapping.source_name}`} type="number" min={0} max={38} placeholder="Escala" value={mapping.scale ?? ''} onChange={event => updateMapping(mapping.source_name, { scale: event.target.value ? Number(event.target.value) : undefined })}/></div> : sized ? <input aria-label={`Longitud de ${mapping.source_name}`} type="number" min={1} max={1000000} placeholder="Longitud" value={mapping.length ?? ''} onChange={event => updateMapping(mapping.source_name, { length: event.target.value ? Number(event.target.value) : undefined })}/> : <span className="muted">No aplica</span>}</td><td><label className="mapping-nullable"><input type="checkbox" aria-label={`Permitir nulos en ${mapping.source_name}`} disabled={!mapping.selected} checked={mapping.nullable} onChange={event => updateMapping(mapping.source_name, { nullable: event.target.checked })}/> Sí</label></td></tr>
        })}</tbody></table></div>} {!selectedMappings.length && <Notice>Selecciona al menos una columna para continuar.</Notice>} {selectedMappings.length > 100 && <Notice>Una entrega admite como máximo 100 columnas. Excluye {number(selectedMappings.length - 100)} para continuar.</Notice>} {!!unsupportedTypes.length && <Notice>La DatasetVersion contiene tipos lógicos no admitidos por Delivery: {unsupportedTypes.map(item => `${item.source_name} (${item.source_type})`).join(', ')}.</Notice>} {invalidParameters && <Notice>Revisa longitud, precisión y escala. La escala no puede superar la precisión.</Notice>}
      </div>}

      {step === 5 && <div className="delivery-step-content form-stack">{targetMode === 'CREATE_TABLE' ? <div className="delivery-strategy selected"><Plus size={22}/><div><strong>Crear y cargar</strong><p>Comprueba nuevamente que el target no exista, crea schema/tabla dentro de la ejecución y carga esta DatasetVersion.</p></div><Badge value="CREATE_AND_LOAD"/></div> : <div className="delivery-strategy-list">{([
          ['APPEND', 'Agregar registros', 'Inserta todas las filas. Una nueva ejecución intencional puede volver a agregarlas.'],
          ['OVERWRITE', 'Reemplazar datos existentes', 'Conserva la tabla, índices y constraints; sustituye sus datos de forma transaccional.'],
          ['UPSERT', 'Actualizar existentes y agregar nuevos', 'Actualiza coincidencias e inserta faltantes; no elimina filas ajenas al dataset.'],
        ] as [WriteStrategy, string, string][]).map(([value, title, copy]) => <button type="button" key={value} className={`delivery-strategy ${strategy === value ? 'selected' : ''}`} onClick={() => { setStrategy(value); if (value !== 'UPSERT') setUpsertKeys([]) }}><span className="strategy-radio"/><div><strong>{title}</strong><p>{copy}</p></div><Badge value={value}/></button>)}</div>}
        {targetMode === 'EXISTING_TABLE' && strategy === 'APPEND' && <Notice>APPEND no ofrece exactly-once universal. Si la confirmación remota queda en UNKNOWN, Trackvance no reintentará automáticamente.</Notice>}
        {targetMode === 'EXISTING_TABLE' && strategy === 'OVERWRITE' && <div className="delivery-danger"><ShieldCheck size={19}/><div><strong>Operación destructiva sobre datos</strong><p>El Run reemplazará las filas actuales, sin eliminar ni recrear la tabla. Revisa target, mapping y constraints antes de publicar.</p></div></div>}
        {targetMode === 'EXISTING_TABLE' && strategy === 'UPSERT' && <fieldset className="delivery-key-picker"><legend>Clave de UPSERT</legend><p>Selecciona una o varias columnas destino cubiertas por una PK o restricción única compatible.</p>{selectedMappings.map(mapping => <label key={mapping.source_name}><input type="checkbox" checked={upsertKeys.includes(mapping.target_name)} onChange={event => setUpsertKeys(keys => event.target.checked ? [...keys, mapping.target_name] : keys.filter(key => key !== mapping.target_name))}/><span><strong>{mapping.target_name}</strong><small>Origen: {mapping.source_name}</small></span></label>)}{!upsertKeys.length && <small className="mapping-error">Selecciona al menos una clave.</small>}</fieldset>}
      </div>}

      {step === 6 && <div className="delivery-step-content form-stack"><div className="delivery-review-grid"><div><span>DatasetVersion</span><strong>{dataset.data?.name} · v{selectedVersion?.version}</strong><code>{versionId}</code></div><div><span>Destino</span><strong>{destination.data?.name} · revisión {destination.data?.version}</strong><code>{draft.destination_version_id}</code></div><div><span>Target</span><strong>{effectiveSchema}.{tableName}</strong><Badge value={targetMode}/></div><div><span>Estrategia</span><Badge value={draft.write_strategy}/><strong>{number(deliveryColumns.length)} columnas</strong></div></div>
        <div className="delivery-preview-map"><div className="delivery-section-copy"><div><h3>Origen → destino</h3><p>Selección, orden, nombres y tipos técnicos que quedarán publicados.</p></div></div><div className="table-scroll"><table><thead><tr><th>#</th><th>Origen</th><th/><th>Destino</th><th>Tipo</th><th>Nulos</th></tr></thead><tbody>{deliveryColumns.map((column, index) => <tr key={column.source_name}><td>{index + 1}</td><td className="mono">{column.source_name}</td><td><ArrowRight size={14}/></td><td className="mono">{column.target_name}</td><td>{column.target_type}{column.precision != null ? `(${column.precision},${column.scale || 0})` : column.length != null ? `(${column.length})` : ''}</td><td>{column.nullable ? 'Sí' : 'No'}</td></tr>)}</tbody></table></div></div>
        <div className="delivery-validation-actions"><button type="button" className="button secondary" disabled={preview.isPending || !mappingValid} onClick={() => preview.mutate({ value: draft, fingerprint: draftFingerprint })}><Eye size={16}/>{preview.isPending ? 'Generando preview…' : 'Generar preview'}</button><button type="button" className="button primary" disabled={preflight.isPending || !mappingValid || !targetValid} onClick={() => preflight.mutate({ value: draft, fingerprint: draftFingerprint })}><RefreshCw size={16}/>{preflight.isPending ? 'Validando…' : 'Ejecutar preflight'}</button></div>
        {preview.error && <ErrorState error={preview.error}/>} {preflight.error && <ErrorState error={preflight.error}/>} {previewData && <section className="delivery-sample"><div className="delivery-section-copy"><div><h3>Preview técnico</h3><p>Muestra acotada; no publica artifacts ni escribe en el destino.</p></div><Badge value="READY">{number(previewData.sampled_rows)} filas</Badge></div>{previewData.source_rows.length ? <div className="table-scroll"><table><thead><tr><th>Antes · DatasetVersion</th><th>Después · representación de salida</th></tr></thead><tbody>{previewData.source_rows.slice(0, 8).map((row, index) => <tr key={index}><td><pre>{jsonValue(row)}</pre></td><td><pre>{jsonValue(previewData.destination_rows[index] || {})}</pre></td></tr>)}</tbody></table></div> : <Notice>La DatasetVersion no contiene filas de muestra.</Notice>}</section>}
        {preflightData && <section className={`delivery-preflight ${preflightData.status.toLowerCase()}`}><div><ShieldCheck size={23}/><div><h3>{preflightData.status === 'PASS' ? 'Preflight aprobado' : 'Preflight no aprobado'}</h3><p>Validación de solo lectura sobre artifact, destino, metadata, permisos y compatibilidad.</p></div><Badge value={preflightData.status}/></div><ul>{preflightData.checks.map(check => <li key={check.code}><Badge value={check.status}/><div><strong>{check.code}</strong><span>{check.message}</span></div></li>)}</ul>{preflightData.warnings?.map(warning => <Notice key={warning}>{warning}</Notice>)}</section>}
      </div>}

      {step === 7 && <div className="delivery-step-content form-stack"><div className="delivery-final"><Send size={31}/><div><h3>{published ? 'Configuración publicada' : 'Lista para publicar'}</h3><p>{published ? 'El snapshot conserva DatasetVersion, revisión del destino, target, mapping y estrategia.' : 'Publicar no escribe en el destino. La escritura comienza únicamente al ejecutar un Run.'}</p></div>{published && <Badge value="PUBLISHED"/>}</div>
        {!published && <div className="delivery-review-grid"><div><span>Entrega</span><strong>{name}</strong></div><div><span>DatasetVersion</span><strong>v{selectedVersion?.version} · {number(selectedVersion?.row_count)} filas</strong></div><div><span>Target</span><strong>{effectiveSchema}.{tableName}</strong></div><div><span>Estrategia</span><Badge value={draft.write_strategy}/></div></div>}
        {publish.error && <ErrorState error={publish.error}/>} {execute.error && <ErrorState error={execute.error}/>} {published && <Notice success>Publicada como versión {published.version || 1}. Ningún dato se ha escrito todavía.</Notice>}
        <div className="delivery-publish-actions">{!published ? <button type="button" className="button primary" disabled={publish.isPending || preflightData?.status !== 'PASS'} onClick={() => publish.mutate({ value: draft, fingerprint: draftFingerprint, publishedName: name.trim(), publishedOwner: owner.trim(), publishedDescription: description.trim() })}><Check size={16}/>{publish.isPending ? 'Publicando…' : initial ? 'Publicar nueva versión' : 'Publicar configuración'}</button> : <><Link className="button secondary" to="/delivery">Volver a entregas</Link><button type="button" className="button primary" disabled={!canExecute || execute.isPending} onClick={() => execute.mutate()}><Play size={16}/>{execute.isPending ? 'Enviando al delivery-worker…' : 'Ejecutar entrega'}</button></>}</div>
        {published && !canExecute && <Notice>Tu rol puede configurar, pero no ejecutar Runs.</Notice>}
      </div>}

      <div className="delivery-builder-footer"><button type="button" className="button secondary" disabled={step === 1 || publish.isPending || execute.isPending || !!published} onClick={() => setStep(current => current - 1)}><ArrowLeft size={15}/> Anterior</button><span>{canContinue ? 'Paso completo' : step === 6 ? 'Se requiere preflight aprobado' : step === 7 ? 'Publica antes de ejecutar' : 'Completa los campos requeridos'}</span>{step < 7 && <button type="button" className="button primary" disabled={!canContinue} onClick={() => setStep(current => current + 1)}>Continuar <ArrowRight size={15}/></button>}</div>
    </section>
  </>
}
