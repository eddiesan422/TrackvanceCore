import { Component, Suspense } from 'react'
import type { ReactNode } from 'react'
import { ErrorState, Loading } from '../components/ui'

/** Keep navigation usable while a feature chunk loads or cannot be retrieved. */
export class RouteContent extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false }

  static getDerivedStateFromError() { return { failed: true } }

  render() {
    if (this.state.failed) return <div><ErrorState error={new Error('No se pudo cargar esta sección. Comprueba la conexión y recarga la página.')}/><button className="button secondary" onClick={() => window.location.reload()}>Recargar página</button></div>
    return <Suspense fallback={<Loading text="Cargando sección…"/>}>{this.props.children}</Suspense>
  }
}
