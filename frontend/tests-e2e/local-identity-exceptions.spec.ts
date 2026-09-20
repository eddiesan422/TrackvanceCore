import { expect, test, type Page } from '@playwright/test'
import type { RecordData } from '../src/api/client'

test.setTimeout(150_000)

async function administrator(page: Page) {
  await page.goto('/')
  await page.getByRole('button', { name: 'Entrar al entorno demo' }).click()
  await expect(page.getByRole('heading', { name: 'Centro de control', exact: true })).toBeVisible()
  const identity = await (await page.request.get('/api/v1/me')).json()
  return { identity, headers: { 'X-CSRF-Token': identity.csrf_token as string } }
}

test('administrador crea, edita, restablece y desactiva un usuario local desde la interfaz', async ({ page }) => {
  const { headers } = await administrator(page)
  const stamp = Date.now(), name = `E2E identidad ${stamp}`, email = `local-${stamp}@example.test`
  const password = 'Local E2E passphrase 2048!', replacement = 'Changed E2E passphrase 4096!'
  await page.goto('/settings/system')
  await page.getByRole('button', { name: 'Usuarios locales' }).click()
  await page.getByRole('button', { name: 'Nuevo usuario' }).click()
  let dialog = page.getByRole('dialog')
  await dialog.getByLabel('Nombre del usuario').fill(name)
  await dialog.getByLabel('Correo electrónico').fill(email)
  await dialog.getByLabel('Rol del usuario').selectOption('Data Analyst')
  await dialog.getByLabel('Contraseña inicial').fill(password)
  const createdResponse = page.waitForResponse(response => new URL(response.url()).pathname === '/api/v1/users' && response.request().method() === 'POST')
  await dialog.getByRole('button', { name: 'Crear usuario' }).click()
  const created = await createdResponse
  expect(created.status()).toBe(201)
  const account = await created.json()
  expect(JSON.stringify(account)).not.toContain(password)
  await page.getByRole('button', { name: `Editar ${name}`, exact: true }).click()
  dialog = page.getByRole('dialog')
  await dialog.getByLabel('Rol del usuario').selectOption('Auditor')
  const edited = page.waitForResponse(response => new URL(response.url()).pathname === `/api/v1/users/${account.id}` && response.request().method() === 'PATCH')
  await dialog.getByRole('button', { name: 'Guardar usuario' }).click()
  expect((await edited).status()).toBe(200)
  await page.getByRole('button', { name: `Restablecer contraseña de ${name}`, exact: true }).click()
  dialog = page.getByRole('dialog')
  await dialog.getByLabel('Nueva contraseña').fill(replacement)
  await dialog.getByLabel('Confirmar contraseña').fill(replacement)
  const reset = page.waitForResponse(response => response.url().endsWith(`/users/${account.id}/reset-password`))
  await dialog.getByRole('button', { name: 'Restablecer contraseña', exact: true }).click()
  expect((await reset).status()).toBe(200)
  await page.getByRole('button', { name: `Editar ${name}`, exact: true }).click()
  dialog = page.getByRole('dialog')
  await dialog.getByLabel('Usuario activo').uncheck()
  const disabled = page.waitForResponse(response => new URL(response.url()).pathname === `/api/v1/users/${account.id}` && response.request().method() === 'PATCH')
  await dialog.getByRole('button', { name: 'Guardar usuario' }).click()
  expect((await disabled).status()).toBe(200)
  const current = await (await page.request.get(`/api/v1/users/${account.id}`)).json()
  expect(current).toMatchObject({ role: 'Auditor', active: false, version: 4 })
  expect((await page.request.post('/api/v1/auth/login', { data: { email, password: replacement } })).status()).toBe(401)
  const audit = await (await page.request.get('/api/v1/audit-events')).json()
  const events = audit.items.filter((event: RecordData) => event.subject_id === account.id)
  expect(events.map((event: RecordData) => event.event_type)).toContain('USER_PASSWORD_RESET')
  expect(JSON.stringify(events)).not.toContain(replacement)
  // A real CSRF negative check goes through the running API.
  expect((await page.request.patch(`/api/v1/users/${account.id}`, { headers: { ...headers, 'X-CSRF-Token': 'invalid' }, data: { version: 4, active: true } })).status()).toBe(403)
})

test('cada rol local recibe accesos permitidos y denegados por el servidor', async ({ page, browser }) => {
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
    const email = `rbac-${Date.now()}-${index}@example.test`, password = 'Role E2E passphrase 8192!'
    const creation = await page.request.post('/api/v1/users', { headers, data: { name: `E2E ${role.name}`, email, password, role: role.name } })
    expect(creation.status(), await creation.text()).toBe(201)
    const context = await browser.newContext({ baseURL })
    try {
      const login = await context.request.post('/api/v1/auth/login', { data: { email, password } })
      expect(login.status()).toBe(200)
      const identity = await login.json(), roleHeaders = { 'X-CSRF-Token': identity.csrf_token }
      expect((await context.request.get('/api/v1/users')).status()).toBe(role.users ? 200 : 403)
      expect((await context.request.get('/api/v1/datasets')).status()).toBe(200)
      expect((await context.request.post('/api/v1/datasets', { headers: roleHeaders, data: { name: `E2E RBAC ${Date.now()} ${index}` } })).status()).toBe(role.write ? 201 : 403)
      expect((await context.request.post('/api/v1/exceptions/missing/comments', { headers: roleHeaders, data: { version: 1, comment: 'Permission check' } })).status()).toBe(role.cases ? 404 : 403)
      expect((await context.request.post('/api/v1/users', { headers: roleHeaders, data: { name: 'Denied', email: 'denied@invalid.test', password: 'short' } })).status()).toBe(role.name === 'Administrator' ? 422 : 403)
      const rolePage = await context.newPage()
      await rolePage.goto('/settings/system')
      if (role.users) {
        await rolePage.getByRole('button', { name: 'Usuarios locales' }).click()
        await expect(rolePage.getByRole('heading', { name: 'Cuentas de esta organización' })).toBeVisible()
        if (role.name === 'Auditor') await expect(rolePage.getByRole('button', { name: 'Nuevo usuario' })).toHaveCount(0)
      } else {
        await expect(rolePage.getByText('Tu rol no permite administrar este entorno.')).toBeVisible()
      }
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
