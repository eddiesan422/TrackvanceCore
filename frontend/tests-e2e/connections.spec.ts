import { test, expect, type Locator } from '@playwright/test'
import fs from 'node:fs/promises'

// Opt in only on the disposable stack provisioned by connections_cycle.py.
// Traces contain network request bodies, including the test password: never retain them.
test.use({ trace: 'off' })
test.setTimeout(150_000)
test.skip(process.env.TV_CONNECTIONS_E2E !== 'true', 'Requires the isolated external source fixtures')

async function selectColumns(dialog: Locator, label: string, columns: string[]) {
  await dialog.getByRole('button', { name: `${label}: abrir selector`, exact: true }).click()
  const options = dialog.getByRole('group', { name: `Opciones de ${label}`, exact: true })
  for (const column of columns) await options.getByRole('checkbox', { name: new RegExp(`^${column} \\(`) }).check()
  await dialog.getByRole('button', { name: `${label}: cerrar selector`, exact: true }).click()
}

for (const [source, label, host, tls] of [
  ['POSTGRESQL', 'PostgreSQL', 'source-postgres', 'disable'],
  ['SQLSERVER', 'SQL Server', 'source-sqlserver', 'off'],
]) {
  test(`${label}: conexión → exploración → snapshot → Data Intake → evidencia`, async ({ page }, testInfo) => {
    const password = process.env.TV_CONNECTIONS_PASSWORD
    expect(password, 'The isolated runner supplies a disposable password').toBeTruthy()
    const name = `E2E conexión ${source} ${Date.now()}`
    const pageErrors: string[] = []
    page.on('pageerror', error => pageErrors.push(error.message))
    await page.goto('/')
    await page.getByRole('button', { name: 'Entrar al entorno demo', exact: true }).click()
    await expect(page.getByRole('heading', { name: 'Centro de control', exact: true })).toBeVisible()
    await page.getByRole('link', { name: 'Conexiones', exact: true }).click()
    await page.getByRole('button', { name: 'Nueva conexión', exact: true }).click()
    const dialog = page.getByRole('dialog')
    await dialog.getByLabel('Tipo de conexión', { exact: true }).selectOption(source)
    await dialog.getByLabel('Nombre de conexión', { exact: true }).fill(name)
    await dialog.getByLabel('Host', { exact: true }).fill(host)
    await dialog.getByLabel('Base de datos', { exact: true }).fill('trackvance_source')
    await dialog.getByLabel('Usuario', { exact: true }).fill('tv_reader')
    await dialog.getByLabel('Contraseña', { exact: true }).fill(password!)
    await dialog.getByLabel('Cifrado de transporte', { exact: true }).selectOption(tls)
    await expect(dialog.getByRole('button', { name: 'Guardar conexión', exact: true })).toBeDisabled()
    await dialog.getByRole('button', { name: 'Probar conexión', exact: true }).click()
    await expect(dialog.getByText('Conexión verificada', { exact: true })).toBeVisible()
    const saveResponse = page.waitForResponse(response => new URL(response.url()).pathname === '/api/v1/connections' && response.request().method() === 'POST')
    await dialog.getByRole('button', { name: 'Guardar conexión', exact: true }).click()
    const saved = await saveResponse
    expect(saved.status()).toBe(201)
    const connection = await saved.json()
    expect(JSON.stringify(connection).includes(password!), 'Connection responses must omit credentials').toBe(false)
    await page.waitForURL(new RegExp(`/connections/${connection.id}$`))
    await expect(page.getByRole('heading', { name, exact: true })).toBeVisible()
    await page.getByLabel('Schema', { exact: true }).selectOption('source_data')
    await expect(page.getByLabel('Tabla o vista', { exact: true })).toBeEnabled()
    await page.getByLabel('Tabla o vista', { exact: true }).selectOption('transactions_view')
    await expect(page.getByRole('table', { name: 'Vista previa de la fuente' }).locator('tbody tr')).toHaveCount(6)
    await page.getByLabel('Tabla o vista', { exact: true }).selectOption('transactions')
    const columns = page.getByRole('table', { name: 'Columnas de la fuente' })
    await expect(columns.getByRole('cell', { name: 'happened_on', exact: true })).toBeVisible()
    await expect(columns.getByRole('cell', { name: 'DATE', exact: true })).toBeVisible()
    await expect(page.getByRole('table', { name: 'Vista previa de la fuente' }).getByRole('cell', { name: '001234567', exact: true })).toHaveCount(2)
    await page.getByLabel('Nombre del dataset', { exact: true }).fill(name)
    await page.screenshot({ path: testInfo.outputPath(`${source.toLowerCase()}-preview.png`), fullPage: true })
    const importResponse = page.waitForResponse(response => new URL(response.url()).pathname === `/api/v1/connections/${connection.id}/datasets` && response.request().method() === 'POST')
    await page.getByRole('button', { name: 'Crear dataset', exact: true }).click()
    const imported = await importResponse
    expect(imported.status()).toBe(201)
    const { dataset, version } = await imported.json()
    expect(version.source_type).toBe(source)
    expect(version.original_artifact_id).toBeNull()
    expect(version.canonical_artifact_id).toBeTruthy()
    await page.getByRole('link', { name: 'Ver dataset', exact: true }).click()
    await expect(page.getByRole('heading', { name, exact: true })).toBeVisible()
    const refreshResponse = page.waitForResponse(response => new URL(response.url()).pathname === `/api/v1/datasets/${dataset.id}/refresh-source`)
    await page.getByRole('button', { name: 'Nueva versión desde la fuente', exact: true }).click()
    const refreshed = await refreshResponse
    expect(refreshed.status()).toBe(201)
    const newVersion = await refreshed.json()
    expect(newVersion.version).toBe(2)
    expect(newVersion.id).not.toBe(version.id)
    await expect(page.getByText('Versión 2 creada desde la fuente. Los snapshots anteriores permanecen intactos.')).toBeVisible()

    await page.goto('/intake')
    await page.getByRole('button', { name: 'Nuevo contrato', exact: true }).click()
    const contractDialog = page.getByRole('dialog')
    await contractDialog.getByLabel('Nombre del contrato', { exact: true }).fill(name)
    await contractDialog.getByLabel('Dataset', { exact: true }).selectOption(dataset.id)
    await selectColumns(contractDialog, 'Columnas obligatorias', ['record_id'])
    await selectColumns(contractDialog, 'Columnas sin duplicados', ['record_id'])
    await selectColumns(contractDialog, 'Columnas con valores positivos', ['amount'])
    await contractDialog.getByLabel('Porcentaje máximo de filas con error (%)', { exact: true }).fill('0')
    const contractResponse = page.waitForResponse(response => new URL(response.url()).pathname === '/api/v1/intake/contracts' && response.request().method() === 'POST')
    await contractDialog.getByRole('button', { name: 'Crear contrato', exact: true }).click()
    const contractResult = await contractResponse
    expect(contractResult.status()).toBe(201)
    const contract = await contractResult.json()
    await expect(contractDialog).not.toBeVisible()
    await page.getByRole('button', { name: 'Nueva ejecución', exact: true }).click()
    const runDialog = page.getByRole('dialog')
    await runDialog.locator('select').first().selectOption(contract.id)
    await runDialog.getByRole('button', { name: 'Validar datos', exact: true }).click()
    await page.waitForURL(/\/runs\/[^/]+$/)
    await expect(page.getByText('Completada', { exact: true }).first()).toBeVisible({ timeout: 60_000 })
    const runId = new URL(page.url()).pathname.split('/').at(-1)
    const runResponse = await page.request.get(`/api/v1/runs/${runId}`)
    expect(runResponse.status()).toBe(200)
    const run = await runResponse.json()
    expect(run.status).toBe('SUCCESS')
    expect(run.decision).toBe('REJECTED')
    expect(run.metrics.total_rows).toBe(6)
    expect(run.metrics.error_rows).toBeGreaterThan(0)
    const evidenceResponse = await page.request.get(`/api/v1/runs/${runId}/evidence`)
    expect(evidenceResponse.status()).toBe(200)
    const evidence = await evidenceResponse.json()
    expect(evidence.inputs[0].ingestion_metadata.source).toEqual(expect.objectContaining({
      connection_id: connection.id, connection_version_id: connection.connection_version_id,
      schema_name: 'source_data', object_name: 'transactions', source_type: source,
    }))
    expect(JSON.stringify(evidence).includes(password!), 'Evidence must omit credentials').toBe(false)
    const downloadResponse = page.waitForResponse(response => response.url().endsWith(`/runs/${runId}/export.xlsx`))
    const downloadEvent = page.waitForEvent('download')
    await page.getByRole('button', { name: 'Exportar Excel', exact: true }).click()
    const excelResponse = await downloadResponse
    expect(excelResponse.status()).toBe(200)
    expect(excelResponse.headers()['content-type']).toBe('application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    const download = await downloadEvent
    expect(download.suggestedFilename()).toBe(`trackvance_intake_${runId}.xlsx`)
    const excelPath = testInfo.outputPath(`${source.toLowerCase()}-intake.xlsx`)
    await download.saveAs(excelPath)
    expect((await fs.readFile(excelPath)).subarray(0, 2).toString()).toBe('PK')
    await page.screenshot({ path: testInfo.outputPath(`${source.toLowerCase()}-intake-result.png`), fullPage: true })
    expect(pageErrors).toEqual([])
  })
}
