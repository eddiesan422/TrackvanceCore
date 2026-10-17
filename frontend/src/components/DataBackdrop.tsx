/** Decorative data flow; never represents live or example operational results. */
export function DataBackdrop() {
  return <div className="data-backdrop" aria-hidden="true">
    <div className="data-grid"/>
    <svg viewBox="0 0 1440 900" preserveAspectRatio="xMidYMid slice" focusable="false">
      <defs><linearGradient id="data-route" x1="0" y1="1" x2="1" y2="0"><stop stopColor="#45a6b7"/><stop offset="1" stopColor="#8ffabe"/></linearGradient></defs>
      {[0, 1, 2, 3, 4].map(index => {
        const path = `M ${490 + index * 112} 940 L ${490 + index * 112} 620 Q ${490 + index * 112} 592 ${518 + index * 112} 568 L ${748 + index * 112} 338 Q ${776 + index * 112} 310 ${776 + index * 112} 282 L ${776 + index * 112} -40`
        return <g key={index}>
          <path className="data-route" d={path}/>
          <path className="data-pulse" d={path} pathLength="100" style={{ animationDelay: `${index * -4}s` }}/>
          <circle cx={776 + index * 112} cy="282" r="4"/>
          <circle cx={490 + index * 112} cy="620" r="4"/>
        </g>
      })}
    </svg>
    <div className="data-glow"/>
  </div>
}
