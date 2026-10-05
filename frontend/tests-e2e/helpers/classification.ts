import { expect, type Page } from '@playwright/test'

/** Create real shared identities through the upload UI, without submitting the enclosing acquisition. */
export async function createClassification(page: Page, macroName: string, domainName: string) {
  const identities: string[] = []
  for (const [kind, name] of [['macrodominio', macroName], ['dominio', domainName]]) {
    await page.getByRole('button', { name: `Crear ${kind}`, exact: true }).click()
    const dialog = page.getByRole('dialog', { name: `Crear ${kind}`, exact: true })
    await expect(dialog.getByRole('button', { name: 'Crear', exact: true })).toBeDisabled()
    await dialog.getByLabel('Nombre', { exact: true }).fill(name)
    const submitted = page.waitForResponse(response => new URL(response.url()).pathname === `/api/v1/governance/${kind === 'macrodominio' ? 'macrodomains' : 'domains'}` && response.request().method() === 'POST')
    await dialog.getByRole('button', { name: 'Crear', exact: true }).click()
    const response = await submitted
    expect(response.status(), await response.text()).toBe(201)
    identities.push((await response.json()).id)
    await expect(dialog).not.toBeVisible()
  }
  return { macro_domain_id: identities[0], domain_id: identities[1] }
}
