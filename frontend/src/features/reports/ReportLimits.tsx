import type { RecordData } from '../../api/client'
import { number } from '../../components/ui'

const names: Record<string, string> = { PREVIEW: 'Vista previa', DOWNLOAD: 'Descarga CSV', XLSX: 'Descarga XLSX', DATASET: 'Generación de dataset' }
export function ReportLimits({ value }: { value: RecordData }) {
  return <details><summary>Límites efectivos por perfil</summary><p className="muted">Estos valores son límites de configuración. El volumen certificado se documenta por separado. La muestra no garantiza que una salida completa termine dentro de sus límites.</p><div className="table-scroll"><table><thead><tr><th>Acción</th><th>Filas de resultado</th><th>Tamaño máximo</th><th>Tiempo</th><th>Memoria motor / proceso</th><th>Persistencia</th></tr></thead><tbody>{Object.entries(value.profiles || {}).map(([profile, raw]) => { const limits = raw as RecordData; return <tr key={profile}><td>{names[profile] || profile}</td><td>{number(limits.max_rows)}</td><td>{number(Number(limits.max_bytes) / 1024**2)} MiB</td><td>{number(limits.timeout_seconds)} s</td><td>{number(Number(limits.memory_bytes) / 1024**2)} / {number(Number(limits.process_memory_bytes) / 1024**2)} MiB</td><td>{limits.filesystem_results ? 'Publicación explícita' : 'Sin archivos de resultado'}</td></tr> })}</tbody></table></div></details>
}
