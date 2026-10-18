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
  for (const context of browser.contexts()) for (const cookie of await context.cookies()) if (cookie.value) privateValues.add(cookie.value)
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
  for (const cookie of await page.context().cookies()) if (cookie.value) privateValues.add(cookie.value)
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

test('references → discover → exact snapshot/review/activate → shifted operational publication without duplicates', async ({ page }, info) => {
  const started = Date.now()
  await page.setViewportSize({ width: 1440, height: 1200 })
  const csrf = await login(page)
  const nonce = randomBytes(6).toString('hex')
  const created = await page.request.post('/api/v1/documents/families', { headers: { 'X-CSRF-Token': csrf }, data: { name: `DOC097 synthetic ${nonce}` } })
  expect(created.status()).toBe(200)
  const family = await created.json()
  await page.goto(`/documents/families/${family.id}`)
  await page.getByRole('tab', { name: 'Configuración', exact: true }).click()
  const builder = page.locator('.visual-template-builder')
  await builder.getByRole('button', { name: 'Crear borrador', exact: true }).click()
  await expect(builder.getByLabel('Nombre del borrador', { exact: true })).toBeVisible()
  const first = await fixture()
  const second = await fixture(true)
  // Two actual inputs in one UI gesture, then a third independent unused variant.
  await builder.getByLabel('Cargar referencias', { exact: true }).setInputFiles([first, second])
  await expect(builder.getByRole('button', { name: 'Seleccionar 000001', exact: true })).toBeVisible({ timeout: 90_000 })
  await builder.getByRole('button', { name: 'Guardar borrador', exact: true }).click()
  await expect(builder.getByText(/Guardado en servidor/)).toBeVisible()
  const draftId = await builder.getByLabel('Borrador guardado', { exact: true }).inputValue()
  expect(draftId).toBeTruthy()
  await page.reload()
  await page.getByRole('tab', { name: 'Configuración', exact: true }).click()
  await expect(builder.getByLabel('Borrador guardado', { exact: true })).toHaveValue(draftId)
  await expect(builder.getByRole('button', { name: 'Seleccionar 000001', exact: true })).toBeVisible({ timeout: 90_000 })
  await expect(builder.getByLabel('Nombre del campo', { exact: true })).toHaveCount(0)
  await expect.poll(async () => { const r = await page.request.get(`/api/v1/documents/families/${family.id}/builder/references?limit=100`); return (await r.json()).items.filter((reference: Json) => reference.status === 'READY').length }, { timeout: 90_000 }).toBe(2)
  let response = page.waitForResponse(r => r.request().method() === 'POST' && new URL(r.url()).pathname.endsWith('/discover'))
  await builder.getByRole('button', { name: 'Analizar documentos y proponer plantilla', exact: true }).click()
  const discovered = await (await response).json()
  expect(discovered.draft.discovered_from).toHaveLength(2)
  expect(discovered.draft.definition.tables).toHaveLength(1)
  // Stable user-facing names remain editable; no field or table was manually marked.
  for (const [before, after] of [['numero_factura', 'invoice'], ['fecha_emision', 'date']]) {
    const inputs = await builder.getByLabel('Nombre del campo', { exact: true }).all()
    const input = (await Promise.all(inputs.map(async element => ({ element, value: await element.inputValue() })))).find(item => item.value === before)?.element
    if (!input) throw new Error(`Discovery omitted ${before}`)
    await input.fill(after)
  }
  const invoiceInput = builder.getByLabel('Nombre del campo', { exact: true }).first()
  const invoiceCard = invoiceInput.locator('xpath=ancestor::fieldset[1]')
  await invoiceCard.getByRole('checkbox', { name: 'Crítico', exact: true }).check()
  await invoiceCard.getByRole('checkbox', { name: 'Clave de negocio', exact: true }).check()
  await expect(invoiceCard.getByLabel('Tipo de dato', { exact: true })).toHaveValue('STRING')
  await invoiceCard.getByLabel('Etiqueta ancla', { exact: true }).fill('MissingInvoice')
  await builder.getByRole('button', { name: 'Probar todas las referencias', exact: true }).click()
  await expect(builder.getByLabel('Corregir valor de este documento: invoice', { exact: true })).toHaveValue('')
  await expect(builder.getByText(/Esperado no localizado/).first()).toBeVisible()
  await builder.getByRole('button', { name: 'Corregir regla para futuros documentos', exact: true }).click()
  await invoiceCard.getByLabel('Etiqueta ancla', { exact: true }).fill('Invoice')
  const holdout = { name: 'unused-variant.pdf', mimeType: 'application/pdf', buffer: Buffer.from(invoicePdf(true).toString().replace('000002', '000003').replace('75.50', '76.50')) }
  await builder.getByLabel('Uso de las nuevas referencias', { exact: true }).selectOption('HOLDOUT')
  await builder.getByLabel('Cargar referencias', { exact: true }).setInputFiles(holdout)
  await expect(builder.getByText(/Variante reservada: permite revisar valores/)).toBeVisible({ timeout: 90_000 })
  await expect.poll(async () => { const r = await page.request.get(`/api/v1/documents/families/${family.id}/builder/references?limit=100`); return (await r.json()).items.filter((reference: Json) => reference.status === 'READY').length }, { timeout: 90_000 }).toBe(3)
  await builder.getByRole('button', { name: 'Guardar borrador', exact: true }).click()
  response = page.waitForResponse(r => r.request().method() === 'POST' && new URL(r.url()).pathname.endsWith('/test'))
  await builder.getByRole('button', { name: 'Probar todas las referencias', exact: true }).click()
  const tested = await (await response).json()
  expect(Object.keys(tested.automatic_results)).toHaveLength(3)
  const refsResponse = await page.request.get(`/api/v1/documents/families/${family.id}/builder/references?limit=100`)
  const references: Json[] = (await refsResponse.json()).items
  for (const [name, isSecond] of [[first.name, false], [second.name, true]] as const) {
    const reference = references.find(r => r.filename === name)!
    oracle(tested.automatic_results[reference.id], isSecond)
  }
  const reserved = references.find(r => r.filename === holdout.name)!
  expect(tested.automatic_results[reserved.id].fields.invoice.value).toBe('000003')
  expect(tested.automatic_results[reserved.id].fields.total.value).toBe('76.50')
  const beforeActivation = await (await page.request.get(`/api/v1/documents/families/${family.id}`)).json()
  expect(beforeActivation.templates).toEqual([])
  expect(beforeActivation.document_count).toBe(0)
  expect(beforeActivation.pending_count).toBe(0)
  expect(beforeActivation.output_datasets).toEqual({})
  await expect(builder.getByRole('button', { name: 'Aprobar y activar esta instantánea', exact: true })).toBeDisabled()
  await expect(builder.getByText(/La cobertura reconocida no demuestra exactitud automática/)).toBeVisible()
  for (const reference of references) {
    await builder.getByLabel('Referencia en revisión', { exact: true }).selectOption(reference.id)
    await expect(builder.getByRole('checkbox', { name: 'Confirmar contra original', exact: true })).toHaveCount(9)
    for (const checkbox of await builder.getByRole('checkbox', { name: 'Confirmar contra original', exact: true }).all()) await checkbox.check()
    await builder.getByLabel('Motivo de la revisión de valores', { exact: true }).fill('Every cell checked against independent complete synthetic invoice oracle')
    await builder.getByRole('button', { name: 'Guardar revisión de este documento', exact: true }).click()
    await expect(builder.getByLabel('Motivo de la revisión de valores', { exact: true })).toHaveValue('')
  }
  response = page.waitForResponse(r => r.request().method() === 'POST' && new URL(r.url()).pathname.endsWith('/activate'))
  await builder.getByRole('button', { name: 'Aprobar y activar esta instantánea', exact: true }).click()
  const activated = await (await response).json()
  expect(activated.definition.fields.find((f: Json) => f.name === 'invoice').visual.label).toBe('Invoice')
  const repeated = await page.request.post(`/api/v1/documents/builder/tests/${tested.id}/activate`, { headers: { 'X-CSRF-Token': csrf } })
  expect((await repeated.json()).id).toBe(activated.id)
  await page.getByRole('button', { name: 'Cargar documentos', exact: true }).click()
  response = page.waitForResponse(r => r.request().method() === 'POST' && new URL(r.url()).pathname.endsWith(`/families/${family.id}/originals`))
  await page.getByRole('dialog').getByLabel('Seleccionar documentos', { exact: true }).setInputFiles(second)
  const received = (await (await response).json()).items[0]
  await expect.poll(async () => (await detail(page, received.id)).extraction?.id, { timeout: 90_000 }).toBeTruthy()
  let secondDetail = await detail(page, received.id)
  oracle(secondDetail.extraction.payload, true)
  expect(secondDetail.template_id).toBe(activated.id)
  await page.goto(`/documents/families/${family.id}?document=${received.id}`)
  const dialog = page.getByRole('dialog', { name: second.name, exact: true })
  await expect(dialog.getByLabel('Valor headers.0.invoice', { exact: true })).toHaveValue('000002')
  for (const checkbox of await dialog.getByRole('checkbox', { name: 'Confirmar valor', exact: true }).all()) await checkbox.check()
  await dialog.getByLabel('Motivo de la revisión', { exact: true }).fill('Shifted second invoice exact header and detail oracle verified')
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
  await info.attach('document-builder-097-oracle.json', { body: JSON.stringify({ nonce, family_id: family.id, draft_id: draftId, tested_snapshot: tested.snapshot_hash, references: references.map(r => ({ id: r.id, sha256: r.sha256, role: r.role })), source: 'synthetic fixtures; not the unavailable four private invoices', second_document_id: received.id, duplicate_document_id: duplicate.id, format: first.mimeType, no_individual_marking: true, all_header_and_detail_values_exact: true, second_layout_shift_points: [15, -23], second_quantity_column_shift_points: 5, publication: secondDetail.publications, elapsed_ms: Date.now() - started }), contentType: 'application/json' })
})
