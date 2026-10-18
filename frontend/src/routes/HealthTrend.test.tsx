import { fireEvent, render, screen } from '@testing-library/react'
import { expect, it } from 'vitest'
import HealthTrend, { healthCoordinates } from './HealthTrend'

it('positions actual dates and breaks missing days, missing metrics and preserves zero', () => {
  const points = [{ date: '2026-10-01', overall: 100 }, { date: '2026-10-02', overall: 0 },
    { date: '2026-10-03', overall: null }, { date: '2026-10-05', overall: 40 }, { date: '2026-10-07', overall: 60 }]
  const { segments } = healthCoordinates(points, 'overall')
  expect(segments.map(group => group.length)).toEqual([2, 1, 1])
  expect(segments[0][1].value).toBe(0)
  expect(segments[1][0].x - segments[0][1].x).toBeCloseTo(3 * (segments[0][1].x - segments[0][0].x))
})

it('selects a local series without changing other values and explains global exclusion', () => {
  const points = [{ date: '2026-10-01', overall: 75, intake: 50, recon: 100, comparable_runs: { overall: 2, intake: 1, recon: 1 } }]
  const { rerender } = render(<HealthTrend points={points} overall={75} modules={[{ module: 'intake', health_score: 50 }]} globalModule=""/>)
  expect(screen.getByRole('radio', { name: 'General' })).toBeChecked()
  fireEvent.click(screen.getByRole('radio', { name: 'Data Intake' }))
  expect(screen.getByRole('heading', { name: 'Evolución temporal · Data Intake' })).toBeVisible()
  expect(screen.getByLabelText('Salud de Data Intake')).toHaveTextContent('50%')
  expect(screen.getByLabelText(/01 de oct.*50%.*1 ejecuciones consideradas/)).toBeVisible()
  rerender(<HealthTrend points={points} overall={75} modules={[]} globalModule="recon"/>)
  expect(screen.getByRole('status')).toHaveTextContent('Los filtros globales excluyen Data Intake')
})

it('does not fabricate a score when metrics are absent', () => {
  render(<HealthTrend points={[{ date: '2026-10-01', overall: null }]} overall={null} modules={[]} globalModule=""/>)
  expect(screen.getAllByText('Sin mediciones comparables')).toHaveLength(2)
  expect(screen.queryByRole('img')).not.toBeInTheDocument()
})
