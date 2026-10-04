import { useQuery } from '@tanstack/react-query'
import { api, ApiError } from '../../api/client'
import { ErrorState, Loading, number } from '../../components/ui'

export type LimitRoute = 'ASYNC_ACQUISITION' | 'LEGACY_UPLOAD'
export type EffectiveLimits = {
  route: LimitRoute; format: string
  limits: Record<string, { max: number; unit: 'bytes' | 'records' | 'columns' | 'seconds' | 'entries' | 'cells' }>
  physical_sheet_rows?: number; header_row_number: number | null; inspection_limited: boolean
}

const names: Record<string, string> = {
  compressed_bytes: 'Archivo recibido', expanded_bytes: 'Contenido expandido', data_rows: 'Registros de datos',
  columns: 'Columnas', cell_bytes: 'Bytes por celda', record_bytes: 'Bytes por registro',
  batch_rows: 'Registros por lote', batch_bytes: 'Bytes por lote', metadata_bytes: 'Metadata del libro',
  memory_bytes: 'Presupuesto del motor de perfil', timeout_seconds: 'Tiempo total de adquisición',
  materialized_cells: 'Celdas materializadas', temporary_bytes: 'Disco temporal',
  metadata_seconds: 'Tiempo de metadata', inspection_seconds: 'Tiempo de inspección',
  shared_strings: 'Entradas de texto compartido', shared_string_entries: 'Entradas de texto compartido',
  styles: 'Estilos', style_entries: 'Estilos', inspection_bytes: 'Bytes de inspección',
  max_rows: 'Registros de datos', max_upload_bytes: 'Archivo recibido',
}

export function limitFormat(filename?: string) {
  const extension = filename?.split('.').pop()?.toLowerCase()
  return ({ xlsx: 'XLSX', json: 'JSON', jsonl: 'JSON_LINES', ndjson: 'JSON_LINES', parquet: 'PARQUET', pq: 'PARQUET', txt: 'TXT', tsv: 'TXT' } as Record<string, string>)[extension || ''] || 'CSV'
}

export function limitValue(value: number, unit: string) {
  if (unit === 'bytes') {
    if (value >= 1024 ** 3) return `${number(value / 1024 ** 3)} GiB`
    if (value >= 1024 ** 2) return `${number(value / 1024 ** 2)} MiB`
    if (value >= 1024) return `${number(value / 1024)} KiB`
    return `${number(value)} bytes`
  }
  const label = ({ records: 'registros', columns: 'columnas', seconds: 'segundos', entries: 'entradas', cells: 'celdas' } as Record<string, string>)[unit] || unit
  return `${number(value)} ${label}`
}

export function limitsKey(format: string, route: LimitRoute) { return ['acquisition-limits', route, format] }

export async function getAcquisitionLimits(format: string, route: LimitRoute): Promise<EffectiveLimits> {
  const value = await api<EffectiveLimits>(`/acquisitions/limits?format=${encodeURIComponent(format)}&route=${route}`)
  const units = ['bytes', 'records', 'columns', 'seconds', 'entries', 'cells']
  if (!value || value.route !== route || !value.limits || !Object.keys(value.limits).length || Object.values(value.limits).some(limit => !Number.isFinite(limit?.max) || limit.max <= 0 || !units.includes(limit.unit))) throw new ApiError('No están disponibles los límites efectivos de esta ruta. Actualiza e inténtalo de nuevo.', 0)
  return value
}

export function useAcquisitionLimits(format: string, route: LimitRoute, enabled: boolean) {
  return useQuery({ queryKey: limitsKey(format, route), queryFn: () => getAcquisitionLimits(format, route), enabled })
}

export function AcquisitionLimits({ limits, loading, error, retry }: { limits?: EffectiveLimits; loading?: boolean; error?: unknown; retry?: () => void }) {
  if (error && !limits) return <ErrorState error={error} retry={retry}/>
  if (!limits) return loading ? <Loading text="Consultando límites efectivos…"/> : null
  return <section className="file-inspection" aria-label="Límites efectivos de la ruta">
    <strong>Límites efectivos · {limits.format} · {limits.route === 'LEGACY_UPLOAD' ? 'Carga rápida' : 'Adquisición en segundo plano'}</strong>
    <div className="table-scroll"><table><thead><tr><th>Control</th><th>Máximo configurado</th></tr></thead><tbody>{Object.entries(limits.limits).map(([name, limit]) => <tr key={name}><td>{names[name] || name.replaceAll('_', ' ')}</td><td>{limitValue(limit.max, limit.unit)}</td></tr>)}</tbody></table></div>
    {limits.physical_sheet_rows != null && <p>Máximo físico por hoja: {number(limits.physical_sheet_rows)} filas, incluidos el encabezado y las filas anteriores.{limits.header_row_number != null && <> Encabezado observado en la fila {number(limits.header_row_number)}.</>}</p>}
    {limits.route === 'LEGACY_UPLOAD' ? <p>Esta ruta analiza dentro de la solicitud HTTP y conserva sus cotas para cargas pequeñas.</p> : <p>El perfil cubre la fuente completa. Los presupuestos de lote y motor son independientes del consumo total del proceso y del contenedor.</p>}
  </section>
}
