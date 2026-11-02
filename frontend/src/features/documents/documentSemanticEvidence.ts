export type DocumentaryEvidenceReference = string | Record<string, unknown>

interface SourceCheck {
  status?: string
  reasons?: string[]
  source_refs?: DocumentaryEvidenceReference[]
}

export interface SemanticSourceChecks {
  version?: string
  integrity?: SourceCheck
  text?: SourceCheck & { available_pages?: number[]; missing_pages?: number[] }
  execution_pages?: SourceCheck & { physical_pages?: number[]; processed_pages?: number[]; missing_pages?: number[] }
  associations?: SourceCheck & { verified?: number; pending?: number }
  cell_corroboration?: SourceCheck & { verified?: number; pending?: number }
  table_coverage?: SourceCheck & { verified?: string[]; pending?: string[] }
  documentary_completeness?: SourceCheck & { scope?: string }
  automatic_eligibility?: SourceCheck
}

export interface SemanticDocumentReview {
  verification_version?: string
  source_checks?: SemanticSourceChecks
  printed_pagination?: PrintedPagination
}

export interface PrintedPagination {
  version?: string
  status?: string
  physical_pages?: number[]
  processed_pages?: number[]
  missing_execution_pages?: number[]
  observations?: { literal?: string; physical_page?: number; number?: number; total?: number; scope?: string; scope_label?: string | null; source_refs?: DocumentaryEvidenceReference[]; independent?: boolean; geometry_kind?: string; mechanism?: string; status?: string; reason?: string }[]
  sequences?: { scope?: string; scope_label?: string | null; expected_total?: number; observed_numbers?: number[]; missing_logical_numbers?: number[]; physical_pages?: number[]; status?: string; source_refs?: DocumentaryEvidenceReference[] }[]
  ignored_mentions?: unknown[]
  reasons?: string[]
  documentary_complete?: boolean | null
  blocking?: boolean
}

export interface SourceAssociationTrace {
  status?: string
  eligible_for_field?: boolean
  observed?: unknown
  raw_observed?: unknown
  label_text?: string
  value_text?: string
  page?: number
  label_refs?: DocumentaryEvidenceReference[]
  value_refs?: DocumentaryEvidenceReference[]
  source_refs?: DocumentaryEvidenceReference[]
  source_section?: string | null
  requested_section?: string | null
  mechanism?: string
  reason?: string
}

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
    rejected_associations?: SourceAssociationTrace[]
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
  SOURCE_EVIDENCE_LIMIT: 'La evidencia supera el límite permitido para comprobarla',
  SOURCE_STRUCTURE_INTEGRITY: 'La integridad de la estructura documental necesita comprobarse',
  INDEPENDENT_REQUEST_UNVERIFIED: 'La independencia de la petición de reconocimiento no está demostrada',
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
  UNCLASSIFIED_SOURCE_SCOPE: 'El alcance de una parte de la fuente todavía no está demostrado',
  UNPARSED_SOURCE_STRUCTURE: 'Hay estructura de la fuente que todavía no pudo asociarse de forma segura',
  SOURCE_DATA_NOT_OBSERVED: 'El dato solicitado no se ha localizado en la fuente admitida; su ausencia documental no está demostrada',
  ROW_ASSOCIATION_NOT_PROVEN: 'La relación entre la ocurrencia de origen y esta fila no está demostrada',
  SOURCE_ROLE_MISMATCH: 'La observación pertenece a otra etiqueta o función documental y conserva su procedencia',
  CAPTION_OWNED_BY_DIFFERENT_ROLE: 'La etiqueta completa corresponde a otra función documental',
  TARGET_IS_ROLE_CAPTION: 'El contenido relacionado es una etiqueta de otro dato, no el valor de este campo',
  TARGET_CAPTION_ROLE_UNPROVEN: 'La función de la etiqueta relacionada todavía no está demostrada',
  OBSERVED_SECTION_OWNS_DIFFERENT_ROLE: 'La sección observada corresponde a otra función documental',
  ANCHOR_IS_ROLE_CAPTION_WITHOUT_VALUE: 'La etiqueta de origen no aporta un valor independiente para este campo',
  DECLARED_MONETARY_ROLE_VALUE_SUFFIX: 'Valor monetario vinculado a su etiqueta mediante el formato declarado',
  MONETARY_SUFFIX_UNRESOLVED: 'El contenido monetario conserva un formato o una interpretación sin resolver',
  DECLARED_VALUE_PATTERN_UNRESOLVED: 'El contenido no permite resolver el patrón de valor declarado',
  INLINE_CAPTION_RELATION_UNPROVEN: 'La relación entre la etiqueta completa y el contenido de la línea no está demostrada',
  SOURCE_SECTION_AMBIGUOUS: 'La sección de origen permite más de una interpretación',
  DOCUMENTARY_SOURCE_CONTRADICTION: 'La evidencia documental conserva valores contradictorios para este dato',
  SOURCE_INVENTORY_CONTRADICTION: 'Los inventarios completos de fuentes independientes no coinciden; la cobertura y sus celdas requieren revisión',
  SOURCE_SCOPE_INCOMPLETE: 'El ámbito completo de la fuente todavía no está acreditado',
  DECLARED_SOURCE_SCOPE_MISMATCH: 'La fuente sitúa el contenido en otra sección o fuera del ámbito declarado',
  DECLARED_SOURCE_SCOPE_UNPROVEN: 'El ámbito declarado no puede comprobarse con la estructura o geometría disponible',
  PAGE_EXECUTION_INCOMPLETE: 'Faltan páginas del archivo por procesar en esta lectura',
  PRINTED_PAGINATION_UNCORROBORATED: 'La numeración propuesta necesita corroboración independiente',
  PRINTED_PAGINATION_LIMIT: 'La numeración supera los límites permitidos para comprobarla',
  PRINTED_PAGINATION_CONFLICT: 'La numeración impresa de la fuente conserva una inconsistencia',
  PRINTED_SCOPE_UNPROVEN: 'No está demostrado si la numeración corresponde al documento o a una sección',
  DOCUMENT_SOURCE_INCOMPLETE: 'La numeración acreditada indica que faltan páginas del documento recibido',
  SECTION_SOURCE_INCOMPLETE: 'La fuente acredita que faltan páginas de una sección',
  PRINTED_SEQUENCE_UNVERIFIED: 'La secuencia de numeración impresa no está completamente acreditada',
  NARRATIVE_SEMANTICS_UNVERIFIED: 'El sentido y la cobertura de la colección narrativa necesitan revisión',
  DECLARED_BUSINESS_RULE_FAILED: 'No se cumple una regla de negocio declarada aplicable al documento',
  CURRENT_CELL_DIFFERS_FROM_ATTESTED_VALUE: 'El valor actual difiere del valor acreditado por la prueba anterior',
  CURRENT_TABLE_DIFFERS_FROM_ATTESTED_VALUE: 'La tabla actual difiere de la tabla acreditada por la prueba anterior',
  HUMAN_REVIEW_APPLIED: 'La revisión autorizada se conserva separada de la aprobación automática',
  REVIEW_REQUIRED: 'El dato necesita revisión contra el original',
  QUALITY_VALUE: 'El dato está ausente, es ambiguo o tiene un formato sin resolver',
  QUALITY_DURABLE_SOURCE_EVIDENCE: 'La evidencia conservada no tiene acreditado su vínculo íntegro con la fuente',
  QUALITY_DOCUMENT_STRUCTURE: 'La estructura documental conserva controles pendientes',
  QUALITY_ROW_COVERAGE: 'No está acreditada la cobertura de todas las ocurrencias de la colección',
  QUALITY_ARITHMETIC: 'Los controles aritméticos declarados necesitan resolución',
  SEMANTIC_TABLE_COVERAGE_REVIEW_REQUIRED: 'La cobertura completa de la colección necesita acreditarse contra la fuente',
  SEMANTIC_INDEPENDENT_REVIEW_REQUIRED: 'La observación semántica necesita revisión contra la fuente',
  SEMANTIC_CONFLICT: 'Las observaciones de origen conservan un conflicto que necesita resolución',
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
  MEASURED_ROLE_COLUMNS: 'Columnas vinculadas por su función y geometría medida',
  MEASURED_CONTINUATION_OF_PREVIOUS_ROLE_COLUMNS: 'Continuación de columnas acreditada por su función y geometría medida',
  LOGICAL_ROLE_COLUMNS: 'Columnas lógicas de la tabla vinculadas por su función',
  LOGICAL_CONTINUATION_OF_PREVIOUS_ROLE_COLUMNS: 'Continuación acreditada de columnas lógicas de la tabla',
}

export function evidenceMechanism(name: string): string { return mechanisms[name] || name }
export function semanticReasonDescription(code: string): string {
  const [name, ...location] = code.split(':')
  return reasons[name] ? `${reasons[name]}${location.length ? `: ${location.join(' · ')}` : ''}` : code
}
export function semanticAssociationDescription(basis: string): string { return associations[basis] || basis }

export function associationDescription(association: DocumentaryAssociation): string {
  const relations: Record<string, string> = { LABEL_VALUE_INLINE: 'Etiqueta y valor en la misma línea', LABEL_VALUE_MEASURED_BELOW: 'Valor localizado debajo de su etiqueta' }
  const scope: Record<string, string> = { EVIDENCE_ONLY: 'Referencia de evidencia', PRINTED_COLUMN: 'Columna impresa', OPTIONAL_COLUMN_ABSENT_COMPLETE_HEADER: 'Columna opcional ausente en encabezado completo' }
  return [association.basis && semanticAssociationDescription(association.basis), association.label && `Etiqueta: ${association.label}`,
    association.relation && (relations[association.relation] || semanticReasonDescription(association.relation)), association.column_role && `Campo de columna: ${association.column_role}`,
    association.source_occurrence && `Ocurrencia: ${association.source_occurrence}`, association.scope && (scope[association.scope] || association.scope),
    association.section ? `Sección observada: ${association.section}` : 'Sección de origen no demostrada',
    association.requested_section && `Sección solicitada por el contrato: ${association.requested_section}`].filter(Boolean).join(' · ')
}

export function evidenceReference(reference: DocumentaryEvidenceReference): string {
  if (typeof reference === 'string') return reference
  const id = reference.observation_id ?? reference.id ?? reference.artifact_id
  const hash = reference.artifact_sha256 ?? reference.artifact_sha ?? reference.sha256 ?? reference.event_sha256 ?? reference.attestation_sha256 ?? reference.source_sha256 ?? reference.table_sha256
  const events = Array.isArray(reference.event_hashes) ? reference.event_hashes.filter((value): value is string => typeof value === 'string') : []
  const stage = reference.stage ?? reference.mechanism
  const page = reference.source_page ?? reference.page
  return [reference.proof_scope === 'PRIOR_ATTESTED_CANDIDATE' ? 'Prueba de un candidato anterior' : reference.proof_scope === 'UNASSOCIATED_LITERAL_ONLY' ? 'Literal sin asociación acreditada; no corrobora este campo' : null,
    typeof id === 'string' ? id : null, typeof hash === 'string' ? `SHA-256 ${hash}` : null, ...events.map(event => `Evento SHA-256 ${event}`), typeof stage === 'string' ? evidenceMechanism(stage) : null,
    typeof page === 'number' && Number.isInteger(page) && page > 0 ? `Página ${page}` : null].filter(Boolean).join(' · ') || 'Referencia de evidencia conservada'
}

export function automaticallyVerified(review?: SemanticValueReview): boolean {
  return review?.state === 'VERIFIED_AUTOMATICALLY' && review.review_required !== true && review.corroboration?.status === 'VERIFIED'
}
