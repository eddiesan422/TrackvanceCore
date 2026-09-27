import { expect, type APIRequestContext } from '@playwright/test'
import type { RecordData } from '../src/api/client'

/** Disposable test mailbox only. Never print bodies or credentials in assertions. */
export async function temporaryCredentials(email: string, excludeMessageId?: string) {
  const base = process.env.TV_MAILPIT_URL
  if (!base) throw new Error('TV_MAILPIT_URL is required for generated credential browser tests')
  let messageId = ''
  await expect.poll(async () => {
    const response = await fetch(`${base}/api/v1/messages`)
    if (!response.ok) return false
    const mailbox = await response.json() as { messages: { ID: string; To: { Address: string }[] }[] }
    messageId = mailbox.messages.find(message => message.ID !== excludeMessageId && message.To.some(recipient => recipient.Address.toLowerCase() === email.toLowerCase()))?.ID || ''
    return Boolean(messageId)
  }, { timeout: 30000, message: 'Generated credential email should arrive in the disposable mailbox' }).toBe(true)
  const message = await (await fetch(`${base}/api/v1/message/${messageId}`)).json() as { Text: string }
  const username = /^Username:\s*(.+)$/m.exec(message.Text)?.[1].trim()
  const password = /^Contraseña temporal:\s*(.+)$/m.exec(message.Text)?.[1].trim()
  if (!username || !password) throw new Error('Credential email does not contain the expected template fields')
  return { username, password, messageId }
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
  return response.json() as Promise<RecordData>
}

export async function activateLocal(request: APIRequestContext, email: string) {
  const temporary = await temporaryCredentials(email)
  const login = await request.post('/api/v1/auth/login', { data: { username: temporary.username, password: temporary.password } })
  expect(login.status(), 'Temporary credentials authenticate').toBe(200)
  const restricted = await login.json() as RecordData
  expect(restricted.user.must_change_password).toBe(true)
  expect((await request.get('/api/v1/datasets')).status()).toBe(403)
  const response = await request.post('/api/v1/auth/first-login/change-password', { headers: { 'X-CSRF-Token': restricted.csrf_token }, data: { new_password: `Local completed ${crypto.randomUUID()}!` } })
  expect(response.status(), 'First access password change succeeds').toBe(200)
  return response.json() as Promise<RecordData>
}
