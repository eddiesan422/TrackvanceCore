import { test, expect, type Page } from '@playwright/test'
import fs from 'node:fs/promises'

test.setTimeout(120_000)

async function signIn(page: Page) {
  await page.goto('/')
  await page.getByRole('button', { name: 'Entrar al entorno demo' }).click()
  await expect(page.getByRole('heading', { name: 'Centro de control', exact: true })).toBeVisible()
  const me = await (await page.request.get('/api/v1/me')).json()
  return { 'X-CSRF-Token': me.csrf_token as string }
}

async function post(page: Page, endpoint: string, data: unknown, headers: Record<string, string>) {
  const response = await page.request.post(`/api/v1${endpoint}`, { data, headers })
  expect(response.ok(), await response.text()).toBeTruthy()
  return response.json()
}

async function executeUI(page: Page, module: string, configId: string, action: string) {
  await page.goto(`/${module}`)
  await page.getByRole('button', { name: 'Nueva ejecución', exact: true }).click()
  const dialog = page.getByRole('dialog')
  await dialog.locator('select').first().selectOption(configId)
  await dialog.getByRole('button', { name: action, exact: true }).click()
  await page.waitForURL(/\/runs\//)
  await expect(page.getByText('Completada', { exact: true }).first()).toBeVisible({ timeout: 30_000 })
  return page.url().split('/runs/')[1]
}

async function downloadExcel(page: Page, run: string, module: string, destination: string) {
  const responsePromise = page.waitForResponse(r => r.url().endsWith(`/runs/${run}/export.xlsx`))
  const downloadPromise = page.waitForEvent('download')
  await page.getByRole('button', { name: 'Exportar Excel', exact: true }).click()
  const response = await responsePromise
  expect(response.status()).toBe(200)
  expect(response.headers()['content-type']).toBe('application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
  expect(response.headers()['content-disposition']).toContain(`trackvance_${module}_${run}.xlsx`)
  const download = await downloadPromise
  expect(download.suggestedFilename()).toBe(`trackvance_${module}_${run}.xlsx`)
  await download.saveAs(destination)
  expect((await fs.readFile(destination)).subarray(0, 2).toString()).toBe('PK')
}

test('upload → Intake fecha no futura → Recon diferencia → excepción → Sentinel cambio de esquema y Excel', async ({ page }, testInfo) => {
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  const headers = await signIn(page)
  const name = `E2E cierre ${Date.now()}`
  const csv = 'order_id,amount,transaction_date\n001234567,100.25,2026-09-01\n1234567,200.00,2999-01-01\n'
  await page.goto('/datasets?upload=1')
  const upload = page.getByRole('dialog')
  await upload.getByLabel('Nombre del dataset', { exact: true }).fill(name)
  await upload.locator('input[type=file]').setInputFiles({ name: 'cierre.csv', mimeType: 'text/csv', buffer: Buffer.from(csv) })
  await upload.getByRole('button', { name: /Cargar/ }).click()
  await page.waitForURL(/\/datasets\//)
  const datasetId = page.url().split('/datasets/')[1]
  const dataset = await (await page.request.get(`/api/v1/datasets/${datasetId}`)).json()
  const contract = await post(page, '/intake/contracts', { name, dataset_id: datasetId, config: { required_columns: ['order_id'], max_error_rate: 0, rules: [{ code: 'DATE_NOT_FUTURE', type: 'date_rule', column: 'transaction_date', parameters: { not_future: true } }] } }, headers)
  const intake = await executeUI(page, 'intake', contract.id, 'Validar datos')
  await expect(page.getByRole('columnheader', { name: 'Línea del archivo' })).toBeVisible()
  await expect(page.getByText('DATE_NOT_FUTURE', { exact: true })).toBeVisible()
  await downloadExcel(page, intake, 'intake', testInfo.outputPath('intake.xlsx'))
  await page.screenshot({ path: testInfo.outputPath('intake-excel-button.png'), fullPage: true })
  const intakeRun = await (await page.request.get(`/api/v1/runs/${intake}`)).json()
  expect(intakeRun.status).toBe('SUCCESS')
  expect(intakeRun.decision).toBe('REJECTED')
  const accepted = await (await page.request.get(`/api/v1/dataset-versions/${intakeRun.output_version_id}/profile`)).json()
  expect(accepted.source_type).toBe('INTAKE_OUTPUT')
  expect(accepted.original_artifact_id).toBeNull()
  expect(accepted.canonical_artifact_id).toBeTruthy()
  await page.goto(`/datasets/${accepted.dataset_id}`)
  await page.getByRole('button', { name: /Identidad|Fuente de la versión/ }).click()
  await expect(page.getByText('Artefacto derivado', { exact: true }).first()).toBeVisible()
  await page.screenshot({ path: testInfo.outputPath('derived-version.png'), fullPage: true })

  const target = await post(page, '/datasets', { name: name + ' destino' }, headers)
  const uploadTarget = await page.request.post(`/api/v1/datasets/${target.id}/versions/upload`, { headers, multipart: { file: { name: 'destino.csv', mimeType: 'text/csv', buffer: Buffer.from('order_id,amount\n001234567,101.25\n1234567,200.00\n') } } })
  expect(uploadTarget.status()).toBe(201)
  const control = await post(page, '/recon/controls', { name: name + ' recon', dataset_id: datasetId, target_dataset_id: target.id, config: { key_columns: ['order_id'], key_normalization: { trim: false, case: 'NONE', unicode_normalization: 'NONE' }, comparison_rules: [{ type: 'numeric_tolerance', source_column: 'amount', target_column: 'amount', parameters: { abs: '0.01' } }] } }, headers)
  const recon = await executeUI(page, 'recon', control.id, 'Conciliar fuentes')
  await page.getByLabel('Filtrar clasificación').selectOption('VALUE_MISMATCH')
  await expect(page.getByRole('cell', { name: /Diferencia de valor/ }).first()).toBeVisible()
  await downloadExcel(page, recon, 'recon', testInfo.outputPath('recon.xlsx'))
  await page.screenshot({ path: testInfo.outputPath('recon-excel-button.png'), fullPage: true })
  await page.getByRole('button', { name: /^Hallazgos/ }).click()
  await page.getByRole('button', { name: 'Crear excepción', exact: true }).first().click()
  await page.getByRole('dialog').waitFor()
  await page.getByLabel('Estado', { exact: true }).selectOption('INVESTIGATING')
  await page.getByRole('button', { name: 'Guardar cambios', exact: true }).click()
  await expect(page.getByText('Los cambios de la excepción quedaron registrados.')).toBeVisible()
  await page.getByLabel('Estado', { exact: true }).selectOption('RESOLVED')
  await page.getByLabel('Causa raíz', { exact: false }).fill('Diferencia de importe en fixture E2E.')
  await page.getByLabel('Resolución o justificación', { exact: true }).fill('Corregir próxima entrega, conservar evidencia.')
  await page.getByRole('button', { name: 'Guardar cambios', exact: true }).click()
  await expect(page.getByText('Los cambios de la excepción quedaron registrados.')).toBeVisible()
  await page.reload()
  await expect(page.getByLabel('Estado', { exact: true })).toHaveValue('RESOLVED')

  const monitor = await post(page, '/monitors', { name: name + ' sentinel', dataset_id: datasetId, config: { required_columns: ['order_id', 'amount', 'transaction_date'], null_columns: ['order_id'], max_null_rate: 0, max_volume_change_pct: 15, max_age_hours: 48, rules: [{ code: 'DATE_SCHEMA', type: 'schema_type', column: 'transaction_date', parameters: { expected_type: 'DATE' } }] } }, headers)
  await executeUI(page, 'sentinel', monitor.id, 'Evaluar monitor')
  const drift = await page.request.post(`/api/v1/datasets/${datasetId}/versions/upload`, { headers, multipart: { file: { name: 'drift.csv', mimeType: 'text/csv', buffer: Buffer.from('order_id,transaction_date\n001234567,invalid\n1234567,invalid\n') } } })
  expect(drift.status()).toBe(201)
  const sentinel = await executeUI(page, 'sentinel', monitor.id, 'Evaluar monitor')
  const sentinelRun = await (await page.request.get(`/api/v1/runs/${sentinel}`)).json()
  expect(sentinelRun.decision).toBe('ALERT')
  expect(sentinelRun.metrics.failed_checks).toBe(2)
  await downloadExcel(page, sentinel, 'sentinel', testInfo.outputPath('sentinel.xlsx'))
  await page.screenshot({ path: testInfo.outputPath('sentinel-excel-button.png'), fullPage: true })
  expect(errors).toEqual([])
  await fs.writeFile(testInfo.outputPath('runs.json'), JSON.stringify({ intake, recon, sentinel, dataset: dataset.id }, null, 2))
})

test('los módulos existentes siguen ejecutándose desde la interfaz', async ({ page }, testInfo) => {
  await signIn(page)
  for (const [module, config, action] of [['intake', 'demo-contract-orders', 'Validar datos'], ['recon', 'demo-control-payments', 'Conciliar fuentes'], ['sentinel', 'demo-monitor-daily', 'Evaluar monitor']]) {
    await executeUI(page, module, config, action)
    await expect(page.getByRole('button', { name: 'Exportar Excel' })).toBeEnabled()
  }
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/')
  await expect(page.getByRole('heading', { name: 'Centro de control', exact: true })).toBeVisible()
  await page.screenshot({ path: testInfo.outputPath('mobile.png'), fullPage: true })
})
