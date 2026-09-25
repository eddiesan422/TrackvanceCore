import { expect, test } from '@playwright/test'
import type { Page } from '@playwright/test'

// Deterministic browser contract tests: API calls are intercepted, no SQL writes.
// Real remote transactions are certified separately by delivery_cycle.py.
async function stubApi(page: Page, state: 'UNKNOWN' | 'PENDING_REPAIR' | 'VALID', writable = true) {
  const writes: string[] = []
  const reviews: Record<string, unknown>[] = []
  let repaired = state === 'VALID'
  const unknown = state === 'UNKNOWN'
  await page.route('**/api/v1/**', async route => {
    const request = route.request(), path = new URL(request.url()).pathname.replace('/api/v1', '')
    const method = request.method()
    if (method !== 'GET') writes.push(`${method} ${path}`)
    const run = { id: 'delivery-fixture', name: 'Entrega operacional de prueba', module: 'DELIVERY', status: unknown ? 'UNKNOWN' : 'SUCCESS', decision: unknown ? 'UNKNOWN' : 'COMMITTED', dataset_name: 'Fixture', dataset_version_id: 'version-fixture', created_at: '2026-09-25T12:00:00Z', metrics: { evidence_status: repaired ? 'VALID' : state === 'PENDING_REPAIR' ? 'PENDING_REPAIR' : undefined }, findings: [] }
    let body: unknown = { items: [], total: 0 }
    let status = 200
    if (path === '/me') body = { user: { id: 'operator-fixture', name: 'Operador de prueba', role: writable ? 'Data Analyst' : 'Auditor', permissions: ['runs:read', 'datasets:read', 'connections:read', 'artifacts:download', ...(writable ? ['runs:execute', 'configurations:write'] : [])] }, organization: { id: 'fixture', name: 'Fixture aislado' }, csrf_token: 'fixture-only' }
    else if (path.startsWith('/dashboard?') || path === '/dashboard') body = { stats: {}, variations: {}, attention: [], datasets_attention: [], recent_runs: [], filter_options: {}, health_history: [], module_status: [] }
    else if (path === '/runs/delivery-fixture') body = run
    else if (path === '/delivery/runs/delivery-fixture/attempts') body = { items: [{ id: 'attempt-fixture', run_id: run.id, attempt_number: 1, status: unknown ? 'UNKNOWN' : 'COMMITTED', rows_attempted: 2, rows_written: unknown ? null : 2, rows_inserted: null, rows_updated: null, bytes_sent: unknown ? null : 18, target_locator: 'fixture.target' }], total: 1 }
    else if (path === '/delivery/runs/delivery-fixture/receipt') body = { run_id: run.id, result: 'COMMITTED', rows_written: 2, target_locator: 'fixture.target' }
    else if (path === '/delivery/runs/delivery-fixture/repair-evidence' && method === 'POST') {
      repaired = true
      body = { run_id: run.id, status: 'REPAIRED', receipt_artifact_id: 'receipt-fixture', manifest_artifact_id: 'manifest-fixture' }
    } else if (path === '/delivery/runs/delivery-fixture/reviews') {
      if (method === 'POST') {
        const input = request.postDataJSON()
        body = { id: 'review-fixture', run_id: run.id, ...input, reviewer_id: 'operator-fixture', reviewer_name: 'Operador de prueba', verified_at: '2026-09-25T12:00:00Z', created_at: '2026-09-25T12:01:00Z' }
        reviews.push(body as Record<string, unknown>)
        status = 201
      } else body = { items: reviews, total: reviews.length }
    }
    await route.fulfill({ status, json: body })
  })
  return writes
}

test('lazy navigation retains shell, deep links, reload and read-only builder permissions', async ({ page }) => {
  await stubApi(page, 'VALID', false)
  const scripts: string[] = []
  page.on('request', request => { if (request.resourceType() === 'script') scripts.push(new URL(request.url()).pathname) })
  await page.goto('/')
  await expect(page.getByRole('heading', { name: 'Centro de control', exact: true })).toBeVisible()
  expect(scripts.some(path => /Delivery(?:RunDetail)?[-.]/.test(path))).toBe(false)
  await page.getByRole('link', { name: /^Data Delivery/ }).click()
  await expect(page.getByRole('heading', { name: 'Data Delivery', exact: true })).toBeVisible()
  await page.getByRole('link', { name: 'Destinos', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Destinos', exact: true })).toBeVisible()
  await page.reload()
  await expect(page.getByRole('heading', { name: 'Destinos', exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Nuevo destino' })).toBeDisabled()
  await page.goto('/delivery/new')
  await expect(page.getByText('No tienes permiso para publicar configuraciones de entrega.')).toBeVisible()
  await page.goto('/runs/delivery-fixture')
  await expect(page.getByRole('heading', { name: 'Entrega operacional de prueba' })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Receipt inmutable' })).toBeVisible()
  await expect(page.locator('.delivery-run-metrics > div').filter({ hasText: 'Insertadas / actualizadas' }).locator('strong')).toHaveText('N/D / N/D')
})

test('PENDING_REPAIR repairs evidence through the explicit action without replay', async ({ page }) => {
  const writes = await stubApi(page, 'PENDING_REPAIR')
  await page.goto('/runs/delivery-fixture')
  await expect(page.getByText('Entrega confirmada. La evidencia local está pendiente de reparación.')).toBeVisible()
  await expect(page.getByRole('button', { name: 'Receipt', exact: true })).toBeDisabled()
  await page.getByRole('button', { name: 'Reparar evidencia', exact: true }).click()
  await expect(page.getByText(/Evidencia local reparada/)).toBeVisible()
  await expect(page.getByRole('button', { name: 'Receipt', exact: true })).toBeEnabled()
  await expect(page.getByRole('button', { name: 'Reparar evidencia', exact: true })).toHaveCount(0)
  await expect(page.getByText('Confirmado', { exact: true }).first()).toBeVisible()
  expect(writes).toEqual(['POST /delivery/runs/delivery-fixture/repair-evidence'])
})

test('UNKNOWN review remains external, survives reload, and never creates a new Run', async ({ page }) => {
  const writes = await stubApi(page, 'UNKNOWN')
  await page.goto('/runs/delivery-fixture')
  await expect(page.getByText('Trackvance perdió la confirmación de la transacción. Verifica el destino antes de decidir cualquier nueva ejecución.')).toBeVisible()
  await page.getByRole('button', { name: 'Revisar resultado', exact: true }).click()
  const dialog = page.getByRole('dialog')
  await expect(dialog.getByRole('button', { name: 'Guardar revisión' })).toBeDisabled()
  await dialog.getByLabel('Resultado observado externamente').selectOption('REMOTE_COMMIT_OBSERVED')
  await dialog.getByLabel('Nota / motivo').fill('Verificación externa en el sistema destino; sin datos sensibles.')
  await dialog.getByRole('button', { name: 'Guardar revisión' }).click()
  await expect(page.getByText('REMOTE_COMMIT_OBSERVED', { exact: true })).toBeVisible()
  await page.reload()
  await expect(page.getByText('REMOTE_COMMIT_OBSERVED', { exact: true })).toBeVisible()
  await expect(page.getByText('Confirmación remota desconocida', { exact: true })).toBeVisible()
  await expect(page.getByText('Confirmación desconocida', { exact: true }).first()).toBeVisible()
  await expect(page.getByRole('button', { name: /Reintentar|Ejecutar entrega|Reparar evidencia/ })).toHaveCount(0)
  expect(writes).toEqual(['POST /delivery/runs/delivery-fixture/reviews'])
})

for (const state of ['UNKNOWN', 'PENDING_REPAIR'] as const) {
  test(`read-only ${state} exposes status without operational mutations`, async ({ page }) => {
    const writes = await stubApi(page, state, false)
    await page.goto('/runs/delivery-fixture')
    await expect(page.getByRole('heading', { name: 'Entrega operacional de prueba' })).toBeVisible()
    await expect(page.getByText(state === 'UNKNOWN' ? 'Confirmación remota desconocida' : 'Entrega confirmada. La evidencia local está pendiente de reparación.', { exact: true })).toBeVisible()
    await expect(page.getByRole('button', { name: /Revisar resultado|Reparar evidencia/ })).toHaveCount(0)
    expect(writes).toEqual([])
  })
}
