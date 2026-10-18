import { randomBytes } from 'node:crypto'
import { expect, test } from '@playwright/test'
import type { Page } from '@playwright/test'
import type { RecordData as Json } from '../src/api/client'
import { fillSecret } from './identity-helpers'

const privateValues = new Set<string>()
test.afterEach(async ({ browser }, info) => {
  for (const error of info.errors) for (const key of ['message', 'stack', 'snippet', 'value', 'errorContext'] as const) {
    if (typeof error[key] === 'string') for (const secret of privateValues) error[key] = error[key]!.split(secret).join('[REDACTED]')
  }
  for (const context of browser.contexts()) for (const page of context.pages()) {
    await page.evaluate(() => document.body.replaceChildren()).catch(() => undefined)
  }
  privateValues.clear()
})

// These tests create only explicitly named synthetic resources in Codex. Never
// capture DOM/request artifacts containing local-account credential material.
test.use({ trace: 'off', screenshot: 'off', video: 'off', actionTimeout: 30_000, navigationTimeout: 30_000 })
test.setTimeout(480_000)
test.skip(process.env.TV_CORE095_DOCUMENT_E2E !== 'true', 'Requires the updated synthetic Codex stack and document oracle receiver')

function knownPdf(invoice: string) {
  const lines = [`Invoice: ${invoice}`, 'Total: 1234.50', 'Date: 2026-10-01', 'ITEM SKU1 10.25', 'ITEM SKU2 20.50']
  const content = `BT /F1 18 Tf 60 710 Td 30 TL ${lines.map(line => `(${line}) Tj T*`).join(' ')} ET`
  const objects = ['<< /Type /Catalog /Pages 2 0 R >>', '<< /Type /Pages /Count 1 /Kids [4 0 R] >>',
    '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
    '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> /Contents 5 0 R >>',
    `<< /Length ${Buffer.byteLength(content)} >>\nstream\n${content}\nendstream`]
  let text = '%PDF-1.4\n'
  const offsets = [0]
  for (let i = 0; i < objects.length; i++) { offsets.push(Buffer.byteLength(text)); text += `${i + 1} 0 obj\n${objects[i]}\nendobj\n` }
  const xref = Buffer.byteLength(text)
  text += `xref\n0 ${offsets.length}\n0000000000 65535 f \n${offsets.slice(1).map(n => `${String(n).padStart(10, '0')} 00000 n \n`).join('')}trailer << /Size ${offsets.length} /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF`
  return Buffer.from(text)
}

async function login(page: Page) {
  const project = process.env.TV_CORE095_TEST_PROJECT || ''
  expect(project === 'trackvance-codex' || /^trackvance-v070-test-e2e-[a-z0-9-]*[a-f0-9]{12}$/.test(project), 'Explicit synthetic-stack identity is mandatory').toBe(true)
  const url = new URL(process.env.TV_E2E_URL || '')
  expect(['127.0.0.1', 'localhost']).toContain(url.hostname)
  const username = process.env.TRACKVANCE_ADMIN_EMAIL
  const password = process.env.TRACKVANCE_ADMIN_PASSWORD
  await page.goto('/')
  if (project === 'trackvance-codex') {
    if (!username || !password) throw new Error('Synthetic Codex credentials must be provided through the process environment')
    await page.getByRole('button', { name: 'Tengo una cuenta local', exact: true }).click()
    await page.getByLabel('Usuario o correo', { exact: true }).fill(username)
    privateValues.add(password)
    await fillSecret(page.getByLabel('Contraseña', { exact: true }), password)
    await page.getByRole('button', { name: 'Iniciar sesión', exact: true }).click()
  } else await page.getByRole('button', { name: 'Entrar al entorno demo', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Centro de control', exact: true })).toBeVisible()
  const me = await (await page.request.get('/api/v1/me')).json()
  return String(me.csrf_token)
}

async function call(page: Page, csrf: string, path: string, data?: Json, status = 200) {
  const response = data === undefined ? await page.request.get(`/api/v1${path}`) : await page.request.post(`/api/v1${path}`, { data, headers: { 'X-CSRF-Token': csrf } })
  expect(response.status(), `HTTP status for ${path.split('?')[0]}`).toBe(status)
  return response.json() as Promise<Json>
}

async function activateDraft(page: Page, csrf: string, family: Json, definition?: Json, existingDraft?: Json, sourceTemplate?: Json) {
  await page.goto(`/documents/families/${family.id}?view=references`)
  const builder = page.locator('.visual-template-builder')
  const before = await call(page, csrf, `/documents/families/${family.id}`)
  let draft = existingDraft
  if (draft) {
    await builder.getByLabel('Borrador guardado', { exact: true }).selectOption(draft.id)
  } else {
    if (sourceTemplate) await builder.getByLabel('Base de un nuevo borrador', { exact: true }).selectOption(sourceTemplate.id)
    const created = page.waitForResponse(r => r.request().method() === 'POST' && new URL(r.url()).pathname.endsWith(`/families/${family.id}/builder/drafts`))
    await builder.getByRole('button', { name: 'Crear borrador', exact: true }).click()
    draft = await (await created).json()
  }
  await expect(builder.getByLabel('Borrador guardado', { exact: true })).toHaveValue(draft!.id)
  await expect(builder.getByLabel('Nombre del borrador', { exact: true })).toHaveValue(draft!.name)
  if (definition) {
    await builder.getByText('Configuración técnica avanzada del borrador', { exact: true }).click()
    await builder.getByLabel('Definición avanzada del borrador', { exact: true }).fill(JSON.stringify(definition))
    await builder.getByRole('button', { name: 'Aplicar cambios avanzados al borrador', exact: true }).click()
  }
  let references = (await call(page, csrf, `/documents/families/${family.id}/builder/references?limit=100`)).items as Json[]
  if (!references.length) {
    await builder.getByLabel('Cargar referencias', { exact: true }).setInputFiles({ name: 'D097-configuration.pdf', mimeType: 'application/pdf', buffer: knownPdf('000000') })
    await expect(builder.getByRole('checkbox', { name: /D097-configuration.pdf/ })).toBeVisible()
    await builder.getByLabel('Uso de las nuevas referencias', { exact: true }).selectOption('HOLDOUT')
    await builder.getByLabel('Cargar referencias', { exact: true }).setInputFiles({ name: 'D097-unused-holdout.pdf', mimeType: 'application/pdf', buffer: knownPdf('000099') })
    await expect(builder.getByRole('checkbox', { name: /D097-unused-holdout.pdf/ })).toBeVisible()
  }
  await expect(builder.locator('.visual-builder-reference-list input[type="checkbox"]')).toHaveCount(2)
  for (const checkbox of await builder.locator('.visual-builder-reference-list input[type="checkbox"]').all()) await checkbox.check()
  await builder.getByRole('button', { name: 'Guardar borrador', exact: true }).click()
  await expect(builder.getByText(/Guardado en servidor/)).toBeVisible()
  const stored = await call(page, csrf, `/documents/builder/drafts/${draft!.id}`)
  const unactivated = await call(page, csrf, `/documents/families/${family.id}`)
  expect(unactivated.templates).toHaveLength(before.templates.length)
  expect(unactivated.document_count).toBe(before.document_count)
  await expect.poll(async () => (await call(page, csrf, `/documents/families/${family.id}/builder/references?limit=100`)).items.filter((r: Json) => r.status === 'READY').length, { timeout: 90_000 }).toBe(2)
  references = (await call(page, csrf, `/documents/families/${family.id}/builder/references?limit=100`)).items
  expect(references.map(r => r.role).sort()).toEqual(['CONFIGURATION', 'HOLDOUT'])
  expect(new Set(references.map(r => r.sha256)).size).toBe(2)
  const response = page.waitForResponse(r => r.request().method() === 'POST' && new URL(r.url()).pathname.endsWith(`/builder/drafts/${draft!.id}/test`))
  await builder.getByRole('button', { name: 'Probar todas las referencias', exact: true }).click()
  const tested = await (await response).json()
  for (const reference of references) {
    const payload = tested.automatic_results[reference.id]
    const values: Json = { invoice: reference.role === 'HOLDOUT' ? '000099' : '000000', total: '1234.50', date: '2026-10-01' }
    expect(Object.fromEntries(Object.entries(payload.fields).map(([name, cell]) => [name, (cell as Json).value]))).toEqual(Object.fromEntries(stored.definition.fields.map((field: Json) => [field.name, values[field.name]])))
    if (stored.definition.tables.length) {
      expect(payload.tables.details.map((row: Json) => [row.sku.value, row.amount.value])).toEqual([['SKU1', '10.25'], ['SKU2', '20.50']])
      expect(payload.table_unparsed.details).toEqual([])
    }
    await builder.getByLabel('Referencia en revisión', { exact: true }).selectOption(reference.id)
    await expect(builder.getByRole('checkbox', { name: 'Confirmar contra original', exact: true })).toHaveCount(stored.definition.fields.length + (stored.definition.tables.length ? 4 : 0))
    for (const checkbox of await builder.getByRole('checkbox', { name: 'Confirmar contra original', exact: true }).all()) await checkbox.check()
    await builder.getByLabel('Motivo de la revisión de valores', { exact: true }).fill('Every header and detail cell checked against the complete independent generated invoice oracle.')
    await builder.getByRole('button', { name: 'Guardar revisión de este documento', exact: true }).click()
    await expect(builder.getByLabel('Motivo de la revisión de valores', { exact: true })).toHaveValue('')
  }
  const activated = page.waitForResponse(r => r.request().method() === 'POST' && new URL(r.url()).pathname.endsWith(`/builder/tests/${tested.id}/activate`))
  await builder.getByRole('button', { name: 'Aprobar y activar esta instantánea', exact: true }).click()
  const template = await (await activated).json()
  expect(template.id).toBeTruthy()
  expect((await call(page, csrf, `/documents/families/${family.id}`)).templates).toHaveLength(before.templates.length + 1)
  return template as Json
}

async function buildFamily(page: Page, csrf: string, nonce: string, mode = 'INDIVIDUAL') {
  await page.getByRole('link', { name: 'Documentos', exact: true }).click()
  await page.getByRole('button', { name: 'Nueva familia', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: 'Nueva familia documental', exact: true })
  await dialog.getByLabel('Nombre', { exact: true }).fill(`D095 synthetic ${nonce}`)
  await dialog.getByLabel('Descripción', { exact: true }).fill('Known generated document corpus. Synthetic Codex fixture only.')
  const familyResponse = page.waitForResponse(r => r.request().method() === 'POST' && new URL(r.url()).pathname === '/api/v1/documents/families')
  await dialog.getByRole('button', { name: 'Crear familia', exact: true }).click()
  const family = await (await familyResponse).json()
  await expect(dialog).toBeHidden({ timeout: 30_000 })
  await page.getByPlaceholder('Buscar familias documentales', { exact: true }).fill(nonce)
  const familyCard = page.locator(`a[href="/documents/families/${family.id}"]`)
  await expect(familyCard).toBeVisible()
  await expect(familyCard).toHaveAccessibleName(new RegExp(`D095 synthetic ${nonce}`))
  await familyCard.click()
  await expect(page).toHaveURL(new RegExp(`/documents/families/${family.id}$`))
  const template = await activateDraft(page, csrf, family, {
    fields: [{ name: 'invoice', label: 'Invoice', pattern: '([0-9]+)', critical: true, business_key: true },
      { name: 'total', label: 'Total', pattern: '([0-9]+\\.[0-9]+)', logical_type: 'DECIMAL', decimal_format_explicit: true },
      { name: 'date', label: 'Date', pattern: '([0-9-]+)', logical_type: 'DATE' }],
    tables: [{ name: 'details', row_label: 'ITEM', row_pattern: 'ITEM (?P<sku>\\S+) (?P<amount>\\d+\\.\\d+)', fields: [{ name: 'sku' }, { name: 'amount', logical_type: 'DECIMAL', decimal_format_explicit: true }] }],
    business_period_field: 'date', publication: { mode, ...(mode === 'SCHEDULED' ? { starts_at: new Date(Date.now() - 1000).toISOString(), interval_seconds: 60, wait_seconds: 0 } : {}) },
  })
  return { family, template }
}

async function upload(page: Page, name: string, invoice: string, example = false, template?: Json) {
  if (example) {
    await page.getByRole('tab', { name: 'Configuración', exact: true }).click()
    const revision = template
      ? page.locator('.document-template').filter({ has: page.getByText(`Revisión ${template.revision}`, { exact: true }) })
      : page.locator('.document-template').first()
    // Refetch after creating a revision is asynchronous. Select its visible
    // revision explicitly; first() could test and activate the preceding policy.
    await revision.getByRole('button', { name: 'Probar con documento de ejemplo', exact: true }).click()
  } else await page.getByRole('button', { name: 'Cargar documentos', exact: true }).click()
  const response = page.waitForResponse(r => r.request().method() === 'POST' && /\/documents\/families\/[^/]+\/originals$/.test(new URL(r.url()).pathname))
  await page.getByRole('dialog').getByLabel('Seleccionar documentos', { exact: true }).setInputFiles({ name, mimeType: 'application/pdf', buffer: knownPdf(invoice) })
  const received = await (await response).json()
  expect(received.items).toHaveLength(1)
  if (template) expect(received.items[0].template_id).toBe(template.id)
  await page.getByRole('tab', { name: 'Documentos', exact: true }).click()
  return received.items[0] as Json
}

async function review(page: Page, document: Json, activate = false) {
  if (document.is_example) {
    // Historical examples remain reviewable through their explicit evidence
    // link; the operational table deliberately excludes them in 0.9.7.
    await page.goto(`/documents/families/${document.family_id}?document=${document.id}`)
  } else {
    const row = page.getByRole('row').filter({ hasText: document.filename })
    await row.getByRole('button', { name: 'Revisar documento', exact: true }).click()
  }
  const dialog = page.getByRole('dialog', { name: document.filename, exact: true })
  await expect(dialog.getByLabel('Valor headers.0.invoice', { exact: true })).toBeVisible({ timeout: 90_000 })
  await dialog.getByRole('button', { name: 'invoice', exact: true }).click()
  await expect(dialog.locator('.document-origin-region')).toBeVisible()
  await expect.poll(() => dialog.locator('.document-source img').evaluate((image: HTMLImageElement) => image.complete && image.naturalWidth > 0)).toBe(true)
  for (const checkbox of await dialog.getByRole('checkbox', { name: 'Confirmar valor', exact: true }).all()) await checkbox.check()
  await dialog.getByLabel('Motivo de la revisión', { exact: true }).fill('Known fixture fields and every table row checked against independent generated oracle.')
  const response = page.waitForResponse(r => r.request().method() === 'POST' && new URL(r.url()).pathname === `/api/v1/documents/originals/${document.id}/review`)
  await dialog.getByRole('button', { name: 'Guardar revisión y comprobar publicación', exact: true }).click()
  const reviewed = await (await response).json()
  expect(reviewed.issues).toEqual([])
  if (activate) {
    await dialog.getByRole('button', { name: 'Activar esta plantilla probada', exact: true }).click()
    await expect(dialog).not.toBeVisible()
  } else await dialog.getByRole('button', { name: 'Cerrar ventana', exact: true }).click()
  return reviewed
}

async function prepareDelivery(page: Page, csrf: string, family: Json, initialBatch: Json, nonce: string, invoices: string[]) {
  // Create/link native Intake through the browser wizard. No copied cases,
  // custom queue, fake approved execution or direct DB writes are involved.
  await page.getByRole('tab', { name: 'Configuración', exact: true }).click()
  const wizard = page.locator('section').filter({ has: page.getByRole('heading', { name: 'Asistente de flujo posterior', exact: true }) }).last()
  await wizard.getByLabel('Finalizar el flujo en', { exact: true }).selectOption('VALIDATION')
  await wizard.getByRole('button', { name: 'Continuar', exact: true }).click()
  await wizard.getByRole('button', { name: 'Continuar', exact: true }).click()
  const pipelineResponse = page.waitForResponse(r => r.request().method() === 'POST' && new URL(r.url()).pathname.endsWith(`/documents/families/${family.id}/pipeline-draft`))
  await wizard.getByRole('button', { name: 'Guardar borrador y preparar prueba', exact: true }).click()
  const prepared = await (await pipelineResponse).json()
  const bindings = prepared.draft.definition.publication.intake_configuration_ids as Record<string, string>
  await activateDraft(page, csrf, family, undefined, prepared.draft)
  const control = process.env.TV_CORE095_DOCUMENT_RECEIVER_CONTROL_URL
  const receiver = process.env.TV_CORE095_DOCUMENT_RECEIVER_URL
  if (!control || !receiver) throw new Error('Document oracle control/publication endpoints must be configured')
  const registered = await page.request.post(`${control}/documents/${nonce}/oracle`, { data: { documents: invoices.map(invoice => ({ invoice, total: '1234.50', date: '2026-10-01' })) } })
  expect(registered.status()).toBe(201)
  const automationIds: Record<string, string> = {}
  for (const structure of ['headers', 'details']) {
    const destination = await call(page, csrf, '/delivery/destinations', { name: `D095 synthetic ${structure} ${nonce}`, sink_type: 'REST', options: { url: `${receiver}/documents/${nonce}/${structure}`, auth: 'NONE', idempotency_supported: true } }, 201)
    const fields = structure === 'headers' ? [['document_id', 'STRING'], ['invoice', 'STRING'], ['total', 'DECIMAL'], ['date', 'DATE']] : [['document_id', 'STRING'], ['sku', 'STRING'], ['amount', 'DECIMAL']]
    const draft = { dataset_version_id: initialBatch.published_versions[structure], destination_id: destination.id, destination_version_id: destination.destination_version_id,
      target: { mode: 'REST', batch_rows: 10, confirmation_path: 'accepted', confirmation_value: true, rows_path: 'rows', batch_id_path: 'batch_id', payload_hash_path: 'payload_sha256' },
      columns: fields.map(([name, type], ordinal) => ({ source_name: name, target_name: name, target_type: type, ordinal, nullable: false, ...(type === 'DECIMAL' ? { precision: 38, scale: 2 } : {}) })) }
    expect((await call(page, csrf, '/delivery/preflight', draft)).status).toBe('PASS')
    const config = await call(page, csrf, '/delivery/configurations', { name: `D095 synthetic delivery ${structure} ${nonce}`, ...draft }, 201)
    const automation = await call(page, csrf, '/delivery/automations', { name: `D095 synthetic chain ${structure} ${nonce}`, configuration_id: config.id, settings: { mode: 'CHAINED', timezone: 'America/Bogota', starts_at: new Date().toISOString(), source_policy: 'INTAKE_OUTPUT', intake_configuration_id: bindings[structure] } }, 201)
    automationIds[structure] = automation.id
  }
  // Return through the visual guided procedure and explicitly enable Delivery.
  await page.reload()
  await page.getByRole('tab', { name: 'Configuración', exact: true }).click()
  await wizard.getByLabel('Finalizar el flujo en', { exact: true }).selectOption('DELIVERY')
  await wizard.getByRole('button', { name: 'Continuar', exact: true }).click()
  for (const structure of ['headers', 'details']) await wizard.getByLabel(`Intake para ${structure}`, { exact: true }).selectOption(bindings[structure])
  await wizard.getByRole('checkbox', { name: 'Activar explícitamente entrega automática con aprobaciones vigentes', exact: true }).check()
  for (const structure of ['headers', 'details']) await wizard.getByLabel(`Automatización de Delivery para ${structure}`, { exact: true }).selectOption(automationIds[structure])
  await wizard.getByRole('button', { name: 'Continuar', exact: true }).click()
  const finalResponse = page.waitForResponse(r => r.request().method() === 'POST' && new URL(r.url()).pathname.endsWith(`/documents/families/${family.id}/pipeline-draft`))
  await wizard.getByRole('button', { name: 'Guardar borrador y preparar prueba', exact: true }).click()
  const finalDraft = (await (await finalResponse).json()).draft
  const finalTemplate = await activateDraft(page, csrf, family, undefined, finalDraft)
  return { finalTemplate, bindings, automationIds, control: control! }
}

test('document browser individual flow → exact native approved output → verified Delivery receiver', async ({ page, browser }, info) => {
  const started = Date.now()
  const csrf = await login(page)
  const nonce = randomBytes(6).toString('hex')
  const { family, template } = await buildFamily(page, csrf, nonce)
  const example = await upload(page, `D095-example-${nonce}.pdf`, '000000', true, template)
  await review(page, example, true)
  const bootstrap = await upload(page, `D095-bootstrap-${nonce}.pdf`, '000000')
  const first = await review(page, bootstrap)
  expect(first.status).toBe('PUBLISHED')
  const initialBatch = await call(page, csrf, `/documents/batches/${first.publications[0].batch_id}`)
  const sourceFamily = await call(page, csrf, `/documents/families/${family.id}`)
  expect(Object.keys(sourceFamily.output_datasets).sort()).toEqual(['details', 'headers'])
  const { finalTemplate, bindings, automationIds, control } = await prepareDelivery(page, csrf, family, initialBatch, nonce, ['000001'])
  const finalExample = await upload(page, `D095-delivery-example-${nonce}.pdf`, '000001', true, finalTemplate)
  await review(page, finalExample, true)
  const finalOriginal = await upload(page, `D095-delivery-${nonce}.pdf`, '000001')
  const final = await review(page, finalOriginal)
  expect(final.status).toBe('PUBLISHED')
  await expect.poll(async () => (await (await page.request.get(`${control}/documents/${nonce}/receipt`)).json()).status, { timeout: 180_000 }).toBe('PASS')
  const received = await (await page.request.get(`${control}/documents/${nonce}/receipt`)).json()
  expect(received).toEqual(expect.objectContaining({ header_rows: 1, detail_rows: 2, all_values_exact: true, header_detail_identity_consistent: true }))
  for (const structure of ['headers', 'details']) {
    const history = await call(page, csrf, `/delivery/automations/${automationIds[structure]}/occurrences`)
    const successful = history.items.find((o: Json) => o.status === 'SUCCESS')
    expect(successful).toBeTruthy()
    const origin = await call(page, csrf, `/runs/${successful.source_run_id}`)
    expect(origin.module).toBe('intake')
    expect(origin.status).toBe('SUCCESS')
    expect(origin.decision).toBe('APPROVED')
    expect(origin.config_id || origin.configuration_id).toBe(bindings[structure])
  }
  // Independent originals authorization is exercised in a second real browser
  // session whose synthetic role can read the dataset but has no document grant.
  const readerRole = await call(page, csrf, '/roles', { name: `D095 synthetic reader ${nonce}`, permissions: ['datasets:read', 'artifacts:download'] }, 201)
  const reader = await call(page, csrf, '/users', { first_name: 'Synthetic', last_name: 'Document Reader', username: `d095_${nonce}`, email: `d095_${nonce}@example.test`, role_id: readerRole.id }, 201)
  const readerContext = await browser.newContext({ baseURL: process.env.TV_E2E_URL })
  try {
    const readerPage = await readerContext.newPage()
    await readerPage.goto('/')
    await readerPage.getByRole('button', { name: 'Tengo una cuenta local', exact: true }).click()
    await readerPage.getByLabel('Usuario o correo', { exact: true }).fill(reader.user.username)
    privateValues.add(reader.temporary_credentials.temporary_password)
    await fillSecret(readerPage.getByLabel('Contraseña', { exact: true }), reader.temporary_credentials.temporary_password)
    await readerPage.getByRole('button', { name: 'Iniciar sesión', exact: true }).click()
    await expect(readerPage.getByRole('heading', { name: 'Cambia tu contraseña', exact: true })).toBeVisible()
    const localPassword = 'D095 fixture ' + randomBytes(24).toString('base64url')
    privateValues.add(localPassword)
    await fillSecret(readerPage.getByLabel('Nueva contraseña', { exact: true }), localPassword)
    await fillSecret(readerPage.getByLabel('Confirmar contraseña', { exact: true }), localPassword)
    const passwordChanged = readerPage.waitForResponse(response => new URL(response.url()).pathname === '/api/v1/auth/first-login/change-password' && response.request().method() === 'POST')
    await readerPage.getByRole('button', { name: 'Guardar y continuar', exact: true }).click()
    expect((await passwordChanged).status()).toBe(200)
    await readerPage.goto(`/datasets/${sourceFamily.output_datasets.headers}`)
    await expect(readerPage.getByRole('heading', { name: `${sourceFamily.name} · headers`, exact: true })).toBeVisible()
    await expect(readerPage.getByRole('link', { name: 'Documentos', exact: true })).toHaveCount(0)
    const denied = await readerPage.goto(`/api/v1/documents/originals/${final.id}/file`)
    expect(denied?.status()).toBe(403)
    await expect(readerPage.locator('body')).toContainText('PERMISSION_DENIED')
    const deniedGlobal = await readerPage.request.get(`/api/v1/documents/originals?search=${final.id}`)
    expect(deniedGlobal.status()).toBe(403)
    const deniedBody = await deniedGlobal.json()
    expect(deniedBody).not.toHaveProperty('total')
    expect(deniedBody).not.toHaveProperty('items')
  } finally {
    await readerContext.close()
    const latestReader = await call(page, csrf, `/users/${reader.user.id}`)
    const removal = await page.request.delete(`/api/v1/users/${reader.user.id}`, { data: { version: latestReader.version }, headers: { 'X-CSRF-Token': csrf } })
    expect(removal.status()).toBe(200)
  }
  await page.goto('/documents')
  await page.getByRole('tab', { name: 'Todos los documentos', exact: true }).click()
  const global = page.getByRole('region', { name: 'Todos los documentos', exact: true })
  await global.getByRole('textbox', { name: 'Buscar nombre, referencia, identificador extraído o familia', exact: true }).fill(final.id)
  await expect(global.locator('tbody tr')).toHaveCount(1)
  const finalBatch = await call(page, csrf, `/documents/batches/${final.publications[0].batch_id}`)
  for (const structure of ['headers', 'details']) {
    await expect(global.locator(`a[href="/datasets/${sourceFamily.output_datasets[structure]}?version=${finalBatch.published_versions[structure]}"]`)).toBeVisible()
  }
  await global.getByRole('link', { name: `Lote ${finalBatch.id.slice(0, 8)}`, exact: true }).click()
  await expect(page).toHaveURL(new RegExp(`/documents/families/${family.id}\\?batch=${finalBatch.id}$`))
  await expect(page.getByRole('tab', { name: 'Lotes y resultados', exact: true })).toHaveAttribute('aria-selected', 'true')
  await expect(page.getByRole('link', { name: 'headers: abrir versión publicada', exact: true })).toHaveAttribute('href', `/datasets/${sourceFamily.output_datasets.headers}?version=${finalBatch.published_versions.headers}`)
  await info.attach('document-native-evidence.json', { body: JSON.stringify({ nonce, family_id: family.id, document_id: final.id, publication: final.publications, receiver: received, elapsed_seconds: (Date.now() - started) / 1000 }), contentType: 'application/json' })
})

test('browser scheduled strict batch retains pending document and exact two-document scope', async ({ page }, info) => {
  const started = Date.now()
  const csrf = await login(page)
  const nonce = randomBytes(6).toString('hex')
  const { family, template } = await buildFamily(page, csrf, nonce, 'SCHEDULED')
  // The short wait was configured and tested in the persisted draft before
  // activation. The real scheduler must now demonstrate BLOCKED.
  await page.reload()
  const example = await upload(page, `D095-scheduled-example-${nonce}.pdf`, '000000', true, template)
  await review(page, example, true)
  const first = await upload(page, `D095-scheduled-one-${nonce}.pdf`, '000001')
  const second = await upload(page, `D095-scheduled-two-${nonce}.pdf`, '000002')
  await review(page, first)
  await expect.poll(async () => {
    const batches = await call(page, csrf, `/documents/families/${family.id}/batches`)
    return batches.items.some((batch: Json) => batch.status === 'BLOCKED' && batch.members.length === 2)
  }, { timeout: 180_000 }).toBe(true)
  const batches = await call(page, csrf, `/documents/families/${family.id}/batches`)
  const blocked = batches.items.find((batch: Json) => batch.status === 'BLOCKED' && batch.members.length === 2)
  expect(blocked.members.map((member: Json) => member.document_id).sort()).toEqual([first.id, second.id].sort())
  expect(Object.keys(blocked.published_versions)).toHaveLength(0)
  await review(page, second)
  await page.goto(`/documents/families/${family.id}?batch=${blocked.id}`)
  await expect(page.getByRole('tab', { name: 'Lotes y resultados', exact: true })).toHaveAttribute('aria-selected', 'true')
  const batchCard = page.locator('.document-batch').filter({ hasText: blocked.id.slice(0, 8) }).first()
  await batchCard.getByRole('button', { name: 'Revisar controles y publicar', exact: true }).click()
  await expect(batchCard.getByRole('link', { name: 'headers: abrir versión publicada', exact: true })).toBeVisible()
  const publication = await call(page, csrf, `/documents/batches/${blocked.id}`)
  expect(publication.status).toBe('PUBLISHED')
  expect(publication.members.every((member: Json) => member.status === 'PUBLISHED')).toBe(true)
  const finalFamily = await call(page, csrf, `/documents/families/${family.id}`)
  for (const [structure, version] of Object.entries(publication.published_versions)) {
    const dataset = await call(page, csrf, `/datasets/${finalFamily.output_datasets[structure]}`)
    const exact = dataset.versions.find((item: Json) => item.id === version)
    expect(exact.row_count).toBe(structure === 'headers' ? 2 : 4)
    expect(exact.ingestion_metadata.document_publication.scope).toBe('PUBLICATION_BATCH_ONLY')
  }
  await info.attach('document-scheduled-evidence.json', { body: JSON.stringify({ nonce, family_id: family.id, batch: publication, elapsed_seconds: (Date.now() - started) / 1000 }), contentType: 'application/json' })
})

test('grouped correction preserves the delivered two-document population without another receiver POST', async ({ page }, info) => {
  const started = Date.now()
  const csrf = await login(page)
  const nonce = randomBytes(6).toString('hex')
  const { family, template } = await buildFamily(page, csrf, nonce)
  const example = await upload(page, `D095-group-example-${nonce}.pdf`, '000000', true, template)
  await review(page, example, true)
  const bootstrap = await upload(page, `D095-group-bootstrap-${nonce}.pdf`, '000000')
  const firstPublication = await review(page, bootstrap)
  const bootstrapBatch = await call(page, csrf, `/documents/batches/${firstPublication.publications[0].batch_id}`)
  const { finalTemplate, bindings, automationIds, control } = await prepareDelivery(page, csrf, family, bootstrapBatch, nonce, ['000001', '000002'])
  const scheduled = await activateDraft(page, csrf, family, {
    ...finalTemplate.definition, publication: { ...finalTemplate.definition.publication, mode: 'SCHEDULED', starts_at: new Date(Date.now() - 1000).toISOString(), interval_seconds: 60, wait_seconds: 0 },
  }, undefined, finalTemplate)
  await page.reload()
  const scheduledExample = await upload(page, `D095-group-scheduled-example-${nonce}.pdf`, '000000', true, scheduled)
  await review(page, scheduledExample, true)
  const originals = [await upload(page, `D095-group-one-${nonce}.pdf`, '000001'), await upload(page, `D095-group-two-${nonce}.pdf`, '000002')]
  for (const original of originals) await review(page, original)
  await expect.poll(async () => {
    const batches = await call(page, csrf, `/documents/families/${family.id}/batches`)
    return batches.items.some((batch: Json) => batch.status === 'PUBLISHED' && batch.members.length === 2)
  }, { timeout: 180_000 }).toBe(true)
  const batches = await call(page, csrf, `/documents/families/${family.id}/batches`)
  const deliveredBatch = batches.items.find((batch: Json) => batch.status === 'PUBLISHED' && batch.members.length === 2)
  expect(deliveredBatch.members.map((member: Json) => member.document_id).sort()).toEqual(originals.map(original => original.id).sort())
  await expect.poll(async () => (await (await page.request.get(`${control}/documents/${nonce}/receipt`)).json()).status, { timeout: 180_000 }).toBe('PASS')
  const beforeReceipt = await (await page.request.get(`${control}/documents/${nonce}/receipt`)).json()
  expect(beforeReceipt).toEqual(expect.objectContaining({ header_rows: 2, detail_rows: 4, post_attempts: 2, all_values_exact: true }))
  const currentRevisions: string[] = []
  await page.getByRole('tab', { name: 'Documentos', exact: true }).click()
  currentRevisions.push((await review(page, originals[0])).extraction.id)
  await page.goto(`/documents/families/${family.id}?batch=${deliveredBatch.id}`)
  const sourceCard = page.locator('.document-batch').filter({ hasText: deliveredBatch.id.slice(0, 8) }).first()
  await sourceCard.getByRole('button', { name: 'Corregir este lote', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: 'Corregir lote publicado', exact: true })
  await expect(dialog).toContainText('mismos 2 documentos publicados')
  await expect(dialog).toContainText('no ejecuta Intake ni Delivery automáticamente')
  await expect(dialog.getByRole('button', { name: 'Publicar corrección del lote', exact: true })).toBeDisabled()
  await dialog.getByRole('checkbox', { name: /Confirmo la revisión de todos los documentos/ }).check()
  const publishedResponse = page.waitForResponse(response => response.request().method() === 'POST' && new URL(response.url()).pathname === `/api/v1/documents/families/${family.id}/batches`)
  await dialog.getByRole('button', { name: 'Publicar corrección del lote', exact: true }).click()
  const blockedCorrection = await (await publishedResponse).json()
  expect(blockedCorrection.status).toBe('BLOCKED')
  expect(blockedCorrection.published_versions).toEqual({})
  expect(blockedCorrection.members.map((member: Json) => member.document_id).sort()).toEqual(originals.map(original => original.id).sort())
  expect(blockedCorrection.members.some((member: Json) => member.reason === 'CORRECTION_REQUIRES_NEW_REVIEW')).toBe(true)
  expect(await (await page.request.get(`${control}/documents/${nonce}/receipt`)).json()).toEqual(beforeReceipt)
  await expect(dialog).toBeHidden()
  await page.getByRole('tab', { name: 'Documentos', exact: true }).click()
  currentRevisions.push((await review(page, originals[1])).extraction.id)
  await page.goto(`/documents/families/${family.id}?batch=${blockedCorrection.id}`)
  const correctionCard = page.locator('.document-batch').filter({ hasText: blockedCorrection.id.slice(0, 8) }).first()
  const retryResponse = page.waitForResponse(response => response.request().method() === 'POST' && new URL(response.url()).pathname === `/api/v1/documents/batches/${blockedCorrection.id}/publish`)
  await correctionCard.getByRole('button', { name: 'Revisar controles y publicar', exact: true }).click()
  const correction = await (await retryResponse).json()
  expect(correction.status).toBe('PUBLISHED')
  expect(correction.members.map((member: Json) => member.document_id).sort()).toEqual(originals.map(original => original.id).sort())
  expect(correction.members.map((member: Json) => member.extraction_id).sort()).toEqual(currentRevisions.sort())
  const currentFamily = await call(page, csrf, `/documents/families/${family.id}`)
  for (const structure of ['headers', 'details']) {
    const dataset = await call(page, csrf, `/datasets/${currentFamily.output_datasets[structure]}`)
    const before = dataset.versions.find((version: Json) => version.id === deliveredBatch.published_versions[structure])
    const after = dataset.versions.find((version: Json) => version.id === correction.published_versions[structure])
    expect(after.row_count).toBe(structure === 'headers' ? 2 : 4)
    expect(after.ingestion_metadata.population_scope).toEqual(before.ingestion_metadata.population_scope)
    expect(after.ingestion_metadata.document_publication).toEqual(expect.objectContaining({ correction: true, automatic_processing_allowed: false, automatic_delivery_allowed: false }))
    await expect.poll(async () => (await call(page, csrf, `/intake/contracts/${bindings[structure]}/occurrences`)).items.some((occurrence: Json) => occurrence.dataset_version_id === after.id && occurrence.status === 'BLOCKED' && occurrence.reason_code === 'DOCUMENT_AUTOMATION_NOT_AUTHORIZED'), { timeout: 60_000 }).toBe(true)
    expect((await call(page, csrf, `/delivery/automations/${automationIds[structure]}/occurrences`)).items.filter((occurrence: Json) => occurrence.status === 'SUCCESS')).toHaveLength(1)
  }
  const preserved = await call(page, csrf, `/documents/batches/${deliveredBatch.id}`)
  expect(preserved).toEqual(deliveredBatch)
  const quietStarted = Date.now()
  await expect.poll(async () => {
    const observed = await (await page.request.get(`${control}/documents/${nonce}/receipt`)).json()
    if (observed.post_attempts !== beforeReceipt.post_attempts) return 'EXTRA_POST'
    return Date.now() - quietStarted >= 6000 ? 'UNCHANGED_FOR_SIX_SECONDS' : 'OBSERVING'
  }, { timeout: 12_000, intervals: [500, 1000] }).toBe('UNCHANGED_FOR_SIX_SECONDS')
  const afterReceipt = await (await page.request.get(`${control}/documents/${nonce}/receipt`)).json()
  expect(afterReceipt).toEqual(beforeReceipt)
  await info.attach('document-grouped-correction-evidence.json', { body: JSON.stringify({ nonce, family_id: family.id, original_batch: deliveredBatch, blocked_correction: blockedCorrection, correction, receiver_before: beforeReceipt, receiver_after: afterReceipt, post_observation_seconds: (Date.now() - quietStarted) / 1000, technical_resolution_claimed: false, elapsed_seconds: (Date.now() - started) / 1000 }), contentType: 'application/json' })
})

test('global documents search, bounded pages, filters and direct evidence links across two families', async ({ page }, info) => {
  const started = Date.now()
  const csrf = await login(page)
  const nonce = randomBytes(6).toString('hex')
  const families: Json[] = []
  const originals: Json[] = []
  const invoiceBase = String(Date.now())
  for (let group = 0; group < 2; group++) {
    const family = await call(page, csrf, '/documents/families', { name: `D095 global ${nonce} ${group === 0 ? 'Alpha' : 'Beta'}`, description: 'Synthetic bounded global-document navigation fixture.' })
    families.push(family)
    const template = await activateDraft(page, csrf, family, {
      fields: [{ name: 'invoice', label: 'Invoice', pattern: '([0-9]+)', business_key: true }],
      publication: group === 0 ? { mode: 'INDIVIDUAL' } : { mode: 'SCHEDULED', starts_at: new Date(Date.now() + 86400_000).toISOString() },
    })
    for (let index = 0; index < (group === 0 ? 6 : 5); index++) {
      const invoice = `${invoiceBase}${group}${index}`
      const response = await page.request.post(`/api/v1/documents/families/${family.id}/originals`, {
        headers: { 'X-CSRF-Token': csrf }, multipart: { template_id: template.id, is_example: 'true',
          files: { name: `${nonce}-${group}-${index}.pdf`, mimeType: 'application/pdf', buffer: knownPdf(invoice) } },
      })
      expect(response.status()).toBe(200)
      const original = (await response.json()).items[0]
      originals.push({ ...original, expected_invoice: invoice })
    }
  }
  // The search oracle is an actual worker extraction in each family, never an
  // injected payload. The other receipts also exercise the non-extracted states.
  for (const original of [originals[0], originals[6]]) {
    await expect.poll(async () => {
      const observed = await call(page, csrf, `/documents/originals/${original.id}`)
      return observed.extraction?.payload.fields.invoice.value
    }, { timeout: 150_000, intervals: [500, 1000, 2000] }).toBe(original.expected_invoice)
  }
  await page.goto('/documents')
  await page.getByRole('tab', { name: 'Todos los documentos', exact: true }).click()
  const section = page.getByRole('region', { name: 'Todos los documentos', exact: true })
  const search = section.getByRole('textbox', { name: 'Buscar nombre, referencia, identificador extraído o familia', exact: true })
  await search.fill(nonce)
  await expect(section.locator('tbody tr')).toHaveCount(0)
  await section.getByLabel('Tipo de documento', { exact: true }).selectOption('LEGACY_REFERENCE')
  await section.getByLabel('Documentos por página', { exact: true }).selectOption('10')
  await section.getByLabel('Ordenar documentos', { exact: true }).selectOption('filename:asc')
  await expect(section.locator('tbody tr')).toHaveCount(10)
  await expect(section.getByRole('status').filter({ hasText: '1–10 de 11' })).toBeVisible()
  const firstPage = await section.locator('tbody tr td:first-child > a').allTextContents()
  const nextPageResponse = page.waitForResponse(response => {
    const url = new URL(response.url())
    return url.pathname === '/api/v1/documents/originals' && url.searchParams.get('offset') === '10' && url.searchParams.get('search') === nonce
  })
  await section.getByRole('button', { name: 'Página siguiente', exact: true }).click()
  const nextPage = await (await nextPageResponse).json()
  expect(nextPage.items).toHaveLength(1)
  expect(nextPage.total).toBe(11)
  expect(firstPage).not.toContain(nextPage.items[0].filename)
  await expect(section.locator('tbody tr')).toHaveCount(1)
  await section.getByLabel('Familia documental', { exact: true }).selectOption(families[1].id)
  await expect(section.locator('tbody tr')).toHaveCount(5)
  await section.getByLabel('Modalidad de recepción', { exact: true }).selectOption('INDIVIDUAL')
  await expect(section.getByText('No hay documentos con estos filtros', { exact: true })).toBeVisible()
  await section.getByLabel('Modalidad de recepción', { exact: true }).selectOption('SCHEDULED')
  await section.getByLabel('Publicación del documento', { exact: true }).selectOption('UNPUBLISHED')
  await expect(section.locator('tbody tr')).toHaveCount(5)
  await search.fill(originals[6].expected_invoice)
  await expect(section.locator('tbody tr')).toHaveCount(1)
  const result = section.locator('tbody tr').first()
  await expect(result.getByRole('link', { name: `${nonce}-1-0.pdf`, exact: true })).toHaveAttribute('href', `/documents/families/${families[1].id}?document=${originals[6].id}`)
  await expect(result.getByRole('link', { name: /Extracción · revisión 1/ })).toBeVisible()
  await expect(result.getByRole('link', { name: 'Abrir original', exact: true })).toHaveAttribute('href', `/api/v1/documents/originals/${originals[6].id}/file`)
  await result.getByRole('link', { name: 'Revisar y corregir', exact: true }).click()
  await expect(page.getByRole('dialog', { name: `${nonce}-1-0.pdf`, exact: true })).toBeVisible()
  await expect(page.getByLabel('Valor headers.0.invoice', { exact: true })).toHaveValue(originals[6].expected_invoice)
  await info.attach('global-documents-evidence.json', { body: JSON.stringify({ nonce, family_ids: families.map(family => family.id), original_ids: originals.map(original => original.id), documents: 11, first_page: 10, second_page: 1, extracted_search_oracles: 2, direct_document_original_family_links: true, elapsed_seconds: (Date.now() - started) / 1000 }), contentType: 'application/json' })
})
