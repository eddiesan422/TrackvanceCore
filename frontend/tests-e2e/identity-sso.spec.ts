import { expect, test, type Page } from '@playwright/test'
import type { RecordData } from '../src/api/client'
import { createAccount, fillSecret, installCredentialPrivacyHooks, issuedCredentials, rememberSecret } from './identity-helpers'

installCredentialPrivacyHooks()

test.use({ trace: 'off', screenshot: 'off', video: 'off', actionTimeout: 20000, navigationTimeout: 30000 })
test.skip(process.env.TV_IDENTITY_SSO_E2E !== 'true', 'Runs only with the disposable OIDC overlay')
test.setTimeout(180_000)

async function administrator(page: Page) {
  await page.goto('/')
  await page.getByRole('button', { name: 'Entrar al entorno demo' }).click()
  await expect(page.getByRole('heading', { name: 'Centro de control', exact: true })).toBeVisible()
  const identity = await (await page.request.get('/api/v1/me')).json() as RecordData
  return { 'X-CSRF-Token': identity.csrf_token as string }
}

async function sso(page: Page, options: { provider: string; email: string; subject: string; profile: string; scenario?: string }) {
  await page.goto('/')
  await page.getByRole('link', { name: `Continuar con ${options.provider}` }).click()
  const authorization = new URL(page.url())
  expect(authorization.searchParams.get('response_type')).toBe('code')
  expect(authorization.searchParams.get('code_challenge_method')).toBe('S256')
  expect(Boolean(authorization.searchParams.get('state'))).toBe(true)
  expect(Boolean(authorization.searchParams.get('nonce'))).toBe(true)
  await page.getByLabel('Email', { exact: true }).fill(options.email)
  await page.getByLabel('Subject', { exact: true }).fill(options.subject)
  await page.locator('select[name=profile]').selectOption({ label: options.profile })
  await page.locator('select[name=scenario]').selectOption(options.scenario || 'valid')
  const callback = page.waitForRequest(request => new URL(request.url()).pathname.endsWith('/callback'))
  await page.getByRole('button', { name: 'Authorize test identity' }).click()
  // Kept in memory solely for the replay assertion; never attach this URL to test output.
  return (await callback).url()
}

test('local first access is restricted, rotates the session and revokes the temporary credential', async ({ page, browser }) => {
  const headers = await administrator(page), stamp = Date.now(), email = `first-${stamp}@example.test`
  const issue = await createAccount(page.request, headers, { email, username: `first.${stamp}` })
  const account = issue.user, temporary = issuedCredentials(issue), context = await browser.newContext({ baseURL: new URL(page.url()).origin })
  try {
    const person = await context.newPage()
    await person.goto('/')
    await person.getByRole('button', { name: 'Tengo una cuenta local' }).click()
    await person.getByLabel('Usuario o correo').fill(temporary.username.toUpperCase())
    await fillSecret(person.getByLabel('Contraseña', { exact: true }), temporary.password)
    await person.getByRole('button', { name: 'Iniciar sesión', exact: true }).click()
    await expect(person.getByRole('heading', { name: 'Cambia tu contraseña' })).toBeVisible()
    const before = await (await context.request.get('/api/v1/me')).json()
    expect((await context.request.get('/api/v1/datasets')).status()).toBe(403)
    expect((await context.request.get('/api/v1/delivery/configurations')).status()).toBe(403)
    const replacement = `Complete local ${crypto.randomUUID()}!`
    await fillSecret(person.getByLabel('Nueva contraseña', { exact: true }), replacement)
    await fillSecret(person.getByLabel('Confirmar contraseña'), replacement)
    await person.getByRole('button', { name: 'Guardar y continuar' }).click()
    await expect(person.getByRole('heading', { name: 'Centro de control', exact: true })).toBeVisible()
    const after = await (await context.request.get('/api/v1/me')).json()
    expect(after.user.must_change_password).toBe(false)
    expect(before.csrf_token !== after.csrf_token, 'Successful first login rotates the session').toBe(true)
    expect((await context.request.get('/api/v1/datasets')).status()).toBe(200)
    await person.getByRole('button', { name: 'Cerrar sesión' }).click()
    expect((await context.request.post('/api/v1/auth/login', { data: { username: temporary.username, password: temporary.password } })).status()).toBe(401)
    expect((await context.request.post('/api/v1/auth/login', { data: { email, password: replacement } })).status()).toBe(200)
    const audit = await (await page.request.get('/api/v1/audit-events')).json()
    expect(JSON.stringify(audit).includes(temporary.password), 'Audit contains no temporary plaintext').toBe(false)
    const current = await (await page.request.get(`/api/v1/users/${account.id}`)).json()
    expect(current.must_change_password).toBe(false)
    expect(current.temporary_password_expires_at).toBeNull()
  } finally { await context.close() }
})

test('role changes reach existing sessions, associated roles remain protected and role reassignment revokes login', async ({ page, browser }) => {
  const headers = await administrator(page), stamp = Date.now()
  await page.goto('/settings/system')
  await page.getByRole('button', { name: 'Roles y permisos', exact: true }).click()
  await page.getByRole('button', { name: 'Nuevo rol' }).click()
  await page.getByLabel('Nombre del rol').fill(`E2E dinámico ${stamp}`)
  const datasetGroup = page.getByRole('group', { name: 'Datasets', exact: true })
  await datasetGroup.getByRole('checkbox', { name: 'Consultar datasets:read', exact: true }).check()
  const creation = page.waitForResponse(response => response.url().endsWith('/api/v1/roles') && response.request().method() === 'POST')
  await page.getByRole('button', { name: 'Crear rol', exact: true }).click()
  const role = await (await creation).json(), email = `dynamic-${stamp}@example.test`
  const issue = await createAccount(page.request, headers, { email, username: `dynamic.${stamp}`, role_id: role.id })
  const account = issue.user, temporary = issuedCredentials(issue), context = await browser.newContext({ baseURL: new URL(page.url()).origin })
  try {
    const login = await context.request.post('/api/v1/auth/login', { data: { username: temporary.username, password: temporary.password } })
    const identity = await login.json()
    const changed = await context.request.post('/api/v1/auth/first-login/change-password', { headers: { 'X-CSRF-Token': identity.csrf_token }, data: { new_password: rememberSecret(`Dynamic ${crypto.randomUUID()}!`) } })
    expect(changed.status()).toBe(200)
    const person = await context.newPage()
    await person.goto('/datasets')
    await expect(person.getByRole('heading', { name: 'Datasets', exact: true })).toBeVisible()
    let response = await page.request.patch(`/api/v1/roles/${role.id}`, { headers, data: { version: role.version, permissions: [] } })
    expect(response.status()).toBe(200)
    const updated = await response.json()
    expect((await context.request.get('/api/v1/datasets')).status()).toBe(403)
    await expect(person.getByText(/Tu rol no permite consultar esta sección/)).toBeVisible({ timeout: 25000 })
    expect((await context.request.get('/api/v1/me')).status()).toBe(200)
    expect((await page.request.patch(`/api/v1/roles/${role.id}`, { headers, data: { version: updated.version, active: false } })).status()).toBe(422)
    expect((await page.request.delete(`/api/v1/roles/${role.id}`, { headers, data: { version: updated.version } })).status()).toBe(422)
    const roles = await (await page.request.get('/api/v1/roles')).json()
    const auditor = roles.items.find((item: RecordData) => item.name === 'Auditor')
    const currentUser = await (await page.request.get(`/api/v1/users/${account.id}`)).json()
    response = await page.request.patch(`/api/v1/users/${account.id}`, { headers, data: { version: currentUser.version, role_id: auditor.id } })
    expect(response.status()).toBe(200)
    expect((await context.request.get('/api/v1/me')).status()).toBe(401)
    expect((await page.request.delete(`/api/v1/roles/${role.id}`, { headers, data: { version: updated.version } })).status()).toBe(200)
  } finally { await context.close() }
})

for (const profile of [
  { provider: 'Microsoft', profile: 'Microsoft personal', domain: 'outlook.com' },
  { provider: 'Microsoft', profile: 'Microsoft organizational', domain: 'example.test' },
  { provider: 'Google', profile: 'Google Gmail', domain: 'gmail.com' },
  { provider: 'Google', profile: 'Google Workspace', domain: 'example.test' },
]) test(`OIDC ${profile.profile}: first access, stable subject, replay and deactivation`, async ({ page, browser }) => {
  const headers = await administrator(page), stamp = `${Date.now()}-${crypto.randomUUID().slice(0, 6)}`
  const email = `sso-${stamp}@${profile.domain}`, subject = `subject-${stamp}`
  const issue = await createAccount(page.request, headers, { email, username: `sso.${stamp}`, role: 'Auditor' })
  const account = issue.user, temporary = issuedCredentials(issue), context = await browser.newContext({ baseURL: new URL(page.url()).origin })
  try {
    const person = await context.newPage()
    const callback = await sso(person, { ...profile, email, subject })
    await expect(person.getByRole('heading', { name: 'Cambia tu contraseña' })).toBeVisible()
    expect((await context.request.get('/api/v1/datasets')).status()).toBe(403)
    const replacement = `SSO local ${crypto.randomUUID()}!`
    await fillSecret(person.getByLabel('Nueva contraseña', { exact: true }), replacement)
    await fillSecret(person.getByLabel('Confirmar contraseña'), replacement)
    await person.getByRole('button', { name: 'Guardar y continuar' }).click()
    await expect(person.getByRole('heading', { name: 'Centro de control', exact: true })).toBeVisible()
    let me = await (await context.request.get('/api/v1/me')).json()
    expect(me.user.id).toBe(account.id)
    expect(me.user.must_change_password).toBe(false)
    expect((await context.request.post('/api/v1/datasets', { headers: { 'X-CSRF-Token': me.csrf_token }, data: { name: 'Denied by role' } })).status()).toBe(403)
    let current = await (await page.request.get(`/api/v1/users/${account.id}`)).json()
    expect(current.external_identities).toHaveLength(1)
    expect(current.external_identities[0].subject).toBe(subject)
    await person.getByRole('button', { name: 'Cerrar sesión' }).click()
    await context.request.get(callback)
    expect((await context.request.get('/api/v1/me')).status()).toBe(401)
    expect((await context.request.post('/api/v1/auth/login', { data: { username: account.username, password: temporary.password } })).status()).toBe(401)
    await sso(person, { ...profile, email: `changed-${stamp}@${profile.domain}`, subject })
    await expect(person.getByRole('button', { name: 'Cerrar sesión' })).toBeVisible()
    me = await (await context.request.get('/api/v1/me')).json()
    expect(me.user.id).toBe(account.id)
    current = await (await page.request.get(`/api/v1/users/${account.id}`)).json()
    expect((await page.request.patch(`/api/v1/users/${account.id}`, { headers, data: { version: current.version, active: false } })).status()).toBe(200)
    expect((await context.request.get('/api/v1/me')).status()).toBe(401)
    await sso(person, { ...profile, email, subject })
    await expect(person.getByText(/No se pudo iniciar sesión con el proveedor/)).toBeVisible()
    current = await (await page.request.get(`/api/v1/users/${account.id}`)).json()
    expect((await page.request.delete(`/api/v1/users/${account.id}`, { headers, data: { version: current.version } })).status()).toBe(200)
    await sso(person, { ...profile, email, subject })
    await expect(person.getByText(/No se pudo iniciar sesión con el proveedor/)).toBeVisible()
  } finally { await context.close() }
})

for (const provider of ['Microsoft', 'Google']) test(`OIDC ${provider}: rejects invalid state, nonce, issuer, audience, signature and expiration`, async ({ page, browser }) => {
  const headers = await administrator(page), stamp = Date.now(), email = `negative-${stamp}@gmail.com`
  await createAccount(page.request, headers, { email, username: `negative.${provider}.${stamp}` })
  for (const scenario of ['wrong_state', 'wrong_nonce', 'wrong_issuer', 'wrong_audience', 'invalid_signature', 'expired', 'unverified_email']) {
    const context = await browser.newContext({ baseURL: new URL(page.url()).origin })
    try {
      const person = await context.newPage()
      await sso(person, { provider, email, subject: `negative-${stamp}-${scenario}`, profile: provider === 'Microsoft' ? 'Microsoft personal' : 'Google Gmail', scenario })
      await expect(person.getByText(/No se pudo iniciar sesión con el proveedor/)).toBeVisible()
      expect((await context.request.get('/api/v1/me')).status(), `Rejected ${scenario} has no Trackvance session`).toBe(401)
    } finally { await context.close() }
  }
})

test('OIDC never provisions an unknown user and rejects Google external-email authority', async ({ page, browser }) => {
  const headers = await administrator(page), stamp = Date.now(), external = `external-${stamp}@example.test`
  await createAccount(page.request, headers, { email: external, username: `external.${stamp}` })
  for (const input of [
    { provider: 'Microsoft', profile: 'Microsoft personal', email: `missing-${stamp}@outlook.com` },
    { provider: 'Google', profile: 'Google Gmail', email: `missing-${stamp}@gmail.com` },
    { provider: 'Google', profile: 'Google external email', email: external },
  ]) {
    const context = await browser.newContext({ baseURL: new URL(page.url()).origin })
    try {
      const person = await context.newPage()
      await sso(person, { ...input, subject: `unknown-${stamp}-${input.provider}` })
      await expect(person.getByText(/No se pudo iniciar sesión con el proveedor/)).toBeVisible()
      expect((await context.request.get('/api/v1/me')).status()).toBe(401)
    } finally { await context.close() }
  }
  const users = await (await page.request.get('/api/v1/users')).json()
  expect(users.items.some((user: RecordData) => user.email === `missing-${stamp}@outlook.com` || user.email === `missing-${stamp}@gmail.com`)).toBe(false)
})
