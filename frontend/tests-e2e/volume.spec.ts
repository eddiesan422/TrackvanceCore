import { expect, test, type Page } from '@playwright/test'
import { readFile, writeFile } from 'node:fs/promises'

test.use({ trace: 'off', screenshot: 'off', video: 'off', actionTimeout: 30_000, navigationTimeout: 30_000 })
test.setTimeout(5_400_000)
test.skip(process.env.TV_VOLUME_E2E !== 'true', 'Requires guarded volume_cycle.py fixtures')

async function terminal(page: Page, path: string) {
  let current: Record<string, any> = {}
  await expect.poll(async () => {
    const response = await page.request.get(`/api/v1${path}`)
    expect(response.status()).toBe(200)
    current = await response.json()
    return ['SUCCESS', 'FAILED', 'FAILED_PRECONDITION', 'CANCELLED', 'UNKNOWN'].includes(current.status)
  }, { timeout: 1_800_000, intervals: [1000] }).toBe(true)
  expect(current.status, JSON.stringify({ error: current.error, error_code: current.error_code })).toBe('SUCCESS')
  return current
}

test('1M: recepción → adquisición persistente → Spark → Delivery encadenado → inbox personal', async ({ page }, testInfo) => {
  const project = process.env.TV_VOLUME_PROJECT || ''
  const fixturePath = process.env.TV_VOLUME_FIXTURE || ''
  const fixtureMetadata = process.env.TV_VOLUME_METADATA || ''
  const destinationId = process.env.TV_VOLUME_DESTINATION || ''
  const schema = process.env.TV_VOLUME_SCHEMA || ''
  expect(project).toMatch(/^trackvance-v070-test-[a-z0-9-]+-[a-f0-9]{12}$/)
  expect(Number(new URL(String(testInfo.project.use.baseURL)).port)).toBeGreaterThanOrEqual(32000)
  expect(Number(new URL(String(testInfo.project.use.baseURL)).port)).toBeLessThanOrEqual(32999)
  expect(fixturePath).toContain(project)
  expect(destinationId).toBeTruthy()
  expect(schema).toMatch(/^volume_[a-f0-9]+$/)
  const fixture = JSON.parse(await readFile(fixtureMetadata, 'utf8'))
  expect(fixture.rows).toBe(1_000_000)
  expect(fixture.formats.CSV.actual_bytes).toBeGreaterThan(100 * 1024 * 1024)
  const stamp = Date.now(), datasetName = `UI Volume ${stamp}`, tableName = `ui_volume_${stamp}`
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))

  await page.goto('/')
  await page.getByRole('button', { name: 'Entrar al entorno demo', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Centro de control', exact: true })).toBeVisible()
  await page.goto('/datasets?upload=1')
  const upload = page.getByRole('dialog')
  await upload.getByLabel('Nombre del dataset', { exact: true }).fill(datasetName)
  const receivedResponse = page.waitForResponse(response => new URL(response.url()).pathname === '/api/v1/datasets/uploads/stage' && response.request().method() === 'POST', { timeout: 300_000 })
  await upload.getByLabel('Archivo', { exact: true }).setInputFiles(fixturePath)
  const received = await receivedResponse
  expect(received.status()).toBe(201)
  // Chromium can evict inspector response bodies for a large raw request.
  // Assert the application's parsed inspection and persisted exact byte count.
  await expect(upload.getByText(/Transferencia completa/)).toBeVisible()
  await expect(upload.getByText(/muestra de 100 registros/)).toBeVisible()
  await expect(upload.getByLabel('Delimitador', { exact: true })).toHaveValue(',')
  await expect(upload.getByLabel('Identificador record_id', { exact: true })).toBeChecked()
  const registeredResponse = page.waitForResponse(response => /\/datasets\/[^/]+\/acquisitions$/.test(new URL(response.url()).pathname) && response.request().method() === 'POST')
  await upload.getByRole('button', { name: 'Registrar adquisición', exact: true }).click()
  const registered = await registeredResponse
  expect(registered.status()).toBe(202)
  const acquisition = await registered.json()
  expect(acquisition.status).toBe('QUEUED')
  expect(acquisition.total_bytes).toBe(fixture.formats.CSV.actual_bytes)
  expect(acquisition.output_version_id).toBeNull()
  await page.waitForURL(`**/datasets/${acquisition.dataset_id}`)
  // The browser can leave and reload while the persisted worker continues.
  await page.goto('/')
  await page.goto(`/datasets/${acquisition.dataset_id}`)
  await expect(page.getByRole('heading', { name: 'Historial de adquisiciones', exact: true })).toBeVisible()
  const acquired = await terminal(page, `/acquisitions/${acquisition.id}`)
  expect(acquired.processed_rows).toBe(fixture.rows)
  expect(acquired.processed_bytes).toBe(fixture.observed_utf8_bytes)
  expect(acquired.output_version_id).toBeTruthy()
  await page.reload()
  await expect(page.getByRole('link', { name: 'Ver versión', exact: true })).toBeVisible()
  const profileResponse = await page.request.get(`/api/v1/dataset-versions/${acquired.output_version_id}/profile`)
  expect(profileResponse.status()).toBe(200)
  const profile = await profileResponse.json()
  expect(profile.profile.row_count).toBe(fixture.rows)
  expect(profile.profile.columns.find((column: { name: string }) => column.name === 'record_id').distinct_count).toBe(fixture.rows)
  expect(profile.profile.columns.find((column: { name: string }) => column.name === 'observed').null_count).toBe(fixture.expected_observed_nulls)

  await page.goto('/intake')
  await page.getByRole('button', { name: 'Nuevo contrato', exact: true }).first().click()
  const contractDialog = page.getByRole('dialog')
  await contractDialog.getByLabel('Nombre del contrato', { exact: true }).fill(`UI Volume contract ${stamp}`)
  await contractDialog.getByLabel('Dataset', { exact: true }).selectOption(acquisition.dataset_id)
  for (const [label, column] of [['Columnas obligatorias', 'record_id'], ['Columnas con valores positivos', 'amount']]) {
    await contractDialog.getByRole('button', { name: `${label}: abrir selector`, exact: true }).click()
    await contractDialog.getByRole('group', { name: `Opciones de ${label}`, exact: true }).getByRole('checkbox', { name: new RegExp(`^${column} \\(`) }).check()
    await contractDialog.getByRole('button', { name: `${label}: cerrar selector`, exact: true }).click()
  }
  await contractDialog.getByLabel('Porcentaje máximo de filas con error (%)', { exact: true }).fill('0')
  const contractResponse = page.waitForResponse(response => new URL(response.url()).pathname === '/api/v1/intake/contracts' && response.request().method() === 'POST')
  await contractDialog.getByRole('button', { name: 'Crear contrato', exact: true }).click()
  const publishedContract = await contractResponse
  expect(publishedContract.status()).toBe(201)
  const contract = await publishedContract.json()

  await page.goto('/delivery/new')
  await page.getByLabel('Nombre de la entrega', { exact: true }).fill(`UI Volume Delivery ${stamp}`)
  await page.getByLabel('Dataset', { exact: true }).selectOption(acquisition.dataset_id)
  await page.getByLabel('DatasetVersion exacta', { exact: true }).selectOption(acquired.output_version_id)
  await page.getByRole('button', { name: 'Continuar', exact: true }).click()
  await page.getByLabel('Destino de publicación', { exact: true }).selectOption(destinationId)
  await page.getByRole('button', { name: 'Continuar', exact: true }).click()
  await page.getByRole('button', { name: /^Crear tabla nueva/ }).click()
  await page.getByLabel('Schema', { exact: true }).selectOption(schema)
  await page.getByLabel('Nueva tabla', { exact: true }).fill(tableName)
  await page.getByRole('button', { name: 'Continuar', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Mapping de salida', exact: true })).toBeVisible()
  await page.getByLabel('Precisión de amount', { exact: true }).fill('24')
  await page.getByLabel('Escala de amount', { exact: true }).fill('8')
  await page.getByRole('button', { name: 'Continuar', exact: true }).click()
  await page.getByRole('button', { name: 'Continuar', exact: true }).click()
  const validationResponse = page.waitForResponse(response => new URL(response.url()).pathname === '/api/v1/delivery/validations' && response.request().method() === 'POST')
  await page.getByRole('button', { name: 'Registrar preflight completo', exact: true }).click()
  const registeredValidation = await validationResponse
  expect(registeredValidation.status()).toBe(202)
  const validation = await registeredValidation.json()
  await terminal(page, `/delivery/validations/${validation.id}`)
  await page.getByRole('button', { name: 'Ver y usar resultado', exact: true }).click({ timeout: 30_000 })
  await expect(page.getByRole('heading', { name: 'Preflight aprobado', exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'Continuar', exact: true }).click()
  const deliveryResponse = page.waitForResponse(response => new URL(response.url()).pathname === '/api/v1/delivery/configurations' && response.request().method() === 'POST')
  await page.getByRole('button', { name: 'Publicar configuración', exact: true }).click()
  const publishedDelivery = await deliveryResponse
  expect(publishedDelivery.status()).toBe(201)
  const configuration = await publishedDelivery.json()

  await page.goto('/delivery/automation')
  await page.getByLabel('Nombre', { exact: true }).fill(`UI Volume chain ${stamp}`)
  await page.getByLabel('Configuración publicada', { exact: true }).selectOption(configuration.id)
  await page.getByLabel('Disparador', { exact: true }).selectOption('CHAINED')
  await page.getByLabel('Contrato Intake disparador', { exact: true }).selectOption(contract.id)
  // The UI has minute precision. Register a valid future minute, then honor its
  // activation fence before creating the Intake that may trigger this chain.
  const chainStartsAt = Math.floor((Date.now() + 60_000) / 60_000) * 60_000
  const parts = new Intl.DateTimeFormat('en-CA', { timeZone: 'America/Bogota', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).formatToParts(new Date(chainStartsAt))
  const part = (type: string) => parts.find(value => value.type === type)?.value || ''
  await page.getByLabel('Inicio en la zona seleccionada', { exact: true }).fill(`${part('year')}-${part('month')}-${part('day')}T${part('hour')}:${part('minute')}`)
  const automationResponse = page.waitForResponse(response => new URL(response.url()).pathname === '/api/v1/delivery/automations' && response.request().method() === 'POST')
  await page.getByRole('button', { name: 'Crear automatización', exact: true }).click()
  const publishedAutomation = await automationResponse
  const automation = await publishedAutomation.json()
  expect(publishedAutomation.status(), JSON.stringify({ code: automation.code, message: automation.message, details: automation.details })).toBe(201)
  await expect.poll(() => Date.now(), { timeout: 70_000, intervals: [1000] }).toBeGreaterThanOrEqual(chainStartsAt)

  await page.goto('/intake')
  await page.getByRole('button', { name: 'Nueva ejecución', exact: true }).click()
  const execute = page.getByRole('dialog')
  await execute.getByLabel('Contratos de datos', { exact: true }).selectOption(contract.id)
  await execute.getByLabel('Motor de procesamiento', { exact: true }).selectOption('PYSPARK')
  const executionResponse = page.waitForResponse(response => new URL(response.url()).pathname === '/api/v1/intake/runs' && response.request().method() === 'POST')
  await execute.getByRole('button', { name: 'Validar datos', exact: true }).click()
  const queuedExecution = await executionResponse
  expect(queuedExecution.status()).toBe(202)
  const execution = await queuedExecution.json()
  const accepted = await terminal(page, `/runs/${execution.id}`)
  expect(accepted.decision).toBe('APPROVED')
  expect(accepted.metrics.total_rows).toBe(fixture.rows)
  expect(accepted.output_version_id).toBeTruthy()
  let occurrence: Record<string, any> = {}
  await expect.poll(async () => {
    const body = await (await page.request.get(`/api/v1/delivery/automations/${automation.id}/occurrences`)).json()
    occurrence = body.items.find((item: { source_run_id: string }) => item.source_run_id === execution.id) || {}
    return Boolean(occurrence.run_id)
  }, { timeout: 60_000, intervals: [1000] }).toBe(true)
  expect(occurrence.dataset_version_id).toBe(accepted.output_version_id)
  const committed = await terminal(page, `/runs/${occurrence.run_id}`)
  expect(committed.decision).toBe('COMMITTED')
  expect(committed.metrics.receipt_artifact_id).toBeTruthy()
  await page.goto(`/runs/${occurrence.run_id}`)
  await expect(page.getByRole('heading', { name: 'Receipt inmutable', exact: true })).toBeVisible()
  await expect(page.getByText(`${schema}.${tableName}`, { exact: true }).first()).toBeVisible()
  const wanted = [acquisition.id, execution.id, committed.id]
  await expect.poll(async () => {
    const inbox = await (await page.request.get('/api/v1/notifications/inbox?limit=100')).json()
    return wanted.every(id => inbox.items.some((item: { resource_id: string }) => item.resource_id === id))
  }, { timeout: 60_000, intervals: [1000] }).toBe(true)
  await page.getByRole('link', { name: 'Notificaciones', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Notificaciones', exact: true })).toBeVisible()
  await page.getByLabel('Origen', { exact: true }).selectOption('CHAINED')
  await expect(page.getByText('Entrega confirmada en el destino.').first()).toBeVisible()
  expect(errors).toEqual([])
  await writeFile(testInfo.outputPath('volume-ui.json'), JSON.stringify({ project, acquisition_id: acquisition.id,
    source_version_id: acquired.output_version_id, intake_run_id: execution.id, output_version_id: accepted.output_version_id,
    delivery_run_id: committed.id, automation_id: automation.id, schema, table: tableName,
    expected_rows: fixture.rows, expected_canonical_rows_sha256: fixture.canonical_rows_sha256 }, null, 2))
})
