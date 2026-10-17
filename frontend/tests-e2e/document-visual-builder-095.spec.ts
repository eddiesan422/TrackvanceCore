import { randomBytes } from 'node:crypto'
import { readFile } from 'node:fs/promises'
import { expect, test } from '@playwright/test'
import type { Page } from '@playwright/test'
import type { RecordData as Json } from '../src/api/client'
import { fillSecret } from './identity-helpers'

const privateValues = new Set<string>()
test.use({ trace: 'off', screenshot: 'off', video: 'off', actionTimeout: 30_000, navigationTimeout: 30_000 })
test.setTimeout(420_000)
test.skip(process.env.TV_CORE095_VISUAL_E2E !== 'true', 'Requires explicitly identified synthetic Codex or disposable functional stack')
test.afterEach(async ({ browser }, info) => {
  for (const error of info.errors) for (const key of ['message', 'stack', 'snippet', 'value', 'errorContext'] as const) if (typeof error[key] === 'string') for (const secret of privateValues) error[key] = error[key]!.split(secret).join('[REDACTED]')
  for (const context of browser.contexts()) for (const page of context.pages()) await page.evaluate(() => document.body.replaceChildren()).catch(() => undefined)
  privateValues.clear()
})

function invoicePdf(second = false) {
  const dx = second ? 15 : 0
  const dy = second ? -23 : 0
  const cells: [number, number, string][] = [[60, 720, `Invoice: ${second ? '000002' : '000001'}`], [60, 680, `Date: ${second ? '2026-10-18' : '2026-10-17'}`], [60, 470, `Total: ${second ? '75.50' : '82.00'}`]]
  for (const [y, row] of [[600, ['Code', 'Qty', 'Amount']], [565, ['A100', second ? '4' : '2', second ? '11.25' : '10.25']], [530, ['B200', second ? '1' : '3', second ? '30.50' : '20.50']]] as [number, string[]][]) row.forEach((value, column) => cells.push([[60, 240 + (second ? 5 : 0), 360][column], y, value]))
  const content = cells.map(([x, y, value]) => `BT /F1 18 Tf 1 0 0 1 ${x + dx} ${y + dy} Tm (${value}) Tj ET`).join('\n')
  const objects = ['<< /Type /Catalog /Pages 2 0 R >>', '<< /Type /Pages /Count 1 /Kids [4 0 R] >>', '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>', '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> /Contents 5 0 R >>', `<< /Length ${Buffer.byteLength(content)} >>\nstream\n${content}\nendstream`]
  let result = '%PDF-1.4\n'
  const offsets = [0]
  objects.forEach((object, index) => { offsets.push(Buffer.byteLength(result)); result += `${index + 1} 0 obj\n${object}\nendobj\n` })
  const xref = Buffer.byteLength(result)
  result += `xref\n0 ${offsets.length}\n0000000000 65535 f \n${offsets.slice(1).map(offset => `${String(offset).padStart(10, '0')} 00000 n \n`).join('')}trailer << /Size ${offsets.length} /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF`
  return Buffer.from(result)
}

async function fixture(second = false) {
  const path = process.env[second ? 'TV_CORE095_VISUAL_SECOND_PNG' : 'TV_CORE095_VISUAL_FIRST_PNG']
  return path ? { name: second ? 'visual-second.png' : 'visual-first.png', mimeType: 'image/png', buffer: await readFile(path) } : { name: second ? 'visual-second.pdf' : 'visual-first.pdf', mimeType: 'application/pdf', buffer: invoicePdf(second) }
}

async function login(page: Page) {
  const project = process.env.TV_CORE095_TEST_PROJECT || ''
  expect(project === 'trackvance-codex' || /^trackvance-v070-test-e2e-[a-z0-9-]*[a-f0-9]{12}$/.test(project)).toBe(true)
  expect(['localhost', '127.0.0.1']).toContain(new URL(process.env.TV_E2E_URL || '').hostname)
  await page.goto('/')
  if (project === 'trackvance-codex') {
    const email = process.env.TRACKVANCE_ADMIN_EMAIL
    const password = process.env.TRACKVANCE_ADMIN_PASSWORD
    if (!email || !password) throw new Error('Synthetic credentials must be supplied in process memory')
    privateValues.add(password)
    await page.getByRole('button', { name: 'Tengo una cuenta local', exact: true }).click()
    await page.getByLabel('Usuario o correo', { exact: true }).fill(email)
    await fillSecret(page.getByLabel('Contraseña', { exact: true }), password)
    await page.getByRole('button', { name: 'Iniciar sesión', exact: true }).click()
  } else await page.getByRole('button', { name: 'Entrar al entorno demo', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Centro de control', exact: true })).toBeVisible()
  return String((await (await page.request.get('/api/v1/me')).json()).csrf_token)
}

async function detail(page: Page, id: string) {
  const response = await page.request.get(`/api/v1/documents/originals/${id}`)
  expect(response.status()).toBe(200)
  return response.json() as Promise<Json>
}

function oracle(payload: Json, second = false) {
  expect(Object.fromEntries(Object.entries(payload.fields).map(([key, cell]) => [key, (cell as Json).value]))).toEqual({ invoice: second ? '000002' : '000001', date: second ? '2026-10-18' : '2026-10-17', total: second ? '75.50' : '82.00' })
  expect(payload.tables.detalle.map((row: Json) => ['code', 'qty', 'amount'].map(name => row[name].value))).toEqual(second ? [['A100', '4', '11.25'], ['B200', '1', '30.50']] : [['A100', '2', '10.25'], ['B200', '3', '20.50']])
  expect(payload.table_unparsed.detalle).toEqual([])
}

test('visual invoice: select without regex → repair on viewer → test/review/activate → shifted second invoice → stable publication without duplicates', async ({ page }, info) => {
  const started = Date.now()
  await page.setViewportSize({ width: 1440, height: 1200 })
  const csrf = await login(page)
  const nonce = randomBytes(6).toString('hex')
  const created = await page.request.post('/api/v1/documents/families', { headers: { 'X-CSRF-Token': csrf }, data: { name: `Visual095 synthetic ${nonce}` } })
  expect(created.status()).toBe(200)
  const family = await created.json()
  await page.goto(`/documents/families/${family.id}`)
  await page.getByRole('tab', { name: 'Configuración', exact: true }).click()
  const builder = page.locator('.visual-template-builder')
  const first = await fixture()
  await builder.getByLabel('Cargar ejemplo para constructor visual', { exact: true }).setInputFiles(first)
  await expect(builder.getByRole('button', { name: 'Seleccionar 000001', exact: true })).toBeVisible({ timeout: 90_000 })
  await expect.poll(() => builder.locator('.visual-builder-page img').evaluate((image: HTMLImageElement) => image.complete && image.naturalWidth > 0)).toBe(true)
  for (const value of ['000001', '2026-10-17', '82.00']) await builder.getByRole('button', { name: `Seleccionar ${value}`, exact: true }).click()
  const invoice = builder.getByRole('group', { name: 'Campo 1', exact: true })
  await invoice.getByRole('checkbox', { name: 'Crítico', exact: true }).check()
  await invoice.getByRole('checkbox', { name: 'Clave de negocio', exact: true }).check()
  await expect(invoice.getByLabel('Tipo de dato', { exact: true })).toHaveValue('STRING')
  await builder.getByRole('button', { name: 'Tabla', exact: true }).click()
  await builder.locator('.visual-builder-page').scrollIntoViewIfNeeded()
  const header = await builder.getByRole('button', { name: 'Seleccionar Code', exact: true }).boundingBox()
  const last = await builder.getByRole('button', { name: 'Seleccionar 20.50', exact: true }).boundingBox()
  if (!header || !last) throw new Error('Real OCR/PDF word boxes are required for the visual gesture')
  await page.mouse.move(header.x - 4, header.y - 4)
  await page.mouse.down()
  await page.mouse.move(last.x + last.width + 4, last.y + last.height + 4, { steps: 12 })
  await page.mouse.up()
  await expect(builder.getByLabel('Columna 3: tipo', { exact: true })).toHaveValue('DECIMAL')
  // Deliberately fail a field, then repair by selecting its actual value again.
  await invoice.getByLabel('Etiqueta que identifica el valor', { exact: true }).fill('MissingInvoice')
  let response = page.waitForResponse(result => result.request().method() === 'POST' && new URL(result.url()).pathname.endsWith('/builder/test'))
  await builder.getByRole('button', { name: 'Crear revisión y probar extracción', exact: true }).click()
  const failedExample = await (await response).json()
  await expect.poll(async () => (await detail(page, failedExample.id)).extraction?.payload.fields.invoice.validity, { timeout: 90_000 }).toBe('MISSING')
  await invoice.getByRole('button', { name: 'Volver a marcar este campo', exact: true }).click()
  await builder.getByRole('button', { name: 'Seleccionar 000001', exact: true }).click()
  await expect(invoice.getByLabel('Etiqueta que identifica el valor', { exact: true })).toHaveValue('Invoice')
  response = page.waitForResponse(result => result.request().method() === 'POST' && new URL(result.url()).pathname.endsWith('/builder/test'))
  await builder.getByRole('button', { name: 'Crear revisión y probar extracción', exact: true }).click()
  const tested = await (await response).json()
  expect(tested.id).not.toBe(failedExample.id)
  expect(tested.template_id).not.toBe(failedExample.template_id)
  await expect(builder.getByRole('heading', { name: 'Resultado de esta prueba', exact: true })).toBeVisible({ timeout: 90_000 })
  oracle((await detail(page, tested.id)).extraction.payload)
  await expect(builder.getByRole('textbox', { name: /Patrón/ })).toHaveCount(0)
  await builder.getByRole('button', { name: 'Revisar resultados y activar plantilla', exact: true }).click()
  let dialog = page.getByRole('dialog', { name: first.name, exact: true })
  await expect(dialog.getByLabel('Valor headers.0.invoice', { exact: true })).toHaveValue('000001')
  expect(await dialog.getByRole('checkbox', { name: 'Confirmar valor', exact: true }).count()).toBe(9)
  for (const checkbox of await dialog.getByRole('checkbox', { name: 'Confirmar valor', exact: true }).all()) await checkbox.check()
  await dialog.getByLabel('Motivo de la revisión', { exact: true }).fill('Every header and detail cell matched the independent synthetic invoice oracle')
  await dialog.getByRole('button', { name: 'Guardar revisión y comprobar publicación', exact: true }).click()
  await dialog.getByRole('button', { name: 'Activar esta plantilla probada', exact: true }).click()
  await expect(dialog).toBeHidden()
  const second = await fixture(true)
  await page.getByRole('button', { name: 'Cargar documentos', exact: true }).click()
  response = page.waitForResponse(result => result.request().method() === 'POST' && new URL(result.url()).pathname.endsWith(`/families/${family.id}/originals`))
  await page.getByRole('dialog').getByLabel('Seleccionar documentos', { exact: true }).setInputFiles(second)
  const received = (await (await response).json()).items[0]
  await expect.poll(async () => (await detail(page, received.id)).extraction?.id, { timeout: 90_000 }).toBeTruthy()
  let secondDetail = await detail(page, received.id)
  oracle(secondDetail.extraction.payload, true)
  expect(secondDetail.template_id).toBe(tested.template_id)
  await page.goto(`/documents/families/${family.id}?document=${received.id}`)
  dialog = page.getByRole('dialog', { name: second.name, exact: true })
  await expect(dialog.getByLabel('Valor headers.0.invoice', { exact: true })).toHaveValue('000002')
  for (const checkbox of await dialog.getByRole('checkbox', { name: 'Confirmar valor', exact: true }).all()) await checkbox.check()
  await dialog.getByLabel('Motivo de la revisión', { exact: true }).fill('Second invoice uses shifted geometry and different quantities, amounts, total and date; all values verified')
  await dialog.getByRole('button', { name: 'Guardar revisión y comprobar publicación', exact: true }).click()
  await expect.poll(async () => (await detail(page, received.id)).status).toBe('PUBLISHED')
  secondDetail = await detail(page, received.id)
  oracle(secondDetail.extraction.payload, true)
  expect(secondDetail.publications).toHaveLength(1)
  const repeat = await page.request.post(`/api/v1/documents/families/${family.id}/originals`, { headers: { 'X-CSRF-Token': csrf }, multipart: { files: second } })
  expect(repeat.status()).toBe(200)
  const duplicate = (await repeat.json()).items[0]
  expect(duplicate.status).toBe('DUPLICATE_FILE')
  expect(duplicate.duplicate_of_id).toBe(received.id)
  expect((await detail(page, received.id)).publications).toEqual(secondDetail.publications)
  await info.attach('visual-builder-oracle.json', { body: JSON.stringify({ nonce, family_id: family.id, failed_example_id: failedExample.id, tested_example_id: tested.id, second_document_id: received.id, duplicate_document_id: duplicate.id, format: first.mimeType, no_user_regex: true, all_18_values_exact: true, second_layout_shift_points: [15, -23], second_quantity_column_shift_points: 5, publication: secondDetail.publications, elapsed_ms: Date.now() - started }), contentType: 'application/json' })
})
