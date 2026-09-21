import { test, expect, type Page } from '@playwright/test'

test.setTimeout(150_000)

async function upload(page: Page, datasetId: string, csv: string, headers: Record<string, string>) {
  const response = await page.request.post(`/api/v1/datasets/${datasetId}/versions/upload`, {
    headers, multipart: { file: { name: 'schedule.csv', mimeType: 'text/csv', buffer: Buffer.from(csv) } },
  })
  expect(response.status(), await response.text()).toBe(201)
  return response.json()
}

test('Sentinel: programación real → snapshot nuevo → anomalía → alerta → histórico', async ({ page }, testInfo) => {
  await page.goto('/')
  await page.getByRole('button', { name: 'Entrar al entorno demo', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Centro de control', exact: true })).toBeVisible()
  const session = await (await page.request.get('/api/v1/me')).json()
  const headers = { 'X-CSRF-Token': session.csrf_token as string }
  const name = `E2E Sentinel programado ${Date.now()}`
  const datasetResponse = await page.request.post('/api/v1/datasets', { headers, data: { name } })
  expect(datasetResponse.status()).toBe(201)
  const dataset = await datasetResponse.json()
  const firstVersion = await upload(page, dataset.id, 'id,email\n1,one@example.test\n2,two@example.test\n', headers)
  await page.goto('/sentinel')
  await page.getByRole('button', { name: 'Nuevo monitor', exact: true }).click()
  const dialog = page.getByRole('dialog')
  await dialog.getByLabel('Nombre del monitor', { exact: true }).fill(name)
  await dialog.getByLabel('Dataset', { exact: true }).selectOption(dataset.id)
  await dialog.getByRole('button', { name: 'Columnas requeridas: abrir selector', exact: true }).click()
  const requiredColumns = dialog.getByRole('group', { name: 'Opciones de Columnas requeridas', exact: true })
  await requiredColumns.getByRole('checkbox', { name: /^id / }).check()
  await requiredColumns.getByRole('checkbox', { name: /^email / }).check()
  await dialog.getByRole('button', { name: 'Columnas a revisar por nulos: abrir selector', exact: true }).click()
  await dialog.getByRole('group', { name: 'Opciones de Columnas a revisar por nulos', exact: true }).getByRole('checkbox', { name: /^email / }).check()
  await dialog.getByLabel('Porcentaje máximo de nulos (%)', { exact: true }).fill('0')
  const published = page.waitForResponse(response => new URL(response.url()).pathname === '/api/v1/monitors' && response.request().method() === 'POST')
  await dialog.getByRole('button', { name: 'Crear monitor', exact: true }).click()
  const monitorResponse = await published
  expect(monitorResponse.status()).toBe(201)
  const monitor = await monitorResponse.json()
  const card = page.locator('.config-card').filter({ has: page.getByRole('heading', { name, exact: true }) })
  await card.getByText('Programación e histórico', { exact: true }).click()
  await card.getByLabel('Periodicidad (minutos)', { exact: true }).fill('1')
  await card.getByRole('button', { name: 'Guardar programación', exact: true }).click()
  await expect.poll(async () => {
    const response = await page.request.get(`/api/v1/monitors/${monitor.id}/occurrences`)
    return (await response.json()).items[0]?.decision
  }, { timeout: 45_000 }).toBe('HEALTHY')
  const first = (await (await page.request.get(`/api/v1/monitors/${monitor.id}/occurrences`)).json()).items[0]
  expect(first.dataset_version_id).toBe(firstVersion.id)
  expect(first.planned_at && first.started_at && first.finished_at).toBeTruthy()
  await expect(card.getByText(/Revisión 1/)).toBeVisible()
  await card.getByLabel('Programación activa', { exact: true }).uncheck()
  await card.getByRole('button', { name: 'Guardar programación', exact: true }).click()
  await expect(card.getByText(/Programación pausada/)).toBeVisible()
  const secondVersion = await upload(page, dataset.id, 'id,email\n1,one@example.test\n2,\n', headers)
  await card.getByLabel('Programación activa', { exact: true }).check()
  await card.getByRole('button', { name: 'Guardar programación', exact: true }).click()
  await expect.poll(async () => {
    const response = await page.request.get(`/api/v1/monitors/${monitor.id}/occurrences`)
    return (await response.json()).items[0]?.decision
  }, { timeout: 45_000 }).toBe('ALERT')
  const second = (await (await page.request.get(`/api/v1/monitors/${monitor.id}/occurrences`)).json()).items[0]
  expect(second.dataset_version_id).toBe(secondVersion.id)
  expect(second.run_id).not.toBe(first.run_id)
  const evidence = await (await page.request.get(`/api/v1/runs/${second.run_id}/evidence`)).json()
  expect(evidence.initiated_by).toEqual(expect.objectContaining({ type: 'SYSTEM', id: 'trackvance:local-scheduler' }))
  expect(evidence.processing.schedule).toEqual(expect.objectContaining({ planned_at: second.planned_at, source_policy: 'LATEST_REGISTERED_SNAPSHOT', schedule_version: 3 }))
  await card.getByRole('button', { name: 'Actualizar', exact: true }).click()
  await expect(card.getByRole('img').first()).toBeVisible()
  await expect(card.locator('.schedule-alerts li').first()).toBeVisible()
  await page.screenshot({ path: testInfo.outputPath('sentinel-scheduler-history.png'), fullPage: true })
  // Stop the disposable test schedule; runs and evidence stay immutable.
  await expect(card.getByText(/Revisión 3/)).toBeVisible()
  await card.getByLabel('Programación activa', { exact: true }).uncheck()
  await card.getByRole('button', { name: 'Guardar programación', exact: true }).click()
  await expect(card.getByText(/Programación pausada/)).toBeVisible()
  await page.goto('/')
  await expect(page.getByRole('heading', { name: 'Centro de control', exact: true })).toBeVisible()
  const dashboard = await (await page.request.get('/api/v1/dashboard?module=sentinel&period=all')).json()
  expect(dashboard.attention_total).toBeGreaterThan(0)
  expect(dashboard.module_status.find((item: { module: string }) => item.module === 'sentinel').alerts).toBeGreaterThan(0)
})
