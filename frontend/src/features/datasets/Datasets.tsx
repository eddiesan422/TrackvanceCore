import { useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { ArrowDown, ArrowUp, ArrowUpDown, ArrowUpRight, Check, Database, FileSpreadsheet, FileType2, Fingerprint, LoaderCircle, Plus, RefreshCw, Upload } from 'lucide-react'
import { api, post } from '../../api/client'
import type { Collection, RecordData } from '../../api/client'
import { Badge, date, Empty, ErrorState, Field, Loading, Modal, Notice, number, PageHeading, SearchBox, label } from '../../components/ui'

import { ProfilingPolicy, SampleValue, VersionIdentity } from './VersionIdentity'
import { usePermission } from '../../app/session'
import { ColumnMultiSelect } from '../runs/ColumnMultiSelect'
import { SourceRefresh } from '../connections/SourceRefresh'

const normalizedDatasetName = (value: string) => value.trim().replace(/\s+/g, ' ').toLocaleLowerCase('es')

type DatasetFileFormat = 'CSV' | 'XLSX' | 'JSON' | 'PARQUET' | 'TXT'
type ReaderOptions = { sheet_name?: string; delimiter?: string }
type InspectedColumn = { name: string; logical_type: string; native_type?: string | null; nullable?: boolean; semantic_tag?: string | null; numeric?: boolean }
type LogicalType = 'STRING' | 'DECIMAL' | 'INT64' | 'DATE' | 'TIMESTAMP' | 'BOOLEAN'
type ColumnOverride = { logical_type?: LogicalType; semantic_tag?: 'IDENTIFIER' }
type UploadInspection = {
  format: string
  format_label?: string
  filename: string
  sheets?: string[]
  selected_sheet?: string | null
  detected_delimiter?: string | null
  columns?: InspectedColumn[]
  row_count?: number | null
}

const ACCEPTED_DATASET_FILES = [
  '.csv', 'text/csv',
  '.xlsx', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  '.json', '.jsonl', '.ndjson', 'application/json',
  '.parquet', '.pq', 'application/vnd.apache.parquet',
  '.txt', '.tsv', 'text/plain', 'text/tab-separated-values',
].join(',')

const extensionFormats: Record<string, DatasetFileFormat> = {
  csv: 'CSV',
  xlsx: 'XLSX',
  json: 'JSON',
  jsonl: 'JSON',
  ndjson: 'JSON',
  parquet: 'PARQUET',
  pq: 'PARQUET',
  txt: 'TXT',
  tsv: 'TXT',
}

const mimeFormats: Record<string, DatasetFileFormat> = {
  'text/csv': 'CSV',
  'application/csv': 'CSV',
  'application/vnd.ms-excel': 'CSV',
  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': 'XLSX',
  'application/json': 'JSON',
  'text/json': 'JSON',
  'application/vnd.apache.parquet': 'PARQUET',
  'application/x-parquet': 'PARQUET',
  'text/plain': 'TXT',
  'text/tab-separated-values': 'TXT',
}

const formatLabels: Record<DatasetFileFormat, string> = {
  CSV: 'CSV delimitado',
  XLSX: 'Excel XLSX',
  JSON: 'JSON tabular',
  PARQUET: 'Apache Parquet',
  TXT: 'TXT delimitado',
}

const logicalTypes: { value: LogicalType; label: string }[] = [
  { value: 'STRING', label: 'Texto' },
  { value: 'DECIMAL', label: 'Decimal' },
  { value: 'INT64', label: 'Entero' },
  { value: 'DATE', label: 'Fecha' },
  { value: 'TIMESTAMP', label: 'Fecha y hora' },
  { value: 'BOOLEAN', label: 'Booleano' },
]

const defaultDomains = ['Operaciones', 'Finanzas', 'Logística', 'Ventas']
const newDomainOption = '__new_domain__'

function candidateFileFormat(file: File): DatasetFileFormat | null {
  const extension = file.name.split('.').pop()?.toLowerCase() || ''
  return extensionFormats[extension] || mimeFormats[file.type.toLowerCase()] || null
}

function normalizedFileFormat(value: string | undefined, fallback: DatasetFileFormat | null): DatasetFileFormat | null {
  const format = String(value || '').toUpperCase()
  if (format === 'EXCEL') return 'XLSX'
  return format in formatLabels ? format as DatasetFileFormat : fallback
}

function delimiterLabel(value?: string | null) {
  if (value === '\t') return 'tabulación'
  if (value === ',') return 'coma (,)'
  if (value === ';') return 'punto y coma (;)'
  if (value === '|') return 'barra vertical (|)'
  return value || 'sin determinar'
}

type DatasetSortKey = 'domain' | 'row_count' | 'version_count' | 'status' | 'updated_at'
type SortDirection = 'asc' | 'desc'
type DatasetSort = { key: DatasetSortKey; direction: SortDirection }

const datasetCollator = new Intl.Collator('es', { numeric: true, sensitivity: 'base' })

function datasetSortValue(dataset: RecordData, key: DatasetSortKey): string | number | null {
  if (key === 'row_count' || key === 'version_count') {
    const rawValue = dataset[key]
    if (rawValue == null || rawValue === '') return null
    const value = Number(rawValue)
    return Number.isFinite(value) ? value : null
  }
  if (key === 'updated_at') {
    const value = Date.parse(String(dataset.updated_at || ''))
    return Number.isNaN(value) ? null : value
  }
  if (key === 'status') return label(dataset.status)
  return dataset.domain == null ? null : String(dataset.domain)
}

function sortDatasets(items: RecordData[], sort: DatasetSort) {
  return items.map((dataset, index) => ({ dataset, index })).sort((left, right) => {
    const leftValue = datasetSortValue(left.dataset, sort.key)
    const rightValue = datasetSortValue(right.dataset, sort.key)
    const leftMissing = leftValue == null || leftValue === ''
    const rightMissing = rightValue == null || rightValue === ''
    if (leftMissing || rightMissing) {
      if (leftMissing !== rightMissing) return leftMissing ? 1 : -1
      return left.index - right.index
    }
    const comparison = typeof leftValue === 'number' && typeof rightValue === 'number'
      ? leftValue - rightValue
      : datasetCollator.compare(String(leftValue), String(rightValue))
    return comparison === 0 ? left.index - right.index : comparison * (sort.direction === 'asc' ? 1 : -1)
  }).map(entry => entry.dataset)
}

function SortableDatasetHeader({ column, children, sort, onSort }: { column: DatasetSortKey; children: string; sort: DatasetSort; onSort: (column: DatasetSortKey) => void }) {
  const active = sort.key === column
  const nextDirection: SortDirection = active && sort.direction === 'asc' ? 'desc' : 'asc'
  const Icon = !active ? ArrowUpDown : sort.direction === 'asc' ? ArrowUp : ArrowDown
  return <th aria-sort={active ? (sort.direction === 'asc' ? 'ascending' : 'descending') : 'none'}><button type="button" className={`sortable-header ${active ? 'active' : ''}`} onClick={() => onSort(column)} aria-label={`Ordenar ${children} ${nextDirection === 'asc' ? 'ascendente' : 'descendente'}`}>{children}<Icon size={13} aria-hidden="true"/></button></th>
}

function datasetOriginLabel(dataset: RecordData) {
  if (dataset.origin_label) return String(dataset.origin_label)
  const origin = String(dataset.origin || dataset.source_type || '').toUpperCase()
  if (['UPLOAD', 'MANUAL', 'ORIGINAL_UPLOAD'].includes(origin)) return 'Manual'
  if (['INTAKE_OUTPUT', 'DATA_INTAKE'].includes(origin)) return 'Data Intake'
  if (['GENERATED_DEMO', 'DEMO'].includes(origin)) return 'Demo'
  if (!Number(dataset.version_count || 0)) return 'Sin versiones'
  return origin ? label(origin) : 'Manual'
}

export function UploadDialog({ open, onClose, datasetId, datasetName, existingDatasets = [] }: { open: boolean; onClose: () => void; datasetId?: string; datasetName?: string; existingDatasets?: RecordData[] }) {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const fileInput = useRef<HTMLInputElement>(null)
  const currentFile = useRef<File | null>(null)
  const inspectionSequence = useRef(0)
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [domain, setDomain] = useState('Operaciones')
  const [addingDomain, setAddingDomain] = useState(false)
  const [customDomain, setCustomDomain] = useState('')
  const [file, setFile] = useState<File | null>(null)
  const [createdDataset, setCreatedDataset] = useState<string>()
  const [dragOver, setDragOver] = useState(false)
  const [validation, setValidation] = useState('')
  const [inspection, setInspection] = useState<UploadInspection | null>(null)
  const [sheetName, setSheetName] = useState('')
  const [delimiter, setDelimiter] = useState('')
  const [selectedIdentifiers, setSelectedIdentifiers] = useState<string[]>([])
  const [columnTypeOverrides, setColumnTypeOverrides] = useState<Record<string, LogicalType>>({})
  const matchingDataset = !datasetId && name.trim() ? existingDatasets.find(dataset => normalizedDatasetName(String(dataset.name || '')) === normalizedDatasetName(name)) : undefined
  const guessedFormat = file ? candidateFileFormat(file) : null
  const inspectedFormat = normalizedFileFormat(inspection?.format, guessedFormat)
  const inspectedColumns = inspection?.columns || []
  const identifierNames = new Set(selectedIdentifiers)
  const selectedDomain = addingDomain ? customDomain.trim() : domain.trim()
  const availableDomains = [...new Set([
    ...defaultDomains,
    ...existingDatasets.map(dataset => String(dataset.domain || '').trim()).filter(Boolean),
  ])].sort(datasetCollator.compare)

  const inspect = useMutation({
    mutationFn: async (variables: { candidate: File; options: ReaderOptions; requestId: number }) => {
      const { candidate, options } = variables
      const body = new FormData()
      body.append('file', candidate)
      body.append('reader_options', JSON.stringify(options))
      return api<UploadInspection>('/datasets/uploads/inspect', { method: 'POST', body })
    },
    onSuccess: (result, variables) => {
      if (currentFile.current !== variables.candidate || inspectionSequence.current !== variables.requestId) return
      setInspection(result)
      setSheetName(result.selected_sheet || variables.options.sheet_name || result.sheets?.[0] || '')
      setSelectedIdentifiers([])
      setColumnTypeOverrides({})
    },
  })

  function configuredReaderOptions(nextSheet = sheetName, nextDelimiter = delimiter): ReaderOptions {
    return {
      ...(inspectedFormat === 'XLSX' && nextSheet ? { sheet_name: nextSheet } : {}),
      ...(inspectedFormat === 'TXT' && nextDelimiter ? { delimiter: nextDelimiter } : {}),
    }
  }

  function inspectFile(candidate: File, options: ReaderOptions = {}) {
    setInspection(null)
    inspect.mutate({ candidate, options, requestId: ++inspectionSequence.current })
  }

  function chooseFile(candidate?: File) {
    if (!candidate) return
    if (candidate.size > 10 * 1024 * 1024) {
      inspectionSequence.current += 1
      currentFile.current = null
      setFile(null)
      setInspection(null)
      inspect.reset()
      setValidation('El prototipo admite archivos de hasta 10 MB.')
      return
    }
    setValidation('')
    setFile(candidate)
    currentFile.current = candidate
    setInspection(null)
    setSheetName('')
    setDelimiter('')
    setSelectedIdentifiers([])
    setColumnTypeOverrides({})
    inspect.reset()
    if (!name) setName(candidate.name.replace(/\.[^.]+$/, '').replaceAll('_', ' '))
    inspectFile(candidate)
  }

  function changeSheet(value: string) {
    setSheetName(value)
    if (file) inspectFile(file, { ...configuredReaderOptions(value), sheet_name: value })
  }

  function changeDelimiter(value: string) {
    setDelimiter(value)
    if (file) inspectFile(file, configuredReaderOptions(sheetName, value))
  }

  function changeColumnType(column: InspectedColumn, logicalType: LogicalType) {
    setColumnTypeOverrides(current => {
      if (logicalType === column.logical_type) {
        const next = { ...current }
        delete next[column.name]
        return next
      }
      return { ...current, [column.name]: logicalType }
    })
  }

  function columnOverrides() {
    const overrides: Record<string, ColumnOverride> = {}
    Object.entries(columnTypeOverrides).forEach(([column, logicalType]) => {
      overrides[column] = { logical_type: logicalType }
    })
    selectedIdentifiers.forEach(column => {
      overrides[column] = { ...overrides[column], logical_type: 'STRING', semantic_tag: 'IDENTIFIER' }
    })
    return overrides
  }

  const upload = useMutation({
    mutationFn: async () => {
      let id = datasetId || createdDataset || matchingDataset?.id
      if (!id) {
        const dataset = await post('/datasets', { name: name.trim(), description: description.trim(), domain: selectedDomain })
        id = dataset.id as string
        setCreatedDataset(id)
      }
      const body = new FormData()
      body.append('file', file!)
      body.append('column_overrides', JSON.stringify(columnOverrides()))
      body.append('reader_options', JSON.stringify(configuredReaderOptions()))
      await api(`/datasets/${id}/versions/upload`, { method: 'POST', body })
      return id
    },
    onSuccess: id => {
      queryClient.invalidateQueries({ queryKey: ['datasets'] })
      queryClient.invalidateQueries({ queryKey: ['dataset', id] })
      queryClient.invalidateQueries({ queryKey: ['dataset-schema', id] })
      queryClient.invalidateQueries({ queryKey: ['dashboard'] })
      onClose()
      navigate(`/datasets/${id}`)
    },
  })

  const needsDatasetCreation = !datasetId && !createdDataset && !matchingDataset
  const canSubmit = !!file && !!inspection && !inspect.isPending && !inspect.error && !upload.isPending
    && (!!datasetId || !!name.trim()) && (!needsDatasetCreation || !!selectedDomain)

  return <Modal
    wide
    open={open}
    onOpenChange={value => { if (!value && !upload.isPending) onClose() }}
    title={datasetId ? 'Cargar nueva versión' : 'Cargar un dataset'}
    description={datasetId ? `Agrega un nuevo corte inmutable a ${datasetName}.` : 'Tus datos son el punto de partida. Conservamos cada versión y su evidencia.'}
  >
    <form onSubmit={event => { event.preventDefault(); if (canSubmit) upload.mutate() }}>
      <div
        className={`upload-zone ${dragOver ? 'drag-over' : ''} ${file ? 'has-file' : ''}`}
        onDragOver={event => { event.preventDefault(); setDragOver(true) }}
        onDragLeave={() => setDragOver(false)}
        onDrop={event => { event.preventDefault(); setDragOver(false); chooseFile(event.dataTransfer.files[0]) }}
      >
        <input ref={fileInput} type="file" accept={ACCEPTED_DATASET_FILES} aria-label="Seleccionar archivo de datos" onChange={event => chooseFile(event.target.files?.[0])}/>
        {file ? <>
          <FileSpreadsheet size={34}/>
          <strong>{file.name}</strong>
          <span>{number(file.size / 1024)} KB · {inspect.isPending ? 'Inspeccionando estructura…' : inspect.error ? 'Revisa el archivo' : inspection ? 'Estructura lista' : 'Pendiente de inspección'}</span>
          <button className="text-button" type="button" onClick={() => fileInput.current?.click()} disabled={upload.isPending}>Cambiar archivo</button>
        </> : <>
          <Upload size={30}/>
          <strong>Arrastra tu archivo de datos aquí</strong>
          <span>o <button className="text-button" type="button" onClick={() => fileInput.current?.click()}>selecciona un archivo</button></span>
          <small>CSV · Excel XLSX · JSON · Parquet · TXT delimitado · hasta 10 MB</small>
        </>}
      </div>

      {validation && <Notice>{validation}</Notice>}
      {file && inspect.isPending && <div className="file-inspection-loading" role="status"><LoaderCircle className="spin" size={18}/><span>Detectando formato, columnas y tipos…</span></div>}
      {file && inspect.error && <ErrorState error={inspect.error} retry={() => inspectFile(file, configuredReaderOptions())}/>}

      {file && inspection && !inspect.isPending && !inspect.error && <section className="file-inspection" aria-label="Inspección del archivo">
        <div className="file-inspection-heading">
          <span className="file-format-icon"><FileType2 size={18}/></span>
          <div><small>Formato detectado</small><strong>{inspection.format_label || (inspectedFormat ? formatLabels[inspectedFormat] : inspection.format)}</strong></div>
          <span className="file-format-badge">{inspectedFormat || inspection.format}</span>
          <div className="file-inspection-count"><strong>{number(inspectedColumns.length)}</strong><small>columnas{inspection.row_count != null ? ` · ${number(inspection.row_count)} filas` : ''}</small></div>
        </div>

        {inspectedFormat === 'XLSX' && (inspection.sheets?.length || 0) > 1 && <Field label="Hoja de Excel" hint="Selecciona la hoja tabular que quieres convertir en esta versión.">
          <select value={sheetName} disabled={inspect.isPending} onChange={event => changeSheet(event.target.value)}>{inspection.sheets?.map(sheet => <option key={sheet} value={sheet}>{sheet}</option>)}</select>
        </Field>}
        {inspectedFormat === 'XLSX' && inspection.sheets?.length === 1 && <p className="reader-detection">Hoja detectada: <strong>{inspection.sheets[0]}</strong></p>}
        {inspectedFormat === 'TXT' && <Field label="Delimitador del TXT" hint="Usa Automático para conservar la detección del lector o elige uno explícitamente.">
          <select value={delimiter} disabled={inspect.isPending} onChange={event => changeDelimiter(event.target.value)}>
            <option value="">Automático{inspection.detected_delimiter ? ` · ${delimiterLabel(inspection.detected_delimiter)}` : ''}</option>
            <option value=",">Coma (,)</option><option value=";">Punto y coma (;)</option><option value={'\t'}>Tabulación</option><option value="|">Barra vertical (|)</option>
          </select>
        </Field>}

        {!!inspectedColumns.length && <div className="schema-editor">
          <div className="schema-editor-heading">
            <div><strong>Esquema detectado</strong><span>Revisa y corrige el tipo antes de crear esta versión.</span></div>
            {!!Object.keys(columnTypeOverrides).length && <span className="schema-change-count">{number(Object.keys(columnTypeOverrides).length)} {Object.keys(columnTypeOverrides).length === 1 ? 'cambio' : 'cambios'}</span>}
          </div>
          <div className="schema-editor-list">
            {inspectedColumns.map(column => {
              const selectedAsIdentifier = identifierNames.has(column.name)
              const selectedType = selectedAsIdentifier ? 'STRING' : columnTypeOverrides[column.name] || column.logical_type
              return <div className="schema-editor-row" key={column.name}>
                <div><strong>{column.name}</strong><small>{column.native_type ? `Origen: ${column.native_type}` : 'Tipo inferido por contenido'}{column.semantic_tag === 'IDENTIFIER' ? ' · Posible identificador' : ''}</small></div>
                <label>
                  <span>Tipo lógico</span>
                  <select
                    aria-label={`Tipo de ${column.name}`}
                    value={selectedType}
                    disabled={selectedAsIdentifier || upload.isPending}
                    onChange={event => changeColumnType(column, event.target.value as LogicalType)}
                  >
                    {logicalTypes.map(type => <option key={type.value} value={type.value}>{type.label} ({type.value})</option>)}
                  </select>
                </label>
                {selectedAsIdentifier && <small className="identifier-type-note">Identificador · se guardará como texto</small>}
              </div>
            })}
          </div>
        </div>}
      </section>}

      {!!inspectedColumns.length && <div className="identifier-fields">
        <ColumnMultiSelect
          label="Columnas identificadoras (opcional)"
          hint="Selecciona campos del esquema. Las sugerencias se muestran como IDENTIFIER, pero tú decides cuáles aplicar."
          columns={inspectedColumns}
          selected={selectedIdentifiers}
          onChange={setSelectedIdentifiers}
          disabled={upload.isPending}
        />
      </div>}

      {!datasetId && <div className="form-stack dataset-metadata-fields">
        <Field label="Nombre del dataset"><input required maxLength={120} value={name} disabled={!!createdDataset} onChange={event => setName(event.target.value)} placeholder="Ej. Facturación septiembre"/></Field>
        <div className="form-grid">
          <div className="area-field-stack">
            <Field label="Área de negocio">
              <select
                value={addingDomain ? newDomainOption : domain}
                disabled={!!createdDataset}
                onChange={event => {
                  if (event.target.value === newDomainOption) {
                    setAddingDomain(true)
                    setCustomDomain('')
                  } else {
                    setAddingDomain(false)
                    setDomain(event.target.value)
                  }
                }}
              >
                {availableDomains.map(area => <option key={area} value={area}>{area}</option>)}
                <option value={newDomainOption}>＋ Agregar nueva área</option>
              </select>
            </Field>
            {addingDomain && <Field label="Nueva área de negocio" hint="Máximo 80 caracteres.">
              <input required autoFocus maxLength={80} value={customDomain} disabled={!!createdDataset} onChange={event => setCustomDomain(event.target.value)} placeholder="Ej. Riesgos"/>
            </Field>}
          </div>
          <Field label="Descripción (opcional)"><input value={description} disabled={!!createdDataset} onChange={event => setDescription(event.target.value)} placeholder="Contexto del archivo"/></Field>
        </div>
        {matchingDataset && <Notice>Ya existe “{matchingDataset.name}”. Este archivo se agregará como una nueva versión inmutable del dataset existente.</Notice>}
      </div>}

      {upload.error && <ErrorState error={upload.error}/>}
      <div className="notice subtle"><Fingerprint size={17}/><span>El original se conserva con su huella SHA-256. El lector normaliza su estructura antes de aplicar reglas de calidad.</span></div>
      <div className="modal-footer">
        {inspection && !inspect.error && <span className="muted"><RefreshCw size={12}/> Esquema revisado</span>}
        <button className="button secondary" type="button" onClick={onClose} disabled={upload.isPending}>Cancelar</button>
        <button className="button primary" disabled={!canSubmit}><Upload size={16}/>{upload.isPending ? 'Cargando y analizando…' : inspect.isPending ? 'Inspeccionando…' : matchingDataset ? 'Cargar como nueva versión' : 'Cargar y analizar'}</button>
      </div>
    </form>
  </Modal>
}
export function DatasetsPage() {
  const canUpload = usePermission('datasets:write')
  const [searchParams, setSearchParams] = useSearchParams(), [search, setSearch] = useState(''), [domain, setDomain] = useState('')
  const [sort, setSort] = useState<DatasetSort>({ key: 'updated_at', direction: 'desc' })
  const [uploadOpen, setUploadOpen] = useState(searchParams.get('upload') === '1')
  const datasets = useQuery({ queryKey: ['datasets'], queryFn: () => api<Collection>('/datasets') })
  const items = datasets.data?.items || []
  const filtered = items.filter(d => `${d.name} ${d.description} ${d.domain}`.toLowerCase().includes(search.toLowerCase()) && (!domain || d.domain === domain))
  const sorted = sortDatasets(filtered, sort)
  const areas = [...new Set(items.map(item => String(item.domain || '')).filter(Boolean))].sort(datasetCollator.compare)
  const toggleSort = (key: DatasetSortKey) => setSort(current => ({ key, direction: current.key === key && current.direction === 'asc' ? 'desc' : 'asc' }))
  return <><PageHeading eyebrow="TUS ACTIVOS DE INFORMACIÓN" title="Datasets" description="Organiza tus fuentes, conserva cada versión y conoce la calidad de tus datos." action={<button className="button primary" disabled={!canUpload} onClick={() => setUploadOpen(true)}><Plus size={17}/> Cargar dataset</button>}/><div className="compact-stats"><div><Database size={19}/><strong>{number(datasets.data?.total)}</strong><span>datasets</span></div><div><FileSpreadsheet size={19}/><strong>{number(items.reduce((sum, d) => sum + (d.row_count || 0), 0))}</strong><span>registros en últimas versiones</span></div><div><Check size={19}/><span>Versiones inmutables y perfil de columnas</span></div></div><section className="panel"><div className="table-toolbar"><SearchBox value={search} onChange={setSearch} placeholder="Buscar por nombre, descripción o área…"/><div className="toolbar-right"><select value={domain} aria-label="Filtrar por área" onChange={e => setDomain(e.target.value)}><option value="">Todas las áreas</option>{areas.map(area => <option key={area}>{area}</option>)}</select><span>{number(filtered.length)} datasets</span></div></div>{datasets.isPending ? <Loading/> : datasets.error ? <ErrorState error={datasets.error} retry={() => datasets.refetch()}/> : !filtered.length ? <Empty title={search || domain ? 'No encontramos datasets' : 'Tu primer dataset empieza aquí'} description={search || domain ? 'Prueba otro nombre o cambia los filtros.' : 'Carga un archivo compatible para analizar sus columnas y empezar un control.'} action={<button className="button secondary" onClick={() => search || domain ? (setSearch(''), setDomain('')) : setUploadOpen(true)}>{search || domain ? 'Limpiar filtros' : 'Cargar archivo'}</button>}/> : <div className="table-scroll"><table><thead><tr><th>Dataset</th><th>Origen</th><SortableDatasetHeader column="domain" sort={sort} onSort={toggleSort}>Área</SortableDatasetHeader><SortableDatasetHeader column="row_count" sort={sort} onSort={toggleSort}>Registros</SortableDatasetHeader><SortableDatasetHeader column="version_count" sort={sort} onSort={toggleSort}>Versiones</SortableDatasetHeader><SortableDatasetHeader column="status" sort={sort} onSort={toggleSort}>Estado</SortableDatasetHeader><SortableDatasetHeader column="updated_at" sort={sort} onSort={toggleSort}>Última actualización</SortableDatasetHeader><th/></tr></thead><tbody>{sorted.map(dataset => <tr key={dataset.id}><td><div className="cell-with-icon"><span className="data-icon"><Database size={19}/></span><div><Link to={`/datasets/${dataset.id}`} className="table-primary">{dataset.name}</Link><small className="table-subtitle">{dataset.description || dataset.owner}</small></div></div></td><td><span className="dataset-origin">{datasetOriginLabel(dataset)}</span></td><td>{dataset.domain}</td><td className="tabular">{number(dataset.row_count)}</td><td><span className="version-pill">v{dataset.version_count || 0}</span></td><td><Badge value={dataset.status}/></td><td className="muted no-wrap">{date(dataset.updated_at, false)}</td><td><Link to={`/datasets/${dataset.id}`} className="icon-button" aria-label={`Abrir ${dataset.name}`}><ArrowUpRight size={17}/></Link></td></tr>)}</tbody></table></div>}</section><UploadDialog key={uploadOpen ? 'open' : 'closed'} open={uploadOpen} existingDatasets={items} onClose={() => { setUploadOpen(false); searchParams.delete('upload'); setSearchParams(searchParams, { replace: true }) }}/></>
}
export function DatasetDetail() {
  const canUpload = usePermission('datasets:write')
  const { id } = useParams(), [versionId, setVersionId] = useState(''), [tab, setTab] = useState('profile'), [upload, setUpload] = useState(false)
  const dataset = useQuery({ queryKey: ['dataset', id], queryFn: () => api(`/datasets/${id}`) })
  const versions: RecordData[] = dataset.data?.versions || [], currentId = versionId || versions[0]?.id
  const profile = useQuery({ queryKey: ['profile', currentId], queryFn: () => api(`/dataset-versions/${currentId}/profile`), enabled: !!currentId })
  if (dataset.isPending) return <Loading/>
  if (dataset.error) return <ErrorState error={dataset.error} retry={() => dataset.refetch()}/>
  const data = dataset.data, version = profile.data, sample: RecordData[] = version?.sample || [], columns: RecordData[] = version?.profile?.columns || []
  return <><PageHeading back="/datasets" eyebrow="DATASET" title={data.name} description={data.description || 'Versiones y perfil de tu activo de información.'} action={<button className="button primary" disabled={!canUpload} onClick={() => setUpload(true)}><Upload size={17}/> Nueva versión</button>}/><div className="detail-summary"><Badge value={data.status}/><span>{data.domain}</span><span>Responsable: <strong>{data.owner}</strong></span><span>Criticidad: <Badge value={data.criticality}/></span></div><>{data.source_binding?.connection_id && <SourceRefresh datasetId={data.id} connectionId={data.source_binding.connection_id} connectionState={data.source_binding.connection_state} onRefreshed={setVersionId}/>}</><section className="panel dataset-detail"><div className="panel-heading"><div><h2>Explorador del dataset</h2><p>Consulta un corte específico y su perfil.</p></div><select aria-label="Seleccionar versión" value={currentId || ''} onChange={e => setVersionId(e.target.value)}>{versions.map(v => <option key={v.id} value={v.id}>Versión {v.version} · {v.filename} · {date(v.created_at, false)}</option>)}</select></div>{!currentId ? <Empty title="Este dataset aún no tiene archivos" description="Carga una primera versión para obtener su perfil y ejecutar controles." action={<button className="button primary" onClick={() => setUpload(true)}>Cargar versión</button>}/> : profile.isPending ? <Loading/> : profile.error ? <ErrorState error={profile.error} retry={() => profile.refetch()}/> : <><div className="profile-stats"><div><span>Filas</span><strong>{number(version?.row_count)}</strong></div><div><span>Columnas</span><strong>{number(version?.column_count)}</strong></div><div><span>{['POSTGRESQL', 'SQLSERVER'].includes(version?.source_type) ? 'Tamaño snapshot' : version?.source_type === 'INTAKE_OUTPUT' ? 'Tamaño derivado' : 'Tamaño original'}</span><strong>{number((version?.size_bytes || 0) / 1024)} <small>KB</small></strong></div><div><span>Perfil</span><Badge value={version?.profile_status}/></div></div><div className="tabs">{[['profile', 'Perfil de columnas'], ['sample', 'Muestra de datos'], ['versions', 'Historial de versiones'], ['evidence', 'Fuente de la versión']].map(([key, text]) => <button key={key} className={tab === key ? 'active' : ''} onClick={() => setTab(key)}>{text}</button>)}</div>{tab === 'profile' && <><ProfilingPolicy profile={version?.profile || {}}/><div className="table-scroll"><table><thead><tr><th>Columna</th><th>Tipo detectado</th><th>Valores distintos</th><th>Valores nulos</th><th>Texto vacío</th><th>Completitud</th></tr></thead><tbody>{columns.map(c => <tr key={c.name}><td className="mono">{c.name}</td><td><span className="type-pill">{c.logical_type}</span>{c.semantic_tag === 'IDENTIFIER' && <small className="reason-code">Identificador · conserva ceros iniciales</small>}</td><td>{number(c.distinct_count)}</td><td>{number(c.null_count)}</td><td>{number(c.empty_string_count ?? c.empty_count)}</td><td><div className="inline-progress"><div><i style={{ width: `${Math.max(0, 100 - Number(c.null_rate || 0) * 100)}%` }}/></div><span>{number(100 - Number(c.null_rate || 0) * 100)}%</span></div></td></tr>)}</tbody></table></div></>}{tab === 'sample' && (sample.length ? <><div className="sample-note">Vista limitada a {number(sample.length)} registros de esta versión.</div><div className="table-scroll"><table><thead><tr>{Object.keys(sample[0]).map(k => <th key={k}>{k}</th>)}</tr></thead><tbody>{sample.map((row, i) => <tr key={i}>{Object.entries(row).map(([key, value]) => <td key={key}><SampleValue value={value}/></td>)}</tr>)}</tbody></table></div></> : <Empty title="Sin registros de muestra" description="El archivo no contiene filas para previsualizar."/>)}{tab === 'versions' && <div className="table-scroll"><table><thead><tr><th>Versión</th><th>Archivo</th><th>Origen</th><th>Filas</th><th>Creada</th><th/></tr></thead><tbody>{versions.map(v => <tr key={v.id}><td><span className="version-pill">v{v.version}</span></td><td>{v.filename}</td><td>{label(v.source_type)}</td><td>{number(v.row_count)}</td><td>{date(v.created_at)}</td><td><button className="text-button" onClick={() => { setVersionId(v.id); setTab('profile') }}>Ver perfil</button></td></tr>)}</tbody></table></div>}{tab === 'evidence' && version && <VersionIdentity version={version} connectionState={data.source_binding?.connection_state}/>}</>}</section><UploadDialog key={upload ? 'open' : 'closed'} open={upload} onClose={() => setUpload(false)} datasetId={id} datasetName={data.name}/></>
}
