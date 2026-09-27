import { activateLocal, createAccount, expectNoCredentialResidue, fillSecret, installCredentialPrivacyHooks, issuedCredentials, readCredentialModal } from './identity-helpers'
import { expect, test, type Page } from '@playwright/test'
import type { RecordData } from '../src/api/client'

installCredentialPrivacyHooks()

test.setTimeout(180_000)

async function administrator(page: Page) {
  await page.goto('/')
  await page.getByRole('button', { name: 'Entrar al entorno demo' }).click()
  await expect(page.getByRole('heading', { name: 'Centro de control', exact: true })).toBeVisible()
  const identity = await (await page.request.get('/api/v1/me')).json()
  return { identity, headers: { 'X-CSRF-Token': identity.csrf_token as string } }
}

test.use({ trace: 'off', screenshot: 'off', video: 'off' })

test('alta visible una vez, primer login y regeneración revocan credenciales y sesiones sin persistir secretos', async ({ page, browser }) => {
  await administrator(page)
  const stamp = Date.now(), name = `E2E Identidad ${stamp}`, email = `local-${stamp}@example.test`, username = `local.${stamp}`
  const roles = await (await page.request.get('/api/v1/roles')).json()
  const roleId = (name: string) => roles.items.find((role: RecordData) => role.name === name).id as string
  await page.goto('/settings/system')
  await expect(page.getByRole('button', { name: 'Notificaciones', exact: true })).toHaveCount(0)
  await page.getByRole('button', { name: 'Usuarios locales' }).click()
  await page.getByRole('button', { name: 'Nuevo usuario' }).click()
  let dialog = page.getByRole('dialog')
  await dialog.getByLabel('Nombres', { exact: true }).fill('E2E')
  await dialog.getByLabel('Apellidos').fill(`Identidad ${stamp}`)
  await dialog.getByLabel('Username', { exact: true }).fill(username)
  await dialog.getByLabel('Correo electrónico').fill(email)
  await dialog.getByLabel('Rol del usuario').selectOption(roleId('Data Analyst'))
  await expect(dialog.locator('input[type=password]')).toHaveCount(0)
  const createdResponse = page.waitForResponse(response => new URL(response.url()).pathname === '/api/v1/users' && response.request().method() === 'POST')
  await dialog.getByRole('button', { name: 'Crear usuario' }).click()
  const created = await createdResponse
  expect(created.status()).toBe(201)
  expect(created.headers()['cache-control']).toContain('no-store')
  const first = await readCredentialModal(page)
  const users = await (await page.request.get('/api/v1/users')).json()
  const account = users.items.find((item: RecordData) => item.username === username)
  expect(Boolean(account), 'Created user appears in normal list').toBe(true)
  expect(JSON.stringify(users).includes(first.password), 'Normal reads never recover the issued secret').toBe(false)
  await page.reload()
  await page.getByRole('button', { name: 'Usuarios locales' }).click()
  await expectNoCredentialResidue(page, [first.password])
  await page.getByRole('button', { name: `Editar ${name}`, exact: true }).click()
  dialog = page.getByRole('dialog')
  await dialog.getByLabel('Rol del usuario').selectOption(roleId('Auditor'))
  await dialog.getByRole('button', { name: 'Guardar usuario' }).click()
  await expect(dialog).toHaveCount(0)
  await page.getByRole('button', { name: 'Cerrar sesión' }).click()
  async function login(password: string) {
    await page.getByRole('button', { name: 'Tengo una cuenta local' }).click()
    await page.getByLabel('Usuario o correo').fill(username)
    await fillSecret(page.getByLabel('Contraseña', { exact: true }), password)
    await page.getByRole('button', { name: 'Iniciar sesión', exact: true }).click()
  }
  async function complete(password: string) {
    await expect(page.getByRole('heading', { name: 'Cambia tu contraseña' })).toBeVisible()
    expect((await page.request.get('/api/v1/datasets')).status()).toBe(403)
    await fillSecret(page.getByLabel('Nueva contraseña', { exact: true }), password)
    await fillSecret(page.getByLabel('Confirmar contraseña'), password)
    await page.getByRole('button', { name: 'Guardar y continuar' }).click()
    await expect(page.getByRole('heading', { name: 'Centro de control', exact: true })).toBeVisible()
  }
  await login(first.password)
  const defined = `First completion ${crypto.randomUUID()}!`
  await complete(defined)
  await expectNoCredentialResidue(page, [first.password, defined])
  const identity = await (await page.request.get('/api/v1/me')).json()
  expect(identity.user.role).toBe('Auditor')
  expect((await page.request.post('/api/v1/users', { headers: { 'X-CSRF-Token': identity.csrf_token }, data: {} })).status()).toBe(403)
  await page.getByRole('button', { name: 'Cerrar sesión' }).click()
  expect((await page.request.post('/api/v1/auth/login', { data: { username, password: first.password } })).status()).toBe(401)
  await login(defined)
  await expect(page.getByRole('heading', { name: 'Centro de control', exact: true })).toBeVisible()
  const adminContext = await browser.newContext({ baseURL: new URL(page.url()).origin })
  try {
    const adminPage = await adminContext.newPage()
    const { headers } = await administrator(adminPage)
    await adminPage.goto('/settings/system')
    await adminPage.getByRole('button', { name: 'Usuarios locales' }).click()
    await adminPage.getByRole('button', { name: `Regenerar credenciales de ${name}`, exact: true }).click()
    const reset = adminPage.waitForResponse(response => response.url().endsWith(`/users/${account.id}/regenerate-credentials`))
    await adminPage.getByRole('dialog').getByRole('button', { name: 'Regenerar credenciales', exact: true }).click()
    expect((await reset).status()).toBe(200)
    const replacement = await readCredentialModal(adminPage, 'Credenciales regeneradas')
    expect(replacement.password !== first.password, 'Regeneration issues a different secret').toBe(true)
    expect((await page.request.get('/api/v1/me')).status(), 'Regeneration revokes an active normal session').toBe(401)
    for (const password of [first.password, defined]) expect((await page.request.post('/api/v1/auth/login', { data: { username, password } })).status()).toBe(401)
    await page.reload()
    await login(replacement.password)
    const secondDefined = `Regenerated completion ${crypto.randomUUID()}!`
    await complete(secondDefined)
    await expectNoCredentialResidue(page, [first.password, replacement.password, defined, secondDefined])
    // Password changes update the account revision in the other browser session.
    await adminPage.getByRole('button', { name: 'Actualizar usuarios', exact: true }).click()
    await expect(adminPage.getByRole('row').filter({ hasText: username }).getByText('Contraseña definida', { exact: true })).toBeVisible()
    await adminPage.getByRole('button', { name: `Editar ${name}`, exact: true }).click()
    dialog = adminPage.getByRole('dialog')
    await dialog.getByLabel('Usuario activo').uncheck()
    const disabled = adminPage.waitForResponse(response => new URL(response.url()).pathname === `/api/v1/users/${account.id}` && response.request().method() === 'PATCH')
    await dialog.getByRole('button', { name: 'Guardar usuario' }).click()
    expect((await disabled).status(), 'Edit uses the refreshed current account revision').toBe(200)
    await expect(dialog).toHaveCount(0)
    expect((await page.request.get('/api/v1/me')).status()).toBe(401)
    expect((await page.request.post('/api/v1/auth/login', { data: { email, password: secondDefined } })).status()).toBe(401)
    const current = await (await adminPage.request.get(`/api/v1/users/${account.id}`)).json()
    expect(current).toMatchObject({ role: 'Auditor', active: false, must_change_password: false })
    const audit = await (await adminPage.request.get('/api/v1/audit-events')).json()
    expect([first.password, replacement.password, defined, secondDefined].some(value => JSON.stringify({ audit, current }).includes(value)), 'Normal user reads and audit exclude all credentials').toBe(false)
    expect((await adminPage.request.patch(`/api/v1/users/${account.id}`, { headers: { ...headers, 'X-CSRF-Token': 'invalid' }, data: { version: current.version, active: true } })).status()).toBe(403)
  } finally { await adminContext.close() }
})

test('cada rol migrado conserva sus accesos con credenciales generadas y primer login obligatorio', async ({ page, browser }) => {
  const { headers } = await administrator(page)
  const baseURL = new URL(page.url()).origin
  const roles = [
    { name: 'Administrator', users: true, write: true, cases: true },
    { name: 'Data Owner / Lead', users: false, write: true, cases: true },
    { name: 'Data Analyst', users: false, write: true, cases: true },
    { name: 'Operations', users: false, write: false, cases: true },
    { name: 'Auditor', users: true, write: false, cases: false },
  ]
  for (const [index, role] of roles.entries()) {
    const email = `rbac-${Date.now()}-${index}@example.test`
    const issue = await createAccount(page.request, headers, { email, username: `rbac.${Date.now()}.${index}`, role: role.name })
    const context = await browser.newContext({ baseURL })
    try {
      const identity = await activateLocal(context.request, issuedCredentials(issue)), roleHeaders = { 'X-CSRF-Token': identity.csrf_token }
      expect((await context.request.get('/api/v1/users')).status()).toBe(role.users ? 200 : 403)
      expect((await context.request.get('/api/v1/datasets')).status()).toBe(200)
      expect((await context.request.post('/api/v1/datasets', { headers: roleHeaders, data: { name: `E2E RBAC ${Date.now()} ${index}` } })).status()).toBe(role.write ? 201 : 403)
      expect((await context.request.post('/api/v1/exceptions/missing/comments', { headers: roleHeaders, data: { version: 1, comment: 'Permission check' } })).status()).toBe(role.cases ? 404 : 403)
      expect((await context.request.post('/api/v1/users', { headers: roleHeaders, data: {} })).status()).toBe(role.name === 'Administrator' ? 422 : 403)
      const rolePage = await context.newPage()
      await rolePage.goto('/settings/system')
      if (role.users) {
        await rolePage.getByRole('button', { name: 'Usuarios locales' }).click()
        await expect(rolePage.getByRole('heading', { name: 'Cuentas de esta organización' })).toBeVisible()
        if (role.name === 'Auditor') await expect(rolePage.getByRole('button', { name: 'Nuevo usuario' })).toHaveCount(0)
      } else await expect(rolePage.getByText('Tu rol no permite administrar este entorno.')).toBeVisible()
    } finally { await context.close() }
  }
})

test('asignación, SLA, adjuntos, resolución automática y reapertura conservan evidencia', async ({ page }, testInfo) => {
  const { headers, identity } = await administrator(page)
  const stamp = Date.now()
  async function post(path: string, data: unknown) {
    const response = await page.request.post(`/api/v1${path}`, { headers, data })
    expect(response.ok(), await response.text()).toBeTruthy()
    return response.json() as Promise<RecordData>
  }
  const dataset = await post('/datasets', { name: `E2E caso operativo ${stamp}` })
  async function upload(csv: string) {
    const response = await page.request.post(`/api/v1/datasets/${dataset.id}/versions/upload`, { headers, multipart: { file: { name: 'quality.csv', mimeType: 'text/csv', buffer: Buffer.from(csv) } } })
    expect(response.status()).toBe(201)
    return response.json() as Promise<RecordData>
  }
  const input = await upload('record_id,amount\nA,10\nB,-1\n')
  const config = await post('/intake/contracts', { name: `E2E automático ${stamp}`, dataset_id: dataset.id, config: { positive_columns: ['amount'] } })
  async function execute(versionId: string) {
    const run = await post('/intake/runs', { contract_id: config.id, dataset_version_id: versionId })
    await expect.poll(async () => (await (await page.request.get(`/api/v1/runs/${run.id}`)).json()).status, { timeout: 30_000 }).toBe('SUCCESS')
    return (await (await page.request.get(`/api/v1/runs/${run.id}`)).json()) as RecordData
  }
  const origin = await execute(input.id)
  const exception = await post(`/findings/${origin.findings[0].id}/exceptions`, {})
  await page.goto(`/exceptions?id=${exception.id}`)
  let dialog = page.getByRole('dialog')
  await dialog.getByLabel('Responsable', { exact: true }).selectOption(identity.user.id)
  await dialog.getByLabel('Prioridad del caso').selectOption('CRITICAL')
  await dialog.getByLabel('SLA (horas)', { exact: true }).fill('24')
  await dialog.getByLabel('Resolver automáticamente tras validación técnica').check()
  await dialog.getByLabel('Estado de gestión', { exact: true }).selectOption('ASSIGNED')
  async function save() {
    const expectedState = await dialog.getByLabel('Estado de gestión', { exact: true }).inputValue()
    const response = page.waitForResponse(candidate => new URL(candidate.url()).pathname === `/api/v1/exceptions/${exception.id}` && candidate.request().method() === 'PATCH')
    await dialog.getByRole('button', { name: 'Guardar gestión', exact: true }).click()
    const savedResponse = await response
    expect(savedResponse.status()).toBe(200)
    const saved = await savedResponse.json()
    await expect(dialog.getByText(new RegExp(`^Versión ${saved.version} ·`))).toBeVisible()
    await expect(dialog.getByLabel('Estado de gestión', { exact: true })).toHaveValue(expectedState)
    await expect(dialog.getByLabel('Estado de gestión', { exact: true })).toBeEnabled()
  }
  await save()
  await dialog.getByLabel('Estado de gestión', { exact: true }).selectOption('INVESTIGATING')
  await save()
  await dialog.getByLabel('Archivo de evidencia').setInputFiles({ name: 'correccion.txt', mimeType: 'text/plain', buffer: Buffer.from('Comprobante local de corrección') })
  await dialog.getByLabel('Descripción del adjunto').fill('Evidencia controlada')
  const attached = page.waitForResponse(response => response.url().endsWith(`/exceptions/${exception.id}/attachments`))
  await dialog.getByRole('button', { name: 'Adjuntar evidencia' }).click()
  expect((await attached).status()).toBe(201)
  await expect(dialog.getByRole('button', { name: 'Descargar correccion.txt' })).toBeVisible()
  const downloadEvent = page.waitForEvent('download')
  await dialog.getByRole('button', { name: 'Descargar correccion.txt' }).click()
  expect((await downloadEvent).suggestedFilename()).toBe('correccion.txt')
  await dialog.getByLabel('Causa raíz', { exact: true }).fill('Importe negativo en la fuente')
  await dialog.getByLabel('Corrección aplicada', { exact: true }).fill('Se corrigió el importe')
  await dialog.getByLabel('Estado de gestión', { exact: true }).selectOption('PENDING_VALIDATION')
  await save()
  await page.screenshot({ path: testInfo.outputPath('case-assignment-sla-evidence.png'), fullPage: true })
  const corrected = await upload('record_id,amount\nA,10\nB,1\n')
  const confirmed = await execute(corrected.id)
  const resolved = await (await page.request.get(`/api/v1/exceptions/${exception.id}`)).json()
  expect(resolved.state).toBe('RESOLVED')
  expect(resolved.validation_run_id).toBe(confirmed.id)
  expect(resolved.events.at(-1)).toMatchObject({ actor_type: 'SYSTEM', event_type: 'AUTO_RESOLVED' })
  expect(resolved.attachments).toHaveLength(1)
  await page.reload()
  dialog = page.getByRole('dialog')
  await dialog.getByLabel('Comentario para el historial').fill('Revisar recurrencia del defecto')
  const reopened = page.waitForResponse(response => new URL(response.url()).pathname === `/api/v1/exceptions/${exception.id}` && response.request().method() === 'PATCH')
  await dialog.getByRole('button', { name: 'Reabrir excepción' }).click()
  const response = await reopened
  expect(response.status()).toBe(200)
  expect((await response.json()).technical_validation.status).toBe('NO_LATER_RUN')
  await expect(dialog.getByLabel('Estado de gestión', { exact: true })).toHaveValue('REOPENED')
  expect((await (await page.request.get(`/api/v1/runs/${origin.id}`)).json()).decision).toBe('REJECTED')
})
