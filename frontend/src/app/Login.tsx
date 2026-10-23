import { useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { ArrowRight, Check, Database, FileCheck2, GitCompareArrows, Monitor, ShieldCheck } from 'lucide-react'
import { api, post } from '../api/client'
import type { Session } from '../api/client'
import { ErrorState, Field } from '../components/ui'
import { Brand } from '../components/Brand'
import { DataBackdrop } from '../components/DataBackdrop'

export function Login({ onLogin }: { onLogin: (session: Session) => void }) {
  const [username, setUsername] = useState(''), [password, setPassword] = useState(''), [showLogin, setShowLogin] = useState(false)
  const providers = useQuery({ queryKey: ['auth-providers'], queryFn: () => api<{ items: { id: string; name: string; start_url: string }[] }>('/auth/providers'), retry: false })
  const demo = useMutation({ mutationFn: () => post<Session>('/auth/demo'), onSuccess: onLogin })
  const login = useMutation({ gcTime: 0, mutationFn: () => post<Session>('/auth/login', { username, password }), onSuccess: session => { setPassword(''); onLogin(session) } })
  return <div className="login-page">
    <DataBackdrop/>
    <header className="login-brand"><Brand/><span>CONTROL DE DATOS, CON EVIDENCIA.</span></header>
    <main className="login-content" id="login-content">
      <section className="login-story" aria-labelledby="welcome-heading">
        <div className="eyebrow"><span/>TRACKVANCE CORE · 0.9.8</div>
        <h1 id="welcome-heading">Confianza en cada dato.<br/><em>Control en cada decisión.</em></h1>
        <p>Una sola plataforma para validar tus archivos, conciliar la información y gestionar las diferencias hasta su resolución.</p>
        <div className="login-features">
          <div><FileCheck2/><span><strong>Calidad desde el origen</strong><small>Datos consistentes, listos para operar.</small></span></div>
          <div><GitCompareArrows/><span><strong>Conciliación trazable</strong><small>Diferencias visibles, con contexto y auditoría.</small></span></div>
          <div><ShieldCheck/><span><strong>Monitoreo con evidencia</strong><small>Seguimiento completo, con registros verificables.</small></span></div>
        </div>
        <div className="login-visual" aria-label="Datos, control y confianza">
          <div className="flow-node"><Database/><span>Datos</span><small>Carga y validación</small></div>
          <div className="flow-line"/>
          <div className="flow-node active"><Check/><span>Control</span><small>Conciliación y análisis</small></div>
          <div className="flow-line"/>
          <div className="flow-node"><ShieldCheck/><span>Confianza</span><small>Decisiones con evidencia</small></div>
        </div>
      </section>
      <section className="login-card" aria-labelledby="access-heading">
        <div className="session-icon"><ShieldCheck size={28}/></div>
        <span className="access-kicker">TU ESPACIO DE TRABAJO</span>
        <h2 id="access-heading">Tu espacio de control</h2>
        <p>Explora el prototipo con un escenario de facturación y pagos listo para ejecutar.</p>
        <div className="demo-details"><Monitor size={24}/><div><strong>Entorno de demostración local</strong><span>Datos sintéticos identificados. Las cargas, ejecuciones y cambios se guardan en este equipo.</span></div></div>
        {demo.error && <ErrorState error={demo.error}/>}
        <button className="button primary full" disabled={demo.isPending} onClick={() => demo.mutate()}>{demo.isPending ? 'Preparando tu espacio…' : 'Entrar al entorno demo'}<ArrowRight size={20}/></button>
        <button className="text-button full account-toggle" aria-expanded={showLogin} aria-controls="local-login" onClick={() => setShowLogin(!showLogin)}>{showLogin ? 'Ocultar acceso con cuenta' : 'Tengo una cuenta local'}</button>
        {showLogin && <form className="login-form" id="local-login" onSubmit={e => { e.preventDefault(); login.mutate() }}>
          <Field label="Usuario o correo"><input required value={username} onChange={e => setUsername(e.target.value)} autoComplete="username"/></Field>
          <Field label="Contraseña"><input type="password" required value={password} onChange={e => setPassword(e.target.value)} autoComplete="current-password"/></Field>
          {login.error && <ErrorState error={login.error}/>}
          <button className="button secondary full" disabled={login.isPending}>Iniciar sesión</button>
        </form>}
        <div className="login-providers">{providers.data?.items.filter(provider => ['microsoft', 'google'].includes(provider.id) && provider.start_url.startsWith('/api/v1/auth/sso/')).map(provider => <a className="button secondary full" key={provider.id} href={provider.start_url}>Continuar con {provider.name}</a>)}</div>
        {new URLSearchParams(window.location.search).has('sso_error') && <p role="alert">No se pudo iniciar sesión con el proveedor. Verifica que tu cuenta esté habilitada en Trackvance.</p>}
        <div className="login-foot"><span className="status-dot"/>Procesamiento y almacenamiento local</div>
      </section>
    </main>
    <footer>© {new Date().getFullYear()} Trackvance Colombia SAS <span>Intake <i/> ReconOps <i/> Sentinel</span></footer>
  </div>
}
