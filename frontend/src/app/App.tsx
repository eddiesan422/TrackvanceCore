import { lazy, useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { NavLink, Navigate, Route, Routes, Link, useLocation, useNavigate } from 'react-router-dom'
import { Activity, BookOpen, Check, ChevronRight, CircleHelp, ClipboardList, Database, Files, FileCheck2, GitCompareArrows, LayoutDashboard, LogOut, Menu, Plug, Search, Send, Settings2, ShieldCheck, Upload, X } from 'lucide-react'
import { api, ApiError, post, setCsrfToken } from '../api/client'
import type { Session } from '../api/client'
import { ErrorState, Loading, Modal, Notice, label } from '../components/ui'
import { Brand } from '../components/Brand'
import { Login } from './Login'
import { RouteContent } from './RouteContent'
const CatalogRoutes = lazy(() => import('../features/catalog/Catalog').then(module => ({ default: module.CatalogRoutes })))
const ReportsRoutes = lazy(() => import('../features/reports/Reports').then(module => ({ default: module.ReportsRoutes })))
const Documents = lazy(() => import('../features/documents/Documents'))
const OperationTracking = lazy(() => import('../routes/OperationTracking'))
const VersionComparison = lazy(() => import('../features/datasets/VersionComparison').then(module => ({ default: module.VersionComparison })))
const Dashboard = lazy(() => import('../routes/Dashboard'))
const HistoryPage = lazy(() => import('../routes/History'))
const DatasetsPage = lazy(() => import('../features/datasets/Datasets').then(module => ({ default: module.DatasetsPage })))
const DatasetDetail = lazy(() => import('../features/datasets/Datasets').then(module => ({ default: module.DatasetDetail })))
const ModulePage = lazy(() => import('../features/runs/Runs').then(module => ({ default: module.ModulePage })))
const RunDetail = lazy(() => import('../features/runs/Runs').then(module => ({ default: module.RunDetail })))
const ConnectionsPage = lazy(() => import('../features/connections/Connections').then(module => ({ default: module.ConnectionsPage })))
const ConnectionDetail = lazy(() => import('../features/connections/Connections').then(module => ({ default: module.ConnectionDetail })))
const DeliveryRoutes = lazy(() => import('../features/delivery/Delivery').then(module => ({ default: module.DeliveryRoutes })))
const ExceptionsPage = lazy(() => import('../routes/Operations').then(module => ({ default: module.ExceptionsPage })))
const AuditPage = lazy(() => import('../routes/Operations').then(module => ({ default: module.AuditPage })))
const RulesPage = lazy(() => import('../routes/Operations').then(module => ({ default: module.RulesPage })))
const SettingsPage = lazy(() => import('../routes/Operations').then(module => ({ default: module.SettingsPage })))
import { AuthContext, operationReadPermissions } from './session'
import { NotificationIndicator } from '../features/notifications/Notifications'
import { FirstLogin } from '../features/identity/FirstLogin'
import '../features/identity/identity.css'
const routePermissions: Record<string, string> = { '/': 'runs:read', '/datasets': 'datasets:read', '/documents': 'documents:read', '/operations': 'exceptions:read', '/catalog': 'catalog:read', '/reports': 'reports:read', '/connections': 'connections:read', '/intake': 'intake:read', '/recon': 'recon:read', '/sentinel': 'sentinel:read', '/delivery': 'delivery:read', '/exceptions': 'exceptions:read', '/rules': 'rules:read', '/audit': 'audit:read', '/notifications': 'notifications:read' }
const navigation = [
  { to: '/', icon: LayoutDashboard, text: 'Centro de control', end: true },
  { to: '/operations', icon: Activity, text: 'Operación' },
  { to: '/documents', icon: Files, text: 'Documentos' },
  { to: '/datasets', icon: Database, text: 'Datasets' },
  { to: '/catalog', icon: BookOpen, text: 'Catálogo' },
  { to: '/reports', icon: FileCheck2, text: 'Reportes' },
  { to: '/connections', icon: Plug, text: 'Conexiones' },
  { to: '/intake', icon: Upload, text: 'Intake', subtitle: 'Calidad de entrada' },
  { to: '/recon', icon: GitCompareArrows, text: 'ReconOps', subtitle: 'Conciliación' },
  { to: '/sentinel', icon: ShieldCheck, text: 'Sentinel', subtitle: 'Monitoreo continuo' },
  { to: '/delivery', icon: Send, text: 'Data Delivery', subtitle: 'Publicación' },
  { to: '/exceptions', icon: ClipboardList, text: 'Excepciones' },
  { to: '/rules', icon: BookOpen, text: 'Biblioteca de reglas' },
  { to: '/audit', icon: Activity, text: 'Auditoría' },
]
const navigationGroups = [
  { label: 'Espacio de trabajo', paths: ['/', '/operations', '/documents', '/datasets', '/catalog', '/reports', '/connections'] },
  { label: 'Procesamiento', paths: ['/intake', '/recon', '/sentinel', '/delivery'] },
  { label: 'Supervisión', paths: ['/exceptions', '/rules', '/audit'] },
]
function LegacyNotificationsRedirect() {
  const location = useLocation()
  const params = new URLSearchParams(location.search)
  params.set('view', 'news')
  return <Navigate replace to={`/operations?${params}`}/>
}
function Shell({ session }: { session: Session }) {
  const queryClient = useQueryClient(), location = useLocation(), navigate = useNavigate()
  const [mobileOpen, setMobileOpen] = useState(false), [help, setHelp] = useState(false)
  const menuTrigger = useRef<HTMLButtonElement>(null)
  useEffect(() => {
    if (!mobileOpen) return
    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    const previous = menuTrigger.current
    const sidebar = document.getElementById('primary-navigation')
    const controls = () => [...(sidebar?.querySelectorAll<HTMLElement>('a[href], button:not(:disabled)') || [])].filter(element => element.getClientRects().length > 0)
    controls()[0]?.focus()
    const keyboard = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { event.preventDefault(); setMobileOpen(false) }
      if (event.key !== 'Tab') return
      const items = controls(), first = items[0], last = items.at(-1)
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus() }
      if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus() }
    }
    document.addEventListener('keydown', keyboard)
    return () => { document.body.style.overflow = previousOverflow; document.removeEventListener('keydown', keyboard); previous?.focus() }
  }, [mobileOpen])
  const logout = useMutation({ mutationFn: () => post('/auth/logout'), onSuccess: () => { navigate('/', { replace: true }); setCsrfToken(''); queryClient.removeQueries({ predicate: query => query.queryKey[0] !== 'session' }); queryClient.setQueryData(['session'], null) } })
  const canRead = (path: string) => path === '/operations' ? [...operationReadPermissions, 'notifications:read'].some(permission => session.user.permissions.includes(permission)) : path === '/' ? ['runs:read', 'intake:read', 'recon:read', 'sentinel:read', 'delivery:read'].some(permission => session.user.permissions.includes(permission)) : session.user.permissions.includes(routePermissions[path])
  const current = navigation.find(n => n.to === '/' ? location.pathname === '/' : location.pathname.startsWith(n.to))
  const protectedRoute = navigation.find(item => item.to === '/' ? location.pathname === '/' : location.pathname === item.to || location.pathname.startsWith(`${item.to}/`))
  const denied = protectedRoute && !canRead(protectedRoute.to) && !location.pathname.startsWith('/delivery/new') && !location.pathname.startsWith('/delivery/destinations')
  return <AuthContext.Provider value={session}><div className="app-shell"><a className="skip-link" href="#main-content">Ir al contenido principal</a><aside id="primary-navigation" aria-label="Navegación del espacio de trabajo" className={`sidebar ${mobileOpen ? 'mobile-open' : ''}`}><Link to="/" className="brand-link" onClick={() => setMobileOpen(false)}><Brand/></Link><div className="workspace"><div className="workspace-avatar">TC</div><div><strong>{session.organization?.name || 'Trackvance Colombia'}</strong><span>Espacio de trabajo local</span></div><ChevronRight size={14}/></div><nav aria-label="Navegación principal">{navigationGroups.map(group => {
    const items = navigation.filter(item => group.paths.includes(item.to) && (canRead(item.to) || (item.to === '/delivery' && session.user.permissions.includes('destinations:read'))))
    return items.length > 0 && <div className="nav-group" role="group" aria-label={group.label} key={group.label}>
      <div className="nav-label">{group.label}</div>
      {items.map(item => <NavLink key={item.to} to={item.to === '/delivery' && !canRead(item.to) ? '/delivery/destinations' : item.to} end={item.end} className={({ isActive }) => `nav-item ${isActive ? 'active' : ''}`} onClick={() => setMobileOpen(false)}>
        <item.icon size={19}/><span>{item.text}</span>
        {item.to === '/intake' && <span className="tiny-label">IN</span>}{item.to === '/recon' && <span className="tiny-label">RE</span>}{item.to === '/sentinel' && <span className="tiny-label">SE</span>}{item.to === '/delivery' && <span className="tiny-label">OUT</span>}
      </NavLink>)}
    </div>
  })}</nav><div className="sidebar-bottom"><div className="local-indicator"><span className="status-dot"/><div><strong>Entorno local</strong><span>Trackvance Core · 0.9.8</span></div><Activity size={16}/></div><NavLink to="/settings/system" className={({ isActive }) => `nav-item ${isActive ? 'active' : ''}`}><Settings2 size={18}/><span>Configuración</span></NavLink><button className="nav-item" onClick={() => setHelp(true)}><CircleHelp size={18}/><span>Guía de inicio</span></button><div className="profile"><div className="avatar">{(session.user?.name || 'Usuario').split(' ').slice(0, 2).map(p => p[0]).join('')}</div><div><strong>{session.user?.name}</strong><span>{label(session.user.role)}</span></div><button className="icon-button" aria-label="Cerrar sesión" title="Cerrar sesión" onClick={() => logout.mutate()} disabled={logout.isPending}><LogOut size={17}/></button></div>{logout.error && <p className="sidebar-error">No se pudo cerrar la sesión.</p>}</div></aside>{mobileOpen && <button className="mobile-backdrop" onClick={() => setMobileOpen(false)} aria-label="Cerrar navegación"/>}<div className="main-shell" inert={mobileOpen}><header className="topbar"><div className="breadcrumbs"><button ref={menuTrigger} className="icon-button mobile-menu" aria-label={mobileOpen ? 'Cerrar navegación' : 'Abrir navegación'} aria-expanded={mobileOpen} aria-controls="primary-navigation" onClick={() => setMobileOpen(!mobileOpen)}>{mobileOpen ? <X size={20}/> : <Menu size={20}/>}</button><span>Espacio de trabajo</span><ChevronRight size={14}/><strong>{current?.text || (location.pathname.startsWith('/runs') ? 'Ejecuciones' : 'Configuración')}</strong></div><div className="topbar-actions">{canRead('/datasets') && <Link to="/datasets" className="top-search"><Search size={16}/><span>Buscar datasets</span></Link>}<span className="environment-chip"><span className="status-dot"/> ENTORNO LOCAL</span><NotificationIndicator/><div className="top-avatar">{session.user?.name?.[0] || 'T'}</div></div></header><main id="main-content" tabIndex={-1}><RouteContent key={location.pathname}>{denied ? <Notice>Tu rol no permite consultar esta sección. Selecciona una opción disponible en la navegación.</Notice> : <Routes><Route path="/" element={<Dashboard/>}/><Route path="/datasets" element={<DatasetsPage/>}/><Route path="/datasets/:id" element={<DatasetDetail/>}/><Route path="/datasets/:datasetId/versions/compare" element={<VersionComparison/>}/><Route path="/documents/*" element={<Documents/>}/><Route path="/operations" element={<OperationTracking/>}/><Route path="/catalog/*" element={<CatalogRoutes/>}/><Route path="/reports/*" element={<ReportsRoutes/>}/><Route path="/connections" element={<ConnectionsPage/>}/><Route path="/connections/:id" element={<ConnectionDetail/>}/><Route path="/intake/*" element={<ModulePage module="intake"/>}/><Route path="/recon/*" element={<ModulePage module="recon"/>}/><Route path="/sentinel/*" element={<ModulePage module="sentinel"/>}/><Route path="/delivery/*" element={<DeliveryRoutes/>}/><Route path="/runs" element={<HistoryPage/>}/><Route path="/runs/:id" element={<RunDetail/>}/><Route path="/notifications" element={<LegacyNotificationsRedirect/>}/><Route path="/exceptions/*" element={<ExceptionsPage/>}/><Route path="/rules" element={<RulesPage/>}/><Route path="/audit" element={<AuditPage/>}/><Route path="/settings/*" element={<SettingsPage/>}/><Route path="*" element={<div className="empty-state"><h1>Página no encontrada</h1><Link to="/" className="button primary">Volver al centro de control</Link></div>}/></Routes>}</RouteContent></main><footer className="app-footer"><span>TRACKVANCE CORE <span className="footer-divider">/</span> Una fuente de confianza para tus datos</span><span>Implementación · v0.9.8</span></footer></div></div><Modal open={help} onOpenChange={setHelp} title="Tu primera ejecución" description="De los datos a la evidencia, en cuatro pasos."><div className="guide-steps">{[['01', 'Prepara tus datos', 'Explora los datasets de demostración o carga un archivo CSV, XLSX, JSON, Parquet o TXT delimitado desde Datasets.'], ['02', 'Elige un control', 'Intake valida, ReconOps concilia, Sentinel monitorea y Data Delivery publica una versión ya registrada.'], ['03', 'Revisa la evidencia', 'Cada ejecución conserva sus resultados y un manifiesto descargable.'], ['04', 'Cierra el ciclo', 'Investiga las excepciones y documenta su causa y resolución.']].map(([n, title, text]) => <div key={n}><b>{n}</b><section><strong>{title}</strong><p>{text}</p></section></div>)}</div><div className="modal-footer"><button className="button primary" onClick={() => setHelp(false)}>Entendido <Check size={16}/></button></div></Modal></AuthContext.Provider>
}
export default function App() {
  const queryClient = useQueryClient()
  const session = useQuery({ queryKey: ['session'], queryFn: async () => { try { return await api<Session>('/me') } catch (e) { if (e instanceof ApiError && e.status === 401) return null; throw e } }, retry: false, staleTime: 0, refetchInterval: 15000, refetchOnWindowFocus: true })
  const previousIdentity = useRef<string | null>(null)
  useEffect(() => {
    const value = session.data
    const identity = value ? JSON.stringify([value.user.id, value.user.role_id, value.user.role_version, value.user.permissions, value.user.must_change_password]) : null
    if (previousIdentity.current !== null && previousIdentity.current !== identity) {
      queryClient.removeQueries({ predicate: query => query.queryKey[0] !== 'session' })
    }
    previousIdentity.current = identity
  }, [session.data, queryClient])
  useEffect(() => {
    const refresh = () => { void queryClient.invalidateQueries({ queryKey: ['session'] }) }
    window.addEventListener('trackvance:session-refresh', refresh)
    return () => window.removeEventListener('trackvance:session-refresh', refresh)
  }, [queryClient])
  if (session.isPending) return <div className="bootstrap"><Loading text="Conectando con tu espacio de trabajo…"/></div>
  if (session.error) return <div className="bootstrap"><ErrorState error={session.error} retry={() => session.refetch()}/></div>
  if (!session.data) return <Login onLogin={data => { setCsrfToken(data.csrf_token); queryClient.setQueryData(['session'], data) }}/>
  setCsrfToken(session.data.csrf_token)
  if (session.data.user.must_change_password) return <FirstLogin onComplete={data => { setCsrfToken(data.csrf_token); queryClient.setQueryData(['session'], data) }} onLogout={() => { setCsrfToken(''); queryClient.removeQueries({ predicate: query => query.queryKey[0] !== 'session' }); queryClient.setQueryData(['session'], null) }}/>
  return <Shell key={`${session.data.user.id}:${session.data.user.role_version}:${session.data.user.permissions.join(',')}`} session={session.data}/>
}
