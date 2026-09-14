import { test, expect, type Locator, type Page } from '@playwright/test'

test.setTimeout(120_000)

async function signIn(page: Page) {
  await page.goto('/')
  await page.getByRole('button', { name: 'Entrar al entorno demo', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Centro de control', exact: true })).toBeVisible()
  const response = await page.request.get('/api/v1/me')
  expect(response.status()).toBe(200)
  const session = await response.json()
  return { 'X-CSRF-Token': session.csrf_token as string }
}

async function uploadVersion(page: Page, datasetId: string, csv: string, headers: Record<string, string>) {
  const response = await page.request.post(`/api/v1/datasets/${datasetId}/versions/upload`, {
    headers,
    multipart: { file: { name: 'builder-fixture.csv', mimeType: 'text/csv', buffer: Buffer.from(csv) } },
  })
  expect(response.status(), await response.text()).toBe(201)
  return response.json()
}

// Fixture creation uses the API; every control publication below is submitted through its real form.
async function dataset(page: Page, name: string, csv: string, headers: Record<string, string>) {
  const response = await page.request.post('/api/v1/datasets', { headers, data: { name } })
  expect(response.status(), await response.text()).toBe(201)
  const record = await response.json()
  await uploadVersion(page, record.id, csv, headers)
  return record.id as string
}

async function publish(page: Page, dialog: Locator, endpoint: string, button: string) {
  const responsePromise = page.waitForResponse(response => new URL(response.url()).pathname === `/api/v1${endpoint}` && response.request().method() === 'POST')
  await dialog.getByRole('button', { name: button, exact: true }).click()
  const response = await responsePromise
  expect(response.status(), await response.text()).toBe(201)
  const created = await response.json()
  await expect(dialog).not.toBeVisible()
  // Confirm publication persisted independently of the successful POST response.
  const collection = await page.request.get(`/api/v1${endpoint}`)
  expect(collection.status()).toBe(200)
  const stored = (await collection.json()).items.find((item: { id: string }) => item.id === created.id)
  expect(stored).toBeTruthy()
  expect(stored.config).toEqual(created.config)
  return stored
}

async function execute(page: Page, configId: string, action: string) {
  await page.getByRole('button', { name: 'Nueva ejecución', exact: true }).click()
  const dialog = page.getByRole('dialog')
  await dialog.locator('select').first().selectOption(configId)
  await dialog.getByRole('button', { name: action, exact: true }).click()
  await page.waitForURL(/\/runs\/[^/]+$/)
  await expect(page.getByText('Completada', { exact: true }).first()).toBeVisible({ timeout: 45_000 })
  const response = await page.request.get(`/api/v1/runs/${new URL(page.url()).pathname.split('/').at(-1)}`)
  expect(response.status()).toBe(200)
  return response.json()
}

test('el formulario Intake publica y ejecuta una regla de fecha no futura', async ({ page }, testInfo) => {
  const headers = await signIn(page), name = `E2E formulario Intake ${Date.now()}`
  const datasetId = await dataset(page, name, 'order_id,transaction_date\nA-01,2020-01-01\nA-02,2999-01-01\n', headers)
  await page.goto('/intake')
  await page.getByRole('button', { name: 'Nuevo contrato', exact: true }).click()
  const dialog = page.getByRole('dialog')
  await dialog.getByLabel('Nombre del contrato', { exact: true }).fill(name)
  await dialog.getByLabel('Dataset', { exact: true }).selectOption(datasetId)
  await dialog.getByLabel('Columnas obligatorias', { exact: true }).fill('order_id, transaction_date')
  await dialog.getByLabel('Porcentaje máximo de filas con error (%)', { exact: true }).fill('0')
  await dialog.getByRole('button', { name: 'Agregar regla', exact: true }).click()
  await dialog.getByLabel('Tipo de regla', { exact: true }).selectOption('date_rule')
  await dialog.getByLabel('Columna de la regla', { exact: true }).fill('transaction_date')
  await dialog.getByLabel('Fechas futuras', { exact: true }).selectOption('REJECT')
  const contract = await publish(page, dialog, '/intake/contracts', 'Crear contrato')
  expect(contract.config.schema_version).toBe(2)
  expect(contract.config.rules).toEqual([expect.objectContaining({ type: 'date_rule', column: 'transaction_date', parameters: expect.objectContaining({ not_future: true, timezone: 'UTC' }) })])
  const run = await execute(page, contract.id, 'Validar datos')
  expect(run.status).toBe('SUCCESS')
  expect(run.decision).toBe('REJECTED')
  expect(run.metrics.error_rows).toBe(1)
  const results = await (await page.request.get(`/api/v1/runs/${run.id}/results`)).json()
  expect(results.items).toContainEqual(expect.objectContaining({ rule_code: contract.config.rules[0].code, column: 'transaction_date', received_value: '2999-01-01' }))
  await page.screenshot({ path: testInfo.outputPath('intake-rule-builder-result.png'), fullPage: true })
})

test('el formulario Recon publica igualdad exacta con normalización declarada de claves y valores', async ({ page }, testInfo) => {
  const headers = await signIn(page), name = `E2E formulario Recon ${Date.now()}`
  const sourceId = await dataset(page, name + ' origen', 'order_id,status\n a-01 ,  Activo  \nb-02,Activo\n', headers)
  const targetId = await dataset(page, name + ' destino', 'order_id,status\nA-01,ACTIVO\nB-02,ACTIVO\n', headers)
  await page.goto('/recon')
  await page.getByRole('button', { name: 'Nuevo control', exact: true }).click()
  const dialog = page.getByRole('dialog')
  await dialog.getByLabel('Nombre del control', { exact: true }).fill(name)
  await dialog.getByLabel('Dataset de origen', { exact: true }).selectOption(sourceId)
  await dialog.getByLabel('Dataset de destino', { exact: true }).selectOption(targetId)
  await dialog.getByLabel('Columnas clave', { exact: true }).fill('order_id')
  await dialog.getByLabel('Claves: espacios externos', { exact: true }).selectOption('TRIM')
  await dialog.getByLabel('Claves: mayúsculas y minúsculas', { exact: true }).selectOption('UPPER')
  await dialog.getByLabel('Claves: Unicode', { exact: true }).selectOption('NFC')
  await dialog.getByLabel('Tipo de comparación', { exact: true }).selectOption('exact_compare')
  await dialog.getByLabel('Columna de origen', { exact: true }).fill('status')
  await dialog.getByLabel('Columna de destino', { exact: true }).fill('status')
  await dialog.getByLabel('Comparación 1: espacios externos', { exact: true }).selectOption('TRIM')
  await dialog.getByLabel('Comparación 1: mayúsculas y minúsculas', { exact: true }).selectOption('UPPER')
  await dialog.getByLabel('Comparación 1: Unicode', { exact: true }).selectOption('NFC')
  const control = await publish(page, dialog, '/recon/controls', 'Crear control')
  expect(control.config.schema_version).toBe(2)
  expect(control.config.key_normalization).toEqual({ trim: true, case: 'UPPER', unicode_normalization: 'NFC' })
  expect(control.config.comparison_rules).toEqual([expect.objectContaining({ type: 'exact_compare', source_column: 'status', target_column: 'status', parameters: expect.objectContaining({ normalization: { trim: true, case: 'UPPER', unicode_normalization: 'NFC' }, equal_nulls: false }) })])
  const run = await execute(page, control.id, 'Conciliar fuentes')
  expect(run.status).toBe('SUCCESS')
  expect(run.metrics.matched).toBe(2)
  expect(run.metrics.mismatched).toBe(0)
  await page.screenshot({ path: testInfo.outputPath('recon-rule-builder-result.png'), fullPage: true })
})

test('el formulario Sentinel publica tipo de esquema comparado con la versión anterior', async ({ page }, testInfo) => {
  const headers = await signIn(page), name = `E2E formulario Sentinel ${Date.now()}`
  const datasetId = await dataset(page, name, 'order_id,transaction_date\nA-01,2020-01-01\nA-02,2020-01-02\n', headers)
  await uploadVersion(page, datasetId, 'order_id,transaction_date\nA-01,no-es-fecha\nA-02,no-es-fecha\n', headers)
  await page.goto('/sentinel')
  await page.getByRole('button', { name: 'Nuevo monitor', exact: true }).click()
  const dialog = page.getByRole('dialog')
  await dialog.getByLabel('Nombre del monitor', { exact: true }).fill(name)
  await dialog.getByLabel('Dataset', { exact: true }).selectOption(datasetId)
  await dialog.getByRole('button', { name: 'Agregar regla', exact: true }).click()
  await dialog.getByLabel('Tipo de regla', { exact: true }).selectOption('schema_type')
  await dialog.getByLabel('Columna de la regla', { exact: true }).fill('transaction_date')
  await dialog.getByLabel('Tipo lógico esperado', { exact: true }).selectOption('PREVIOUS')
  const monitor = await publish(page, dialog, '/monitors', 'Crear monitor')
  expect(monitor.config.schema_version).toBe(2)
  expect(monitor.config.rules).toEqual([expect.objectContaining({ type: 'schema_type', column: 'transaction_date', parameters: {} })])
  const run = await execute(page, monitor.id, 'Evaluar monitor')
  expect(run.status).toBe('SUCCESS')
  expect(run.decision).toBe('ALERT')
  expect(run.metrics.checks).toContainEqual(expect.objectContaining({ code: 'SCHEMA_TYPE', status: 'FAIL' }))
  await page.screenshot({ path: testInfo.outputPath('sentinel-rule-builder-result.png'), fullPage: true })
})
