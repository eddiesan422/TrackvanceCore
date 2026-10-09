import { Link } from 'react-router-dom'

export function SourceLinks({ source, candidate = false }: { source: Record<string, unknown>; candidate?: boolean }) {
  const input = String(source.input_dataset_id || ''), output = String(source.output_dataset_id || '')
  const inputVersion = String(source.input_version_id || ''), outputVersion = String(source.output_version_id || '')
  const run = String(source.approval_run_id || '')
  return <span className="report-source-links">
    {output && outputVersion && <Link className="text-link" to={`/catalog/datasets/${encodeURIComponent(output)}?version_id=${encodeURIComponent(outputVersion)}`}>{candidate ? 'Ver salida candidata aprobada' : 'Ver salida aprobada'}</Link>}
    {input && <> · <Link to={`/catalog/datasets/${encodeURIComponent(input)}${inputVersion ? `?version_id=${encodeURIComponent(inputVersion)}` : ''}`}>Ver dataset de origen</Link></>}
    {run && <> · <Link to={`/runs/${encodeURIComponent(run)}`}>Ver validación Intake</Link></>}
  </span>
}
