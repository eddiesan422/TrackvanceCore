import { expect, test } from '@playwright/test'

// Opt in only on the disposable stack provisioned by delivery_cycle.py.
// Delivery traffic may exercise credential-backed endpoints; never retain traces.
test.use({ trace: 'off' })
test.setTimeout(180_000)
test.skip(process.env.TV_DELIVERY_E2E !== 'true', 'Requires the isolated Data Delivery fixtures')

test('destino real → builder → preflight → publicación → receipt', async ({ page }, testInfo) => {
  const tableName = `ui_delivery_${Date.now()}`
  const pageErrors: string[] = []
  page.on('pageerror', error => pageErrors.push(error.message))

  await page.goto('/')
  await page.getByRole('button', { name: 'Entrar al entorno demo', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Centro de control', exact: true })).toBeVisible()

  await page.getByRole('link', { name: /^Data Delivery/ }).click()
  await expect(page.getByRole('heading', { name: 'Data Delivery', exact: true })).toBeVisible()
  await page.getByRole('link', { name: 'Destinos', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Destinos', exact: true })).toBeVisible()

  const destinationLink = page.getByRole('link', { name: 'Destino real POSTGRESQL', exact: true })
  await expect(destinationLink).toBeVisible()
  await destinationLink.click()
  await expect(page.getByRole('heading', { name: 'Destino real POSTGRESQL', exact: true })).toBeVisible()
  const testResponse = page.waitForResponse(response => new URL(response.url()).pathname.match(/\/api\/v1\/delivery\/destinations\/[^/]+\/test$/) && response.request().method() === 'POST')
  await page.getByRole('button', { name: 'Probar destino', exact: true }).click()
  expect((await testResponse).status()).toBe(200)
  await expect(page.getByText('Destino verificado', { exact: true })).toBeVisible()

  await page.getByRole('link', { name: 'Preparar entrega', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Nueva entrega', exact: true })).toBeVisible()
  await page.getByLabel('Nombre de la entrega', { exact: true }).fill(`Entrega UI ${Date.now()}`)
  await page.getByLabel('Dataset', { exact: true }).selectOption({ label: 'Dataset Data Delivery E2E' })
  await page.getByLabel('DatasetVersion exacta', { exact: true }).selectOption({ index: 1 })
  await expect(page.getByText('Parquet canónico disponible', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: /Continuar/ }).click()

  await expect(page.getByLabel('Destino de publicación', { exact: true })).toHaveValue(/.+/)
  await expect(page.getByText(/Destino real POSTGRESQL/).last()).toBeVisible()
  await page.getByRole('button', { name: /Continuar/ }).click()

  await page.getByRole('button', { name: /Crear tabla nueva/ }).click()
  await page.getByLabel('Schema', { exact: true }).selectOption('existing_delivery')
  await page.getByLabel('Nueva tabla', { exact: true }).fill(tableName)
  await page.getByRole('button', { name: /Continuar/ }).click()

  await expect(page.getByRole('heading', { name: 'Mapping de salida', exact: true })).toBeVisible()
  await expect(page.locator('table tbody tr')).not.toHaveCount(0)
  await page.getByRole('button', { name: /Continuar/ }).click()

  await expect(page.getByText('Crear y cargar', { exact: true }).first()).toBeVisible()
  await page.getByRole('button', { name: /Continuar/ }).click()

  const previewResponse = page.waitForResponse(response => new URL(response.url()).pathname === '/api/v1/delivery/preview' && response.request().method() === 'POST')
  await page.getByRole('button', { name: 'Generar preview', exact: true }).click()
  expect((await previewResponse).status()).toBe(200)
  await expect(page.getByRole('heading', { name: 'Preview técnico', exact: true })).toBeVisible()

  const preflightResponse = page.waitForResponse(response => new URL(response.url()).pathname === '/api/v1/delivery/preflight' && response.request().method() === 'POST')
  await page.getByRole('button', { name: 'Ejecutar preflight', exact: true }).click()
  expect((await preflightResponse).status()).toBe(200)
  await expect(page.getByRole('heading', { name: 'Preflight aprobado', exact: true })).toBeVisible()
  await page.screenshot({ path: testInfo.outputPath('delivery-preflight.png'), fullPage: true })
  await page.getByRole('button', { name: /Continuar/ }).click()

  const publishResponse = page.waitForResponse(response => new URL(response.url()).pathname === '/api/v1/delivery/configurations' && response.request().method() === 'POST')
  await page.getByRole('button', { name: 'Publicar configuración', exact: true }).click()
  const published = await publishResponse
  expect(published.status()).toBe(201)
  const configuration = await published.json()
  expect(configuration.module).toBe('DELIVERY')
  expect(configuration.config).toEqual(expect.objectContaining({
    dataset_version_id: expect.any(String),
    destination_version_id: expect.any(String),
    target: expect.objectContaining({ mode: 'CREATE_TABLE', schema_name: 'existing_delivery', table_name: tableName }),
    write_strategy: 'CREATE_AND_LOAD',
  }))
  await expect(page.getByText(/Publicada como versión 1/)).toBeVisible()

  const runResponse = page.waitForResponse(response => new URL(response.url()).pathname === '/api/v1/delivery/runs' && response.request().method() === 'POST')
  await page.getByRole('button', { name: 'Ejecutar entrega', exact: true }).click()
  const queued = await runResponse
  expect(queued.status()).toBe(202)
  const run = await queued.json()
  await page.waitForURL(new RegExp(`/runs/${run.id}$`))
  await expect(page.getByRole('heading', { name: /Entrega UI/ })).toBeVisible()
  await expect(page.getByText('Confirmado', { exact: true }).first()).toBeVisible({ timeout: 90_000 })
  await expect(page.getByRole('heading', { name: 'Receipt inmutable', exact: true })).toBeVisible()
  await expect(page.getByText(`existing_delivery.${tableName}`, { exact: true }).first()).toBeVisible()
  await expect(page.getByText('Filas preparadas', { exact: true })).toBeVisible()
  await expect(page.locator('.delivery-run-metrics').getByText('Filas enviadas', { exact: true })).toBeVisible()
  await expect(page.getByText(/Filas enviadas no significa filas físicas finales/)).toBeVisible()
  // A direct reload of the generic Run URL must load the Delivery feature chunk.
  await page.reload()
  await expect(page.getByRole('heading', { name: 'Receipt inmutable', exact: true })).toBeVisible()
  await expect(page.getByText(`existing_delivery.${tableName}`, { exact: true }).first()).toBeVisible()
  await page.screenshot({ path: testInfo.outputPath('delivery-receipt.png'), fullPage: true })
  expect(pageErrors).toEqual([])
})
