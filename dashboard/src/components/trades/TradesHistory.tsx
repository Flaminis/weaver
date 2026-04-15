import { Card, CardContent } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { ScrollArea } from '@/components/ui/scroll-area'
import type { TraderState } from '@/lib/types'
import { fmtPnl } from '@/lib/format'

export function TradesHistory({ data }: { data?: TraderState }) {
  const trades = [...(data?.trades || [])].reverse()

  const totalPnl = trades.reduce((s, t) => s + t.pnl, 0)
  const wins = trades.filter(t => t.pnl > 0).length
  const avgHold = trades.length > 0 ? trades.reduce((s, t) => s + t.hold_sec, 0) / trades.length : 0

  return (
    <Card>
      <CardContent className="p-0">
        <div className="flex items-center justify-between px-4 py-2 border-b border-border">
          <div className="flex items-center gap-2">
            <span className="text-xs font-semibold">Trade History</span>
            <Badge variant="outline" className="text-[9px] h-4">{trades.length}</Badge>
          </div>
          {trades.length > 0 && (
            <div className="flex items-center gap-3 text-[9px] font-mono">
              <span className={totalPnl >= 0 ? 'text-green-400' : 'text-red-400'}>{fmtPnl(totalPnl)}</span>
              <span className="text-muted-foreground">{wins}W/{trades.length - wins}L</span>
              <span className="text-muted-foreground">avg {avgHold.toFixed(0)}s</span>
            </div>
          )}
        </div>
        {trades.length === 0 ? (
          <div className="text-center py-6 text-xs text-muted-foreground">No closed trades yet</div>
        ) : (
          <ScrollArea className="max-h-[400px]">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="text-[9px]">Time</TableHead>
                  <TableHead className="text-[9px]">Match</TableHead>
                  <TableHead className="text-[9px]">Side</TableHead>
                  <TableHead className="text-[9px]">Entry</TableHead>
                  <TableHead className="text-[9px]">Exit</TableHead>
                  <TableHead className="text-[9px]">PnL</TableHead>
                  <TableHead className="text-[9px]">Hold</TableHead>
                  <TableHead className="text-[9px]">Reason</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {trades.map((t, i) => (
                  <TableRow key={i}>
                    <TableCell className="text-[10px] font-mono text-muted-foreground">
                      {new Date(t.ts * 1000).toLocaleTimeString()}
                    </TableCell>
                    <TableCell className="text-[10px] font-mono">{t.match}</TableCell>
                    <TableCell>
                      <Badge variant={t.direction === 'buy_a' ? 'default' : 'destructive'} className="text-[8px] h-3.5 px-1">
                        {t.direction === 'buy_a' ? 'A' : 'B'}
                      </Badge>
                    </TableCell>
                    <TableCell className="text-[10px] font-mono tabular-nums">{(t.entry * 100).toFixed(1)}¢</TableCell>
                    <TableCell className="text-[10px] font-mono tabular-nums">{(t.exit * 100).toFixed(1)}¢</TableCell>
                    <TableCell className={`text-[10px] font-mono tabular-nums font-bold ${t.pnl >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                      {fmtPnl(t.pnl)}
                    </TableCell>
                    <TableCell className="text-[10px] font-mono tabular-nums">{t.hold_sec.toFixed(0)}s</TableCell>
                    <TableCell className="text-[10px] text-muted-foreground truncate max-w-[200px]" title={t.reason}>
                      {t.reason}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </ScrollArea>
        )}
      </CardContent>
    </Card>
  )
}
