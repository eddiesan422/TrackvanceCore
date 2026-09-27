import { expect, test, type APIRequestContext, type Locator, type Page } from '@playwright/test'
import { spawnSync } from 'node:child_process'
import { resolve } from 'node:path'
import type { RecordData } from '../src/api/client'
import type { UserCredentialIssue } from '../src/features/identity/types'

// These values live only in this worker until the final stdin-only leak probe.
const secrets = new Set<string>()
export function rememberSecret(value: string) { secrets.add(value); return value }
export function installCredentialPrivacyHooks() {
  process.env.PLAYWRIGHT_NO_COPY_PROMPT = '1'
  test.afterEach(async ({ browser }, info) => {
    for (const error of info.errors) {
      for (const key of ['message', 'stack', 'snippet', 'value', 'errorContext'] as const) {
        if (typeof error[key] === 'string') for (const secret of secrets) error[key] = error[key]!.split(secret).join('[REDACTED]')
      }
    }
    for (const context of browser.contexts()) for (const page of context.pages()) {
      await page.evaluate(() => document.body.replaceChildren()).catch(() => undefined)
    }
  })
  test.afterAll(() => {
    test.setTimeout(150000)
    try {
      if (!secrets.size) return
      const project = process.env.COMPOSE_PROJECT_NAME
      if (!project) throw new Error('Credential tests require an isolated COMPOSE_PROJECT_NAME for the leak probe')
      const probe = spawnSync(process.env.TV_PYTHON || 'python', [resolve(process.cwd(), '../scripts/tests/credential_leak_probe.py'), '--project', project], { input: JSON.stringify({ secrets: [...secrets] }), encoding: 'utf8', timeout: 120000 })
      // Never forward diagnostic process output: it may originate from failed infrastructure.
      if (probe.status !== 0 || probe.error) throw new Error('Credential privacy probe failed; raw output suppressed')
      let parsed: Record<string, unknown>
      try { parsed = JSON.parse(probe.stdout) }
      catch { throw new Error('Credential privacy probe returned an invalid aggregate') }
      const aggregate: Record<string, string | number> = {}
      for (const key of ['status', 'database', 'container_logs', 'artifacts']) {
        if (parsed[key] !== 'PASS') throw new Error('Credential privacy probe did not pass every surface')
        aggregate[key] = 'PASS'
      }
      for (const key of ['secrets_scanned', 'artifact_files_scanned', 'browser_files_scanned']) {
        if (typeof parsed[key] !== 'number' || !Number.isSafeInteger(parsed[key]) || parsed[key] < 0) throw new Error('Credential privacy probe returned an invalid count')
        aggregate[key] = parsed[key]
      }
      console.log(`CREDENTIAL_PRIVACY_PROBE ${JSON.stringify(aggregate)}`)
    } finally { secrets.clear() }
  })
}

/** Native setter avoids Playwright fill call logs containing secret values. */
export async function fillSecret(locator: Locator, value: string) {
  rememberSecret(value)
  await locator.evaluate((element, secret) => {
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!
    setter.call(element, secret)
    element.dispatchEvent(new Event('input', { bubbles: true }))
  }, value)
}

export function issuedCredentials(issue: UserCredentialIssue) {
  const credentials = issue.temporary_credentials
  if (!credentials?.temporary_password || !credentials.username) throw new Error('Credential issuance response is incomplete')
  return { username: credentials.username, password: rememberSecret(credentials.temporary_password) }
}

export async function readCredentialModal(page: Page, title = 'Usuario creado') {
  const dialog = page.getByRole('dialog', { name: title, exact: true })
  await expect(dialog).toBeVisible()
  const username = await dialog.getByLabel('Username', { exact: true }).inputValue()
  const password = rememberSecret(await dialog.getByLabel('Contraseña temporal', { exact: true }).inputValue())
  expect(password.length === 32, 'Generated credential has the required length').toBe(true)
  await expect(dialog.getByLabel('Contraseña temporal', { exact: true })).toHaveAttribute('type', 'password')
  await page.context().grantPermissions(['clipboard-read', 'clipboard-write'], { origin: new URL(page.url()).origin })
  await page.bringToFront()
  try {
    for (const [label, expected] of [
      ['Copiar username', username], ['Copiar contraseña', password],
      ['Copiar credenciales', `Username: ${username}\nContraseña temporal: ${password}`],
    ]) {
      await dialog.getByRole('button', { name: label, exact: true }).click()
      await expect.poll(async () => {
        const copied = await page.evaluate(() => navigator.clipboard.readText())
        // Windows clipboard text uses CRLF; credential values themselves stay exact.
        return (label === 'Copiar credenciales' ? copied.replace(/\r\n/g, '\n') : copied) === expected
      }, { message: `${label}: clipboard contains the requested credential` }).toBe(true)
    }
  } finally {
    await page.evaluate(() => navigator.clipboard.writeText(''))
  }
  await dialog.getByRole('button', { name: 'Entendido', exact: true }).click()
  await expect(page.getByLabel('Contraseña temporal', { exact: true })).toHaveCount(0)
  await expectNoCredentialResidue(page, [password])
  return { username, password }
}

export async function expectNoCredentialResidue(page: Page, values: string[]) {
  const leaked = await page.evaluate(passwords => {
    const surfaces = [document.documentElement.outerHTML, ...Array.from(document.querySelectorAll('input'), input => input.value), JSON.stringify(localStorage), JSON.stringify(sessionStorage), document.cookie, location.href]
    return passwords.some(secret => surfaces.some(surface => surface.includes(secret)))
  }, values)
  expect(leaked, 'Credential is absent from DOM, storage, cookies and URL').toBe(false)
  const cookies = JSON.stringify(await page.context().cookies())
  expect(values.some(secret => cookies.includes(secret)), 'Credential is absent from all browser cookies').toBe(false)
}

export async function createAccount(request: APIRequestContext, headers: Record<string, string>, input: { email: string; username: string; role?: string; role_id?: string; active?: boolean }) {
  let roleId = input.role_id
  if (!roleId) {
    const roles = await (await request.get('/api/v1/roles', { headers })).json() as { items: RecordData[] }
    roleId = roles.items.find(role => role.name === (input.role || 'Data Analyst'))?.id
  }
  if (!roleId) throw new Error('Requested test role does not exist')
  const response = await request.post('/api/v1/users', { headers, data: { first_name: 'E2E', last_name: 'Identity', email: input.email, username: input.username, role_id: roleId, active: input.active ?? true } })
  expect(response.status(), 'Pre-provisioning creates an account').toBe(201)
  const issue = await response.json() as UserCredentialIssue
  issuedCredentials(issue)
  expect(response.headers()['cache-control']).toContain('no-store')
  return issue
}

export async function activateLocal(request: APIRequestContext, temporary: { username: string; password: string }) {
  const login = await request.post('/api/v1/auth/login', { data: { username: temporary.username, password: temporary.password } })
  expect(login.status(), 'Temporary credentials authenticate').toBe(200)
  const restricted = await login.json() as RecordData
  expect(restricted.user.must_change_password).toBe(true)
  expect((await request.get('/api/v1/datasets')).status()).toBe(403)
  const response = await request.post('/api/v1/auth/first-login/change-password', { headers: { 'X-CSRF-Token': restricted.csrf_token }, data: { new_password: rememberSecret(`Local completed ${crypto.randomUUID()}!`) } })
  expect(response.status(), 'First access password change succeeds').toBe(200)
  return response.json() as Promise<RecordData>
}
