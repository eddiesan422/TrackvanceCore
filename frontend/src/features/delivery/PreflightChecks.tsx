import { Badge } from '../../components/ui'

export function PreflightChecks({ checks }: { checks: { code: string; status: string; message: string }[] }) {
  return <ul aria-label="Comprobaciones de preflight">{checks.map((check, index) =>
    <li key={`${check.code}-${index}`} data-outcome={check.status}>
      <Badge value={check.status}/><div><strong>{check.code}</strong><span>{check.message}</span></div>
    </li>,
  )}</ul>
}
