export function fmtCents(v: number): string {
  return (v * 100).toFixed(1) + '¢'
}

export function fmtUsd(v: number): string {
  return '$' + v.toFixed(2)
}

export function fmtPnl(v: number): string {
  const sign = v >= 0 ? '+' : ''
  return sign + '$' + v.toFixed(2)
}

export function fmtPct(v: number): string {
  return (v * 100).toFixed(0) + '%'
}

export function fmtTime(sec: number): string {
  const h = Math.floor(sec / 3600)
  const m = Math.floor((sec % 3600) / 60)
  if (h > 0) return `${h}h ${m}m`
  return `${m}m`
}

export function fmtAge(sec: number): string {
  if (sec < 60) return `${Math.round(sec)}s`
  return `${Math.floor(sec / 60)}m ${Math.round(sec % 60)}s`
}

export function fmtGameClock(timer?: { timer: number; paused: boolean; issued_at: string }): string {
  if (!timer) return '--:--'
  let base = timer.timer || 0
  if (!timer.paused && timer.issued_at) {
    base += (Date.now() - new Date(timer.issued_at).getTime()) / 1000
  }
  const s = Math.max(0, Math.floor(base))
  return `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`
}
