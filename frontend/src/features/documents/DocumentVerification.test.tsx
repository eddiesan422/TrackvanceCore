import { screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { renderApp } from '../../test/render'
import DocumentVerification from './DocumentVerification'

const reason = { code: 'INDEPENDENT_SOURCE_REQUIRED', path: 'headers.0.invoice_number' }
const verification = { policy_version: 'TRACKVANCE_AUTOMATIC_VERIFICATION_098_V1', status: 'VERIFYING',
  reasons_before: [reason], remaining_reasons: [reason], resolved_paths: [],
  scope: { kind: 'WHOLE_DOCUMENT', pages: [1, 2] }, primary_reused: true, human_reviewed: false }

describe('Automatic document verification visibility', () => {
  it('binds deterministic reliability to current evidence and gives pending controls their exact row', () => {
    const reliability = { version: 'SEMANTIC_RELIABILITY_V1', policy_sha256: 'policy-hash', calibration: { status: 'PENDING' },
      binding: { payload_sha256: 'current-value-hash', definition_sha256: 'contract-hash', source_attestation_sha256: 'source-proof-hash' }, tables: [],
      document: { status: 'REVIEW_REQUIRED', reasons: ['OBSERVATION_UNCERTAINTY:items.1.amount'], required_cells: 3, critical_cells: 1,
        verified_cells: 2, reviewed_cells: 0, pending_cells: 1, gates: { business_rules: { status: 'PENDING', reasons: ['QUALITY_BUSINESS_UNVERIFIED'] } } } }
    const { rerender } = renderApp(<DocumentVerification semanticReview={{ verification_version: 'SEMANTIC_AUTO_V2', reliability }}/>)
    const panel = screen.getByLabelText('Fiabilidad de la revisión actual')
    expect(within(panel).getByText(/Calibración estadística pendiente/)).toBeVisible()
    expect(within(panel).getByText('Celdas corroboradas: 2 · revisadas de forma autorizada: 0 · pendientes: 1')).toBeVisible()
    expect(within(panel).getByText(/items · fila 2 · amount/)).toBeVisible()
    expect(within(panel).getByText('Reglas de negocio: pendientes de comprobar')).toBeVisible()
    expect(within(panel).getByText('Revisión de valores: current-value-hash')).toBeInTheDocument()
    expect(panel.textContent).not.toMatch(/\d\s*%|100 %|probabilidad/)
    rerender(<DocumentVerification semanticReview={{ verification_version: 'SEMANTIC_AUTO_V1', reliability }}/>)
    expect(screen.queryByLabelText('Fiabilidad de la revisión actual')).not.toBeInTheDocument()
  })

  it('keeps conflicting complete independent inventories in review without attributing the discrepancy to the model', () => {
    renderApp(<DocumentVerification verification={{ ...verification, status: 'REVIEW_REQUIRED', reasons_before: [], remaining_reasons: [{ code: 'SOURCE_INVENTORY_CONTRADICTION', table: 'items' }] }}/>)
    expect(screen.getByText('Verificación terminada con excepciones')).toBeVisible()
    expect(screen.getByText('Los inventarios completos de fuentes independientes no coinciden; la cobertura y sus celdas requieren revisión: items')).toBeVisible()
    expect(screen.queryByText(/SOURCE_INVENTORY_CONTRADICTION|error del modelo|Aprobación automática verificada/)).not.toBeInTheDocument()
    expect(screen.queryByRole('checkbox')).not.toBeInTheDocument()
  })

  it('identifies a missing logical page without inventing a gap in the completed physical reading', () => {
    renderApp(<DocumentVerification semanticReview={{ verification_version: 'SEMANTIC_AUTO_V2', printed_pagination: {
      version: 'DOCUMENT_PRINTED_PAGINATION_V1', status: 'INCOMPLETE', physical_pages: [1, 2], processed_pages: [1, 2], missing_execution_pages: [],
      observations: [{ literal: 'Página 1 de 3', physical_page: 1, number: 1, total: 3, scope: 'DOCUMENT', independent: true, status: 'SUPPORTED', source_refs: [{ id: 'measured-footer', page: 1, artifact_sha256: 'footer-sha' }] }],
      sequences: [{ scope: 'DOCUMENT', expected_total: 3, observed_numbers: [1, 3], missing_logical_numbers: [2], physical_pages: [1, 2], status: 'INCOMPLETE', source_refs: [{ id: 'footer-sequence' }] }],
      reasons: ['DOCUMENT_SOURCE_INCOMPLETE'], documentary_complete: null, blocking: true,
    } }}/>)
    expect(screen.getByText(/Páginas físicas recibidas: 1, 2 · procesadas: 1, 2 · pendientes de lectura: ninguna/)).toBeVisible()
    expect(screen.getByText(/Ámbito: Documento · números observados: 1, 3 · total declarado: 3 · números documentales faltantes acreditados: 2/)).toBeVisible()
    expect(screen.getByText('La numeración acreditada indica que faltan páginas del documento recibido')).toBeVisible()
    expect(screen.getByText('Este control mantiene bloqueada la aprobación automática.')).toBeVisible()
    expect(screen.queryByText('Faltan páginas del archivo por procesar en esta lectura')).not.toBeInTheDocument()
    expect(screen.queryByText(/Aprobación automática verificada|DOCUMENT_SOURCE_INCOMPLETE/)).not.toBeInTheDocument()
  })

  it('keeps consistent section numbering separate from the global received-page count', () => {
    renderApp(<DocumentVerification semanticReview={{ verification_version: 'SEMANTIC_AUTO_V2', printed_pagination: {
      status: 'CONSISTENT', physical_pages: [1, 2, 3], processed_pages: [1, 2, 3], missing_execution_pages: [],
      sequences: [{ scope: 'SECTION', scope_label: 'Anexo A', expected_total: 2, observed_numbers: [1, 2], missing_logical_numbers: [], physical_pages: [2, 3], status: 'CONSISTENT' }],
      reasons: [], documentary_complete: null, blocking: false,
    } }}/>)
    expect(screen.getByText('La numeración impresa acreditada es coherente dentro de su ámbito.')).toBeVisible()
    expect(screen.getByText(/Ámbito: Sección: Anexo A · números observados: 1, 2 · total declarado: 2 · números impresos no observados: ninguna/)).toBeVisible()
    expect(screen.queryByText(/páginas documentales ausentes|mantiene bloqueada|Secuencia impresa acreditada completa/)).not.toBeInTheDocument()
  })

  it('shows which printed continuation conflicts with the document counter and preserves its sources', () => {
    renderApp(<DocumentVerification semanticReview={{ verification_version: 'SEMANTIC_AUTO_V2', printed_pagination: {
      version: 'DOCUMENT_PRINTED_PAGINATION_V2', status: 'CONFLICT', physical_pages: [1], processed_pages: [1], missing_execution_pages: [],
      continuations: [
        { literal: 'Continúa en la hoja 4 de esta factura.', physical_page: 1, target_printed_page: 4, scope: 'DOCUMENT',
          independent: true, status: 'CONFLICT', reason: 'PRINTED_PAGINATION_CONFLICT',
          source_refs: [{ id: 'continuation-clause' }], counter_refs: [{ id: 'document-counter' }] },
      ],
      reasons: ['PRINTED_PAGINATION_CONFLICT'], documentary_complete: null, blocking: true,
    } }}/>)
    const details = screen.getByText('Continuaciones impresas y procedencia (1)').parentElement!
    const continuation = within(details).getByRole('listitem', { hidden: true })
    expect(continuation).toHaveTextContent('Continúa en la hoja 4 de esta factura.')
    expect(continuation).toHaveTextContent('página física: 1 · destino impreso: 4')
    expect(continuation).toHaveTextContent('En conflicto con la numeración impresa · ámbito registrado: Documento')
    expect(continuation).toHaveTextContent('Evidencia de la continuación: continuation-clause')
    expect(continuation).toHaveTextContent('Numeración relacionada: document-counter')
    expect(screen.getByText('Este control mantiene bloqueada la aprobación automática.')).toBeVisible()
    expect(screen.queryByText(/Secuencia impresa acreditada completa|PRINTED_PAGINATION_CONFLICT/)).not.toBeInTheDocument()
  })

  it('does not label partially observed section counters as missing pages and translates current proof invalidation reasons', () => {
    renderApp(<DocumentVerification semanticReview={{ verification_version: 'SEMANTIC_AUTO_V2', source_checks: {
      documentary_completeness: { status: 'PENDING', reasons: ['CURRENT_TABLE_DIFFERS_FROM_ATTESTED_VALUE'] },
      automatic_eligibility: { status: 'BLOCKED', reasons: ['REVIEW_REQUIRED:movements.0.amount', 'HUMAN_REVIEW_APPLIED'] },
    }, printed_pagination: { status: 'CONSISTENT', physical_pages: [1, 2, 3], processed_pages: [1, 2, 3], missing_execution_pages: [],
      sequences: [{ scope: 'SECTION', scope_label: 'Anexo B', expected_total: 2, observed_numbers: [1], missing_logical_numbers: [2], physical_pages: [2], status: 'PARTIALLY_OBSERVED' }], documentary_complete: null, blocking: false,
    } }}/>)
    expect(screen.getByText(/números impresos no observados: 2/)).toBeVisible()
    expect(screen.getByText('La observación parcial de la numeración no demuestra que falten páginas de esta sección.')).toBeVisible()
    expect(screen.queryByText(/números documentales faltantes acreditados/)).not.toBeInTheDocument()
    expect(screen.getByText('La tabla actual difiere de la tabla acreditada por la prueba anterior')).toBeVisible()
    expect(screen.getByText('El dato necesita revisión contra el original: movements.0.amount')).toBeVisible()
    expect(screen.getByText('La revisión autorizada se conserva separada de la aprobación automática')).toBeVisible()
    expect(screen.queryByText(/CURRENT_TABLE_DIFFERS|REVIEW_REQUIRED:|HUMAN_REVIEW_APPLIED/)).not.toBeInTheDocument()
  })

  it('does not turn unobserved numbering or an uncorroborated model proposal into documentary completeness', () => {
    const { rerender } = renderApp(<DocumentVerification semanticReview={{ verification_version: 'SEMANTIC_AUTO_V2', printed_pagination: { status: 'NOT_OBSERVED', physical_pages: [1], processed_pages: [1], missing_execution_pages: [], observations: [], sequences: [], documentary_complete: null, blocking: false } }}/>)
    expect(screen.getByText('No se ha acreditado numeración impresa. Esto no demuestra que el documento esté completo.')).toBeVisible()
    expect(screen.queryByText(/Secuencia impresa acreditada completa|páginas documentales ausentes/)).not.toBeInTheDocument()
    rerender(<DocumentVerification semanticReview={{ verification_version: 'SEMANTIC_AUTO_V2', printed_pagination: { status: 'UNVERIFIED', physical_pages: [1], processed_pages: [1], missing_execution_pages: [], observations: [{ literal: 'Page 1 of 2', physical_page: 1, number: 1, total: 2, scope: 'UNKNOWN', independent: false, status: 'UNVERIFIED' }], sequences: [], reasons: ['PRINTED_PAGINATION_UNCORROBORATED', 'PRINTED_SCOPE_UNPROVEN'], documentary_complete: null, blocking: true } }}/>)
    expect(screen.getByText('La numeración propuesta necesita corroboración independiente')).toBeVisible()
    expect(screen.getByText('No está demostrado si la numeración corresponde al documento o a una sección')).toBeVisible()
    expect(screen.queryByText(/Respaldada por fuente independiente|números documentales faltantes: 2|Secuencia impresa acreditada completa/)).not.toBeInTheDocument()
  })

  it('keeps available native text and processed pages distinct from association, table and documentary completeness', () => {
    renderApp(<DocumentVerification verification={{ ...verification, status: 'REVIEW_REQUIRED', scope: { kind: 'WHOLE_DOCUMENT', pages: [1] },
      reasons_before: [], remaining_reasons: [{ code: 'UNCLASSIFIED_SOURCE_SCOPE', table: 'movements' }] }} semanticReview={{ verification_version: 'SEMANTIC_AUTO_V2', source_checks: {
      version: 'SEMANTIC_SOURCE_CHECKS_V2', integrity: { status: 'VERIFIED', source_refs: [{ id: 'source-file', artifact_sha256: 'file-proof' }] },
      text: { status: 'AVAILABLE', available_pages: [1], missing_pages: [] }, execution_pages: { status: 'COMPLETE', physical_pages: [1], processed_pages: [1], missing_pages: [] },
      associations: { status: 'PENDING', verified: 1, pending: 4 }, cell_corroboration: { status: 'PENDING', verified: 1, pending: 4 },
      table_coverage: { status: 'PENDING', verified: [], pending: ['movements'] }, documentary_completeness: { status: 'PENDING', scope: 'DECLARED_EXTRACTION_CONTRACT', reasons: [] },
      automatic_eligibility: { status: 'BLOCKED', reasons: [] },
    } }}/>)
    expect(screen.getByText('Texto de fuente').parentElement).toHaveTextContent('Disponible · páginas con texto: 1 · sin texto admitido: ninguna')
    expect(screen.getByText('Páginas del archivo').parentElement).toHaveTextContent('Recibidas: 1 · procesadas: 1 · pendientes de lectura: ninguna')
    expect(screen.getByText('Asociaciones campo–valor').parentElement).toHaveTextContent('Pendiente · acreditadas: 1 · pendientes: 4')
    expect(screen.getByText('Cobertura de tablas').parentElement).toHaveTextContent('Pendiente · acreditadas: ninguna · pendientes: movements')
    expect(screen.getByText('Completitud documental').parentElement).toHaveTextContent('Pendiente · para la información solicitada por el contrato')
    expect(screen.getByText('Elegibilidad automática').parentElement).toHaveTextContent('No acreditada para aprobación automática')
    expect(screen.getByText(/El alcance de una parte de la fuente todavía no está demostrado: movements/)).toBeVisible()
    expect(screen.getByText(/Alcance de lectura: todas las páginas recibidas/)).toBeVisible()
    expect(screen.queryByText(/documento completo/)).not.toBeInTheDocument()
    expect(screen.queryByText(/UNCLASSIFIED_SOURCE_SCOPE|Aprobación automática verificada|error del modelo/)).not.toBeInTheDocument()
    expect(screen.queryByRole('checkbox')).not.toBeInTheDocument()
  })

  it('distinguishes missing source or coverage from an arithmetic discrepancy over corroborated inputs', () => {
    const { rerender } = renderApp(<DocumentVerification verification={{ ...verification, status: 'REVIEW_REQUIRED', reasons_before: [],
      remaining_reasons: [{ code: 'QUALITY_BUSINESS_UNVERIFIED', table: 'items' }] }}/>)
    expect(screen.getByText(/Falta comprobar la fuente o la cobertura necesaria para evaluar la coherencia de los importes: items/)).toBeVisible()
    expect(screen.queryByText(/discrepancia|contradicción/i)).not.toBeInTheDocument()
    expect(screen.queryByText(/QUALITY_BUSINESS_UNVERIFIED/)).not.toBeInTheDocument()
    rerender(<DocumentVerification verification={{ ...verification, status: 'REVIEW_REQUIRED', reasons_before: [],
      remaining_reasons: [{ code: 'QUALITY_BUSINESS_DISCREPANCY', table: 'items' }] }}/>)
    expect(screen.getByText(/Los importes acreditados en la fuente presentan una discrepancia en el cálculo declarado: items/)).toBeVisible()
    expect(screen.queryByText(/Falta comprobar la fuente o la cobertura/)).not.toBeInTheDocument()
    expect(screen.queryByText(/QUALITY_BUSINESS_DISCREPANCY/)).not.toBeInTheDocument()
    expect(screen.queryByRole('checkbox')).not.toBeInTheDocument()
  })

  it('shows the additional reading reason, scope and reuse without demanding confirmation', () => {
    renderApp(<DocumentVerification verification={verification}/>)
    expect(screen.getByRole('region', { name: 'Verificación automática de la extracción' })).toBeVisible()
    expect(screen.getByText('Comprobando la extracción')).toBeVisible()
    expect(screen.getByText(/Se reutilizó el reconocimiento original/)).toBeVisible()
    expect(screen.getByText(/documento completo \(2 páginas\)/)).toBeVisible()
    expect(screen.getByText(/No necesitas confirmar campos/)).toBeVisible()
    expect(screen.queryByRole('checkbox')).not.toBeInTheDocument()
  })

  it('distinguishes resolved controls from remaining exceptions and leaves approval to the validator', () => {
    renderApp(<DocumentVerification verification={{ ...verification, status: 'REVIEW_REQUIRED',
      remaining_reasons: [{ code: 'INDEPENDENT_ROW_STRUCTURE_REQUIRED', table: 'detalle' }],
      resolved_paths: ['headers.0.invoice_number'] }}/>)
    expect(screen.getByText('Verificación terminada con excepciones')).toBeVisible()
    expect(screen.getByText('Controles resueltos (1)')).toBeVisible()
    expect(screen.getByText(/Falta comprobar la estructura y continuidad de la tabla: detalle/)).toBeVisible()
    expect(screen.getByText(/Los campos ya verificados no necesitan confirmación/)).toBeVisible()
    expect(screen.queryByText('Aprobación automática verificada')).not.toBeInTheDocument()
  })

  it('explains a frozen semantic opt-out instead of silently changing its configuration', () => {
    renderApp(<DocumentVerification verification={{ ...verification, status: 'BLOCKED',
      blocked_reason: 'QWEN_DISABLED_IN_FROZEN_CONFIGURATION' }}/>)
    expect(screen.getByText(/La configuración congelada no permite la lectura semántica/)).toBeVisible()
    expect(screen.getByText(/este resultado conserva su configuración/)).toBeVisible()
  })

  it('does not label completed controls as human acceptance', () => {
    renderApp(<DocumentVerification verification={{ ...verification, status: 'CONTROLS_COMPLETED', remaining_reasons: [] }}/>)
    expect(screen.getByText('Controles adicionales completados')).toBeVisible()
    expect(screen.getByText(/Esta comprobación no declara revisión humana/)).toBeVisible()
    expect(screen.queryByText(/Controles que siguen pendientes/)).not.toBeInTheDocument()
  })

  it('explains the missing validation policy before another reading can be requested', () => {
    renderApp(<DocumentVerification verification={{ ...verification, status: 'BLOCKED',
      remaining_reasons: [{ kind: 'CONFIGURATION', code: 'VALIDATION_POLICY_REQUIRED' }],
      blocked_reason: 'DECLARED_CONTROLS_OR_SOURCE_BINDING_REQUIRED' }}/>)
    expect(screen.getByText(/Falta declarar la política de validación de esta plantilla antes de solicitar otra lectura/)).toBeVisible()
    expect(screen.queryByText('VALIDATION_POLICY_REQUIRED')).not.toBeInTheDocument()
    expect(screen.queryByRole('checkbox')).not.toBeInTheDocument()
  })
})
