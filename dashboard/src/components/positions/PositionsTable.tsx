import { Card, CardContent } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { ScrollArea } from '@/components/ui/scroll-area'
import type { TraderState } from '@/lib/types'
import { fmtAge, fmtPnl } from '@/lib/format'

export function PositionsTable({ data }: { data?: TraderState }) {
  const openPos = data?.positions.filter(p => !p.closed) || []
  const trades = [...(data?.trades || [])].reverse()

  return (
    <div className="space-y-3">
      {/* Open positions */}
      <Card>
        <CardContent className="p-0">
          <div className="flex items-center justify-between px-4 py-2 border-b border-border">
            <span className="text-xs font-semibold">Open Positions</span>
            <Badge variant="outline" className="text-[9px] h-4">{openPos.length}</Badge>
          </div>
          {openPos.length === 0 ? (
            <div className="text-center py-6 text-xs text-muted-foreground">No open positions</div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="text-[9px]">Match</TableHead>
                  <TableHead className="text-[9px]">Side</TableHead>
                  <TableHead className="text-[9px]">Entry</TableHead>
                  <TableHead className="text-[9px]">Current</TableHead>
                  <TableHead className="text-[9px]">PnL</TableHead>
                  <TableHead className="text-[9px]">Size</TableHead>
                  <TableHead className="text-[9px]">Age</TableHead>
                  <TableHead className="text-[9px]">Reason</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {openPos.map((p, i) => (
                  <TableRow key={i}>
                    <TableCell className="text-[10px] font-mono">{p.match_name}</TableCell>
                    <TableCell>
                      <Badge variant={p.direction === 'buy_a' ? 'default' : 'destructive'} className="text-[8px] h-3.5 px-1">
                        {p.direction === 'buy_a' ? 'A' : 'B'}
                      </Badge>
                    </TableCell>
                    <TableCell className="text-[10px] font-mono tabular-nums">{(p.entry_price * 100).toFixed(1)}¢</TableCell>
                    <TableCell className="text-[10px] font-mono tabular-nums">{(p.current_price * 100).toFixed(1)}¢</TableCell>
                    <TableCell className={`text-[10px] font-mono tabular-nums font-bold ${p.unrealized_pnl >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                      {fmtPnl(p.unrealized_pnl)}
                    </TableCell>
                    <TableCell className="text-[10px] font-mono tabular-nums">{p.size.toFixed(1)}</TableCell>
                    <TableCell className="text-[10px] font-mono tabular-nums">{fmtAge(p.age_sec)}</TableCell>
                    <TableCell className="text-[10px] text-muted-foreground truncate max-w-[200px]" title={p.signal_reason}>
                      {p.signal_reason}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      {/* Trade history */}
      <Card>
        <CardContent className="p-0">
          <div className="flex items-center justify-between px-4 py-2 border-b border-border">
            <span className="text-xs font-semibold">Trade History</span>
            <Badge variant="outline" className="text-[9px] h-4">{trades.length}</Badge>
          </div>
          {trades.length === 0 ? (
            <div className="text-center py-6 text-xs text-muted-foreground">No trades yet</div>
          ) : (
            <ScrollArea className="max-h-[300px]">
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
    </div>
  )
}
