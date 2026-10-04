import { expect, test, type Page } from '@playwright/test'
import { readFile, writeFile } from 'node:fs/promises'

test.use({ trace: 'off', screenshot: 'off', video: 'off', actionTimeout: 30_000, navigationTimeout: 30_000 })
test.setTimeout(5_400_000)
test.skip(process.env.TV_CORRECTIONS_E2E !== 'true', 'Requires guarded corrections_cycle.py fixtures')

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

test('XLSX completo: recepción → adquisición persistente → Spark → Delivery encadenado → inbox personal', async ({ page }, testInfo) => {
  const project = process.env.TV_CORRECTIONS_PROJECT || ''
  const fixturePath = process.env.TV_CORRECTIONS_FIXTURE || ''
  const fixtureMetadata = process.env.TV_CORRECTIONS_METADATA || ''
  const destinationId = process.env.TV_CORRECTIONS_DESTINATION || ''
  const schema = process.env.TV_CORRECTIONS_SCHEMA || ''
  expect(project).toMatch(/^trackvance-v070-test-[a-z0-9-]+-[a-f0-9]{12}$/)
  expect(new URL(String(testInfo.project.use.baseURL)).hostname).toMatch(/^(localhost|127\.0\.0\.1)$/)
  expect(Number(new URL(String(testInfo.project.use.baseURL)).port)).toBeGreaterThanOrEqual(32000)
  expect(Number(new URL(String(testInfo.project.use.baseURL)).port)).toBeLessThanOrEqual(32999)
  expect(fixturePath).toContain(project)
  expect(destinationId).toBeTruthy()
  expect(schema).toMatch(/^volume_[a-f0-9]+$/)
  const fixture = JSON.parse(await readFile(fixtureMetadata, 'utf8'))
  expect([400_000, 1_000_000]).toContain(fixture.rows)
  expect(fixture.generator).toBe('OOXML_DIVERSE_V1')
  expect(fixture.oracle.status).toBe('PASS')
  expect(fixture.sheet).toBe('Datos')
  expect(fixture.header_physical_row).toBe(4)
  expect(fixture.declared_dimension_trusted).toBe(false)
  const stamp = Date.now(), datasetName = `UI Corrections ${stamp}`, tableName = `ui_corrections_${stamp}`
  const businessArea = `Riesgos XLSX ${stamp}`
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))

  await page.goto('/')
  await page.getByRole('button', { name: 'Entrar al entorno demo', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Centro de control', exact: true })).toBeVisible()
  await page.goto('/datasets?upload=1')
  const upload = page.getByRole('dialog')
  await upload.getByLabel('Nombre del dataset', { exact: true }).fill(datasetName)
  await expect(upload.getByLabel('Área de negocio', { exact: true })).toHaveValue('Operaciones')
  await upload.getByLabel('Área de negocio', { exact: true }).selectOption('__new_domain__')
  await expect(upload.getByRole('button', { name: 'Registrar adquisición', exact: true })).toBeDisabled()
  await upload.getByLabel('Nueva área de negocio', { exact: true }).fill(businessArea)
  const receivedResponse = page.waitForResponse(response => new URL(response.url()).pathname === '/api/v1/datasets/uploads/stage' && response.request().method() === 'POST', { timeout: 300_000 })
  await upload.getByLabel('Archivo', { exact: true }).setInputFiles(fixturePath)
  const received = await receivedResponse
  expect(received.status()).toBe(201)
  // Chromium can evict inspector response bodies for a large raw request.
  // Assert the application's parsed inspection and persisted exact byte count.
  await expect(upload.getByText(/Transferencia completa/)).toBeVisible()
  await expect(upload.getByLabel('Hoja', { exact: true })).toHaveValue('Información')
  await upload.getByLabel('Hoja', { exact: true }).selectOption('Datos')
  await expect(upload.getByLabel('Identificador record_id', { exact: true })).toBeVisible()
  await upload.getByLabel('Identificador record_id', { exact: true }).check()
  await expect(upload.getByRole('region', { name: 'Límites efectivos de la ruta' })).toContainText('XLSX')
  await expect(upload.getByRole('region', { name: 'Límites efectivos de la ruta' })).toContainText('1.000.000 registros')
  await expect(upload.getByRole('region', { name: 'Límites efectivos de la ruta' })).toContainText('Encabezado observado en la fila 4')
  const registeredResponse = page.waitForResponse(response => /\/datasets\/[^/]+\/acquisitions$/.test(new URL(response.url()).pathname) && response.request().method() === 'POST')
  await upload.getByRole('button', { name: 'Registrar adquisición', exact: true }).click()
  const registered = await registeredResponse
  expect(registered.status()).toBe(202)
  const acquisition = await registered.json()
  expect(acquisition.status).toBe('QUEUED')
  expect(acquisition.total_bytes).toBe(fixture.formats.XLSX.actual_bytes)
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
  expect(acquired.published_rows).toBe(fixture.rows)
  expect(acquired.error).toBeNull()
  const registeredDataset = await (await page.request.get(`/api/v1/datasets/${acquisition.dataset_id}`)).json()
  expect(registeredDataset.domain).toBe(businessArea)
  await page.reload()
  await expect(page.getByRole('link', { name: 'Ver versión', exact: true })).toBeVisible()
  const profileResponse = await page.request.get(`/api/v1/dataset-versions/${acquired.output_version_id}/profile`)
  expect(profileResponse.status()).toBe(200)
  const profile = await profileResponse.json()
  expect(profile.profile.row_count).toBe(fixture.rows)
  expect(profile.profile.columns.find((column: { name: string }) => column.name === 'record_id').distinct_count).toBe(fixture.rows)
  expect(profile.profile.columns.find((column: { name: string }) => column.name === 'observed').null_count).toBe(fixture.expected_observed_nulls)
  expect(profile.profile.columns.find((column: { name: string }) => column.name === 'late_type').logical_type).toBe('STRING')
  await page.goto('/datasets?upload=1')
  await expect(page.getByRole('dialog').getByLabel('Área de negocio', { exact: true }).getByRole('option', { name: businessArea, exact: true })).toBeAttached()
  await page.getByRole('dialog').getByRole('button', { name: 'Usar carga rápida limitada', exact: true }).click()
  await expect(page.getByRole('dialog').getByLabel('Área de negocio', { exact: true }).getByRole('option', { name: businessArea, exact: true })).toBeAttached()
  await page.getByRole('dialog').getByRole('button', { name: 'Cancelar', exact: true }).click()
  await page.goto(`/datasets/${acquisition.dataset_id}`)
  await page.getByRole('button', { name: 'Nueva versión', exact: true }).click()
  await expect(page.getByRole('dialog').getByLabel('Área de negocio', { exact: true })).toHaveValue(businessArea)
  await expect(page.getByRole('dialog').getByLabel('Área de negocio', { exact: true })).toBeDisabled()
  await page.getByRole('dialog').getByRole('button', { name: 'Cerrar', exact: true }).click()

  await page.goto('/intake')
  await page.getByRole('button', { name: 'Nuevo contrato', exact: true }).first().click()
  const contractDialog = page.getByRole('dialog')
  await contractDialog.getByLabel('Nombre del contrato', { exact: true }).fill(`UI Corrections contract ${stamp}`)
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
  await page.getByLabel('Nombre de la entrega', { exact: true }).fill(`UI Corrections Delivery ${stamp}`)
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
  // CREATE_TABLE preserves each full-profile logical type. The source schema,
  // rather than a generic text mapping, determines DATE and late STRING fields.
  for (const column of profile.profile.columns) {
    await expect(page.getByLabel(`Tipo destino de ${column.name}`, { exact: true })).toHaveValue(column.logical_type)
  }
  await expect(page.getByLabel('Tipo destino de date', { exact: true })).toHaveValue('DATE')
  await expect(page.getByLabel('Tipo destino de amount', { exact: true })).toHaveValue('DECIMAL')
  await expect(page.getByLabel('Tipo destino de late_type', { exact: true })).toHaveValue('STRING')
  await expect(page.getByLabel('Tipo destino de record_id', { exact: true })).toHaveValue('STRING')
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
  await page.getByLabel('Nombre', { exact: true }).fill(`UI Corrections chain ${stamp}`)
  await page.getByLabel('Configuración publicada', { exact: true }).selectOption(configuration.id)
  await page.getByLabel('Disparador', { exact: true }).selectOption('CHAINED')
  await page.getByLabel('Contrato Intake disparador', { exact: true }).selectOption(contract.id)
  // The UI has minute precision. Register a valid future minute, then honor its
  // activation fence before creating the Intake that may trigger this chain.
  const chainStartsAt = Math.floor((Date.now() + 60_000) / 60_000) * 60_000
  const parts = new Intl.DateTimeFormat('en-CA', { timeZone: 'America/Bogota', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).formatToParts(new Date(chainStartsAt))
  const part = (type: string) => parts.find(value => value.type === type)?.value || ''
  await page.getByLabel('Inicio en la zona seleccionada', { exact: true }).fill(`${part('year')}-${part('month')}-${part('day')}T${part('hour')}:${part('minute')}`)
  const localStart = await page.getByLabel('Inicio en la zona seleccionada', { exact: true }).inputValue()
  const zone = page.getByLabel('Zona horaria', { exact: true })
  for (const draft of ['', 'America/Bogot', 'Invalid/Zone']) {
    await zone.fill(draft)
    await expect(page.getByRole('alert')).toContainText('zona horaria IANA válida')
    await expect(page.getByRole('button', { name: 'Crear automatización', exact: true })).toBeDisabled()
    await expect(page.getByLabel('Nombre', { exact: true })).toHaveValue(`UI Corrections chain ${stamp}`)
    await expect(page.getByLabel('Inicio en la zona seleccionada', { exact: true })).toHaveValue(localStart)
  }
  await zone.fill('America/Bogota')
  const automationResponse = page.waitForResponse(response => new URL(response.url()).pathname === '/api/v1/delivery/automations' && response.request().method() === 'POST')
  await page.getByRole('button', { name: 'Crear automatización', exact: true }).click()
  const publishedAutomation = await automationResponse
  const automation = await publishedAutomation.json()
  expect(publishedAutomation.status(), JSON.stringify({ code: automation.code, message: automation.message, details: automation.details })).toBe(201)
  expect(new Date(automation.settings.starts_at).getTime()).toBe(chainStartsAt)
  expect(automation.settings.timezone).toBe('America/Bogota')
  await page.waitForURL(`**/delivery/automation/${automation.id}`)
  await expect(page.getByRole('heading', { name: 'Nueva revisión de la automatización', exact: true })).toBeVisible()
  await expect(page.getByLabel('Inicio en la zona seleccionada', { exact: true })).toHaveValue(localStart)
  const preservedInstant = automation.settings.starts_at
  for (const draft of ['', 'America/Bogot', 'Invalid/Zone']) {
    await zone.fill(draft)
    await expect(page.getByRole('alert')).toContainText('zona horaria IANA válida')
    await expect(page.getByRole('button', { name: 'Guardar nueva revisión', exact: true })).toBeDisabled()
    await expect(page.getByLabel('Inicio en la zona seleccionada', { exact: true })).toHaveValue(localStart)
  }
  await zone.fill('America/Bogota')
  await page.getByLabel('Nombre', { exact: true }).fill(`UI Corrections chain revised ${stamp}`)
  const revisionResponse = page.waitForResponse(response => new URL(response.url()).pathname === `/api/v1/delivery/automations/${automation.id}/versions` && response.request().method() === 'POST')
  await page.getByRole('button', { name: 'Guardar nueva revisión', exact: true }).click()
  const revision = await revisionResponse
  expect(revision.status()).toBe(201)
  const persisted = await revision.json()
  expect(persisted.settings.starts_at).toBe(preservedInstant)
  expect(persisted.settings.timezone).toBe('America/Bogota')
  expect(persisted.settings.intake_configuration_id).toBe(contract.id)
  await page.reload()
  await expect(page.getByLabel('Inicio en la zona seleccionada', { exact: true })).toHaveValue(localStart)
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
  await page.getByRole('button', { name: 'Marcar todas como leídas', exact: true }).click()
  await expect(page.getByRole('link', { name: 'Ver notificaciones', exact: true })).toBeVisible()
  await page.getByLabel('Lectura', { exact: true }).selectOption('READ')
  const confirmed = page.getByRole('row').filter({ has: page.getByRole('link', { name: 'Ver detalle', exact: true }).and(page.locator(`a[href="/runs/${committed.id}"]`)) })
  await expect(confirmed).toHaveCount(1)
  const unreadResponse = page.waitForResponse(response => /\/notifications\/inbox\/[^/]+\/unread$/.test(new URL(response.url()).pathname) && response.request().method() === 'POST')
  await confirmed.getByRole('button', { name: 'Marcar como no leída', exact: true }).click()
  const unread = await unreadResponse
  expect(unread.status()).toBe(200)
  const personal = await unread.json()
  expect(personal.read_at).toBeNull()
  await expect(confirmed).toHaveCount(0)
  await expect(page.getByRole('link', { name: 'Ver notificaciones, 1 sin leer', exact: true })).toBeVisible()
  await page.getByLabel('Lectura', { exact: true }).selectOption('UNREAD')
  await expect(confirmed.getByRole('button', { name: 'Marcar leída', exact: true })).toBeVisible()
  await page.reload()
  await page.getByLabel('Origen', { exact: true }).selectOption('CHAINED')
  await page.getByLabel('Lectura', { exact: true }).selectOption('UNREAD')
  await expect(confirmed.getByRole('button', { name: 'Marcar leída', exact: true })).toBeVisible()
  await expect(page.getByRole('link', { name: 'Ver notificaciones, 1 sin leer', exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'Cerrar sesión', exact: true }).click()
  await page.getByRole('button', { name: 'Entrar al entorno demo', exact: true }).click()
  // A hard navigation must wait for the async login to establish its session.
  await expect(page.getByRole('heading', { name: 'Centro de control', exact: true })).toBeVisible()
  await page.goto('/notifications')
  await page.getByLabel('Origen', { exact: true }).selectOption('CHAINED')
  await page.getByLabel('Lectura', { exact: true }).selectOption('UNREAD')
  await expect(confirmed.getByRole('button', { name: 'Marcar leída', exact: true })).toBeVisible()
  await expect(page.getByRole('link', { name: 'Ver notificaciones, 1 sin leer', exact: true })).toBeVisible()
  await confirmed.getByRole('button', { name: 'Marcar leída', exact: true }).click()
  await expect(confirmed).toHaveCount(0)
  await expect(page.getByRole('link', { name: 'Ver notificaciones', exact: true })).toBeVisible()
  await page.getByLabel('Lectura', { exact: true }).selectOption('ALL')
  await expect(confirmed.getByRole('button', { name: 'Marcar como no leída', exact: true })).toBeVisible()
  expect(errors).toEqual([])
  await writeFile(testInfo.outputPath('corrections-ui.json'), JSON.stringify({ project, acquisition_id: acquisition.id,
    source_version_id: acquired.output_version_id, intake_run_id: execution.id, output_version_id: accepted.output_version_id,
    delivery_run_id: committed.id, automation_id: automation.id, schema, table: tableName,
    business_area: businessArea, timezone_validation: 'PASS', preserved_starts_at: preservedInstant, unread_persistence: 'PASS', notification_id: personal.id, expected_rows: fixture.rows, expected_canonical_rows_sha256: fixture.canonical_rows_sha256 }, null, 2))
})
