import { useMutation } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { Download } from 'lucide-react'
import { download } from '../../api/client'
import type { RecordData } from '../../api/client'
import { ErrorState, Field, label, Notice, number } from '../../components/ui'
import { usePermission } from '../../app/session'

export function SampleValue({ value }: { value: unknown }) {
  return value == null ? <span className="null-value">null</span> : value === '' ? <span className="empty-value">"" (texto vacío)</span> : <span className="observed-value">{String(value)}</span>
}

export function ProfilingPolicy({ profile }: { profile: RecordData }) {
  return <div className="sample-note">{profile.profiling_policy === 'OBSERVED_EXACT_V2'
    ? 'Perfil de valores observados: distingue espacios, mayúsculas y Unicode. Null y texto vacío se contabilizan por separado.'
    : 'Perfil histórico: conserva el cálculo de la versión anterior del sistema, que podía normalizar espacios y texto vacío. Una nueva carga aplica la política de valores observados sin modificar este historial.'}</div>
}

function visibleDelimiter(value: unknown) {
  if (value === '\t') return 'Tabulación (\\t)'
  if (value === ',') return 'Coma (,)'
  if (value === ';') return 'Punto y coma (;)'
  if (value === '|') return 'Barra vertical (|)'
  return String(value || '')
}

export function VersionIdentity({ version, connectionState }: { version: RecordData; connectionState?: string }) {
  const derived = version.is_derived || version.source_type === 'INTAKE_OUTPUT'
  const ingestion = version.ingestion_metadata || {}
  const source = ingestion.source || {}
  const external = ['POSTGRESQL', 'SQLSERVER'].includes(version.source_type)
  const readerOptions = ingestion.reader_options || {}
  const canDownload = usePermission('artifacts:download')
  const request = useMutation({ mutationFn: (artifact: RecordData) => download(`/artifacts/${artifact.artifact_id}/download`, artifact.name) })
  return <div className="evidence-fields"><Field label="Fuente de la versión"><input readOnly value={external ? `Snapshot de ${version.source_type === 'POSTGRESQL' ? 'PostgreSQL' : 'SQL Server'}` : derived ? 'Versión derivada de Intake' : label(version.source_type)}/></Field>
    {!external && (version.has_original_upload || (!derived && !version.artifacts?.length)) && <Field label="Archivo original"><input readOnly value={version.filename || ''}/></Field>}
    {external && <><Field label="Snapshot canónico"><input readOnly value={version.filename || 'snapshot.parquet'}/></Field><Field label="Schema de origen"><input readOnly value={source.schema_name || ''}/></Field><Field label="Tabla o vista de origen"><input readOnly value={source.object_name || ''}/></Field><Field label="Versión de la conexión"><input readOnly value={source.connection_version || ''}/></Field><Field label="Identificador de configuración de conexión"><input readOnly className="mono" value={source.connection_version_id || ''}/></Field><Field label="Hash de configuración de conexión"><input readOnly className="mono" value={source.config_hash || ''}/></Field>{source.connection_id && connectionState !== 'DELETED' && <Link className="button secondary" to={`/connections/${source.connection_id}`}>Ver conexión de origen</Link>}</>}
    {derived && <Field label="Artefacto derivado"><input readOnly value={version.filename || 'accepted.parquet'}/></Field>}
    {ingestion.source_format && <Field label="Formato de origen"><input readOnly value={ingestion.format_label || label(ingestion.source_format)}/></Field>}
    {readerOptions.sheet_name && <Field label="Hoja de Excel"><input readOnly value={readerOptions.sheet_name}/></Field>}
    {readerOptions.delimiter && <Field label="Delimitador"><input readOnly value={visibleDelimiter(readerOptions.delimiter)}/></Field>}
    <Field label={external ? 'SHA-256 del snapshot' : derived ? 'SHA-256 del artefacto derivado' : 'SHA-256 del archivo original'}><input className="mono" readOnly value={version.sha256 || ''}/></Field><Field label="Identificador de esquema"><input className="mono" readOnly value={version.schema_hash || ''}/></Field>
    {version.parent_version_id && <Field label="DatasetVersion de entrada"><input readOnly className="mono" value={version.parent_version_id}/></Field>}
    {version.source_run_id && <Link className="button secondary" to={`/runs/${version.source_run_id}`}>Ver ejecución de Intake de origen</Link>}
    {(version.artifacts || []).map((artifact: RecordData) => <article key={artifact.artifact_id} className="artifact-card"><div><strong>{label(artifact.kind)}</strong><p>{artifact.name} · {number(artifact.size_bytes / 1024)} KB</p><code>{artifact.sha256}</code></div><button type="button" className="button secondary small" disabled={!canDownload || request.isPending} onClick={() => request.mutate(artifact)}><Download size={14}/>{request.isPending ? 'Descargando…' : artifact.kind === 'ORIGINAL_UPLOAD' ? 'Descargar original' : artifact.name?.toLowerCase().endsWith('.parquet') ? 'Descargar Parquet' : 'Descargar artefacto'}</button></article>)}
    {request.error && <ErrorState error={request.error}/>}<Notice>{external ? 'Este snapshot inmutable conserva los datos consultados, su huella y la configuración de la fuente externa. Consultar nuevamente la fuente crea una nueva versión.' : derived ? 'Este resultado reutilizable se conserva como Parquet canónico, con referencia a la versión de entrada y a la ejecución que lo produjo.' : 'Esta versión es inmutable. El archivo recibido y su Parquet canónico conservan identidades y huellas independientes.'}</Notice>
  </div>
}
