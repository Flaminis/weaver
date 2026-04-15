import { Badge } from '@/components/ui/badge'
import { Separator } from '@/components/ui/separator'
import type { TraderState } from '@/lib/types'
import { fmtUsd, fmtPnl, fmtPct, fmtTime } from '@/lib/format'

function fmtCountdown(sec: number): string {
  if (sec <= 0) return ''
  const m = Math.floor(sec / 60)
  const s = Math.floor(sec % 60)
  return m > 0 ? `${m}m ${s}s` : `${s}s`
}

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
              <Badge
                variant="destructive"
                className="text-[9px] h-4 px-1.5 animate-pulse"
                title={data.circuit_reason || 'Circuit breaker active'}
              >
                CIRCUIT BREAKER{data.circuit_seconds_left > 0 ? ` · ${fmtCountdown(data.circuit_seconds_left)}` : ''}
              </Badge>
            )}
            <Separator orientation="vertical" className="h-4" />
            <span className="text-[10px] text-muted-foreground">{fmtTime(data.uptime_sec)}</span>
            {data.poly_ws && (
              <span
                className={`text-[8px] font-mono px-1.5 py-0.5 rounded ${
                  data.poly_ws.connected && data.poly_ws.last_msg_age >= 0 && data.poly_ws.last_msg_age < 10
                    ? 'bg-green-500/15 text-green-400'
                    : data.poly_ws.connected && data.poly_ws.last_msg_age < 30
                      ? 'bg-yellow-500/15 text-yellow-400'
                      : 'bg-red-500/15 text-red-400'
                }`}
                title={`Polymarket WS: ${data.poly_ws.connected ? 'connected' : 'disconnected'} | ${data.poly_ws.active_books}/${data.poly_ws.subscriptions} books active | last msg ${data.poly_ws.last_msg_age >= 0 ? data.poly_ws.last_msg_age.toFixed(0) + 's ago' : 'never'}`}
              >
                WS:{data.poly_ws.connected ? 'OK' : 'DOWN'}
                {data.poly_ws.last_msg_age >= 0 && <span className="text-[#555] ml-0.5">{data.poly_ws.last_msg_age.toFixed(0)}s</span>}
                <span className="text-[#555] ml-0.5">{data.poly_ws.active_books}/{data.poly_ws.subscriptions}</span>
              </span>
            )}
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
