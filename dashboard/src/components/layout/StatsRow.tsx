import { Card, CardContent } from '@/components/ui/card'
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
    ['Session PnL', fmtPnl(data.daily_pnl), data.daily_pnl >= 0 ? 'text-green-400' : 'text-red-400'],
    ['W / L', `${wins} / ${losses}`],
    ['Open Pos', String(openPos)],
    ['Matches', `${withMarket}/${matchCount}`],
    ['Events', String(eventCount)],
    ['Exposure', fmtUsd(data.exposure)],
    ['Streak', data.consecutive_losses > 0 ? `-${data.consecutive_losses}` : '0',
      data.consecutive_losses >= 3 ? 'text-red-400' : undefined],
  ]

  return (
    <div className="grid grid-cols-3 sm:grid-cols-4 md:grid-cols-7 gap-2 my-2 px-4">
      {stats.map(([label, value, color]) => (
        <Card key={label} className="rounded-lg">
          <CardContent className="p-2 text-center">
            <div className="text-[8px] text-muted-foreground uppercase tracking-wider">{label}</div>
            <div className={`text-sm font-mono font-semibold tabular-nums ${color || 'text-foreground'}`}>
              {value}
            </div>
          </CardContent>
        </Card>
      ))}
    </div>
  )
}
