export type DocumentaryEvidenceReference = string | Record<string, unknown>

export interface DocumentaryAssociation {
  basis?: string
  label?: string
  section?: string | null
  requested_section?: string
  relation?: string
  column_role?: string
  source_occurrence?: string
  scope?: string
}

export interface SemanticValueReview {
  path?: string
  state?: string
  observed?: unknown
  normalized?: unknown
  verified?: unknown
  review_required?: boolean
  evidence_refs?: DocumentaryEvidenceReference[]
  corroboration?: {
    status?: string
    reasons?: string[]
    mechanisms?: string[]
    source_refs?: DocumentaryEvidenceReference[]
    association?: DocumentaryAssociation | DocumentaryAssociation[]
    prior_proof?: Record<string, unknown>
  }
}

const mechanisms: Record<string, string> = {
  PDF_NATIVE: 'Texto nativo del PDF', NATIVE: 'Texto nativo del PDF', PADDLE: 'Reconocimiento estructural Paddle',
  PADDLE_STRUCTURED: 'Reconocimiento estructural Paddle', QWEN: 'Observación visual Qwen', QWEN_VISUAL: 'Observación visual Qwen',
}

const reasons: Record<string, string> = {
  SOURCE_EVIDENCE_UNAVAILABLE: 'No hay evidencia de fuente suficiente para corroborar el dato',
  SOURCE_REGISTRY_BINDING: 'La evidencia debe vincularse al registro del original',
  SOURCE_ARTIFACT_INTEGRITY: 'La integridad del artefacto de fuente necesita comprobarse',
  SOURCE_EVENT_INTEGRITY: 'La integridad del registro de reconocimiento necesita comprobarse',
  SOURCE_IR_INTEGRITY: 'La representación documental necesita comprobar su integridad',
  SOURCE_CONFIGURATION_BINDING: 'La evidencia debe vincularse a la configuración congelada',
  RECOGNITION_INCOMPLETE_OR_RESOURCES_UNVERIFIED: 'El reconocimiento o la liberación de recursos siguen pendientes',
  VALUE_UNRESOLVED: 'El valor todavía no está resuelto',
  OBSERVATION_UNCERTAINTY: 'Las observaciones conservan incertidumbre o alternativas relevantes',
  SOURCE_VALUE_BINDING: 'El valor necesita una asociación demostrable con su fuente',
  INDEPENDENT_SOURCE_REQUIRED: 'El valor necesita corroboración de un mecanismo distinto',
  ASSOCIATION_NOT_PROVEN: 'La relación entre el valor y el campo solicitado no está demostrada',
  ASSOCIATION_AMBIGUOUS: 'La fuente permite más de una asociación con el campo solicitado',
  SOURCE_CONTRADICTION: 'La fuente conserva valores contradictorios',
  OPTIONAL_ABSENCE_UNVERIFIED: 'La ausencia opcional no está acreditada contra la fuente',
  ROW_COVERAGE_UNPROVEN: 'La cobertura de todas las filas no está acreditada',
  ROW_IDENTITY_AMBIGUOUS: 'La identidad de una ocurrencia de fila sigue siendo ambigua',
  UNMATCHED_SOURCE_ROW: 'Hay una fila de la fuente sin correspondencia en el resultado',
  UNMATCHED_OUTPUT_ROW: 'Hay una fila del resultado sin correspondencia en la fuente',
  MISSING_REQUIRED_COLUMN: 'No está acreditada una columna obligatoria',
  UNASSOCIATED_SOURCE_TABLE: 'Una tabla de la fuente no está asociada con la colección solicitada',
  UNRESOLVED_TABLE_CONTINUATION: 'La continuación de una tabla entre páginas sigue pendiente',
  NARRATIVE_SEMANTICS_UNVERIFIED: 'El sentido y la cobertura de la colección narrativa necesitan revisión',
  DECLARED_BUSINESS_RULE_FAILED: 'No se cumple una regla de negocio declarada aplicable al documento',
  CURRENT_CELL_DIFFERS_FROM_ATTESTED_VALUE: 'El valor actual difiere del valor acreditado por la prueba anterior',
  TEXT_SCOPE_UNVERIFIED: 'El alcance completo del texto necesita revisión contra la fuente',
  QUALITY_BUSINESS_SCOPE_REQUIRED: 'Falta declarar que los controles representan la factura completa',
  QUALITY_BUSINESS_CONTRACT_REQUIRED: 'La declaración de controles de cabecera y detalle necesita comprobarse para este perfil',
  QUALITY_BUSINESS_UNVERIFIED: 'Falta comprobar la fuente o la cobertura necesaria para evaluar la coherencia de los importes',
  QUALITY_BUSINESS_DISCREPANCY: 'Los importes acreditados en la fuente presentan una discrepancia en el cálculo declarado',
  QUALITY_BUSINESS_UNKNOWN: 'No se pudo determinar el resultado del control de importes',
  QUALITY_BUSINESS_SOURCE_SCOPE_UNVERIFIED: 'El alcance completo de la factura no está demostrado para comprobar sus importes',
}

const associations: Record<string, string> = {
  NATIVE_LABEL_VALUE: 'Etiqueta y valor vinculados en texto nativo', NATIVE_MEASURED_COLUMN: 'Ocurrencia vinculada a una columna medida del PDF',
  PADDLE_LABEL_VALUE: 'Etiqueta y valor vinculados en la lectura estructural', PADDLE_LOGICAL_COLUMN: 'Ocurrencia vinculada a una columna lógica de la tabla',
  PDF_NATIVE_LABEL_VALUE: 'Etiqueta y valor vinculados en texto nativo',
  PDF_NATIVE_SOURCE_COLUMN: 'Ocurrencia vinculada a una columna de la fuente PDF',
  PADDLE_SOURCE_COLUMN: 'Ocurrencia vinculada a una columna de la lectura estructural',
}

export function evidenceMechanism(name: string): string { return mechanisms[name] || name }
export function semanticReasonDescription(code: string): string { return reasons[code] || code }
export function semanticAssociationDescription(basis: string): string { return associations[basis] || basis }

export function associationDescription(association: DocumentaryAssociation): string {
  const relations: Record<string, string> = { LABEL_VALUE_INLINE: 'Etiqueta y valor en la misma línea', LABEL_VALUE_MEASURED_BELOW: 'Valor localizado debajo de su etiqueta' }
  const scope: Record<string, string> = { EVIDENCE_ONLY: 'Referencia de evidencia', PRINTED_COLUMN: 'Columna impresa', OPTIONAL_COLUMN_ABSENT_COMPLETE_HEADER: 'Columna opcional ausente en encabezado completo' }
  return [association.basis && semanticAssociationDescription(association.basis), association.label && `Etiqueta: ${association.label}`,
    association.relation && (relations[association.relation] || association.relation), association.column_role && `Campo de columna: ${association.column_role}`,
    association.source_occurrence && `Ocurrencia: ${association.source_occurrence}`, association.scope && (scope[association.scope] || association.scope),
    association.section ? `Sección observada: ${association.section}` : 'Sección de origen no demostrada',
    association.requested_section && `Sección solicitada por el contrato: ${association.requested_section}`].filter(Boolean).join(' · ')
}

export function evidenceReference(reference: DocumentaryEvidenceReference): string {
  if (typeof reference === 'string') return reference
  const id = reference.observation_id ?? reference.id ?? reference.artifact_id
  const hash = reference.artifact_sha256 ?? reference.artifact_sha ?? reference.sha256 ?? reference.event_sha256
  const events = Array.isArray(reference.event_hashes) ? reference.event_hashes.filter((value): value is string => typeof value === 'string') : []
  const stage = reference.stage ?? reference.mechanism
  const page = reference.source_page ?? reference.page
  return [reference.proof_scope === 'PRIOR_ATTESTED_CANDIDATE' ? 'Prueba de un candidato anterior' : null,
    typeof id === 'string' ? id : null, typeof hash === 'string' ? `SHA-256 ${hash}` : null, ...events.map(event => `Evento SHA-256 ${event}`), typeof stage === 'string' ? evidenceMechanism(stage) : null,
    typeof page === 'number' && Number.isInteger(page) && page > 0 ? `Página ${page}` : null].filter(Boolean).join(' · ') || 'Referencia de evidencia conservada'
}

export function automaticallyVerified(review?: SemanticValueReview): boolean {
  return review?.state === 'VERIFIED_AUTOMATICALLY' && review.review_required !== true && review.corroboration?.status === 'VERIFIED'
}
