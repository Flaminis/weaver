import { useState } from 'react'
import { Card, CardContent } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import type { TraderState, PositionData } from '@/lib/types'
import { fmtAge, fmtPnl } from '@/lib/format'

function SellStatus({ p }: { p: PositionData }) {
  if (p.sell_order_id) return <span className="text-amber-400">GTC pending</span>
  if (p.age_sec >= 30) return <span className="text-orange-400">Awaiting exit</span>
  return <span className="text-[#555]">Holding</span>
}

export function PositionsTable({ data }: { data?: TraderState }) {
  const openPos = data?.positions.filter(p => !p.closed) || []
  const [expanded, setExpanded] = useState<number | null>(null)

  return (
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
                <TableHead className="text-[9px]">Exit</TableHead>
                <TableHead className="text-[9px]">Reason</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {openPos.map((p, i) => (
                <>
                  <TableRow key={i} onClick={() => setExpanded(expanded === i ? null : i)} className="cursor-pointer">
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
                    <TableCell className="text-[10px] font-mono"><SellStatus p={p} /></TableCell>
                    <TableCell className="text-[10px] text-muted-foreground truncate max-w-[200px]" title={p.signal_reason}>
                      {p.signal_reason}
                    </TableCell>
                  </TableRow>
                  {expanded === i && p.exit_story && (
                    <TableRow key={`${i}-story`}>
                      <TableCell colSpan={9} className="p-0">
                        <pre className="whitespace-pre-wrap font-mono text-[7px] leading-snug text-[#bbb] border-y border-white/[0.06] p-2 bg-black/30 max-h-32 overflow-y-auto">
                          {p.exit_story}
                        </pre>
                      </TableCell>
                    </TableRow>
                  )}
                </>
              ))}
            </TableBody>
          </Table>
        )}
      </CardContent>
    </Card>
  )
}
