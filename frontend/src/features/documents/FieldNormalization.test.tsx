import { useState } from 'react'
import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import type { RecordData } from '../../api/client'
import { renderApp } from '../../test/render'
import FieldNormalization from './FieldNormalization'

function ConfiguredField({ kind = 'DECIMAL' }: { kind?: string }) {
  const [normalization, setNormalization] = useState<RecordData | undefined>()
  return <><FieldNormalization field={{ name: 'amount', logical_type: kind, normalization }} onChange={setNormalization}/><output aria-label="Contrato elegido">{JSON.stringify(normalization || {})}</output></>
}

describe('Explicit field normalization declarations', () => {
  it('does not migrate a historical field until a declaration is selected', async () => {
    const user = userEvent.setup()
    renderApp(<ConfiguredField/>)
    expect(screen.getByLabelText('Contrato elegido')).toHaveTextContent('{}')
    await user.click(screen.getByRole('checkbox', { name: 'Declarar normalización de amount' }))
    await user.selectOptions(screen.getByLabelText('Signo del importe amount'), 'DEDUCTION_MAGNITUDE')
    await user.type(screen.getByLabelText('Decimales de amount'), '2')
    expect(screen.getByLabelText('Contrato elegido')).toHaveTextContent('"numeric_semantics":"DEDUCTION_MAGNITUDE"')
    expect(screen.getByLabelText('Contrato elegido')).toHaveTextContent('"decimal_scale":2')
    expect(screen.getByText(/no modifica el texto observado/)).toBeVisible()
    await user.click(screen.getByRole('checkbox', { name: 'Declarar normalización de amount' }))
    expect(screen.getByLabelText('Contrato elegido')).toHaveTextContent('{}')
  })

  it('records only explicitly added currency representations and preserves unknown aliases', async () => {
    const user = userEvent.setup()
    renderApp(<ConfiguredField kind="STRING"/>)
    await user.click(screen.getByRole('checkbox', { name: 'Declarar normalización de amount' }))
    expect(screen.queryByLabelText('Signo del importe amount')).not.toBeInTheDocument()
    await user.type(screen.getByLabelText('Código de moneda para amount'), 'cop')
    await user.type(screen.getByLabelText('Representaciones equivalentes de amount'), '$')
    expect(screen.getByRole('button', { name: 'Añadir equivalencias de moneda' })).toBeDisabled()
    await user.clear(screen.getByLabelText('Representaciones equivalentes de amount'))
    await user.type(screen.getByLabelText('Representaciones equivalentes de amount'), 'COP; Peso colombiano')
    await user.click(screen.getByRole('button', { name: 'Añadir equivalencias de moneda' }))
    expect(screen.getByLabelText('Contrato elegido')).toHaveTextContent('"currency_equivalences":{"COP":["COP","Peso colombiano"]}')
    expect(screen.getByLabelText('Contrato elegido')).not.toHaveTextContent('$')
  })
})
