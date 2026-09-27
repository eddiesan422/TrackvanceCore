import { useQuery } from '@tanstack/react-query'
import { api } from '../../api/client'
import { Badge, ErrorState, Loading } from '../../components/ui'

export function AuthenticationPanel() {
  const providers = useQuery({ queryKey: ['auth-providers'], queryFn: () => api<{ local_enabled: boolean; statuses: Record<string, string> }>('/auth/providers') })
  return <section className="panel"><div className="panel-heading"><div><h2>Autenticación</h2><p>Estado de los métodos de acceso configurados para esta instalación.</p></div><button className="button secondary" onClick={() => providers.refetch()}>Actualizar autenticación</button></div>{providers.isPending ? <Loading/> : providers.error ? <ErrorState error={providers.error} retry={() => providers.refetch()}/> : <div className="table-scroll"><table><thead><tr><th>Método</th><th>Estado</th><th>Acceso</th></tr></thead><tbody><tr><td><strong>Login local</strong></td><td><Badge value={providers.data.local_enabled ? 'ACTIVE' : 'INACTIVE'}>{providers.data.local_enabled ? 'Habilitado' : 'Deshabilitado'}</Badge></td><td>Username o correo y contraseña</td></tr>{[['microsoft', 'Microsoft'], ['google', 'Google']].map(([key, name]) => <tr key={key}><td><strong>{name}</strong></td><td><Badge value={providers.data.statuses?.[key] === 'ENABLED' ? 'ACTIVE' : 'INACTIVE'}>{providers.data.statuses?.[key] === 'ENABLED' ? 'Habilitado' : providers.data.statuses?.[key] === 'DISABLED' ? 'Deshabilitado' : 'No configurado'}</Badge></td><td>Usuario previamente creado en Trackvance</td></tr>)}</tbody></table></div>}</section>
}
