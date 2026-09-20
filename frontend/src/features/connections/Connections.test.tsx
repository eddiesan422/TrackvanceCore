import { act, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { Route, Routes } from 'react-router-dom'
import { api, post } from '../../api/client'
import { renderApp } from '../../test/render'
import { ConnectionDetail, ConnectionDialog, ConnectionsPage } from './Connections'
import type { ConnectionRecord } from './Connections'
import { SourceRefresh } from './SourceRefresh'

vi.mock('../../api/client', async importOriginal => ({ ...await importOriginal<typeof import('../../api/client')>(), api: vi.fn(), post: vi.fn() }))

const permissions = ['connections:read', 'connections:manage', 'connections:use', 'datasets:write']
const connection: ConnectionRecord = { id: 'source-db', name: 'Ventas', source_type: 'POSTGRESQL', host: 'db.local', port: 5432, database: 'ventas', username: 'reader', options: { connect_timeout: 5, query_timeout: 30, sslmode: 'require' }, enabled: true, version: 3, last_test_status: 'SUCCESS', last_test_at: '2026-09-19T12:00:00Z', last_test_message: 'Conexión verificada correctamente.' }
const tested = { status: 'SUCCESS', message: 'Acceso verificado.', tested_at: '2026-09-19T13:00:00Z' }

beforeEach(() => vi.resetAllMocks())

function renderDetail(granted = permissions) {
  return renderApp(<ConnectionDetail/>, { permissions: granted, path: '/connections/source-db', route: '/connections/:id' })
}

function mockSource() {
  vi.mocked(api).mockImplementation(async (path, options) => {
    if (path === '/connections/source-db') return connection
    if (path === '/connections/source-db?version=3' && options?.method === 'DELETE') return { ok: true }
    if (path.endsWith('/schemas')) return { items: ['comercial'], total: 1 }
    if (path.includes('/objects?')) return { items: [{ name: 'ventas_vista', kind: 'VIEW' }], total: 1 }
    if (path.includes('/preview?')) return { columns: [
      { name: 'document_id', native_type: 'varchar(20)', logical_type: 'STRING', nullable: false, numeric: false },
      { name: 'amount', native_type: 'numeric(12,2)', logical_type: 'DECIMAL', nullable: true, numeric: true },
    ], rows: [{ document_id: '001234567', amount: null }], sampled_rows: 1 }
    throw new Error(`Unexpected test request: ${path}`)
  })
}

describe('Connections management', () => {
  it('requires a successful test of the current configuration before saving', async () => {
    vi.mocked(post).mockResolvedValue(tested)
    const saved = vi.fn(), user = userEvent.setup()
    renderApp(<ConnectionDialog close={vi.fn()} saved={saved}/>, { permissions })
    expect(screen.getByRole('button', { name: 'Probar conexión' })).toBeDisabled()
    await user.type(screen.getByLabelText('Nombre de conexión'), 'Ventas')
    await user.type(screen.getByLabelText('Host'), 'db.local')
    await user.type(screen.getByLabelText('Base de datos'), 'ventas')
    await user.type(screen.getByLabelText('Usuario'), 'reader')
    await user.type(screen.getByLabelText('Contraseña'), 'private-credential')
    expect(screen.getByLabelText('Contraseña')).toHaveAttribute('type', 'password')
    expect(screen.getByRole('button', { name: 'Guardar conexión' })).toBeDisabled()
    await user.click(screen.getByRole('button', { name: 'Probar conexión' }))
    expect(await screen.findByText('Conexión verificada')).toBeInTheDocument()
    expect(post).toHaveBeenCalledWith('/connections/test', expect.objectContaining({ source_type: 'POSTGRESQL', password: 'private-credential', options: { connect_timeout: 5, query_timeout: 30, sslmode: 'require' } }))
    expect(screen.getByRole('button', { name: 'Guardar conexión' })).toBeEnabled()
    await user.type(screen.getByLabelText('Host'), '2')
    expect(screen.getByRole('button', { name: 'Guardar conexión' })).toBeDisabled()
    expect(screen.queryByText('Conexión verificada')).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Probar conexión' }))
    await screen.findByText('Conexión verificada')
    vi.mocked(post).mockResolvedValueOnce(connection)
    await user.click(screen.getByRole('button', { name: 'Guardar conexión' }))
    await waitFor(() => expect(saved).toHaveBeenCalled())
    expect(post).toHaveBeenCalledWith('/connections', expect.objectContaining({ name: 'Ventas', host: 'db.local2', source_type: 'POSTGRESQL' }))
  })

  it('uses SQL Server defaults and exposes a failed test without allowing save', async () => {
    vi.mocked(post).mockRejectedValue(new Error('No se pudo conectar. Revisa las credenciales.'))
    const user = userEvent.setup()
    renderApp(<ConnectionDialog close={vi.fn()} saved={vi.fn()}/>, { permissions })
    await user.selectOptions(screen.getByLabelText('Tipo de conexión'), 'SQLSERVER')
    expect(screen.getByLabelText('Puerto')).toHaveValue(1433)
    await user.type(screen.getByLabelText('Nombre de conexión'), 'SQL Ventas')
    await user.type(screen.getByLabelText('Host'), 'sql.local')
    await user.type(screen.getByLabelText('Base de datos'), 'ventas')
    await user.type(screen.getByLabelText('Usuario'), 'reader')
    await user.type(screen.getByLabelText('Contraseña'), 'invalid-credential')
    await user.click(screen.getByRole('button', { name: 'Probar conexión' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Revisa las credenciales')
    expect(post).toHaveBeenCalledWith('/connections/test', expect.objectContaining({ source_type: 'SQLSERVER', port: 1433, options: { connect_timeout: 5, query_timeout: 30, encryption: 'require' } }))
    expect(screen.getByRole('button', { name: 'Guardar conexión' })).toBeDisabled()
  })

  it('preserves exact database identifiers and credentials when testing and saving', async () => {
    vi.mocked(post).mockResolvedValueOnce(tested).mockResolvedValueOnce(connection)
    const saved = vi.fn(), user = userEvent.setup()
    renderApp(<ConnectionDialog close={vi.fn()} saved={saved}/>, { permissions })
    await user.type(screen.getByLabelText('Nombre de conexión'), ' Ventas ')
    await user.type(screen.getByLabelText('Host'), ' db.local ')
    await user.type(screen.getByLabelText('Base de datos'), ' ventas ')
    await user.type(screen.getByLabelText('Usuario'), ' lector ')
    await user.type(screen.getByLabelText('Contraseña'), ' credential with spaces ')
    await user.click(screen.getByRole('button', { name: 'Probar conexión' }))
    await screen.findByText('Conexión verificada')
    await user.click(screen.getByRole('button', { name: 'Guardar conexión' }))
    await waitFor(() => expect(saved).toHaveBeenCalled())
    const exactInput = expect.objectContaining({ name: 'Ventas', host: 'db.local', database: ' ventas ', username: ' lector ', password: ' credential with spaces ' })
    expect(post).toHaveBeenCalledWith('/connections/test', exactInput)
    expect(post).toHaveBeenCalledWith('/connections', exactInput)
  })

  it('ignores an old successful test when configuration changes while testing', async () => {
    let resolveTest!: (value: unknown) => void
    vi.mocked(post).mockImplementation(() => new Promise(resolve => { resolveTest = resolve }))
    const user = userEvent.setup()
    renderApp(<ConnectionDialog connection={connection} close={vi.fn()} saved={vi.fn()}/>, { permissions })
    await user.click(screen.getByRole('button', { name: 'Probar conexión' }))
    expect(screen.getByRole('button', { name: 'Probando conexión…' })).toBeDisabled()
    await user.type(screen.getByLabelText('Host'), '2')
    await act(async () => { resolveTest(tested) })
    expect(screen.getByRole('button', { name: 'Guardar conexión' })).toBeDisabled()
    expect(screen.queryByText('Conexión verificada')).not.toBeInTheDocument()
  })

  it('edits with optimistic version and preserves a blank password without sending immutable type', async () => {
    vi.mocked(post).mockResolvedValue(tested)
    vi.mocked(api).mockResolvedValue({ ...connection, version: 4 })
    const saved = vi.fn(), user = userEvent.setup()
    renderApp(<ConnectionDialog connection={connection} close={vi.fn()} saved={saved}/>, { permissions })
    expect(screen.getByLabelText('Tipo de conexión')).toBeDisabled()
    expect(screen.getByLabelText('Contraseña')).toHaveValue('')
    await user.click(screen.getByRole('button', { name: 'Probar conexión' }))
    await screen.findByText('Conexión verificada')
    expect(post).toHaveBeenCalledWith('/connections/test', expect.objectContaining({ connection_id: 'source-db' }))
    expect(vi.mocked(post).mock.calls[0][1]).not.toHaveProperty('password')
    await user.click(screen.getByRole('button', { name: 'Guardar conexión' }))
    await waitFor(() => expect(saved).toHaveBeenCalled())
    const [path, options] = vi.mocked(api).mock.calls[0]
    expect(path).toBe('/connections/source-db')
    expect(options?.method).toBe('PATCH')
    const payload = JSON.parse(String(options?.body))
    expect(payload.version).toBe(3)
    expect(payload).not.toHaveProperty('password')
    expect(payload).not.toHaveProperty('source_type')
  })

  it('requires the password again when an authentication endpoint field changes', async () => {
    const user = userEvent.setup()
    renderApp(<ConnectionDialog connection={connection} close={vi.fn()} saved={vi.fn()}/>, { permissions })
    const password = screen.getByLabelText('Contraseña')
    const testButton = screen.getByRole('button', { name: 'Probar conexión' })

    expect(password).not.toBeRequired()
    expect(testButton).toBeEnabled()
    expect(screen.getByText(/Puedes dejarla vacía mientras conserves host, puerto/)).toBeInTheDocument()
    await user.clear(screen.getByLabelText('Nombre de conexión'))
    await user.type(screen.getByLabelText('Nombre de conexión'), 'Ventas principal')
    await user.clear(screen.getByLabelText('Tiempo máximo de consulta (segundos)'))
    await user.type(screen.getByLabelText('Tiempo máximo de consulta (segundos)'), '45')
    expect(password).not.toBeRequired()
    expect(testButton).toBeEnabled()

    await user.type(screen.getByLabelText('Host'), '2')
    expect(password).toBeRequired()
    expect(testButton).toBeDisabled()
    expect(screen.getByText(/Indícala nuevamente al cambiar host/)).toBeInTheDocument()
    await user.type(password, 'replacement-password')
    expect(testButton).toBeEnabled()
  })

  it('shows loading, retry and saved connection navigation', async () => {
    let rejectList!: (value: unknown) => void
    vi.mocked(api).mockImplementationOnce(() => new Promise((_, reject) => { rejectList = reject }))
    const user = userEvent.setup()
    renderApp(<ConnectionsPage/>, { permissions })
    expect(screen.getByText('Cargando conexiones…')).toBeInTheDocument()
    await act(async () => { rejectList(new Error('Servicio temporalmente no disponible.')) })
    expect(await screen.findByRole('alert')).toHaveTextContent('Servicio temporalmente no disponible')
    vi.mocked(api).mockResolvedValue({ items: [connection], total: 1 })
    await user.click(screen.getByRole('button', { name: 'Volver a intentar' }))
    expect(await screen.findByRole('link', { name: 'Ventas' })).toHaveAttribute('href', '/connections/source-db')
    expect(screen.getByText('Conectada')).toBeInTheDocument()
    await user.type(screen.getByLabelText('Buscar conexión o base de datos…'), 'inexistente')
    expect(screen.getByText('No hay coincidencias')).toBeInTheDocument()
  })

  it('enforces read and management permissions in the UI', async () => {
    const view = renderApp(<ConnectionsPage/>, { permissions: [] })
    expect(screen.getByText('No tienes permisos para consultar las conexiones.')).toBeInTheDocument()
    expect(api).not.toHaveBeenCalled()
    view.unmount()
    vi.mocked(api).mockResolvedValue({ items: [], total: 0 })
    renderApp(<ConnectionsPage/>, { permissions: ['connections:read'] })
    await screen.findByText('Conecta tu primera fuente')
    expect(screen.getByRole('button', { name: 'Nueva conexión' })).toBeDisabled()
  })

  it('disables a connection using only the optimistic version and enabled flag', async () => {
    mockSource()
    const user = userEvent.setup()
    renderDetail()
    await user.click(await screen.findByRole('button', { name: 'Deshabilitar conexión' }))
    await user.click(screen.getByRole('button', { name: 'Confirmar cambio' }))
    await waitFor(() => expect(api).toHaveBeenCalledWith('/connections/source-db', { method: 'PATCH', body: JSON.stringify({ version: 3, enabled: false }) }))
  })

  it('refreshes the saved test status after a failed connectivity test', async () => {
    mockSource()
    vi.mocked(post).mockRejectedValue(new Error('La fuente no está disponible.'))
    const user = userEvent.setup()
    renderDetail()
    await user.click(await screen.findByRole('button', { name: 'Probar conexión' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('La fuente no está disponible')
    await waitFor(() => expect(vi.mocked(api).mock.calls.filter(([path]) => path === '/connections/source-db').length).toBeGreaterThan(1))
  })

  it('explains historical preservation before removing a connection', async () => {
    mockSource()
    const user = userEvent.setup()
    renderApp(<Routes><Route path="/connections/:id" element={<ConnectionDetail/>}/><Route path="/connections" element={<p>Listado de conexiones</p>}/></Routes>, { permissions, path: '/connections/source-db' })
    await user.click(await screen.findByRole('button', { name: 'Eliminar conexión' }))
    const dialog = screen.getByRole('dialog')
    expect(within(dialog).getByText(/configuraciones históricas, datasets y snapshots se conservarán/)).toBeInTheDocument()
    expect(vi.mocked(api).mock.calls.some(([, options]) => options?.method === 'DELETE')).toBe(false)
    await user.click(within(dialog).getByRole('button', { name: 'Confirmar eliminación' }))
    await waitFor(() => expect(api).toHaveBeenCalledWith('/connections/source-db?version=3', { method: 'DELETE' }))
    expect(await screen.findByText('Listado de conexiones')).toBeInTheDocument()
  })
})

describe('Source discovery and snapshots', () => {
  it('discovers a view, previews native types and observed values, then registers a dataset', async () => {
    mockSource()
    vi.mocked(post).mockResolvedValue({ dataset: { id: 'sales-dataset', name: 'ventas_vista' }, version: { id: 'snapshot-v1' } })
    const user = userEvent.setup()
    renderDetail()
    await screen.findByRole('option', { name: 'comercial' })
    await user.selectOptions(screen.getByLabelText('Schema'), 'comercial')
    await screen.findByRole('option', { name: 'ventas_vista · Vista' })
    await user.selectOptions(screen.getByLabelText('Tabla o vista'), 'ventas_vista')
    const columns = await screen.findByRole('table', { name: 'Columnas de la fuente' })
    expect(within(columns).getByText('numeric(12,2)')).toBeInTheDocument()
    expect(within(columns).getByText('DECIMAL')).toBeInTheDocument()
    const preview = screen.getByRole('table', { name: 'Vista previa de la fuente' })
    expect(within(preview).getByText('001234567')).toBeInTheDocument()
    expect(within(preview).getByText('null')).toBeInTheDocument()
    expect(api).toHaveBeenCalledWith('/connections/source-db/preview?schema_name=comercial&object_name=ventas_vista&limit=20')
    await user.click(screen.getByRole('button', { name: 'Crear dataset' }))
    expect(await screen.findByRole('link', { name: 'Ver dataset' })).toHaveAttribute('href', '/datasets/sales-dataset')
    expect(screen.getByRole('link', { name: /Ir a Data Intake/ })).toHaveAttribute('href', '/intake')
    expect(post).toHaveBeenCalledWith('/connections/source-db/datasets', { name: 'ventas_vista', domain: 'Operaciones', description: '', schema_name: 'comercial', object_name: 'ventas_vista' })
  })

  it('does not explore without source permission and disables actions for a disabled connection', async () => {
    mockSource()
    const view = renderDetail(['connections:read'])
    expect(await screen.findByText('No tienes permiso para explorar esta fuente.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Probar conexión' })).toBeDisabled()
    expect(api).not.toHaveBeenCalledWith('/connections/source-db/schemas')
    view.unmount()
    vi.mocked(api).mockResolvedValue({ ...connection, enabled: false })
    renderDetail()
    expect(await screen.findByText(/La conexión está deshabilitada/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Probar conexión' })).toBeDisabled()
  })

  it('provides a retry for preview failures without exposing an import form', async () => {
    mockSource()
    const baseMock = vi.mocked(api).getMockImplementation()!
    vi.mocked(api).mockImplementation((path, options) => path.includes('/preview?') ? Promise.reject(new Error('Permisos insuficientes para consultar la fuente.')) : baseMock(path, options))
    const user = userEvent.setup()
    renderDetail()
    await screen.findByRole('option', { name: 'comercial' })
    await user.selectOptions(screen.getByLabelText('Schema'), 'comercial')
    await screen.findByRole('option', { name: 'ventas_vista · Vista' })
    await user.selectOptions(screen.getByLabelText('Tabla o vista'), 'ventas_vista')
    expect(await screen.findByRole('alert')).toHaveTextContent('Permisos insuficientes')
    expect(screen.getByRole('button', { name: 'Volver a intentar' })).toBeEnabled()
    expect(screen.queryByRole('button', { name: 'Crear dataset' })).not.toBeInTheDocument()
  })

  it('creates another immutable version on source refresh and exposes failure/retry', async () => {
    vi.mocked(post).mockRejectedValueOnce(new Error('La fuente no está disponible.')).mockResolvedValueOnce({ id: 'snapshot-v2', version: 2 })
    const onRefreshed = vi.fn(), user = userEvent.setup()
    renderApp(<SourceRefresh datasetId="sales" connectionId="source-db" onRefreshed={onRefreshed}/>, { permissions })
    await user.click(screen.getByRole('button', { name: 'Nueva versión desde la fuente' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('La fuente no está disponible')
    expect(onRefreshed).not.toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: 'Nueva versión desde la fuente' }))
    expect(await screen.findByText(/Versión 2 creada desde la fuente/)).toBeInTheDocument()
    expect(onRefreshed).toHaveBeenCalledWith('snapshot-v2')
    expect(post).toHaveBeenCalledWith('/datasets/sales/refresh-source')
    expect(screen.getByRole('link', { name: 'Ver conexión de origen' })).toHaveAttribute('href', '/connections/source-db')
  })

  it('requires both source-use and dataset-write permissions for refresh', () => {
    renderApp(<SourceRefresh datasetId="sales" connectionId="source-db" onRefreshed={vi.fn()}/>, { permissions: ['connections:use'] })
    expect(screen.getByRole('button', { name: 'Nueva versión desde la fuente' })).toBeDisabled()
  })

  it.each([
    ['DISABLED', /conexión de origen está deshabilitada/, true],
    ['DELETED', /conexión de origen fue eliminada/, false],
  ] as const)('explains an unavailable %s source and keeps refresh disabled', (connectionState, message, hasLink) => {
    renderApp(<SourceRefresh datasetId="sales" connectionId="source-db" connectionState={connectionState} onRefreshed={vi.fn()}/>, { permissions })
    expect(screen.getByRole('button', { name: 'Nueva versión desde la fuente' })).toBeDisabled()
    expect(screen.getByText(message)).toHaveTextContent('snapshots existentes permanecen disponibles')
    expect(screen.queryByRole('link', { name: 'Ver conexión de origen' }) !== null).toBe(hasLink)
    expect(post).not.toHaveBeenCalled()
  })
})
