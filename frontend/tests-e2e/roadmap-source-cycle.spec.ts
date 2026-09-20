import { test, expect, type Page, type Locator } from '@playwright/test'
import { execFileSync } from 'node:child_process'

test.use({ trace: 'off' }) // Local authentication and connector forms contain disposable passwords.
test.setTimeout(180_000)
test.skip(process.env.TV_CONNECTIONS_E2E !== 'true', 'Requires isolated real source fixtures')

function fixture(engine: string, action: string) {
  const project = process.env.COMPOSE_PROJECT_NAME || ''
  expect(project).toMatch(/^trackvance-connections-e2e-[a-z0-9-]+$/)
  execFileSync(process.env.TV_PYTHON || 'python', ['../scripts/tests/roadmap_source_fixture.py', '--project', project, '--engine', engine, '--action', action], { stdio: 'pipe' })
}

async function columns(dialog: Locator, label: string, names: string[]) {
  await dialog.getByRole('button', { name: `${label}: abrir selector`, exact: true }).click()
  const options = dialog.getByRole('group', { name: `Opciones de ${label}`, exact: true })
  for (const name of names) await options.getByRole('checkbox', { name: new RegExp(`^${name} \\(`) }).check()
  await dialog.getByRole('button', { name: `${label}: cerrar selector`, exact: true }).click()
}

async function execute(page: Page, contractId: string) {
  await page.goto('/intake')
  await page.getByRole('button', { name: 'Nueva ejecución', exact: true }).click()
  const dialog = page.getByRole('dialog')
  await dialog.locator('select').first().selectOption(contractId)
  await dialog.getByRole('button', { name: 'Validar datos', exact: true }).click()
  await page.waitForURL(/\/runs\/[^/]+$/)
  await expect(page.getByText('Completada', { exact: true }).first()).toBeVisible({ timeout: 60_000 })
  const id = new URL(page.url()).pathname.split('/').at(-1)
  return (await page.request.get(`/api/v1/runs/${id}`)).json()
}

for (const [engine, engineLabel, host, tls] of [
  ['POSTGRESQL', 'PostgreSQL', 'source-postgres', 'disable'],
  ['SQLSERVER', 'SQL Server', 'source-sqlserver', 'off'],
]) {
  test(`${engineLabel}: fuente → regla condicional → excepción con SLA → fuente corregida → resolución técnica`, async ({ page }, testInfo) => {
    fixture(engine, 'setup')
    const password = process.env.TV_CONNECTIONS_PASSWORD
    expect(password).toBeTruthy()
    const name = `E2E roadmap ${engine} ${Date.now()}`
    await page.goto('/')
    await page.getByRole('button', { name: 'Entrar al entorno demo', exact: true }).click()
    await expect(page.getByRole('heading', { name: 'Centro de control', exact: true })).toBeVisible()
    const session = await (await page.request.get('/api/v1/me')).json()
    await page.goto('/connections')
    await page.getByRole('button', { name: 'Nueva conexión', exact: true }).click()
    let dialog = page.getByRole('dialog')
    await dialog.getByLabel('Tipo de conexión', { exact: true }).selectOption(engine)
    await dialog.getByLabel('Nombre de conexión', { exact: true }).fill(name)
    await dialog.getByLabel('Host', { exact: true }).fill(host)
    await dialog.getByLabel('Base de datos', { exact: true }).fill('trackvance_source')
    await dialog.getByLabel('Usuario', { exact: true }).fill('tv_reader')
    await dialog.getByLabel('Contraseña', { exact: true }).fill(password!)
    await dialog.getByLabel('Cifrado de transporte', { exact: true }).selectOption(tls)
    await dialog.getByRole('button', { name: 'Probar conexión', exact: true }).click()
    await expect(dialog.getByText('Conexión verificada', { exact: true })).toBeVisible()
    await dialog.getByRole('button', { name: 'Guardar conexión', exact: true }).click()
    await page.waitForURL(/\/connections\/[^/]+$/)
    await page.getByLabel('Schema', { exact: true }).selectOption('source_data')
    await page.getByLabel('Tabla o vista', { exact: true }).selectOption('roadmap_transactions')
    await expect(page.getByRole('table', { name: 'Vista previa de la fuente' }).locator('tbody tr')).toHaveCount(3)
    await page.getByLabel('Nombre del dataset', { exact: true }).fill(name)
    const importedResponse = page.waitForResponse(response => /\/connections\/[^/]+\/datasets$/.test(new URL(response.url()).pathname) && response.request().method() === 'POST')
    await page.getByRole('button', { name: 'Crear dataset', exact: true }).click()
    const imported = await importedResponse
    expect(imported.status()).toBe(201)
    const source = await imported.json()
    await page.goto('/intake')
    await page.getByRole('button', { name: 'Nuevo contrato', exact: true }).click()
    dialog = page.getByRole('dialog')
    await dialog.getByLabel('Nombre del contrato', { exact: true }).fill(name)
    await dialog.getByLabel('Dataset', { exact: true }).selectOption(source.dataset.id)
    await columns(dialog, 'Columnas obligatorias', ['record_id', 'country'])
    await columns(dialog, 'Columnas con valores positivos', ['amount'])
    await dialog.getByLabel('Porcentaje máximo de filas con error (%)', { exact: true }).fill('0')
    await dialog.getByRole('button', { name: 'Agregar regla', exact: true }).click()
    await dialog.getByLabel('Tipo de regla', { exact: true }).selectOption('required')
    await dialog.getByLabel('Columna de la regla', { exact: true }).selectOption('department')
    await dialog.getByLabel('Aplicación de la regla', { exact: true }).selectOption('CONDITIONAL')
    await dialog.getByLabel('Columna de la condición', { exact: true }).selectOption('country')
    await dialog.getByLabel('Operador de la condición', { exact: true }).selectOption('eq')
    await dialog.getByLabel('Valor de la condición', { exact: true }).fill('CO')
    const published = page.waitForResponse(response => new URL(response.url()).pathname === '/api/v1/intake/contracts' && response.request().method() === 'POST')
    await dialog.getByRole('button', { name: 'Crear contrato', exact: true }).click()
    const contractResponse = await published
    expect(contractResponse.status()).toBe(201)
    const contract = await contractResponse.json()
    const failed = await execute(page, contract.id)
    expect(failed.decision).toBe('REJECTED')
    expect(failed.metrics.error_rows).toBe(1)
    await page.getByRole('button', { name: /^Hallazgos/ }).click()
    await page.getByRole('button', { name: 'Crear excepción', exact: true }).click()
    await page.waitForURL(/\/exceptions\?id=/)
    const caseId = new URL(page.url()).searchParams.get('id')!
    await expect(page.getByRole('button', { name: 'Resolver excepción', exact: true })).toBeDisabled()
    await page.getByLabel('Estado de gestión', { exact: true }).selectOption('ASSIGNED')
    await page.getByLabel('Responsable', { exact: true }).selectOption(session.user.id)
    await page.getByLabel('Prioridad del caso', { exact: true }).selectOption('CRITICAL')
    await page.getByLabel('SLA (horas)', { exact: true }).fill('24')
    await page.getByLabel('Comentario para el historial', { exact: true }).fill('Corregir departamento en la fuente; validar nuevamente.')
    await page.getByRole('button', { name: 'Guardar gestión', exact: true }).click()
    await expect.poll(async () => (await (await page.request.get(`/api/v1/exceptions/${caseId}`)).json()).state).toBe('ASSIGNED')
    await page.getByLabel('Estado de gestión', { exact: true }).selectOption('INVESTIGATING')
    await page.getByRole('button', { name: 'Guardar gestión', exact: true }).click()
    await expect.poll(async () => (await (await page.request.get(`/api/v1/exceptions/${caseId}`)).json()).state).toBe('INVESTIGATING')
    await page.getByLabel('Estado de gestión', { exact: true }).selectOption('PENDING_VALIDATION')
    await page.getByLabel('Causa raíz', { exact: true }).fill('Departamento faltante en registro colombiano.')
    await page.getByLabel('Corrección aplicada', { exact: true }).fill('Departamento corregido en la base fuente.')
    await page.getByRole('button', { name: 'Guardar gestión', exact: true }).click()
    await expect.poll(async () => (await (await page.request.get(`/api/v1/exceptions/${caseId}`)).json()).state).toBe('PENDING_VALIDATION')
    fixture(engine, 'correct')
    await page.goto(`/datasets/${source.dataset.id}`)
    await page.getByRole('button', { name: 'Nueva versión desde la fuente', exact: true }).click()
    await expect(page.getByText('Versión 2 creada desde la fuente. Los snapshots anteriores permanecen intactos.')).toBeVisible()
    const corrected = await execute(page, contract.id)
    expect(corrected.decision).toBe('APPROVED')
    expect(corrected.dataset_version_id).not.toBe(source.version.id)
    const original = await (await page.request.get(`/api/v1/runs/${failed.id}`)).json()
    expect(original.decision).toBe('REJECTED')
    await page.goto(`/exceptions?id=${caseId}`)
    await expect(page.getByRole('button', { name: 'Resolver excepción', exact: true })).toBeEnabled()
    await page.getByRole('button', { name: 'Resolver excepción', exact: true }).click()
    await expect.poll(async () => (await (await page.request.get(`/api/v1/exceptions/${caseId}`)).json()).state).toBe('RESOLVED')
    const resolved = await (await page.request.get(`/api/v1/exceptions/${caseId}`)).json()
    expect(resolved.validation_run_id).toBe(corrected.id)
    expect(resolved.configuration_id).toBe(contract.id)
    expect(resolved.run_id).toBe(failed.id)
    expect(resolved.assigned_user_id).toBe(session.user.id)
    expect(resolved.priority).toBe('CRITICAL')
    expect(resolved.due_at).toBeTruthy()
    await page.screenshot({ path: testInfo.outputPath(`${engine.toLowerCase()}-technical-resolution.png`), fullPage: true })
  })
}
