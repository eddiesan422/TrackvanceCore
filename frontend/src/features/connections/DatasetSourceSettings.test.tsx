import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, expect, it, vi } from 'vitest'
import { api } from '../../api/client'
import { renderApp } from '../../test/render'
import { DatasetSourceSettings } from './DatasetSourceSettings'

vi.mock('../../api/client', async original => ({ ...await original<typeof import('../../api/client')>(), api: vi.fn() }))
const permissions = ['datasets:read', 'datasets:write', 'connections:read', 'connections:use']
const settings = { connection_id: 'rest', schema_name: 'REST', object_name: 'collection', column_overrides: {}, responsible_user_id: 'stable-user-id', interval_seconds: 30, timezone: 'America/Bogota', enabled: true, publication_policy: 'EVERY_CUT', empty_policy: 'BLOCK' }
const state = { dataset_id: 'dataset', version: 1, settings, binding: settings, enabled: true, next_run_at: '2026-10-18T12:00:30Z', last_attempt_at: null, last_query_success_at: null, last_publication_at: null,
  health: { state: 'SCHEDULER_DEGRADED', message: 'El proceso está activo sin un ciclo de despacho sano comprobado.', action: 'Revisa el diagnóstico del scheduler y su último ciclo.', delay_seconds: 30, last_activity_at: null }, scheduler: { status: 'RUNNING', execution_status: 'UNKNOWN', last_cycle_at: null, last_successful_dispatch_at: null }, worker: { status: 'OFFLINE' } }

beforeEach(() => {
  vi.resetAllMocks()
  vi.mocked(api).mockImplementation(async (path, options) => {
    if (path === '/connections') return { items: [{ id: 'rest', name: 'Facturas REST', source_type: 'REST', version: 1, options: { url: 'https://example.test/api/invoices?api_key=hidden', collection: 'payload.invoices' } }, { id: 'sql', name: 'Ventas SQL', source_type: 'POSTGRESQL', version: 1, options: {} }] }
    if (path === '/datasets/dataset/source-settings') return options?.method === 'PUT' ? { ...state, settings: JSON.parse(String(options.body)), version: 2 } : state
    if (path.startsWith('/datasets/dataset/acquisition-occurrences')) return { items: [{ id: 'cut', planned_at: '2026-10-18T12:00:00Z', status: 'BLOCKED', acquisition_id: null, reason_message: 'El responsable no está habilitado.', reason_code: 'ACQUISITION_RESPONSIBLE_UNAVAILABLE', coalesced_intervals: 2 }], total: 1 }
    if (path === '/connections/sql/schemas') return { items: ['sales'] }
    if (path.includes('/connections/sql/objects?')) return { items: [{ name: 'invoices', kind: 'VIEW' }] }
    throw new Error(`Unexpected request ${path}`)
  })
})

it('shows REST endpoint and collection without exposing query credentials, and distinguishes liveness from dispatch', async () => {
  renderApp(<DatasetSourceSettings datasetId="dataset" onRefreshed={vi.fn()}/>, { permissions })
  expect(await screen.findByText('Fuente REST · Facturas REST')).toBeInTheDocument()
  expect(screen.getByText('Endpoint: https://example.test/api/invoices')).toBeInTheDocument()
  expect(screen.getByText('Colección JSON: payload.invoices')).toBeInTheDocument()
  expect(screen.queryByText(/api_key=hidden/)).not.toBeInTheDocument()
  expect(screen.getByText('El proceso está activo sin un ciclo de despacho sano comprobado.')).toBeInTheDocument()
  expect(await screen.findByText('Bloqueado antes de consultar')).toBeInTheDocument()
  expect(screen.getByText('El responsable no está habilitado.')).toBeInTheDocument()
})

it('saves 30 seconds exactly and confirms effective interval, timezone and next cut', async () => {
  const user = userEvent.setup()
  renderApp(<DatasetSourceSettings datasetId="dataset" onRefreshed={vi.fn()}/>, { permissions })
  const frequency = await screen.findByLabelText('Frecuencia')
  expect(frequency).toHaveValue(30)
  expect(frequency).toHaveAttribute('min', '30')
  await user.selectOptions(screen.getByLabelText('Unidad de frecuencia'), '60')
  expect(frequency).toHaveValue(0.5)
  await user.click(screen.getByRole('button', { name: 'Guardar origen y actualización' }))
  await waitFor(() => expect(screen.getByText(/Guardado: intervalo efectivo de 30 segundos · zona America\/Bogota/)).toBeInTheDocument())
  const request = vi.mocked(api).mock.calls.find(([, options]) => options?.method === 'PUT')
  expect(JSON.parse(String(request?.[1]?.body))).toMatchObject({ interval_seconds: 30, timezone: 'America/Bogota', expected_version: 1, schema_name: 'REST', object_name: 'collection' })
})

it('assists SQL schema/object selection and preserves advanced identifiers', async () => {
  const user = userEvent.setup()
  renderApp(<DatasetSourceSettings datasetId="dataset" onRefreshed={vi.fn()}/>, { permissions })
  await screen.findByText('Fuente REST · Facturas REST')
  await user.selectOptions(screen.getByLabelText('Conexión'), 'sql')
  await screen.findByRole('option', { name: 'sales' })
  await user.selectOptions(screen.getByLabelText('Esquema'), 'sales')
  await screen.findByRole('option', { name: 'invoices · Vista' })
  await user.selectOptions(screen.getByLabelText('Tabla o vista'), 'invoices')
  await user.click(screen.getByRole('checkbox', { name: 'Opciones avanzadas de selección' }))
  expect(screen.getByLabelText('Espacio de datos guardado')).toHaveValue('sales')
  expect(screen.getByLabelText('Objeto guardado')).toHaveValue('invoices')
  await user.click(screen.getByRole('button', { name: 'Guardar origen y actualización' }))
  await waitFor(() => expect(vi.mocked(api).mock.calls.some(([, options]) => options?.method === 'PUT')).toBe(true))
  const request = vi.mocked(api).mock.calls.find(([, options]) => options?.method === 'PUT')
  expect(JSON.parse(String(request?.[1]?.body))).toMatchObject({ connection_id: 'sql', schema_name: 'sales', object_name: 'invoices', interval_seconds: 30 })
})
