import { useState } from 'react'
import { Card, CardContent } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { ScrollArea } from '@/components/ui/scroll-area'
import type { TraderState, EventData } from '@/lib/types'

const EVT_EMOJI: Record<string, string> = { kill: '⚔', drake: '🐉', baron: '👿', inhibitor: '💥', tower: '🏰' }

function execBadge(ev: EventData): { label: string; cls: string; tip: string } {
  if (ev.action === 'GATED') {
    return { label: 'GATED', cls: 'bg-yellow-500/15 text-yellow-400 border-yellow-500/30', tip: ev.gate_reason || 'Risk gate' }
  }
  if (ev.action === 'SKIP_SIZE') {
    return { label: 'SIZE', cls: 'bg-orange-500/12 text-orange-400/70 border-orange-500/25', tip: 'Position too small' }
  }
  if (ev.action === 'ORDER_FAIL') {
    return { label: 'FAIL', cls: 'bg-red-500/15 text-red-400 border-red-500/30', tip: ev.order_error || 'Order failed' }
  }
  if (ev.action === 'TRADE') {
    const x = ev.trade_exec
    if (x === 'dry_run') return { label: 'SIM', cls: 'bg-amber-500/15 text-amber-400 border-amber-500/30', tip: 'Paper trade — no Polymarket order' }
    if (x === 'polymarket_ok') return { label: 'FILL', cls: 'bg-green-500/15 text-green-400 border-green-500/30', tip: 'Fill verified — position opened' }
    if (x === 'no_fill_confirmed') return { label: 'NO FILL', cls: 'bg-slate-500/20 text-slate-300 border-slate-500/35', tip: 'Order sent but no matching fill' }
    if (x === 'fill_rejected') return { label: 'BAD FILL', cls: 'bg-red-500/12 text-red-300 border-red-500/30', tip: `Fill size sanity check failed${ev.fill_reported_shares ? ` (${ev.fill_reported_shares} sh)` : ''}` }
    if (x === 'no_order_id' || x === 'order_error') return { label: 'ERR', cls: 'bg-red-500/15 text-red-400 border-red-500/30', tip: ev.order_error || 'No order id from CLOB' }
    return { label: 'SIG', cls: 'bg-white/[0.06] text-[#888] border-white/[0.08]', tip: 'Signal (legacy — exec not recorded)' }
  }
  if (ev.action.startsWith('SPREAD')) return { label: 'SPREAD', cls: 'bg-yellow-500/10 text-yellow-400/70 border-yellow-500/20', tip: ev.action }
  if (ev.action.startsWith('PRICED')) return { label: 'PRICED', cls: 'bg-purple-500/10 text-purple-400/70 border-purple-500/20', tip: ev.action }
  if (ev.action.startsWith('LOW_EDGE')) return { label: 'LOW EDGE', cls: 'bg-orange-500/10 text-orange-400/70 border-orange-500/20', tip: ev.action }
  return { label: ev.action.replace(/_/g, ' ').slice(0, 12), cls: 'bg-white/[0.03] text-[#555] border-white/[0.05]', tip: ev.action }
}

function limitPrice(ev: EventData): number | null {
  if (ev.attempt_limit_price != null && ev.attempt_limit_price > 0) return ev.attempt_limit_price
  if (!ev.signal_dir) return null
  if (ev.signal_dir === 'buy_a' && ev.buy_price_a > 0) return ev.buy_price_a + 0.01
  if (ev.signal_dir === 'buy_b' && ev.buy_price_b > 0) return ev.buy_price_b + 0.01
  return null
}

type FilterKey = 'ALL' | 'TRADE' | 'GATED' | 'SKIP'

export function EventsPanel({ data }: { data?: TraderState }) {
  const [filter, setFilter] = useState<FilterKey>('ALL')
  const [expanded, setExpanded] = useState<number | null>(null)
  const events = [...(data?.events || [])].reverse()

  const tradeEvents = events.filter(e => e.action === 'TRADE')
  const gatedEvents = events.filter(e => e.action === 'GATED' || e.action === 'SKIP_SIZE' || e.action === 'ORDER_FAIL')
  const skipEvents = events.filter(e => e.action !== 'TRADE' && e.action !== 'GATED' && e.action !== 'SKIP_SIZE' && e.action !== 'ORDER_FAIL')

  const filtered = filter === 'ALL' ? events
    : filter === 'TRADE' ? tradeEvents
    : filter === 'GATED' ? gatedEvents
    : skipEvents

  const filters: { key: FilterKey; label: string; count: number }[] = [
    { key: 'ALL', label: 'All', count: events.length },
    { key: 'TRADE', label: 'Trades', count: tradeEvents.length },
    { key: 'GATED', label: 'Gated', count: gatedEvents.length },
    { key: 'SKIP', label: 'Skip', count: skipEvents.length },
  ]

  return (
    <Card>
      <CardContent className="p-0">
        <div className="flex items-center justify-between px-4 py-2 border-b border-border">
          <div className="flex items-center gap-2">
            <span className="text-xs font-semibold">Signals & Events</span>
            <Badge variant="outline" className="text-[9px] h-4">{filtered.length}</Badge>
          </div>
          <div className="flex gap-1">
            {filters.map(f => (
              <button
                key={f.key}
                onClick={() => { setFilter(f.key); setExpanded(null) }}
                className={`text-[8px] px-1.5 py-0.5 rounded transition-colors ${
                  filter === f.key ? 'bg-primary text-primary-foreground' : 'bg-muted text-muted-foreground hover:bg-muted/80'
                }`}
              >
                {f.label} ({f.count})
              </button>
            ))}
          </div>
        </div>
        <ScrollArea className="max-h-[500px]">
          <div className="font-mono text-[10px]">
            {filtered.length === 0 && (
              <div className="text-center py-6 text-xs text-muted-foreground">No events yet</div>
            )}
            {filtered.map((ev, i) => {
              const b = execBadge(ev)
              const lim = limitPrice(ev)
              const isOpen = expanded === i
              return (
                <div key={i}>
                  <div
                    onClick={() => setExpanded(isOpen ? null : i)}
                    className={`flex items-center gap-1.5 px-2 py-[3px] cursor-pointer transition-colors ${isOpen ? 'bg-white/[0.03]' : 'hover:bg-white/[0.02]'}`}
                  >
                    <span className="text-[#444] w-[68px] shrink-0">{ev.time}</span>
                    <span className="w-3 shrink-0">{EVT_EMOJI[ev.etype] || '•'}</span>
                    <span className={`font-bold w-10 shrink-0 ${
                      ev.etype === 'kill' ? 'text-red-400' : ev.etype === 'baron' ? 'text-yellow-400'
                      : ev.etype === 'drake' ? 'text-purple-400' : ev.etype === 'tower' ? 'text-blue-400'
                      : 'text-muted-foreground'
                    }`}>
                      {ev.etype.toUpperCase().slice(0, 5)}
                    </span>
                    <span className="text-[#444] w-10 shrink-0">[{ev.clock}]</span>
                    <span className="truncate flex-1 min-w-0 text-muted-foreground">{ev.desc}</span>
                    {ev.signal_dir && lim != null && (
                      <span className="text-[8px] text-[#777] shrink-0">
                        {ev.signal_dir === 'buy_a' ? 'A' : 'B'} ≤{(lim * 100).toFixed(1)}¢
                      </span>
                    )}
                    <span className="text-[#555] w-10 text-right shrink-0">{(ev.mid * 100).toFixed(1)}¢</span>
                    <Badge variant="outline" className={`text-[6px] h-2.5 px-1 shrink-0 ${b.cls}`} title={b.tip}>
                      {b.label}
                    </Badge>
                    <span className="text-[#333] shrink-0">{isOpen ? '▾' : '▸'}</span>
                  </div>
                  {isOpen && (
                    <div className="px-3 py-2 bg-black/20 border-y border-white/[0.03] text-[8px] space-y-1.5">
                      {ev.exec_story && (
                        <pre className="whitespace-pre-wrap font-mono text-[7px] leading-snug text-[#bbb] border border-white/[0.06] rounded-md p-2 bg-black/30 max-h-40 overflow-y-auto">
                          {ev.exec_story}
                        </pre>
                      )}
                      {ev.action === 'TRADE' && ev.trade_exec === 'polymarket_ok' && (
                        <div className="text-green-400 font-bold">Fill verified — {ev.signal_dir?.toUpperCase()} ${ev.signal_size?.toFixed(2)} — {ev.signal_reason}</div>
                      )}
                      {ev.action === 'TRADE' && ev.trade_exec === 'dry_run' && (
                        <div className="text-amber-400 font-bold">Dry run: simulated position only</div>
                      )}
                      {ev.action === 'TRADE' && ev.trade_exec === 'no_fill_confirmed' && (
                        <div className="text-slate-300 font-bold">No confirmed fill — no position opened</div>
                      )}
                      {ev.action === 'TRADE' && ev.trade_exec === 'fill_rejected' && (
                        <div className="text-red-300 font-bold">
                          Fill size{ev.fill_reported_shares != null ? ` (${Number(ev.fill_reported_shares).toFixed(2)} sh)` : ''} failed checks — no position
                          {ev.clob_order_id ? ` · ${ev.clob_order_id.slice(0, 16)}…` : ''}
                        </div>
                      )}
                      {ev.action === 'TRADE' && (ev.trade_exec === 'no_order_id' || ev.trade_exec === 'order_error') && (
                        <div className="text-red-400 font-bold">CLOB order failed — {ev.order_error || 'no order id'}</div>
                      )}
                      {ev.action === 'GATED' && (
                        <div className="text-yellow-400 font-bold">Risk gate: {ev.gate_reason || 'blocked'}</div>
                      )}
                      {ev.action === 'SKIP_SIZE' && (
                        <div className="text-orange-400 font-bold">Size too small to trade</div>
                      )}
                      {ev.action === 'ORDER_FAIL' && (
                        <div className="text-red-400 font-bold">Order failed{ev.order_error ? `: ${ev.order_error}` : ''}</div>
                      )}
                      {ev.action !== 'TRADE' && ev.action !== 'GATED' && ev.action !== 'SKIP_SIZE' && ev.action !== 'ORDER_FAIL' && (
                        <div className="text-orange-400">{ev.action.replace(/_/g, ' ')}: {ev.signal_reason || ev.desc}</div>
                      )}
                      <div className="flex gap-4 text-[#666]">
                        <span>Mid <span className="text-[#999]">{(ev.mid * 100).toFixed(1)}¢</span></span>
                        <span>Bid <span className="text-green-400/60">{(ev.bid * 100).toFixed(1)}¢</span></span>
                        <span>Ask <span className="text-red-400/60">{(ev.ask * 100).toFixed(1)}¢</span></span>
                        <span>Sprd <span className="text-[#999]">{(ev.spread * 100).toFixed(1)}¢</span></span>
                        <span>Δ2s <span className={ev.recent_move_2s > 0 ? 'text-green-400/60' : ev.recent_move_2s < 0 ? 'text-red-400/60' : 'text-[#999]'}>
                          {ev.recent_move_2s > 0 ? '+' : ''}{(ev.recent_move_2s * 100).toFixed(1)}¢
                        </span></span>
                      </div>
                      {ev.signal_dir && (
                        <div className="flex gap-3 text-[#666]">
                          <span>Dir <span className="text-[#bbb]">{ev.signal_dir}</span></span>
                          {ev.signal_size != null && <span>Size <span className="text-[#bbb]">${ev.signal_size.toFixed(2)}</span></span>}
                          {ev.signal_impact != null && <span>Imp <span className="text-[#bbb]">{(ev.signal_impact * 100).toFixed(1)}¢</span></span>}
                          {ev.edge != null && <span>Edge <span className={ev.edge >= 0.02 ? 'text-green-400' : 'text-red-400/60'}>{(ev.edge * 100).toFixed(1)}¢</span></span>}
                          {ev.p_fair != null && <span>pFair <span className="text-[#bbb]">{(ev.p_fair * 100).toFixed(1)}¢</span></span>}
                        </div>
                      )}
                      {ev.book_snapshot && (ev.book_snapshot.bids.length > 0 || ev.book_snapshot.asks.length > 0) && (
                        <div className="flex gap-3">
                          <div className="flex-1">{ev.book_snapshot.bids.slice(0, 3).map((lvl, j) => (
                            <div key={j} className="flex justify-between"><span className="text-green-400/50">{(lvl.p * 100).toFixed(1)}¢</span><span className="text-[#555]">${Math.round(lvl.s * lvl.p)}</span></div>
                          ))}</div>
                          <div className="w-px bg-white/[0.05]" />
                          <div className="flex-1">{ev.book_snapshot.asks.slice(0, 3).map((lvl, j) => (
                            <div key={j} className="flex justify-between"><span className="text-red-400/50">{(lvl.p * 100).toFixed(1)}¢</span><span className="text-[#555]">${Math.round(lvl.s * lvl.p)}</span></div>
                          ))}</div>
                        </div>
                      )}
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        </ScrollArea>
      </CardContent>
    </Card>
  )
}
