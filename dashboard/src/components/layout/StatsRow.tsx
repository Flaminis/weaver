import type { TraderState } from '@/lib/types'
import { fmtPnl, fmtUsd } from '@/lib/format'

export function StatsRow({ data }: { data?: TraderState }) {
  if (!data) return null

  const openPos = data.positions.filter(p => !p.closed).length
  const matchCount = Object.keys(data.matches).length
  const withMarket = Object.values(data.matches).filter(m => m.has_market).length
  const eventCount = data.events.length
  const wins = data.trades.filter(t => t.pnl > 0).length
  const losses = data.trades.filter(t => t.pnl < 0).length

  const stats: [string, string, string?][] = [
    ['PnL', fmtPnl(data.daily_pnl), data.daily_pnl >= 0 ? 'text-green-400' : 'text-red-400'],
    ['W/L', `${wins}/${losses}`],
    ['Pos', String(openPos)],
    ['Mkts', `${withMarket}/${matchCount}`],
    ['Evts', String(eventCount)],
    ['Exp', fmtUsd(data.exposure)],
    ['Strk', data.consecutive_losses > 0 ? `-${data.consecutive_losses}` : '0',
      data.consecutive_losses >= 3 ? 'text-red-400' : undefined],
  ]

  return (
    <div className="flex gap-1.5 px-4 py-1">
      {stats.map(([label, value, color]) => (
        <div key={label} className="flex items-center gap-1 bg-white/[0.02] rounded px-2 py-0.5 border border-white/[0.04]">
          <span className="text-[7px] text-[#555] uppercase">{label}</span>
          <span className={`text-[10px] font-mono font-bold tabular-nums ${color || 'text-foreground'}`}>{value}</span>
        </div>
      ))}
    </div>
  )
}
