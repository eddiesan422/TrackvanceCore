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

async function selectAllIntakeColumns(page: Page, label: string) {
  const dialog = page.getByRole('dialog')
  await dialog.getByRole('button', { name: `${label}: abrir selector`, exact: true }).click()
  const options = dialog.getByRole('group', { name: `Opciones de ${label}`, exact: true })
  await options.getByRole('checkbox', { name: /^Todos \(/ }).check()
  await dialog.getByRole('button', { name: `${label}: cerrar selector`, exact: true }).click()
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
  await expect(page.locator('.results-table').getByText('DATE_NOT_FUTURE', { exact: true })).toBeVisible()
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
  const acceptedDataset = await (await page.request.get(`/api/v1/datasets/${accepted.dataset_id}`)).json()
  await page.goto('/datasets')
  const acceptedRow = page.locator('tbody tr').filter({ has: page.getByRole('link', { name: acceptedDataset.name, exact: true }) })
  await expect(acceptedRow.getByText('Data Intake', { exact: true })).toBeVisible()

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
  await page.getByLabel('Estado de gestión', { exact: true }).selectOption('INVESTIGATING')
  await page.getByRole('button', { name: 'Guardar gestión', exact: true }).click()
  await expect(page.getByText('Los cambios de la excepción quedaron registrados.')).toBeVisible()
  const exceptionDialog = page.getByRole('dialog')
  await exceptionDialog.getByText('Cierre administrativo', { exact: true }).click()
  await exceptionDialog.getByLabel('Decisión administrativa', { exact: true }).selectOption('ACCEPTED')
  await exceptionDialog.getByLabel('Motivo administrativo', { exact: true }).fill('Fixture histórico aceptado para continuar la validación integral.')
  await exceptionDialog.getByRole('button', { name: 'Registrar cierre administrativo', exact: true }).click()
  await expect(page.getByText('Los cambios de la excepción quedaron registrados.')).toBeVisible()
  await page.reload()
  await expect(page.getByText(/decisión administrativa: Aceptada/)).toBeVisible()

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

test('los módulos existentes siguen ejecutándose desde la interfaz sobre una instalación limpia', async ({ page }, testInfo) => {
  const headers = await signIn(page)
  const stamp = Date.now()
  const source = await post(page, '/datasets', { name: `E2E módulos ${stamp} origen` }, headers)
  const target = await post(page, '/datasets', { name: `E2E módulos ${stamp} destino` }, headers)
  for (const [datasetId, filename] of [[source.id, 'origen.csv'], [target.id, 'destino.csv']]) {
    const response = await page.request.post(`/api/v1/datasets/${datasetId}/versions/upload`, {
      headers,
      multipart: { file: { name: filename, mimeType: 'text/csv', buffer: Buffer.from('order_id,amount\nA-01,10.00\n') } },
    })
    expect(response.status(), await response.text()).toBe(201)
  }
  const intake = await post(page, '/intake/contracts', {
    name: `E2E módulos ${stamp} intake`, dataset_id: source.id,
    config: { required_columns: ['order_id'], max_error_rate: 0 },
  }, headers)
  const recon = await post(page, '/recon/controls', {
    name: `E2E módulos ${stamp} recon`, dataset_id: source.id, target_dataset_id: target.id,
    config: {
      key_columns: ['order_id'],
      key_normalization: { trim: false, case: 'NONE', unicode_normalization: 'NONE' },
      comparison_rules: [{ type: 'exact_compare', source_column: 'amount', target_column: 'amount', parameters: {} }],
    },
  }, headers)
  const sentinel = await post(page, '/monitors', {
    name: `E2E módulos ${stamp} sentinel`, dataset_id: source.id,
    config: { required_columns: ['order_id'], max_null_rate: 0, max_volume_change_pct: 100, max_age_hours: 48 },
  }, headers)
  for (const [module, config, action] of [['intake', intake.id, 'Validar datos'], ['recon', recon.id, 'Conciliar fuentes'], ['sentinel', sentinel.id, 'Evaluar monitor']]) {
    await executeUI(page, module, config, action)
    await expect(page.getByRole('button', { name: 'Exportar Excel' })).toBeEnabled()
  }
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/')
  await expect(page.getByRole('heading', { name: 'Centro de control', exact: true })).toBeVisible()
  await page.screenshot({ path: testInfo.outputPath('mobile.png'), fullPage: true })
})

test('un nombre de dataset existente se carga como una versión nueva', async ({ page }) => {
  const headers = await signIn(page)
  const name = `E2E dataset repetido ${Date.now()}`
  const dataset = await post(page, '/datasets', { name }, headers)
  const csv = 'order_id,amount\nA-01,10.50\n'
  const firstUpload = await page.request.post(`/api/v1/datasets/${dataset.id}/versions/upload`, {
    headers,
    multipart: { file: { name: 'primera.csv', mimeType: 'text/csv', buffer: Buffer.from(csv) } },
  })
  expect(firstUpload.status(), await firstUpload.text()).toBe(201)

  await page.goto('/datasets?upload=1')
  const dialog = page.getByRole('dialog')
  const fileName = `${name.replaceAll(' ', '_')}.csv`
  await dialog.locator('input[type=file]').setInputFiles({ name: fileName, mimeType: 'text/csv', buffer: Buffer.from(csv) })
  await expect(dialog.getByText(`Ya existe “${name}”. Este archivo se agregará como una nueva versión inmutable del dataset existente.`)).toBeVisible()
  const versionResponse = page.waitForResponse(response => new URL(response.url()).pathname === `/api/v1/datasets/${dataset.id}/versions/upload` && response.request().method() === 'POST')
  await dialog.getByRole('button', { name: 'Cargar como nueva versión', exact: true }).click()
  expect((await versionResponse).status()).toBe(201)
  await page.waitForURL(new RegExp(`/datasets/${dataset.id}$`))

  const detail = await (await page.request.get(`/api/v1/datasets/${dataset.id}`)).json()
  expect(detail.version_count).toBe(2)
  const collection = await (await page.request.get('/api/v1/datasets')).json()
  expect(collection.items.filter((item: { name: string }) => item.name === name)).toHaveLength(1)
})

test('corrige esquema e identificadores, crea área y usa Todos en Data Intake', async ({ page }) => {
  await signIn(page)
  const stamp = Date.now()
  const name = `E2E esquema editable ${stamp}`
  const area = `Riesgos ${stamp}`
  const csv = [
    'document_number,amount,active,transaction_date',
    '001234,10.50,true,2026-09-01',
    '1234,20.00,false,2026-09-02',
    '',
  ].join('\n')

  await page.goto('/datasets?upload=1')
  let dialog = page.getByRole('dialog')
  await dialog.getByLabel('Nombre del dataset', { exact: true }).fill(name)
  await dialog.locator('input[type=file]').setInputFiles({ name: 'editable.csv', mimeType: 'text/csv', buffer: Buffer.from(csv) })
  await expect(dialog.getByLabel('Tipo de transaction_date')).toHaveValue('DATE')
  await expect(dialog.getByLabel('Otros identificadores por nombre')).toHaveCount(0)
  await dialog.getByLabel('Tipo de transaction_date').selectOption('STRING')
  await dialog.getByRole('button', { name: 'Columnas identificadoras (opcional): abrir selector', exact: true }).click()
  const identifierOptions = dialog.getByRole('group', { name: 'Opciones de Columnas identificadoras (opcional)', exact: true })
  await identifierOptions.getByRole('checkbox', { name: /^document_number \(/ }).check()
  await dialog.getByRole('button', { name: 'Columnas identificadoras (opcional): cerrar selector', exact: true }).click()
  await dialog.getByLabel('Área de negocio', { exact: true }).selectOption('__new_domain__')
  await dialog.getByLabel('Nueva área de negocio', { exact: true }).fill(area)
  await dialog.getByRole('button', { name: 'Cargar y analizar', exact: true }).click()
  await page.waitForURL(/\/datasets\/[^/?]+$/)
  const datasetId = page.url().split('/datasets/')[1]

  let detail = await (await page.request.get(`/api/v1/datasets/${datasetId}`)).json()
  expect(detail.domain).toBe(area)
  expect(detail.origin).toBe('MANUAL')
  expect(detail.origin_label).toBe('Manual')
  let schema = Object.fromEntries(detail.versions[0].schema.map((column: { name: string }) => [column.name, column]))
  expect(schema.document_number).toEqual(expect.objectContaining({ logical_type: 'STRING', semantic_tag: 'IDENTIFIER' }))
  expect(schema.transaction_date.logical_type).toBe('STRING')
  const firstProfile = await (await page.request.get(`/api/v1/dataset-versions/${detail.versions[0].id}/profile`)).json()
  expect(firstProfile.sample[0].document_number).toBe('001234')

  await page.getByRole('button', { name: 'Nueva versión', exact: true }).click()
  dialog = page.getByRole('dialog')
  await dialog.locator('input[type=file]').setInputFiles({ name: 'editable-v2.csv', mimeType: 'text/csv', buffer: Buffer.from(csv) })
  await expect(dialog.getByLabel('Tipo de active')).toHaveValue('STRING')
  await dialog.getByLabel('Tipo de active').selectOption('BOOLEAN')
  await dialog.getByLabel('Tipo de transaction_date').selectOption('STRING')
  await dialog.getByRole('button', { name: 'Columnas identificadoras (opcional): abrir selector', exact: true }).click()
  await dialog.getByRole('group', { name: 'Opciones de Columnas identificadoras (opcional)', exact: true })
    .getByRole('checkbox', { name: /^document_number \(/ }).check()
  await dialog.getByRole('button', { name: 'Columnas identificadoras (opcional): cerrar selector', exact: true }).click()
  const versionUpload = page.waitForResponse(response =>
    new URL(response.url()).pathname === `/api/v1/datasets/${datasetId}/versions/upload`
      && response.request().method() === 'POST',
  )
  await dialog.getByRole('button', { name: 'Cargar y analizar', exact: true }).click()
  expect((await versionUpload).status()).toBe(201)
  await expect(dialog).not.toBeVisible()

  detail = await (await page.request.get(`/api/v1/datasets/${datasetId}`)).json()
  expect(detail.version_count).toBe(2)
  schema = Object.fromEntries(detail.versions[0].schema.map((column: { name: string }) => [column.name, column]))
  expect(schema.document_number).toEqual(expect.objectContaining({ logical_type: 'STRING', semantic_tag: 'IDENTIFIER' }))
  expect(schema.active.logical_type).toBe('BOOLEAN')
  expect(schema.transaction_date.logical_type).toBe('STRING')

  await page.goto('/datasets')
  const datasetRow = page.locator('tbody tr').filter({ has: page.getByRole('link', { name, exact: true }) })
  await expect(datasetRow.getByText('Manual', { exact: true })).toBeVisible()
  await expect(datasetRow.getByText(area, { exact: true })).toBeVisible()

  await page.goto('/intake')
  await page.getByRole('button', { name: 'Nuevo contrato', exact: true }).click()
  dialog = page.getByRole('dialog')
  await dialog.getByLabel('Nombre del contrato', { exact: true }).fill(`${name} contrato`)
  await dialog.getByLabel('Dataset', { exact: true }).selectOption(datasetId)
  for (const label of ['Columnas obligatorias', 'Columnas sin duplicados', 'Columnas numéricas', 'Columnas con valores positivos']) {
    await selectAllIntakeColumns(page, label)
  }
  const contractResponse = page.waitForResponse(response =>
    new URL(response.url()).pathname === '/api/v1/intake/contracts'
      && response.request().method() === 'POST',
  )
  await dialog.getByRole('button', { name: 'Crear contrato', exact: true }).click()
  const created = await contractResponse
  expect(created.status(), await created.text()).toBe(201)
  const contract = await created.json()
  expect(contract.config.required_columns).toEqual(['document_number', 'amount', 'active', 'transaction_date'])
  expect(contract.config.unique_columns).toEqual(['document_number', 'amount', 'active', 'transaction_date'])
  expect(contract.config.numeric_columns).toEqual(['document_number', 'amount', 'active', 'transaction_date'])
  expect(contract.config.positive_columns).toEqual(['amount'])
})

test('la carga JSON detecta formato, aplana columnas y conserva metadata', async ({ page }, testInfo) => {
  await signIn(page)
  const stamp = Date.now()
  const payload = JSON.stringify({
    records: [
      { record_key: `A-${stamp}`, amount: 10.5, customer: { name: 'Ángela' } },
      { record_key: `B-${stamp}`, amount: 20, customer: { name: 'Cliente 12' } },
    ],
    source: 'e2e-local',
  })
  await page.goto('/datasets?upload=1')
  const dialog = page.getByRole('dialog')
  const inspectionResponse = page.waitForResponse(response =>
    new URL(response.url()).pathname === '/api/v1/datasets/uploads/inspect'
      && response.request().method() === 'POST',
  )
  await dialog.locator('input[type=file]').setInputFiles({
    name: `multiformato_${stamp}.json`,
    mimeType: 'application/json',
    buffer: Buffer.from(payload),
  })

  expect((await inspectionResponse).status()).toBe(200)
  await expect(dialog.getByText('JSON tabular', { exact: true })).toBeVisible()
  await expect(dialog.getByText('customer.name', { exact: true })).toBeVisible()
  await expect(dialog.getByText(/columnas · 2 filas/)).toBeVisible()
  await page.screenshot({ path: testInfo.outputPath('json-upload-inspection.png'), fullPage: true })

  await dialog.getByRole('button', { name: 'Cargar y analizar', exact: true }).click()
  await page.waitForURL(/\/datasets\/[^/?]+$/)
  const datasetId = page.url().split('/datasets/')[1]
  const detail = await (await page.request.get(`/api/v1/datasets/${datasetId}`)).json()
  const version = detail.versions[0]
  expect(version.ingestion_metadata.source_format).toBe('JSON')
  expect(version.ingestion_metadata.root_key).toBe('records')
  const profile = await (await page.request.get(`/api/v1/dataset-versions/${version.id}/profile`)).json()
  expect(profile.sample[0]['customer.name']).toBe('Ángela')
})

test('la tabla de datasets ordena sus columnas en ambas direcciones', async ({ page }) => {
  await signIn(page)
  await page.goto('/datasets')
  await expect(page.locator('tbody tr').first()).toBeVisible()

  for (const column of ['Área', 'Registros', 'Versiones', 'Estado', 'Última actualización']) {
    const ascending = page.getByRole('button', { name: `Ordenar ${column} ascendente` })
    await ascending.click()
    const descending = page.getByRole('button', { name: `Ordenar ${column} descendente` })
    await expect(descending.locator('xpath=..')).toHaveAttribute('aria-sort', 'ascending')

    if (column === 'Registros') {
      const values = (await page.locator('tbody tr td:nth-child(4)').allTextContents()).map(value => Number(value.replace(/\D/g, '')))
      expect(values).toEqual([...values].sort((left, right) => left - right))
    }

    await descending.click()
    await expect(page.getByRole('button', { name: `Ordenar ${column} ascendente` }).locator('xpath=..')).toHaveAttribute('aria-sort', 'descending')
  }
})

test('el Centro de control permite filtrar y actuar sobre la operación', async ({ page }, testInfo) => {
  await signIn(page)

  await expect(page.getByLabel('Filtrar por periodo')).toBeVisible()
  await expect(page.getByLabel('Filtrar por dataset')).toBeVisible()
  await expect(page.getByLabel('Filtrar por módulo')).toBeVisible()
  await expect(page.getByLabel('Filtrar por estado')).toBeVisible()
  await expect(page.getByLabel('Filtrar por criticidad')).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Requiere tu atención', exact: true })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Salud de los datos', exact: true })).toBeVisible()
  await expect(page.getByRole('img', { name: 'Evolución de salud por módulo' })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Datasets que necesitan atención', exact: true })).toBeVisible()

  const dashboardResponse = page.waitForResponse(response => {
    const url = new URL(response.url())
    return response.request().method() === 'GET'
      && url.pathname === '/api/v1/dashboard'
      && url.searchParams.get('module') === 'sentinel'
  })
  await page.getByLabel('Filtrar por módulo').selectOption('sentinel')
  expect((await dashboardResponse).status()).toBe(200)
  await expect(page.getByLabel('Filtrar por módulo')).toHaveValue('sentinel')
  await expect(page).toHaveURL(/(?:\?|&)module=sentinel(?:&|$)/)

  await page.screenshot({ path: testInfo.outputPath('operational-dashboard.png'), fullPage: true })
})
