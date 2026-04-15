import { useEffect, useRef, useState } from 'react'
import { Card, CardContent } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import type { MatchData, GameTeam, PositionData, EventData } from '@/lib/types'
import { fmtCents, fmtGameClock } from '@/lib/format'
import { createChart, AreaSeries, LineSeries, HistogramSeries, createSeriesMarkers, type IChartApi, type ISeriesApi, type ISeriesMarkersPluginApi, type Time } from 'lightweight-charts'

// ── Mini Chart with event markers ───────────────────────────────────

const TEAM_A_COLOR = '#58a6ff'
const TEAM_B_COLOR = '#f97583'
const TEAM_A_ARROW = '#58a6ff'
const TEAM_B_ARROW = '#f97583'
const EVT_EMOJI: Record<string, string> = {
  kill: '⚔', drake: '🐉', baron: '👿', inhibitor: '💥', tower: '🏰',
}

function MiniChart({ priceHistory, events, teamA, teamB, hoveredTs, sideA, sideB }: {
  priceHistory: [number, number][]
  events: EventData[]
  teamA: string
  teamB: string
  hoveredTs?: number | null
  sideA?: string
  sideB?: string
}) {
  const colorA = (sideA || '').toLowerCase() === 'blue' ? '#58a6ff' : '#f85149'
  const colorB = (sideB || '').toLowerCase() === 'blue' ? '#58a6ff' : '#f85149'
  const ref = useRef<HTMLDivElement>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const seriesRef = useRef<ISeriesApi<'Area'> | null>(null)
  const seriesBRef = useRef<ISeriesApi<'Line'> | null>(null)
  const markersRef = useRef<ISeriesMarkersPluginApi<Time> | null>(null)
  const evtBarRef = useRef<ISeriesApi<'Histogram'> | null>(null)
  const teamARef = useRef(teamA)
  const teamBRef = useRef(teamB)
  teamARef.current = teamA
  teamBRef.current = teamB

  useEffect(() => {
    if (!ref.current || chartRef.current) return
    const chart = createChart(ref.current, {
      layout: { background: { color: 'transparent' }, textColor: 'hsl(0 0% 40%)', fontSize: 9 },
      grid: { vertLines: { color: 'hsl(0 0% 10%)' }, horzLines: { color: 'hsl(0 0% 10%)' } },
      rightPriceScale: { borderColor: 'transparent', scaleMargins: { top: 0.08, bottom: 0.12 } },
      timeScale: { borderColor: 'transparent', timeVisible: true, secondsVisible: true },
      crosshair: {
        vertLine: { color: 'hsl(0 0% 30%)', width: 1, style: 2, labelBackgroundColor: 'hsl(0 0% 15%)' },
        horzLine: { color: 'hsl(0 0% 30%)', width: 1, style: 2, labelBackgroundColor: 'hsl(0 0% 15%)' },
      },
      handleScroll: true, handleScale: true,
      localization: { priceFormatter: (p: number) => (p * 100).toFixed(1) + '¢' },
    })
    chartRef.current = chart

    evtBarRef.current = chart.addSeries(HistogramSeries, {
      priceScaleId: 'evt', priceFormat: { type: 'custom' as const, formatter: () => '' },
      lastValueVisible: false, priceLineVisible: false,
    })
    chart.priceScale('evt').applyOptions({ scaleMargins: { top: 0.92, bottom: 0 }, visible: false })

    seriesBRef.current = chart.addSeries(LineSeries, {
      color: colorB + '80', lineWidth: 2, lineStyle: 0,
      priceLineVisible: false, lastValueVisible: true,
      crosshairMarkerVisible: false,
      priceFormat: { type: 'custom' as const, formatter: (p: number) => {
        const n = teamBRef.current.split(' ')[0].slice(0, 5)
        return `${n} ${(p * 100).toFixed(1)}¢`
      }},
    })

    seriesRef.current = chart.addSeries(AreaSeries, {
      lineColor: colorA, topColor: colorA + '10',
      bottomColor: colorA + '02', lineWidth: 2,
      priceFormat: { type: 'custom' as const, formatter: (p: number) => {
        const n = teamARef.current.split(' ')[0].slice(0, 5)
        return `${n} ${(p * 100).toFixed(1)}¢`
      }},
      lastValueVisible: true, priceLineVisible: false,
    })
    markersRef.current = createSeriesMarkers(seriesRef.current, [])

    const ro = new ResizeObserver(() => {
      if (ref.current) chart.applyOptions({ width: ref.current.clientWidth, height: ref.current.clientHeight })
    })
    ro.observe(ref.current)
    return () => { ro.disconnect(); chart.remove(); chartRef.current = null; seriesRef.current = null }
  }, [])

  useEffect(() => {
    if (!seriesRef.current || !seriesBRef.current) return
    if (priceHistory.length === 0 && events.length === 0) return

    const allPoints = new Map<number, number>()

    for (const [ts, mid] of priceHistory) {
      allPoints.set(Math.floor(ts), mid)
    }

    for (const ev of events) {
      const t = Math.floor(ev.ts)
      if (!allPoints.has(t)) {
        allPoints.set(t, ev.mid || 0.5)
      }
    }

    const sorted = Array.from(allPoints.entries()).sort((a, b) => a[0] - b[0])

    let last = 0
    const dataA: { time: Time; value: number }[] = []
    const dataB: { time: Time; value: number }[] = []
    for (const [ts, mid] of sorted) {
      let t = ts; if (t <= last) t = last + 1; last = t
      if (mid > 0) {
        dataA.push({ time: t as Time, value: mid })
        dataB.push({ time: t as Time, value: 1 - mid })
      }
    }
    seriesRef.current.setData(dataA)
    seriesBRef.current.setData(dataB)
  }, [priceHistory, events])

  useEffect(() => {
    if (!markersRef.current || !evtBarRef.current || events.length === 0) return

    function isA(evTeam: string): boolean {
      const t = evTeam.toLowerCase()
      const a = teamA.toLowerCase()
      const b = teamB.toLowerCase()
      if (t === a || t.includes(a) || a.includes(t)) return true
      if (t === b || t.includes(b) || b.includes(t)) return false
      return t.split(' ')[0] === a.split(' ')[0]
    }

    const markers = events.filter(e => e.etype !== 'status').slice(-40).map(ev => {
      const forA = isA(ev.team)
      const emoji = EVT_EMOJI[ev.etype] || '•'
      const shortTeam = ev.team.split(' ')[0]
      return {
        time: Math.floor(ev.ts) as Time,
        position: forA ? 'belowBar' as const : 'aboveBar' as const,
        color: forA ? colorA : colorB,
        shape: forA ? 'arrowUp' as const : 'arrowDown' as const,
        text: `${emoji} ${shortTeam}`,
      }
    })
    markersRef.current.setMarkers(markers.sort((a, b) => (a.time as number) - (b.time as number)))

    const bars = events.filter(e => e.etype !== 'status').slice(-40).map(ev => ({
      time: Math.floor(ev.ts) as Time,
      value: 1,
      color: isA(ev.team) ? colorA : colorB,
    }))
    const deduped = new Map<number, typeof bars[0]>()
    for (const d of bars) deduped.set(d.time as number, d)
    evtBarRef.current.setData(Array.from(deduped.values()).sort((a, b) => (a.time as number) - (b.time as number)))
  }, [events, teamA, teamB])

  useEffect(() => {
    if (!chartRef.current || !hoveredTs) return
    try {
      chartRef.current.setCrosshairPosition(undefined as any, { time: Math.floor(hoveredTs) as Time } as any, seriesRef.current!)
    } catch {}
    return () => { try { chartRef.current?.clearCrosshairPosition() } catch {} }
  }, [hoveredTs])

  function zoomRange(seconds: number) {
    if (!chartRef.current) return
    const now = Math.floor(Date.now() / 1000)
    chartRef.current.timeScale().setVisibleRange({
      from: (now - seconds) as Time,
      to: now as Time,
    })
  }

  function fitAll() {
    chartRef.current?.timeScale().fitContent()
  }

  return (
    <div className="w-full h-full relative group/chart">
      <div ref={ref} className="w-full h-full" />
      <div className="absolute top-1 right-1 flex gap-0.5 opacity-0 group-hover/chart:opacity-100 transition-opacity z-10">
        {[['Fit', fitAll], ['1m', () => zoomRange(60)], ['5m', () => zoomRange(300)], ['15m', () => zoomRange(900)]] .map(([label, fn]) => (
          <button
            key={label as string}
            onClick={fn as () => void}
            className="text-[8px] px-1.5 py-0.5 rounded bg-muted/80 text-muted-foreground hover:bg-muted hover:text-foreground transition-colors backdrop-blur-sm"
          >
            {label as string}
          </button>
        ))}
      </div>
    </div>
  )
}

// ── Role icons ──────────────────────────────────────────────────────

const ROLE_LABELS: Record<string, string> = { top: 'TOP', jun: 'JNG', mid: 'MID', adc: 'ADC', sup: 'SUP' }
const ROLE_ORDER = ['top', 'jun', 'mid', 'adc', 'sup']
const DDRAGON = 'https://ddragon.leagueoflegends.com/cdn/14.24.1/img/champion'
const CHAMP_FIXES: Record<string, string> = {
  jarvaniv: 'JarvanIV', monkeyking: 'MonkeyKing', wukong: 'MonkeyKing',
  renataglasc: 'Renata', bellaveth: 'Belveth', ksante: 'KSante',
}
function champIcon(slug: string): string {
  const fixed = CHAMP_FIXES[slug.toLowerCase()] || slug.charAt(0).toUpperCase() + slug.slice(1)
  return `${DDRAGON}/${fixed}.png`
}

// ── Score + Draft display ───────────────────────────────────────────

function GameScoreAndDraft({ t1, t2, nameA, nameB, draft, mid, spread }: {
  t1: GameTeam; t2: GameTeam; nameA: string; nameB: string
  draft?: { picks: import('@/lib/types').DraftPick[] }
  mid?: number; spread?: number
}) {
  const sideA = (t1.side || '?').toUpperCase()
  const sideB = (t2.side || '?').toUpperCase()
  const colorA = sideA === 'BLUE' ? 'text-blue-400 border-blue-400/40' : 'text-red-400 border-red-400/40'
  const colorB = sideB === 'BLUE' ? 'text-blue-400 border-blue-400/40' : 'text-red-400 border-red-400/40'
  const bgA = sideA === 'BLUE' ? 'bg-blue-500/5' : 'bg-red-500/5'
  const bgB = sideB === 'BLUE' ? 'bg-blue-500/5' : 'bg-red-500/5'

  const picksA: Record<string, string> = {}
  const picksB: Record<string, string> = {}
  for (const p of (draft?.picks || [])) {
    if (p.team_id === t1.id) picksA[p.role] = p.champion_slug
    if (p.team_id === t2.id) picksB[p.role] = p.champion_slug
  }

  return (
    <div className="grid grid-cols-[1fr_auto_1fr] gap-0 border-t border-border">
      {/* Team A */}
      <div className={`px-2 py-1.5 ${bgA}`}>
        <div className="flex items-center gap-1.5 mb-1">
          <Badge variant="outline" className={`text-[7px] h-3 px-1 ${colorA}`}>{sideA}</Badge>
          <span className="text-[11px] font-bold truncate">{nameA}</span>
        </div>
        {Object.keys(picksA).length > 0 && (
          <div className="flex gap-1">
            {ROLE_ORDER.map(r => {
              const champ = picksA[r]
              return (
                <div key={r} className="flex flex-col items-center" title={champ ? `${ROLE_LABELS[r]}: ${champ}` : ROLE_LABELS[r]}>
                  {champ ? (
                    <img src={champIcon(champ)} alt={champ} className="w-6 h-6 rounded-sm border border-border/30" loading="lazy"
                      onError={e => { (e.target as HTMLImageElement).style.display = 'none' }} />
                  ) : (
                    <div className="w-6 h-6 rounded-sm bg-muted/30 flex items-center justify-center text-[7px] text-muted-foreground/40">?</div>
                  )}
                  <div className="text-[6px] text-muted-foreground/40 mt-px">{ROLE_LABELS[r]}</div>
                </div>
              )
            })}
          </div>
        )}
      </div>

      {/* Score center */}
      <div className="flex flex-col items-center justify-center px-4 border-x border-border/30 min-w-[180px] py-2 gap-1.5">
        {/* Kill score */}
        <div className="flex items-baseline gap-4">
          <span className={`text-2xl font-mono font-black tabular-nums ${t1.kills > t2.kills ? 'text-green-400' : t1.kills < t2.kills ? 'text-red-400/60' : 'text-foreground'}`}>{t1.kills}</span>
          <span className="text-muted-foreground/20 text-xs font-medium">vs</span>
          <span className={`text-2xl font-mono font-black tabular-nums ${t2.kills > t1.kills ? 'text-green-400' : t2.kills < t1.kills ? 'text-red-400/60' : 'text-foreground'}`}>{t2.kills}</span>
        </div>

        {/* Prices */}
        {mid != null && mid > 0 && (
          <div className="flex items-center gap-1 text-[10px] font-mono tabular-nums">
            <span className="font-bold" style={{ color: colorA }}>{(mid * 100).toFixed(0)}¢</span>
            <span className="text-muted-foreground/15">—</span>
            <span className="font-bold" style={{ color: colorB }}>{((1 - mid) * 100).toFixed(0)}¢</span>
            {spread != null && spread < 0.5 && (
              <span className={`ml-1 text-[8px] ${spread <= 0.01 ? 'text-green-400/50' : spread <= 0.03 ? 'text-yellow-400/50' : 'text-red-400/50'}`}>
                {(spread * 100).toFixed(0)}¢
              </span>
            )}
          </div>
        )}

        {/* Objectives */}
        <div className="flex gap-2">
          <ObjStat icon="🏰" a={t1.towers} b={t2.towers} />
          <ObjStat icon="🐉" a={t1.drakes} b={t2.drakes} />
          <ObjStat icon="👿" a={t1.nashors} b={t2.nashors} />
          {(t1.inhibitors > 0 || t2.inhibitors > 0) && <ObjStat icon="💥" a={t1.inhibitors} b={t2.inhibitors} />}
        </div>
      </div>

      {/* Team B */}
      <div className={`px-2 py-1.5 text-right ${bgB}`}>
        <div className="flex items-center gap-1.5 justify-end mb-1">
          <span className="text-[11px] font-bold truncate">{nameB}</span>
          <Badge variant="outline" className={`text-[7px] h-3 px-1 ${colorB}`}>{sideB}</Badge>
        </div>
        {Object.keys(picksB).length > 0 && (
          <div className="flex gap-1 justify-end">
            {ROLE_ORDER.map(r => {
              const champ = picksB[r]
              return (
                <div key={r} className="flex flex-col items-center" title={champ ? `${ROLE_LABELS[r]}: ${champ}` : ROLE_LABELS[r]}>
                  {champ ? (
                    <img src={champIcon(champ)} alt={champ} className="w-6 h-6 rounded-sm border border-border/30" loading="lazy"
                      onError={e => { (e.target as HTMLImageElement).style.display = 'none' }} />
                  ) : (
                    <div className="w-6 h-6 rounded-sm bg-muted/30 flex items-center justify-center text-[7px] text-muted-foreground/40">?</div>
                  )}
                  <div className="text-[6px] text-muted-foreground/40 mt-px">{ROLE_LABELS[r]}</div>
                </div>
              )
            })}
          </div>
        )}
      </div>
    </div>
  )
}

function ObjStat({ icon, a, b }: { icon: string; a: number; b: number }) {
  return (
    <div className="flex items-center gap-0.5 text-[9px] font-mono tabular-nums">
      <span className="text-[8px]">{icon}</span>
      <span className={a > b ? 'text-green-400 font-bold' : a > 0 ? 'text-foreground' : 'text-muted-foreground/30'}>{a}</span>
      <span className="text-muted-foreground/20">:</span>
      <span className={b > a ? 'text-green-400 font-bold' : b > 0 ? 'text-foreground' : 'text-muted-foreground/30'}>{b}</span>
    </div>
  )
}

// ── Event feed with expandable details ──────────────────────────────

function EventFeed({ events, onHover, teamA, teamB, sideA, sideB }: {
  events: EventData[]; onHover?: (ts: number | null) => void
  teamA: string; teamB: string; sideA?: string; sideB?: string
}) {
  const cA = (sideA || '').toLowerCase() === 'blue' ? '#58a6ff' : '#f85149'
  const cB = (sideB || '').toLowerCase() === 'blue' ? '#58a6ff' : '#f85149'
  const [expandedIdx, setExpandedIdx] = useState<number | null>(null)
  if (events.length === 0) return null

  function isA(t: string): boolean {
    const lo = t.toLowerCase(), a = teamA.toLowerCase(), b = teamB.toLowerCase()
    if (lo === a || lo.includes(a) || a.includes(lo)) return true
    if (lo === b || lo.includes(b) || b.includes(lo)) return false
    return lo.split(' ')[0] === a.split(' ')[0]
  }
  const ACTION_COLORS: Record<string, string> = {
    TRADE: 'bg-green-500/20 text-green-400 border-green-500/30',
    SPREAD: 'bg-yellow-500/15 text-yellow-400 border-yellow-500/20',
    LOW: 'bg-orange-500/15 text-orange-400 border-orange-500/20',
    PRICED: 'bg-purple-500/15 text-purple-400 border-purple-500/20',
    PRICE: 'bg-muted text-muted-foreground border-border',
    NEAR: 'bg-muted text-muted-foreground border-border',
    TOWER: 'bg-muted/50 text-muted-foreground/50 border-transparent',
  }
  function actionColor(action: string) {
    for (const [prefix, cls] of Object.entries(ACTION_COLORS)) {
      if (action.startsWith(prefix)) return cls
    }
    return 'bg-muted text-muted-foreground border-border'
  }

  const reversed = [...events].reverse().slice(0, 10)

  return (
    <div className="border-t border-border font-mono text-[9px] max-h-52 overflow-y-auto">
      {reversed.map((ev, i) => {
        const isOpen = expandedIdx === i
        return (
          <div key={i}
            onMouseEnter={() => onHover?.(ev.ts)}
            onMouseLeave={() => onHover?.(null)}
          >
            {/* Row */}
            <div
              onClick={() => setExpandedIdx(isOpen ? null : i)}
              className={`flex gap-1 items-center px-2 py-0.5 cursor-pointer transition-colors
                ${isOpen ? 'bg-muted/50' : 'hover:bg-muted/20'}
                ${i === 0 ? 'animate-in fade-in slide-in-from-top-1 duration-200' : ''}`}
            >
              <span className="text-muted-foreground/30 w-[70px] shrink-0">{ev.time}</span>
              <span className="shrink-0 w-4">{EVT_EMOJI[ev.etype] || '•'}</span>
              <span className="font-bold w-10 shrink-0" style={{ color: isA(ev.team) ? cA : cB }}>
                {ev.etype.toUpperCase().slice(0, 5)}
              </span>
              <span className="text-muted-foreground/70 w-10 shrink-0">[{ev.clock}]</span>
              <span className="truncate flex-1" style={{ color: (isA(ev.team) ? cA : cB) + 'b0' }}>{ev.desc}</span>
              <span className="text-muted-foreground/30 w-10 text-right shrink-0">{(ev.mid * 100).toFixed(1)}¢</span>
              <Badge variant="outline" className={`text-[6px] h-2.5 px-1 shrink-0 ${actionColor(ev.action)}`}>
                {ev.action === 'TRADE' ? 'BUY' : ev.action.replace(/_/g, ' ').slice(0, 12)}
              </Badge>
              <span className="text-muted-foreground/20 shrink-0">{isOpen ? '▼' : '▸'}</span>
            </div>

            {/* Expanded details */}
            {isOpen && (
              <div className="px-3 py-1.5 bg-background/60 border-y border-border/30 space-y-1 text-[8px]">
                {/* Why / Action */}
                <div className="flex gap-4">
                  <div className="flex-1 space-y-0.5">
                    <div className="text-muted-foreground/50 uppercase tracking-wider">Decision</div>
                    <div className={ev.action === 'TRADE' ? 'text-green-400 font-bold' : 'text-orange-400'}>
                      {ev.action === 'TRADE'
                        ? `BUY ${ev.signal_dir?.toUpperCase()} $${ev.signal_size?.toFixed(2)} — ${ev.signal_reason}`
                        : ev.action.replace(/_/g, ' ')}
                    </div>
                    {ev.signal_impact != null && (
                      <div className="text-muted-foreground/60">
                        Impact: {(ev.signal_impact * 100).toFixed(1)}c | Conf: {((ev.signal_confidence || 0) * 100).toFixed(0)}%
                      </div>
                    )}
                  </div>
                </div>

                {/* Prices */}
                <div className="flex gap-4">
                  <div>
                    <span className="text-muted-foreground/50">Mid </span>
                    <span>{(ev.mid * 100).toFixed(1)}¢</span>
                  </div>
                  <div>
                    <span className="text-muted-foreground/50">Bid </span>
                    <span className="text-green-400">{(ev.bid * 100).toFixed(1)}¢</span>
                  </div>
                  <div>
                    <span className="text-muted-foreground/50">Ask </span>
                    <span className="text-red-400">{(ev.ask * 100).toFixed(1)}¢</span>
                  </div>
                  <div>
                    <span className="text-muted-foreground/50">Spread </span>
                    <span>{(ev.spread * 100).toFixed(1)}¢</span>
                  </div>
                  <div>
                    <span className="text-muted-foreground/50">Move(2s) </span>
                    <span className={ev.recent_move_2s > 0 ? 'text-green-400' : ev.recent_move_2s < 0 ? 'text-red-400' : ''}>
                      {ev.recent_move_2s > 0 ? '+' : ''}{(ev.recent_move_2s * 100).toFixed(1)}¢
                    </span>
                  </div>
                </div>

                <div className="flex gap-4">
                  <div>
                    <span className="text-muted-foreground/50">Buy A: </span>
                    <span>{(ev.buy_price_a * 100).toFixed(1)}¢</span>
                  </div>
                  <div>
                    <span className="text-muted-foreground/50">Buy B: </span>
                    <span>{(ev.buy_price_b * 100).toFixed(1)}¢</span>
                  </div>
                  <div>
                    <span className="text-muted-foreground/50">Mkt: </span>
                    <span>{ev.market_type}</span>
                  </div>
                  {ev.holding && (
                    <div>
                      <span className="text-muted-foreground/50">Holding: </span>
                      <span className="text-yellow-400">{ev.holding}</span>
                    </div>
                  )}
                </div>

                {/* Orderbook snapshot */}
                {ev.book_snapshot && (ev.book_snapshot.bids.length > 0 || ev.book_snapshot.asks.length > 0) && (
                  <div>
                    <div className="text-muted-foreground/50 uppercase tracking-wider mb-0.5">Book Snapshot</div>
                    <div className="flex gap-2">
                      <div className="flex-1">
                        {ev.book_snapshot.bids.slice(0, 4).map((b, j) => (
                          <div key={j} className="flex justify-between">
                            <span className="text-green-400/70">{(b.p * 100).toFixed(1)}¢</span>
                            <span className="text-muted-foreground/50">${Math.round(b.s * b.p)}</span>
                          </div>
                        ))}
                      </div>
                      <div className="w-px bg-border/30" />
                      <div className="flex-1">
                        {ev.book_snapshot.asks.slice(0, 4).map((a, j) => (
                          <div key={j} className="flex justify-between">
                            <span className="text-red-400/70">{(a.p * 100).toFixed(1)}¢</span>
                            <span className="text-muted-foreground/50">${Math.round(a.s * a.p)}</span>
                          </div>
                        ))}
                      </div>
                    </div>
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

// ── Mini Book ───────────────────────────────────────────────────────

function MiniBook({ bids, asks }: { bids: { p: number; s: number }[]; asks: { p: number; s: number }[] }) {
  if (bids.length === 0 && asks.length === 0) return null

  const sortedBids = [...bids].sort((a, b) => b.p - a.p).slice(0, 5)
  const sortedAsks = [...asks].sort((a, b) => a.p - b.p).slice(0, 5)
  const maxSize = Math.max(...sortedBids.map(b => b.s), ...sortedAsks.map(a => a.s), 1)

  return (
    <div className="flex gap-1 text-[8px] font-mono tabular-nums px-2 py-1 border-t border-border">
      {/* Bids: highest (best) first */}
      <div className="flex-1 space-y-px">
        {sortedBids.map((b, i) => (
          <div key={i} className="relative flex justify-between px-1 rounded-sm">
            <div className="absolute inset-y-0 right-0 bg-green-500/10 rounded-sm" style={{ width: `${(b.s / maxSize) * 100}%` }} />
            <span className="relative text-green-400">{(b.p * 100).toFixed(1)}</span>
            <span className="relative text-muted-foreground">${Math.round(b.s * b.p)}</span>
          </div>
        ))}
      </div>
      {/* Asks: lowest (best) first */}
      <div className="flex-1 space-y-px">
        {sortedAsks.map((a, i) => (
          <div key={i} className="relative flex justify-between px-1 rounded-sm">
            <div className="absolute inset-y-0 left-0 bg-red-500/10 rounded-sm" style={{ width: `${(a.s / maxSize) * 100}%` }} />
            <span className="relative text-red-400">{(a.p * 100).toFixed(1)}</span>
            <span className="relative text-muted-foreground">${Math.round(a.s * a.p)}</span>
          </div>
        ))}
      </div>
    </div>
  )
}

// ── Match Card ──────────────────────────────────────────────────────

export function MatchCard({ match, position }: { match: MatchData; position?: PositionData }) {
  const active = match.games.find(g => g.status === 'running')
    || [...match.games].reverse().find(g => g.status === 'finished')
    || match.games[0]

  const t1 = active?.teams?.[0]
  const t2 = active?.teams?.[1]
  const [clock, setClock] = useState('--:--')
  const [hoveredTs, setHoveredTs] = useState<number | null>(null)

  useEffect(() => {
    if (!active?.timer || active.timer.paused) {
      setClock(fmtGameClock(active?.timer)); return
    }
    const iv = setInterval(() => setClock(fmtGameClock(active.timer)), 1000)
    setClock(fmtGameClock(active.timer))
    return () => clearInterval(iv)
  }, [active?.timer])

  const isFinished = !match.active
  const opacity = isFinished ? 'opacity-50' : ''

  return (
    <Card className={`overflow-hidden ${opacity}`}>
      <CardContent className="p-0">
        {/* Header: match name, status, price */}
        <div className="flex items-center justify-between px-3 py-1.5 bg-card">
          <div className="flex items-center gap-2 min-w-0">
            <span className="text-xs font-bold truncate">{match.name}</span>
            {match.league && <span className="text-[8px] text-muted-foreground/40">{match.league}</span>}
            {isFinished && <Badge variant="secondary" className="text-[7px] h-3 px-1">DONE</Badge>}
            {match.llf_connected && <div className="w-1.5 h-1.5 rounded-full bg-green-500 shrink-0" title="LLF connected" />}
            {active && !isFinished && (
              <>
                {active.status === 'running' && <Badge variant="destructive" className="text-[7px] h-3 px-1">LIVE</Badge>}
                <span className="text-[10px] text-muted-foreground">G{active.position}</span>
                <span className="text-sm font-mono font-bold tabular-nums">{clock}</span>
              </>
            )}
          </div>
          <div className="flex items-center gap-2 shrink-0 text-[10px]">
            {match.event_count > 0 && <span className="text-muted-foreground/40">{match.event_count} evts</span>}
            {match.has_book && (
              <>
                <span className="font-mono font-bold text-sm tabular-nums">{fmtCents(match.mid)}</span>
                <span className="text-muted-foreground text-[9px]">sprd {fmtCents(match.spread)}</span>
              </>
            )}
          </div>
        </div>

        {/* Position bar */}
        {position && !position.closed && (
          <div className={`px-3 py-0.5 text-[9px] font-mono ${position.unrealized_pnl >= 0 ? 'bg-green-500/10 text-green-400' : 'bg-red-500/10 text-red-400'}`}>
            {position.direction.toUpperCase()} {position.size.toFixed(1)} @ {(position.entry_price * 100).toFixed(1)}¢
            → {position.unrealized_pnl >= 0 ? '+' : ''}{(position.unrealized_pnl * 100).toFixed(1)}¢
            ({position.age_sec.toFixed(0)}s)
          </div>
        )}

        {/* Score + Draft */}
        {t1 && t2 && (
          <GameScoreAndDraft t1={t1} t2={t2} nameA={match.team_a} nameB={match.team_b}
            draft={active?.draft} mid={match.mid} spread={match.spread} />
        )}

        {/* Chart with events */}
        {match.price_history.length > 0 && (
          <div className="h-40 border-t border-border">
            <MiniChart
              priceHistory={match.price_history}
              events={match.match_events || []}
              teamA={match.team_a}
              teamB={match.team_b}
              sideA={t1?.side || undefined}
              sideB={t2?.side || undefined}
              hoveredTs={hoveredTs}
            />
          </div>
        )}

        {/* Event feed — expandable with full details */}
        <EventFeed events={match.match_events || []} onHover={setHoveredTs}
          teamA={match.team_a} teamB={match.team_b} sideA={t1?.side || undefined} sideB={t2?.side || undefined} />

        {/* Orderbook */}
        {match.has_book && <MiniBook bids={match.book_bids} asks={match.book_asks} />}
      </CardContent>
    </Card>
  )
}

// ── Match Grid ──────────────────────────────────────────────────────

export function MatchGrid({ data }: { data?: import('@/lib/types').TraderState }) {
  const [showAll, setShowAll] = useState(false)

  if (!data) {
    return <div className="flex items-center justify-center py-12 text-muted-foreground text-sm">Connecting to Oracle-LoL...</div>
  }

  const matches = Object.values(data.matches)
  const live = matches.filter(m => m.active && m.has_market && (m.has_book || m.llf_connected || m.event_count > 0))
    .sort((a, b) => b.event_count - a.event_count)
  const upcoming = matches.filter(m => m.active && m.has_market && !m.has_book && !m.llf_connected && m.event_count === 0)
  const noMarket = matches.filter(m => m.active && !m.has_market)
  const finished = matches.filter(m => !m.active).sort((a, b) => b.finished_at - a.finished_at)

  return (
    <div className="px-4">
      {live.length > 0 && (
        <>
          <div className="text-[10px] text-muted-foreground uppercase tracking-wider mb-2">
            Live ({live.length})
          </div>
          <div className="grid grid-cols-1 xl:grid-cols-2 2xl:grid-cols-3 gap-3 mb-4">
            {live.map(m => {
              const pos = data.positions.find(p => p.match_id === m.match_id && !p.closed)
              return <MatchCard key={m.match_id} match={m} position={pos} />
            })}
          </div>
        </>
      )}

      {upcoming.length > 0 && (
        <>
          <button onClick={() => setShowAll(!showAll)}
            className="text-[10px] text-muted-foreground uppercase tracking-wider mb-2 hover:text-foreground flex items-center gap-1">
            <span>{showAll ? '▼' : '▸'}</span>
            Upcoming ({upcoming.length}) · No Market ({noMarket.length}) · Finished ({finished.length})
          </button>
          {showAll && (
            <div className="grid grid-cols-1 xl:grid-cols-2 2xl:grid-cols-3 gap-3 mb-4">
              {upcoming.map(m => <MatchCard key={m.match_id} match={m} />)}
              {finished.map(m => <MatchCard key={m.match_id} match={m} />)}
            </div>
          )}
        </>
      )}

      {live.length === 0 && upcoming.length === 0 && (
        <div className="text-center py-12 text-muted-foreground text-sm">No live matches with Polymarket markets found</div>
      )}
    </div>
  )
}
