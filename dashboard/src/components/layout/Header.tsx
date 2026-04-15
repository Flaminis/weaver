import { Badge } from '@/components/ui/badge'
import { Separator } from '@/components/ui/separator'
import type { TraderState } from '@/lib/types'
import { fmtUsd, fmtPnl, fmtPct, fmtTime } from '@/lib/format'

export function Header({ data }: { data?: TraderState }) {
  return (
    <header className="flex items-center justify-between px-4 py-2.5 border-b border-border">
      <div className="flex items-center gap-3">
        <div className="flex items-center gap-1.5">
          <div className={`w-2 h-2 rounded-full ${data ? 'bg-green-500 animate-pulse' : 'bg-red-500'}`} />
          <span className="text-sm font-bold tracking-tight">ORACLE-LoL</span>
        </div>
        {data && (
          <>
            <Badge variant={data.dry_run ? 'secondary' : 'destructive'} className="text-[9px] h-4 px-1.5">
              {data.dry_run ? 'DRY RUN' : 'LIVE'}
            </Badge>
            {data.circuit_active && (
              <Badge variant="destructive" className="text-[9px] h-4 px-1.5 animate-pulse">
                CIRCUIT BREAKER
              </Badge>
            )}
            <Separator orientation="vertical" className="h-4" />
            <span className="text-[10px] text-muted-foreground">{fmtTime(data.uptime_sec)}</span>
          </>
        )}
      </div>

      {data && (
        <div className="flex items-center gap-4 text-[11px]">
          <Kpi label="PnL" value={fmtPnl(data.daily_pnl)} color={data.daily_pnl >= 0 ? 'text-green-400' : 'text-red-400'} />
          <Kpi label="Capital" value={fmtUsd(data.capital)} />
          <Kpi label="Exposure" value={fmtUsd(data.exposure)} />
          <Kpi label="Trades" value={String(data.total_trades)} />
          <Kpi label="WR" value={fmtPct(data.win_rate)} color={data.win_rate >= 0.5 ? 'text-green-400' : 'text-red-400'} />
          {data.consecutive_losses > 0 && (
            <Kpi label="Losses" value={String(data.consecutive_losses)} color="text-red-400" />
          )}
        </div>
      )}
    </header>
  )
}

function Kpi({ label, value, color }: { label: string; value: string; color?: string }) {
  return (
    <div className="text-center">
      <div className="text-[9px] text-muted-foreground uppercase tracking-wider">{label}</div>
      <div className={`font-mono font-semibold tabular-nums ${color || 'text-foreground'}`}>{value}</div>
    </div>
  )
}
