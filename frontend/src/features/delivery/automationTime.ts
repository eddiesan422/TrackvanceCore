export function timeZoneError(zone: string) {
  if (!zone || zone.length > 80 || !/^[A-Za-z][A-Za-z0-9_+/-]*$/.test(zone)) return 'Indica una zona horaria IANA válida, por ejemplo America/Bogota.'
  try { new Intl.DateTimeFormat('en', { timeZone: zone }).format(0) }
  catch { return 'Indica una zona horaria IANA válida, por ejemplo America/Bogota.' }
  return ''
}

export function wallTime(instant: Date, zone: string) {
  const error = timeZoneError(zone)
  if (error) throw new Error(error)
  if (!Number.isFinite(instant.getTime())) throw new Error('Selecciona una fecha de inicio válida.')
  const parts = new Intl.DateTimeFormat('en-CA', { timeZone: zone, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).formatToParts(instant)
  const value = (type: string) => parts.find(part => part.type === type)?.value || ''
  return `${value('year')}-${value('month')}-${value('day')}T${value('hour')}:${value('minute')}`
}

export function zonedStart(local: string, zone: string) {
  const error = timeZoneError(zone)
  if (error) throw new Error(error)
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(local)) throw new Error('Selecciona una fecha de inicio válida.')
  const base = Date.parse(`${local}:00Z`)
  if (!Number.isFinite(base) || wallTime(new Date(base), 'UTC') !== local) throw new Error('Selecciona una fecha de inicio válida.')
  const candidates = new Set<number>()
  for (const shift of [-86400000, 0, 86400000]) {
    const probe = base + shift
    const displayed = Date.parse(`${wallTime(new Date(probe), zone)}:00Z`)
    const candidate = base - (displayed - probe)
    if (wallTime(new Date(candidate), zone) === local) candidates.add(candidate)
  }
  if (!candidates.size) throw new Error('La hora seleccionada no existe en esa zona por el cambio de horario. Elige otra hora.')
  return new Date(Math.min(...candidates)).toISOString()
}
