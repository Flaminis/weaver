import { useEffect, useMemo, useRef, useState } from 'react'
import { Card, CardContent } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import type { MatchData, GameTeam, PositionData, EventData } from '@/lib/types'
import { fmtCents, fmtGameClock } from '@/lib/format'
import { createChart, AreaSeries, LineSeries, HistogramSeries, createSeriesMarkers, type IChartApi, type ISeriesApi, type ISeriesMarkersPluginApi, type Time } from 'lightweight-charts'

const DDRAGON = 'https://ddragon.leagueoflegends.com/cdn/14.24.1/img/champion'
const CHAMP_FIXES: Record<string, string> = {
  jarvaniv: 'JarvanIV', monkeyking: 'MonkeyKing', wukong: 'MonkeyKing',
  renataglasc: 'Renata', bellaveth: 'Belveth', ksante: 'KSante',
}
function champUrl(slug: string) {
  const f = CHAMP_FIXES[slug.toLowerCase()] || slug.charAt(0).toUpperCase() + slug.slice(1)
  return `${DDRAGON}/${f}.png`
}

const EVT_EMOJI: Record<string, string> = { kill: '⚔', drake: '🐉', baron: '👿', inhibitor: '💥', tower: '🏰' }
const ROLES = ['top', 'jun', 'mid', 'adc', 'sup']

/** Row badge: BUY meant “model said trade”; only `polymarket_ok` is a real CLOB buy. */
function eventActionBadge(ev: EventData): { label: string; className: string } {
  if (ev.action === 'GATED') {
    return { label: 'GATED', className: 'bg-yellow-500/15 text-yellow-400 border-yellow-500/30' }
  }
  if (ev.action === 'SKIP_SIZE') {
    return { label: 'SIZE', className: 'bg-orange-500/12 text-orange-400/70 border-orange-500/25' }
  }
  if (ev.action === 'ORDER_FAIL') {
    return { label: 'FAIL', className: 'bg-red-500/15 text-red-400 border-red-500/30' }
  }
  if (ev.action === 'TRADE') {
    const x = ev.trade_exec
    if (x === 'dry_run') return { label: 'SIM', className: 'bg-amber-500/15 text-amber-400 border-amber-500/30' }
    if (x === 'polymarket_ok') return { label: 'BUY', className: 'bg-green-500/15 text-green-400 border-green-500/30' }
    if (x === 'no_fill_confirmed') return { label: 'NO FILL', className: 'bg-slate-500/20 text-slate-300 border-slate-500/35' }
    if (x === 'fill_rejected') return { label: 'BAD FILL', className: 'bg-red-500/12 text-red-300 border-red-500/30' }
    if (x === 'no_order_id' || x === 'order_error') return { label: 'FAIL', className: 'bg-red-500/15 text-red-400 border-red-500/30' }
    return { label: 'SIG', className: 'bg-white/[0.06] text-[#888] border-white/[0.08]' }
  }
  if (ev.action.startsWith('LOW_EDGE')) {
    return { label: 'LOW EDGE', className: 'bg-orange-500/10 text-orange-400/70 border-orange-500/20' }
  }
  if (ev.action.startsWith('SPREAD')) {
    return { label: 'SPREAD', className: 'bg-yellow-500/10 text-yellow-400/70 border-yellow-500/20' }
  }
  if (ev.action.startsWith('PRICED')) {
    return { label: 'PRICED IN', className: 'bg-purple-500/10 text-purple-400/70 border-purple-500/20' }
  }
  return { label: ev.action.replace(/_/g, ' ').slice(0, 12), className: 'bg-white/[0.03] text-[#555] border-white/[0.05]' }
}

/** CLOB FAK limit (stored) or book ref + 1¢ tick (matches trader). */
function inferredBuyLimit(ev: EventData): number | null {
  if (ev.attempt_limit_price != null && ev.attempt_limit_price > 0) return ev.attempt_limit_price
  if (!ev.signal_dir) return null
  if (ev.signal_dir === 'buy_a' && ev.buy_price_a > 0) return ev.buy_price_a + 0.01
  if (ev.signal_dir === 'buy_b' && ev.buy_price_b > 0) return ev.buy_price_b + 0.01
  return null
}

// ── Chart ───────────────────────────────────────────────────────────

function Chart({ priceHistory, modelProbHistory, events, teamA, teamB, sideA, sideB, hoveredTs }: {
  priceHistory: [number, number][]; modelProbHistory?: [number, number][]; events: EventData[]
  teamA: string; teamB: string; sideA?: string; sideB?: string; hoveredTs?: number | null
}) {
  const el = useRef<HTMLDivElement>(null)
  const chart = useRef<IChartApi | null>(null)
  const sA = useRef<ISeriesApi<'Area'> | null>(null)
  const sB = useRef<ISeriesApi<'Line'> | null>(null)
  const sModel = useRef<ISeriesApi<'Line'> | null>(null)
  const sMkA = useRef<ISeriesMarkersPluginApi<Time> | null>(null)
  const sMkB = useRef<ISeriesMarkersPluginApi<Time> | null>(null)
  const sEv = useRef<ISeriesApi<'Histogram'> | null>(null)
  const tARef = useRef(teamA); tARef.current = teamA
  const tBRef = useRef(teamB); tBRef.current = teamB
  // Once the user pans/zooms, new setData() calls should NOT snap the view.
  // Reset on Fit / preset-range buttons so auto-follow works again on demand.
  const userScrolledRef = useRef(false)
  const preserveRange = (fn: () => void) => {
    const c = chart.current
    if (!c || !userScrolledRef.current) { fn(); return }
    const ts = c.timeScale()
    const range = ts.getVisibleLogicalRange()
    fn()
    if (range) {
      try { ts.setVisibleLogicalRange(range) } catch {}
    }
  }
  const resetFollow = () => { userScrolledRef.current = false }

  const cA = (sideA || '').toLowerCase() === 'blue' ? '#58a6ff' : '#f85149'
  const cB = (sideB || '').toLowerCase() === 'blue' ? '#58a6ff' : '#f85149'
  const cARef = useRef(cA); cARef.current = cA
  const cBRef = useRef(cB); cBRef.current = cB

  useEffect(() => {
    if (sA.current) sA.current.applyOptions({ lineColor: cA, topColor: cA + '10', bottomColor: cA + '02' })
    if (sB.current) sB.current.applyOptions({ color: cB + '70' })
  }, [cA, cB])

  useEffect(() => {
    if (!el.current || chart.current) return
    const c = createChart(el.current, {
      layout: { background: { color: 'transparent' }, textColor: '#555', fontSize: 9 },
      grid: { vertLines: { color: '#1a1a1a' }, horzLines: { color: '#1a1a1a' } },
      rightPriceScale: { borderColor: 'transparent', scaleMargins: { top: 0.05, bottom: 0.08 } },
      timeScale: { borderColor: 'transparent', timeVisible: true, secondsVisible: true },
      crosshair: {
        vertLine: { color: '#333', width: 1, style: 2, labelBackgroundColor: '#222' },
        horzLine: { color: '#333', width: 1, style: 2, labelBackgroundColor: '#222' },
      },
      handleScroll: true, handleScale: true,
      localization: { priceFormatter: (p: number) => (p * 100).toFixed(1) + '¢' },
    })
    chart.current = c
    sEv.current = c.addSeries(HistogramSeries, {
      priceScaleId: 'ev', priceFormat: { type: 'custom', formatter: () => '' },
      lastValueVisible: false, priceLineVisible: false,
    })
    c.priceScale('ev').applyOptions({ scaleMargins: { top: 0.92, bottom: 0 }, visible: false })
    sB.current = c.addSeries(LineSeries, {
      color: cB + '50', lineWidth: 1, lineStyle: 2, priceLineVisible: false, lastValueVisible: true,
      crosshairMarkerVisible: false,
      priceFormat: { type: 'custom', formatter: (p: number) => `${tBRef.current.split(' ')[0].slice(0,5)} ${(p*100).toFixed(1)}¢` },
    })
    sModel.current = c.addSeries(LineSeries, {
      color: '#f5a623', lineWidth: 1, lineStyle: 2, priceLineVisible: false,
      lastValueVisible: true, crosshairMarkerVisible: false, pointMarkersVisible: true, pointMarkersRadius: 1.5,
      priceFormat: { type: 'custom', formatter: (p: number) => `Model ${(p*100).toFixed(1)}%` },
    })
    sA.current = c.addSeries(AreaSeries, {
      lineColor: cA, topColor: cA + '10', bottomColor: cA + '02', lineWidth: 2,
      lastValueVisible: true, priceLineVisible: false,
      priceFormat: { type: 'custom', formatter: (p: number) => (p * 100).toFixed(1) + '¢' },
    })
    sMkA.current = createSeriesMarkers(sA.current, [])
    sMkB.current = createSeriesMarkers(sB.current, [])
    const ro = new ResizeObserver(() => { if (el.current) c.applyOptions({ width: el.current.clientWidth, height: el.current.clientHeight }) })
    ro.observe(el.current)
    const markScrolled = () => { userScrolledRef.current = true }
    const node = el.current
    node.addEventListener('wheel', markScrolled, { passive: true })
    node.addEventListener('pointerdown', markScrolled)
    node.addEventListener('touchstart', markScrolled, { passive: true })
    return () => {
      node.removeEventListener('wheel', markScrolled)
      node.removeEventListener('pointerdown', markScrolled)
      node.removeEventListener('touchstart', markScrolled)
      ro.disconnect(); c.remove(); chart.current = null
    }
  }, [])

  useEffect(() => {
    if (!sA.current || !sB.current) return
    const pts = new Map<number, number>()
    for (const [ts, mid] of priceHistory) pts.set(Math.floor(ts), mid)
    for (const ev of events) { const t = Math.floor(ev.ts); if (!pts.has(t) && ev.mid > 0) pts.set(t, ev.mid) }
    const sorted = Array.from(pts.entries()).sort((a, b) => a[0] - b[0])
    let last = 0
    const dA: {time: Time; value: number}[] = [], dB: {time: Time; value: number}[] = []
    for (const [ts, mid] of sorted) {
      let t = ts; if (t <= last) t = last + 1; last = t
      if (mid > 0) { dA.push({time: t as Time, value: mid}); dB.push({time: t as Time, value: 1 - mid}) }
    }
    preserveRange(() => { sA.current!.setData(dA); sB.current!.setData(dB) })
  }, [priceHistory, events])

  useEffect(() => {
    if (!sModel.current) return
    if (!modelProbHistory || modelProbHistory.length === 0) {
      preserveRange(() => sModel.current!.setData([]))
      return
    }
    let last = 0
    const dM: {time: Time; value: number}[] = []
    for (const [ts, p] of modelProbHistory) {
      let t = Math.floor(ts); if (t <= last) t = last + 1; last = t
      if (p > 0) dM.push({ time: t as Time, value: p })
    }
    preserveRange(() => sModel.current!.setData(dM))
  }, [modelProbHistory])

  useEffect(() => {
    if (!sMkA.current || !sMkB.current || !sEv.current) return
    if (events.length === 0) {
      sMkA.current.setMarkers([])
      sMkB.current.setMarkers([])
      preserveRange(() => sEv.current!.setData([]))
      return
    }
    // Align event times to chart candle keys (same rules as setData) so markers sit on the series.
    const pts = new Map<number, number>()
    for (const [ts, mid] of priceHistory) pts.set(Math.floor(ts), mid)
    for (const ev of events) {
      const t = Math.floor(ev.ts)
      if (!pts.has(t) && ev.mid > 0) pts.set(t, ev.mid)
    }
    const sorted = Array.from(pts.entries()).sort((a, b) => a[0] - b[0])
    let last = 0
    const origToChart = new Map<number, number>()
    for (const [ts] of sorted) {
      let t = ts
      if (t <= last) t = last + 1
      last = t
      origToChart.set(ts, t)
    }
    const aLo = teamA.toLowerCase()
    const isA = (t: string) => {
      const lo = t.toLowerCase()
      return lo.includes(aLo) || aLo.includes(lo) || lo.split(' ')[0] === aLo.split(' ')[0]
    }
    const curCA = cARef.current, curCB = cBRef.current
    const filtered = events.filter(e => e.etype !== 'status' && e.etype !== 'init').slice(-40)
    const mkA: Parameters<ISeriesMarkersPluginApi<Time>['setMarkers']>[0] = []
    const mkB: Parameters<ISeriesMarkersPluginApi<Time>['setMarkers']>[0] = []
    for (const ev of filtered) {
      const chartT = origToChart.get(Math.floor(ev.ts))
      if (chartT === undefined) continue
      const time = chartT as Time
      const text = `${EVT_EMOJI[ev.etype] || '•'} ${ev.team.split(' ')[0]}`
      const row = { time, position: 'belowBar' as const, shape: 'arrowUp' as const, text }
      if (isA(ev.team)) mkA.push({ ...row, color: curCA })
      else mkB.push({ ...row, color: curCB })
    }
    const sortT = (a: { time: Time }, b: { time: Time }) => (a.time as number) - (b.time as number)
    sMkA.current.setMarkers(mkA.sort(sortT))
    sMkB.current.setMarkers(mkB.sort(sortT))
    const bars = filtered.map(ev => {
      const chartT = origToChart.get(Math.floor(ev.ts))
      const time = (chartT ?? Math.floor(ev.ts)) as Time
      return { time, value: 1, color: isA(ev.team) ? curCA : curCB }
    })
    const ded = new Map<number, typeof bars[0]>()
    for (const d of bars) ded.set(d.time as number, d)
    const evData = Array.from(ded.values()).sort((a, b) => (a.time as number) - (b.time as number))
    preserveRange(() => sEv.current!.setData(evData))
  }, [events, teamA, priceHistory])

  useEffect(() => {
    if (!chart.current || !hoveredTs) return
    try { chart.current.setCrosshairPosition(undefined as any, {time: Math.floor(hoveredTs) as Time} as any, sA.current!) } catch {}
    return () => { try { chart.current?.clearCrosshairPosition() } catch {} }
  }, [hoveredTs])

  return (
    <div className="w-full h-full relative group/c">
      <div ref={el} className="w-full h-full" />
      <div className="absolute top-1 right-1 flex gap-0.5 opacity-0 group-hover/c:opacity-100 transition-opacity z-10">
        {([['Fit', () => { resetFollow(); chart.current?.timeScale().fitContent() }],
          ['1m', () => { resetFollow(); const n = Math.floor(Date.now()/1000); chart.current?.timeScale().setVisibleRange({from: (n-60) as Time, to: n as Time}) }],
          ['5m', () => { resetFollow(); const n = Math.floor(Date.now()/1000); chart.current?.timeScale().setVisibleRange({from: (n-300) as Time, to: n as Time}) }],
        ] as [string, () => void][]).map(([l, fn]) => (
          <button key={l as string} onClick={fn as () => void}
            className="text-[7px] px-1.5 py-0.5 rounded bg-black/50 text-muted-foreground hover:text-foreground backdrop-blur-sm">
            {l as string}
          </button>
        ))}
      </div>
    </div>
  )
}

// ── Event feed ──────────────────────────────────────────────────────

function Events({ events, onHover, teamA, teamB, sideA, sideB }: {
  events: EventData[]; onHover: (ts: number | null) => void
  teamA: string; teamB: string; sideA?: string; sideB?: string
}) {
  const [open, setOpen] = useState<number | null>(null)
  if (!events.length) return null
  const cA = (sideA || '').toLowerCase() === 'blue' ? '#58a6ff' : '#f85149'
  const cB = (sideB || '').toLowerCase() === 'blue' ? '#58a6ff' : '#f85149'
  const aLo = teamA.toLowerCase()
  const isA = (t: string) => { const lo = t.toLowerCase(); return lo.includes(aLo) || aLo.includes(lo) }
  // Show ALL events the backend sends (currently capped at 200/match in the
  // API). The outer container is max-h-48 overflow-y-auto, so the user can
  // scroll through the full history without older events getting evicted.
  const rev = [...events].reverse()

  return (
    <div className="border-t border-border/50 text-[9px] font-mono max-h-48 overflow-y-auto">
      {rev.map((ev, i) => {
        const forA = isA(ev.team)
        const c = forA ? cA : cB
        const isOpen = open === i
        const isTradeIntent = Boolean(ev.signal_dir)
        const modelDir = ev.signal_dir || ev.model_dir
        const isBuyA = modelDir === 'buy_a'
        const showModel = Boolean(modelDir) && ev.pre_event_mid != null && ev.p_fair != null
        const limPx = inferredBuyLimit(ev)
        const refPx = isBuyA ? ev.buy_price_a : ev.buy_price_b
        const buyTitle = isTradeIntent
          ? [
              `Buy outcome token ${isBuyA ? 'A' : 'B'} (${isBuyA ? teamA : teamB})`,
              limPx != null ? `FAK limit ≤ ${(limPx * 100).toFixed(1)}¢` : '',
              refPx > 0 ? `Book ref ${(refPx * 100).toFixed(1)}¢ (+1¢ tick → limit)` : '',
            ].filter(Boolean).join('\n')
          : `Model win-prob shift for ${isBuyA ? teamA : teamB}${ev.signal_impact != null ? ` (Δ ${(ev.signal_impact * 100).toFixed(1)}c)` : ''}`
        return (
          <div key={i} onMouseEnter={() => onHover(ev.ts)} onMouseLeave={() => onHover(null)}>
            <div onClick={() => setOpen(isOpen ? null : i)}
              className={`flex items-center gap-1.5 px-2 py-[3px] cursor-pointer transition-colors ${isOpen ? 'bg-white/[0.03]' : 'hover:bg-white/[0.02]'}`}>
              <span className="text-[#444] w-[68px] shrink-0">{ev.time}</span>
              <span className="w-3 shrink-0">{EVT_EMOJI[ev.etype] || '•'}</span>
              <span className="font-bold w-14 shrink-0 tabular-nums" style={{color: c}}
                    title={ev.new_value != null ? `Team now at ${ev.new_value}` : ''}>
                {ev.etype.toUpperCase().slice(0,4)}
                {ev.delta != null && ev.delta !== 0 && (
                  <span className={ev.delta > 1 ? 'ml-1' : 'ml-1 opacity-70'}>
                    {ev.delta > 0 ? `+${ev.delta}` : ev.delta}
                  </span>
                )}
              </span>
              <span className="text-[#444] w-10 shrink-0">[{ev.clock}]</span>
              <span className="truncate flex-1 min-w-0" style={{color: c + 'b0'}} title={ev.desc}>
                {ev.old_value != null && ev.new_value != null ? (
                  <>
                    <span className="tabular-nums font-bold" style={{color: c}}>{ev.old_value}→{ev.new_value}</span>
                    <span className="opacity-70 ml-1.5">{(ev.team || '').split(/\s+/).slice(0, 2).join(' ').slice(0, 22)}</span>
                  </>
                ) : ev.desc}
              </span>
              {showModel && (() => {
                // pre_event_mid / p_fair are stored from the favored side's
                // perspective already, so flip only for legacy rows where
                // we inferred the side from signal_dir. Since backend now
                // stores in model_dir's frame, no flip needed.
                const pre = ev.pre_event_mid!
                const fair = ev.p_fair!
                const teamLabel = (isBuyA ? teamA : teamB).split(/\s+/)[0]?.slice(0, 10) || '?'
                return (
                  <div className="w-[120px] shrink-0 text-right leading-tight" title={buyTitle}>
                    <div className="text-[8px] font-bold" style={{ color: isBuyA ? cA : cB }}>
                      {isTradeIntent
                        ? `Buy ${isBuyA ? 'A' : 'B'} · ${teamLabel}`
                        : `${isBuyA ? 'A' : 'B'} · ${teamLabel}`}
                    </div>
                    <div className="text-[7px]">
                      <span className="text-[#777]">{(pre * 100).toFixed(0)}%</span>
                      <span className="text-[#555]">→</span>
                      <span className={fair > pre ? 'text-green-400' : fair < pre ? 'text-red-400' : 'text-[#777]'}>{(fair * 100).toFixed(0)}%</span>
                    </div>
                  </div>
                )
              })()}
              {(() => {
                const buyPx = ev.attempt_ref_px != null && ev.attempt_ref_px > 0
                  ? ev.attempt_ref_px
                  : (isBuyA ? ev.buy_price_a : ev.buy_price_b)
                return (
                  <span className="text-[#666] w-12 text-right shrink-0 tabular-nums" title={`Buy price: ${(buyPx * 100).toFixed(1)}¢${limPx != null ? ` | Limit: ≤${(limPx * 100).toFixed(1)}¢` : ''}`}>
                    {buyPx > 0 ? `${(buyPx * 100).toFixed(1)}¢` : `${(ev.mid * 100).toFixed(1)}¢`}
                  </span>
                )
              })()}
              {!showModel && (
                <span className="text-[#555] w-12 text-right shrink-0">{(ev.mid*100).toFixed(1)}¢</span>
              )}
              <Badge variant="outline" className={`text-[6px] h-2.5 px-1 shrink-0 ${eventActionBadge(ev).className}`} title={
                ev.action === 'TRADE' && ev.trade_exec === 'dry_run'
                  ? 'Paper trade — no Polymarket order (run with --live for real orders)'
                  : ev.action === 'TRADE' && ev.trade_exec === 'polymarket_ok'
                    ? 'Fill verified — position opened in bot; check Polymarket Activity for your wallet'
                    : ev.action === 'TRADE' && ev.trade_exec === 'no_fill_confirmed'
                      ? 'Order id returned but no matching fill in trades API — bot did not open a position'
                      : ev.action === 'TRADE' && ev.trade_exec === 'fill_rejected'
                        ? 'Fill size failed sanity check — no position opened'
                        : ev.action === 'GATED' && ev.gate_reason
                          ? `Blocked: ${ev.gate_reason}`
                          : ev.action === 'TRADE' && !ev.trade_exec
                            ? 'Legacy row: signal only, execution not recorded'
                            : undefined
              }>
                {eventActionBadge(ev).label}{ev.signal_size != null && ev.signal_size > 0 && ev.action === 'TRADE' ? ` $${Math.round(ev.signal_size)}` : ''}
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
                  <div className="text-amber-400 font-bold">Dry run: simulated position only — no order on Polymarket. Start trader with <span className="font-mono">--live</span> for real trades.</div>
                )}
                {ev.action === 'TRADE' && ev.trade_exec === 'no_fill_confirmed' && (
                  <div className="text-slate-300 font-bold">No confirmed fill — bot did not open a position. If Polymarket shows a stray order, cancel/reconcile in the app.</div>
                )}
                {ev.action === 'TRADE' && ev.trade_exec === 'fill_rejected' && (
                  <div className="text-red-300 font-bold">Reported fill size {!Number.isNaN(Number(ev.fill_reported_shares)) ? `(${Number(ev.fill_reported_shares).toFixed(2)} sh)` : ''} failed checks — no position. {ev.clob_order_id ? `Order ${ev.clob_order_id.slice(0, 16)}…` : ''}</div>
                )}
                {ev.action === 'TRADE' && (ev.trade_exec === 'no_order_id' || ev.trade_exec === 'order_error') && (
                  <div className="text-red-400 font-bold">CLOB order did not complete — {ev.order_error || 'no order id from API'}</div>
                )}
                {ev.action === 'TRADE' && !ev.trade_exec && (
                  <div className="text-[#888] font-bold">Signal only (legacy): {ev.signal_dir?.toUpperCase()} ${ev.signal_size?.toFixed(2)} — {ev.signal_reason}</div>
                )}
                {ev.action === 'GATED' && (
                  <div className="text-yellow-400 font-bold">Risk gate: {ev.gate_reason || 'blocked'}</div>
                )}
                {(ev.action === 'SKIP_SIZE' || ev.action === 'ORDER_FAIL') && (
                  <div className="text-orange-400 font-bold">{ev.action.replace(/_/g, ' ')}{ev.order_error ? ` — ${ev.order_error}` : ''}</div>
                )}
                {ev.action !== 'TRADE' && ev.action !== 'GATED' && ev.action !== 'SKIP_SIZE' && ev.action !== 'ORDER_FAIL' && (
                  <div className="text-orange-400">{ev.action.replace(/_/g,' ')}</div>
                )}
                {ev.signal_dir && (ev.signal_impact != null || ev.edge != null || ev.p_fair != null) && (() => {
                  const buyA = ev.signal_dir === 'buy_a'
                  const pre = ev.pre_event_mid != null ? (buyA ? ev.pre_event_mid : 1 - ev.pre_event_mid) : null
                  const fair = ev.p_fair != null ? (buyA ? ev.p_fair : 1 - ev.p_fair) : null
                  return (
                    <div className="flex gap-3 text-[#666]">
                      {pre != null && <span>Pre <span className="text-[#999]">{(pre * 100).toFixed(1)}%</span></span>}
                      {ev.signal_impact != null && <span>Impact <span className="text-[#bbb]">{ev.signal_impact > 0 ? '+' : ''}{(ev.signal_impact * 100).toFixed(1)}¢</span></span>}
                      {fair != null && <span>Fair <span className={fair > (pre || 0) ? 'text-green-400' : 'text-red-400/80'}>{(fair * 100).toFixed(1)}%</span></span>}
                      {ev.edge != null && <span>Edge <span className={ev.edge >= 0.02 ? 'text-green-400' : ev.edge > 0 ? 'text-yellow-400' : 'text-red-400/60'}>{(ev.edge * 100).toFixed(1)}¢</span></span>}
                      {ev.signal_size != null && ev.signal_size > 0 && <span>Size <span className="text-[#bbb]">${ev.signal_size.toFixed(0)}</span></span>}
                    </div>
                  )
                })()}
                <div className="flex gap-4 text-[#666]">
                  <span>Mid <span className="text-[#999]">{(ev.mid*100).toFixed(1)}¢</span></span>
                  <span>Bid <span className="text-green-400/60">{(ev.bid*100).toFixed(1)}¢</span></span>
                  <span>Ask <span className="text-red-400/60">{(ev.ask*100).toFixed(1)}¢</span></span>
                  <span>Sprd <span className="text-[#999]">{(ev.spread*100).toFixed(1)}¢</span></span>
                  <span>Δ2s <span className={ev.recent_move_2s > 0 ? 'text-green-400/60' : ev.recent_move_2s < 0 ? 'text-red-400/60' : 'text-[#999]'}>
                    {ev.recent_move_2s > 0 ? '+' : ''}{(ev.recent_move_2s*100).toFixed(1)}¢
                  </span></span>
                  <span>Mkt <span className="text-[#999]">{ev.market_type}</span></span>
                </div>
                {ev.book_snapshot && (ev.book_snapshot.bids.length > 0 || ev.book_snapshot.asks.length > 0) && (
                  <div className="flex gap-3">
                    <div className="flex-1">{ev.book_snapshot.bids.slice(0,4).map((b,j) => (
                      <div key={j} className="flex justify-between"><span className="text-green-400/50">{(b.p*100).toFixed(1)}¢</span><span className="text-[#555]">${Math.round(b.s*b.p)}</span></div>
                    ))}</div>
                    <div className="w-px bg-white/[0.05]" />
                    <div className="flex-1">{ev.book_snapshot.asks.slice(0,4).map((a,j) => (
                      <div key={j} className="flex justify-between"><span className="text-red-400/50">{(a.p*100).toFixed(1)}¢</span><span className="text-[#555]">${Math.round(a.s*a.p)}</span></div>
                    ))}</div>
                  </div>
                )}
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}

// ── Match Card ──────────────────────────────────────────────────────

export function MatchCard({ match, position }: { match: MatchData; position?: PositionData }) {
  const active = match.games.find(g => g.status === 'running')
    || [...match.games].reverse().find(g => g.status === 'finished')
    || match.games[0]
  const t1 = active?.teams?.[0], t2 = active?.teams?.[1]
  const [clock, setClock] = useState('--:--')
  const [hTs, setHTs] = useState<number | null>(null)
  const g = match.gamma || {} as any
  const sA = (t1?.side || '').toLowerCase(), sB = (t2?.side || '').toLowerCase()
  const cA = sA === 'blue' ? '#58a6ff' : '#f85149'
  const cB = sB === 'blue' ? '#58a6ff' : '#f85149'

  useEffect(() => {
    if (!active?.timer || active.timer.paused) { setClock(fmtGameClock(active?.timer)); return }
    const iv = setInterval(() => setClock(fmtGameClock(active.timer)), 1000)
    setClock(fmtGameClock(active.timer)); return () => clearInterval(iv)
  }, [active?.timer])

  const [boardAgeTick, setBoardAgeTick] = useState(0)
  useEffect(() => {
    const iv = setInterval(() => setBoardAgeTick(x => x + 1), 1000)
    return () => clearInterval(iv)
  }, [])
  const boardAgeSec = useMemo(() => {
    if (!match.llf_scoreboard_updated_at || match.llf_scoreboard_updated_at <= 0) return null
    return Math.max(0, Math.floor(Date.now() / 1000 - match.llf_scoreboard_updated_at))
  }, [boardAgeTick, match.llf_scoreboard_updated_at])

  const picks1: Record<string,string> = {}, picks2: Record<string,string> = {}
  for (const p of (active?.draft?.picks || [])) {
    if (t1 && p.team_id === t1.id) picks1[p.role] = p.champion_slug
    if (t2 && p.team_id === t2.id) picks2[p.role] = p.champion_slug
  }

  return (
    <Card className={`overflow-hidden ${!match.active ? 'opacity-40' : ''}`}>
      <CardContent className="p-0">

        {/* ── Top bar ── */}
        <div className="flex items-center justify-between px-3 py-2 border-b border-border/30">
          <div className="flex items-center gap-1.5 min-w-0">
            <span className="text-[11px] font-bold truncate">{match.name}</span>
            {g.league && <span className="text-[7px] text-[#555] bg-white/[0.03] rounded px-1 py-px">{g.league}{g.league_tier ? ` T${g.league_tier}` : ''}</span>}
            {g.live && <Badge variant="destructive" className="text-[7px] h-3.5 px-1.5 animate-pulse">LIVE</Badge>}
            {match.llf_status && (
              <span className={`text-[7px] px-1 py-px rounded font-mono ${
                match.llf_status === 'streaming' ? 'bg-green-500/20 text-green-400' :
                match.llf_status.startsWith('hello') ? 'bg-yellow-500/20 text-yellow-400' :
                match.llf_status === 'connected' || match.llf_status === 'scoreboard' ? 'bg-blue-500/20 text-blue-400' :
                match.llf_status.startsWith('not_open') ? 'bg-orange-500/15 text-orange-400/70' :
                match.llf_status === 'timeout' ? 'bg-red-500/15 text-red-400/70' :
                'bg-white/5 text-[#666]'
              }`} title={`LLF: ${match.llf_status} | msgs: ${match.llf_msg_count} | last: ${match.llf_last_msg_age > 0 ? match.llf_last_msg_age.toFixed(0) + 's ago' : '—'} (${match.llf_last_msg_type})`}>
                LLF:{match.llf_status.split(':')[0].split('(')[0]}
                {match.llf_last_msg_age > 0 && match.llf_last_msg_age < 999 && <span className="text-[#555] ml-0.5">{match.llf_last_msg_age.toFixed(0)}s</span>}
              </span>
            )}
            {active && active.status === 'running' && (
              <>
                <span className="text-[9px] text-[#666]">G{active.position}</span>
                <span className="text-sm font-mono font-black tabular-nums">{clock}</span>
              </>
            )}
          </div>
          <div className="flex items-center gap-2 shrink-0">
            {match.has_book && match.mid > 0 && (
              <span className="text-lg font-mono font-black tabular-nums">{fmtCents(match.mid)}</span>
            )}
            {match.has_book && <span className="text-[8px] text-[#555]">sprd {fmtCents(match.spread)}</span>}
          </div>
        </div>

        {/* ── Meta bar ── */}
        {(g.volume > 0 || g.score) && (
          <div className="flex items-center gap-3 px-3 py-1 text-[8px] text-[#555] font-mono bg-white/[0.01] border-b border-border/20">
            {g.score && <span className="text-[#888]">{g.score}</span>}
            {g.volume > 0 && <span>Vol <span className="text-[#888]">${(g.volume/1000).toFixed(0)}K</span></span>}
            {g.liquidity > 0 && <span>Liq <span className="text-[#888]">${(g.liquidity/1000).toFixed(0)}K</span></span>}
            {g.open_interest > 0 && <span>OI <span className="text-[#888]">${(g.open_interest/1000).toFixed(0)}K</span></span>}
            {g.start_time && <span>Start <span className="text-[#888]">{new Date(g.start_time).toLocaleDateString('en-GB',{day:'numeric',month:'short'})} {new Date(g.start_time).toLocaleTimeString('en-GB',{hour:'2-digit',minute:'2-digit',timeZone:'Asia/Almaty'})} ALT</span></span>}
            {match.total_markets > 0 && <span>{match.total_markets} mkts</span>}
          </div>
        )}

        {/* ── Position ── */}
        {position && !position.closed && (
          <div className={`px-3 py-1 text-[9px] font-mono font-bold ${position.unrealized_pnl >= 0 ? 'bg-green-500/8 text-green-400' : 'bg-red-500/8 text-red-400'}`}>
            ▶ {position.direction.toUpperCase()} {position.size.toFixed(1)} @ {(position.entry_price*100).toFixed(1)}¢
            → {position.unrealized_pnl >= 0 ? '+' : ''}{(position.unrealized_pnl*100).toFixed(1)}¢
            ({position.age_sec.toFixed(0)}s)
          </div>
        )}

        {/* ── Teams + Score ── */}
        {t1 && t2 && (
          <div className="grid grid-cols-[1fr_auto_1fr] border-b border-border/30">
            {/* Team A */}
            <div className="px-3 py-2" style={{background: cA + '06'}}>
              <div className="flex items-center gap-1.5 mb-1.5">
                <div className="w-1 h-4 rounded-full" style={{background: cA}} />
                <span className="text-[11px] font-bold truncate">{match.team_a}</span>
                <span className="text-[7px] uppercase tracking-wider" style={{color: cA + '80'}}>{sA || '?'}</span>
              </div>
              {Object.keys(picks1).length > 0 && (
                <div className="flex gap-1">
                  {ROLES.map(r => picks1[r] ? (
                    <img key={r} src={champUrl(picks1[r])} alt={picks1[r]} title={`${r.toUpperCase()}: ${picks1[r]}`}
                      className="w-7 h-7 rounded border border-white/10" loading="lazy"
                      onError={e => { (e.target as HTMLImageElement).style.display = 'none' }} />
                  ) : <div key={r} className="w-7 h-7 rounded bg-white/[0.03] border border-white/5" />)}
                </div>
              )}
            </div>

            {/* Score */}
            <div className="flex flex-col items-center justify-center px-4 min-w-[150px] border-x border-border/20 py-2">
              <div className="flex items-baseline gap-3">
                <span className={`text-2xl font-mono font-black ${t1.kills > t2.kills ? 'text-green-400' : t1.kills < t2.kills ? 'text-red-400/50' : 'text-[#888]'}`}>{t1.kills}</span>
                <span className="text-[10px] text-[#333]">vs</span>
                <span className={`text-2xl font-mono font-black ${t2.kills > t1.kills ? 'text-green-400' : t2.kills < t1.kills ? 'text-red-400/50' : 'text-[#888]'}`}>{t2.kills}</span>
              </div>
              {match.mid > 0 && (
                <div className="flex items-center gap-1.5 text-[10px] font-mono font-bold mt-0.5">
                  <span style={{color: cA}}>{(match.mid*100).toFixed(0)}¢</span>
                  <span className="text-[#333]">—</span>
                  <span style={{color: cB}}>{((1-match.mid)*100).toFixed(0)}¢</span>
                </div>
              )}
              <div className="flex gap-2.5 mt-1 text-[9px] font-mono">
                <span title="Towers">🏰 <span className={t1.towers > t2.towers ? 'text-green-400' : 'text-[#666]'}>{t1.towers}</span>:<span className={t2.towers > t1.towers ? 'text-green-400' : 'text-[#666]'}>{t2.towers}</span></span>
                <span title="Drakes">🐉 <span className={t1.drakes > t2.drakes ? 'text-green-400' : 'text-[#666]'}>{t1.drakes}</span>:<span className={t2.drakes > t1.drakes ? 'text-green-400' : 'text-[#666]'}>{t2.drakes}</span></span>
                <span title="Barons">👿 <span className={t1.nashors > t2.nashors ? 'text-green-400' : 'text-[#666]'}>{t1.nashors}</span>:<span className={t2.nashors > t1.nashors ? 'text-green-400' : 'text-[#666]'}>{t2.nashors}</span></span>
                {(t1.inhibitors > 0 || t2.inhibitors > 0) && <span title="Inhibs">💥 {t1.inhibitors}:{t2.inhibitors}</span>}
              </div>
              <div
                className={`text-[7px] font-mono mt-0.5 tabular-nums ${
                  boardAgeSec === null ? 'text-[#444]' :
                  boardAgeSec < 20 ? 'text-[#555]' :
                  boardAgeSec < 45 ? 'text-amber-500/80' : 'text-orange-400/90'
                }`}
                title={match.llf_scoreboard_updated_at ? `LLF scoreboard snapshot (Unix ${match.llf_scoreboard_updated_at.toFixed(3)})` : 'No scoreboard timestamp yet'}
              >
                {boardAgeSec === null ? 'scoreboard —' : `scoreboard ${boardAgeSec}s ago`}
              </div>
            </div>

            {/* Team B */}
            <div className="px-3 py-2 text-right" style={{background: cB + '06'}}>
              <div className="flex items-center gap-1.5 justify-end mb-1.5">
                <span className="text-[7px] uppercase tracking-wider" style={{color: cB + '80'}}>{sB || '?'}</span>
                <span className="text-[11px] font-bold truncate">{match.team_b}</span>
                <div className="w-1 h-4 rounded-full" style={{background: cB}} />
              </div>
              {Object.keys(picks2).length > 0 && (
                <div className="flex gap-1 justify-end">
                  {ROLES.map(r => picks2[r] ? (
                    <img key={r} src={champUrl(picks2[r])} alt={picks2[r]} title={`${r.toUpperCase()}: ${picks2[r]}`}
                      className="w-7 h-7 rounded border border-white/10" loading="lazy"
                      onError={e => { (e.target as HTMLImageElement).style.display = 'none' }} />
                  ) : <div key={r} className="w-7 h-7 rounded bg-white/[0.03] border border-white/5" />)}
                </div>
              )}
            </div>
          </div>
        )}

        {/* ── Chart ── */}
        {(match.price_history.length > 0 || (match.match_events || []).length > 0) && (
          <div className="h-56">
            <Chart priceHistory={match.price_history} modelProbHistory={match.model_prob_history} events={match.match_events || []}
              teamA={match.team_a} teamB={match.team_b} sideA={t1?.side||undefined} sideB={t2?.side||undefined} hoveredTs={hTs} />
          </div>
        )}

        {/* ── Events ── */}
        <Events events={match.match_events || []} onHover={setHTs}
          teamA={match.team_a} teamB={match.team_b} sideA={t1?.side||undefined} sideB={t2?.side||undefined} />

        {/* ── Book ── */}
        {match.has_book && (match.book_bids.length > 0 || match.book_asks.length > 0) && (
          <div className="flex gap-px text-[8px] font-mono tabular-nums border-t border-border/30 bg-white/[0.01]">
            <div className="flex-1 p-1.5 space-y-px">
              {[...match.book_bids].sort((a,b)=>b.p-a.p).slice(0,5).map((b,i) => {
                const max = Math.max(...match.book_bids.map(x=>x.s), 1)
                return (
                  <div key={i} className="relative flex justify-between px-1 py-px rounded-sm">
                    <div className="absolute inset-y-0 right-0 bg-green-500/8 rounded-sm" style={{width:`${(b.s/max)*100}%`}} />
                    <span className="relative text-green-400/70">{(b.p*100).toFixed(1)}</span>
                    <span className="relative text-[#555]">${Math.round(b.s*b.p)}</span>
                  </div>
                )
              })}
            </div>
            <div className="w-px bg-border/20" />
            <div className="flex-1 p-1.5 space-y-px">
              {[...match.book_asks].sort((a,b)=>a.p-b.p).slice(0,5).map((a,i) => {
                const max = Math.max(...match.book_asks.map(x=>x.s), 1)
                return (
                  <div key={i} className="relative flex justify-between px-1 py-px rounded-sm">
                    <div className="absolute inset-y-0 left-0 bg-red-500/8 rounded-sm" style={{width:`${(a.s/max)*100}%`}} />
                    <span className="relative text-red-400/70">{(a.p*100).toFixed(1)}</span>
                    <span className="relative text-[#555]">${Math.round(a.s*a.p)}</span>
                  </div>
                )
              })}
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  )
}

// ── Grid ────────────────────────────────────────────────────────────

export function MatchGrid({ data }: { data?: import('@/lib/types').TraderState }) {
  const [showAll, setShowAll] = useState(false)
  if (!data) return <div className="flex items-center justify-center py-16 text-[#555] text-sm">Connecting to Oracle-LoL...</div>

  const all = Object.values(data.matches)
  const isLive = (m: typeof all[0]) => {
    if (!m.active || !m.has_market) return false
    if (m.status === 'running') return true
    if (m.gamma?.live) return true
    if (m.event_count > 0) return true
    return false
  }
  const live = all.filter(isLive).sort((a, b) => b.event_count - a.event_count)
  const upcoming = all.filter(m => m.active && m.has_market && !isLive(m))
    .sort((a, b) => new Date(a.gamma?.start_time || '').getTime() - new Date(b.gamma?.start_time || '').getTime())
  const rest = all.filter(m => !isLive(m) && !(m.active && m.has_market))

  return (
    <div className="px-4">
      {live.length > 0 && (
        <>
          <div className="text-[9px] text-[#555] uppercase tracking-widest mb-2 font-semibold">
            Live ({live.length})
          </div>
          <div className="grid grid-cols-1 xl:grid-cols-2 2xl:grid-cols-3 gap-3 mb-3">
            {live.map(m => <MatchCard key={m.match_id} match={m} position={data.positions.find(p => p.match_id === m.match_id && !p.closed)} />)}
          </div>
        </>
      )}
      {upcoming.length > 0 && (
        <>
          <div className="text-[9px] text-[#555] uppercase tracking-widest mb-2 font-semibold mt-2">
            Upcoming ({upcoming.length})
          </div>
          <div className="grid grid-cols-1 xl:grid-cols-2 2xl:grid-cols-3 gap-3 mb-3">
            {upcoming.map(m => <MatchCard key={m.match_id} match={m} />)}
          </div>
        </>
      )}
      {rest.length > 0 && (
        <button onClick={() => setShowAll(!showAll)}
          className="text-[9px] text-[#444] uppercase tracking-widest mb-2 hover:text-[#888] transition-colors flex items-center gap-1 font-semibold mt-2">
          {showAll ? '▾' : '▸'} No Market / Other ({rest.length})
        </button>
      )}
      {showAll && (
        <div className="grid grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4 gap-2 mb-3 opacity-40">
          {rest.slice(0, 12).map(m => (
            <div key={m.match_id} className="text-[9px] text-[#555] bg-white/[0.02] rounded-lg px-3 py-2 border border-white/[0.03]">
              <div className="font-bold text-[10px] text-[#777]">{m.name}</div>
              <div className="text-[#444]">{m.league} · {m.status}</div>
            </div>
          ))}
        </div>
      )}
      {live.length === 0 && upcoming.length === 0 && <div className="text-center py-16 text-[#555] text-sm">No live matches</div>}
    </div>
  )
}
