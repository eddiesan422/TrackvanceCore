/** Shared metadata fixture; product selectors always read their organization-scoped API. */
export function governanceFixture(path: string) {
  if (path.startsWith('/governance/macrodomains')) return { items: [{ id: 'macro-risk', name: 'Riesgos', active: true, version: 1 }, { id: 'macro-finance', name: 'Finanzas', active: true, version: 1 }], total: 2 }
  if (path.startsWith('/governance/domains')) return { items: [{ id: 'domain-credit', name: 'Crédito', macro_domain_id: 'macro-risk', active: true, version: 1 }], total: 1 }
  return null
}
