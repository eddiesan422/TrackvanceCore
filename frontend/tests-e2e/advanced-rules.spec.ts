import { test, expect, type Locator, type Page } from '@playwright/test'

test.setTimeout(120_000)

async function session(page: Page) {
  await page.goto('/')
  await page.getByRole('button', { name: 'Entrar al entorno demo', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Centro de control', exact: true })).toBeVisible()
  return { 'X-CSRF-Token': (await (await page.request.get('/api/v1/me')).json()).csrf_token as string }
}
async function dataset(page: Page, name: string, csv: string, headers: Record<string, string>) {
  const created = await page.request.post('/api/v1/datasets', { headers, data: { name } })
  expect(created.status(), await created.text()).toBe(201)
  const record = await created.json()
  const response = await page.request.post(`/api/v1/datasets/${record.id}/versions/upload`, { headers, multipart: { file: { name: 'advanced.csv', mimeType: 'text/csv', buffer: Buffer.from(csv) } } })
  expect(response.status(), await response.text()).toBe(201)
  return { ...record, version: await response.json() }
}
async function choose(dialog: Locator, label: string, columns: string[]) {
  await dialog.getByRole('button', { name: `${label}: abrir selector`, exact: true }).click()
  const group = dialog.getByRole('group', { name: `Opciones de ${label}`, exact: true })
  for (const column of columns) await group.getByRole('checkbox', { name: new RegExp(`^${column} \\(`) }).check()
  await dialog.getByRole('button', { name: `${label}: cerrar selector`, exact: true }).click()
}
async function publish(page: Page, dialog: Locator, endpoint: string, action: string) {
  const pending = page.waitForResponse(response => new URL(response.url()).pathname === `/api/v1${endpoint}` && response.request().method() === 'POST')
  await dialog.getByRole('button', { name: action, exact: true }).click()
  const response = await pending
  expect(response.status(), await response.text()).toBe(201)
  return response.json()
}
async function execute(page: Page, configId: string, action: string) {
  await page.getByRole('button', { name: 'Nueva ejecución', exact: true }).click()
  const dialog = page.getByRole('dialog')
  await dialog.locator('select').first().selectOption(configId)
  await dialog.getByRole('button', { name: action, exact: true }).click()
  await page.waitForURL(/\/runs\/[^/]+$/)
  await expect(page.getByText('Completada', { exact: true }).first()).toBeVisible({ timeout: 45_000 })
  return (await page.request.get(`/api/v1/runs/${new URL(page.url()).pathname.split('/').at(-1)}`)).json()
}

test('Intake declara referencia inmutable desde esquema y exporta evidencia', async ({ page }) => {
  const headers = await session(page), name = `E2E referencia ${Date.now()}`
  const countries = await dataset(page, name + ' países', 'code\nCO\nUS\n', headers)
  const customers = await dataset(page, name + ' clientes', 'country,department\nCO,Antioquia\nXX,West\n', headers)
  await page.goto('/intake')
  await page.getByRole('button', { name: 'Nuevo contrato', exact: true }).click()
  const dialog = page.getByRole('dialog')
  await dialog.getByLabel('Nombre del contrato', { exact: true }).fill(name)
  await dialog.getByLabel('Dataset', { exact: true }).selectOption(customers.id)
  await dialog.getByRole('button', { name: 'Agregar regla', exact: true }).click()
  await dialog.getByLabel('Tipo de regla', { exact: true }).selectOption('reference')
  await choose(dialog, 'Columnas de la regla', ['country'])
  await dialog.getByLabel('Dataset de referencia', { exact: true }).selectOption(countries.id)
  await dialog.getByLabel('DatasetVersion de referencia', { exact: true }).selectOption(countries.version.id)
  await choose(dialog, 'Columnas de referencia', ['code'])
  const contract = await publish(page, dialog, '/intake/contracts', 'Crear contrato')
  const run = await execute(page, contract.id, 'Validar datos')
  expect(run.decision).toBe('REJECTED')
  expect(run.metrics.rules[0]).toMatchObject({ evaluated_count: 2, failed_count: 1 })
  const manifest = await (await page.request.get(`/api/v1/runs/${run.id}/evidence`)).json()
  expect(manifest.references[0]).toMatchObject({ dataset_version_id: countries.version.id })
  expect(manifest.references[0].canonical_sha256).toMatch(/^[a-f0-9]{64}$/)
  const downloaded = page.waitForEvent('download')
  await page.getByRole('button', { name: 'Exportar Excel', exact: true }).click()
  expect((await downloaded).suggestedFilename()).toBe(`trackvance_intake_${run.id}.xlsx`)
})

test('Recon concilia N:1 con transformación explícita y SUM/COUNT independientes', async ({ page }) => {
  const headers = await session(page), name = `E2E agregado avanzado ${Date.now()}`
  const source = await dataset(page, name + ' pagos', 'id,amount\nA,"1,10"\nA,"2,20"\n', headers)
  const target = await dataset(page, name + ' total', 'id,amount,records\nA,3.30,2\n', headers)
  await page.goto('/recon')
  await page.getByRole('button', { name: 'Nuevo control', exact: true }).click()
  const dialog = page.getByRole('dialog')
  await dialog.getByLabel('Nombre del control', { exact: true }).fill(name)
  await dialog.getByLabel('Dataset de origen', { exact: true }).selectOption(source.id)
  await dialog.getByLabel('Dataset de destino', { exact: true }).selectOption(target.id)
  await choose(dialog, 'Columnas clave', ['id'])
  const transforms = dialog.getByRole('region', { name: 'Transformaciones de origen' })
  await transforms.getByRole('button', { name: /Agregar transformación/ }).click()
  await transforms.getByLabel('Columna de transformación', { exact: true }).selectOption('amount')
  await transforms.getByLabel('Transformación', { exact: true }).selectOption('decimal_parse')
  await transforms.getByLabel('Parámetro decimal_separator', { exact: true }).fill(',')
  await transforms.getByLabel('Parámetro thousands_separator', { exact: true }).fill('.')
  await dialog.getByLabel('Conciliación 1:N', { exact: true }).selectOption('AGGREGATE')
  await dialog.getByLabel('Fuente que se agrupa', { exact: true }).selectOption('SOURCE')
  await dialog.getByLabel('Columna a sumar', { exact: true }).selectOption('amount')
  await dialog.getByLabel('Columna del resultado agregado', { exact: true }).fill('total')
  await dialog.getByRole('button', { name: 'Agregar agregación', exact: true }).click()
  await dialog.getByLabel('Operación de agregación', { exact: true }).nth(1).selectOption('count')
  await dialog.getByLabel('Columna del resultado agregado', { exact: true }).nth(1).fill('count')
  await dialog.getByLabel('Columna de origen', { exact: true }).selectOption('total')
  await dialog.getByLabel('Columna de destino', { exact: true }).selectOption('amount')
  await dialog.getByLabel('Tolerancia absoluta', { exact: true }).fill('0')
  await dialog.getByRole('button', { name: 'Agregar comparación', exact: true }).click()
  await dialog.getByLabel('Tipo de comparación', { exact: true }).nth(1).selectOption('numeric_tolerance')
  await dialog.getByLabel('Columna de origen', { exact: true }).nth(1).selectOption('count')
  await dialog.getByLabel('Columna de destino', { exact: true }).nth(1).selectOption('records')
  const control = await publish(page, dialog, '/recon/controls', 'Crear control')
  const run = await execute(page, control.id, 'Conciliar fuentes')
  expect(run.decision).toBe('CONFORME')
  expect(run.metrics.comparisons).toHaveLength(2)
  expect(run.metrics.diagnostics.source_transforms[0].type).toBe('decimal_parse')
  await page.getByText('2 comparaciones', { exact: true }).click()
  await expect(page.getByText('count / records', { exact: true }).last()).toBeVisible()
  const rows = await (await page.request.get(`/api/v1/runs/${run.id}/results`)).json()
  expect(rows.items[0].source_rows).toEqual([2, 3])
})
