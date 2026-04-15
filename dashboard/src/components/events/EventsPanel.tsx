import { useState } from 'react'
import { Card, CardContent } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { ScrollArea } from '@/components/ui/scroll-area'
import type { TraderState, EventData } from '@/lib/types'

const ACTION_COLORS: Record<string, string> = {
  TRADE: 'bg-green-500/20 text-green-400',
  SPREAD_WIDE: 'bg-yellow-500/20 text-yellow-400',
  LOW_EDGE: 'bg-orange-500/20 text-orange-400',
  PRICED_IN: 'bg-purple-500/20 text-purple-400',
  TOWER_SKIP: 'bg-muted text-muted-foreground',
  PRICE_BAND: 'bg-muted text-muted-foreground',
  NEAR_RESOLVED: 'bg-muted text-muted-foreground',
}

const TYPE_COLORS: Record<string, string> = {
  kill: 'text-red-400',
  tower: 'text-blue-400',
  drake: 'text-purple-400',
  baron: 'text-yellow-400',
  inhibitor: 'text-red-400',
}

function getActionColor(action: string): string {
  if (action.startsWith('SPREAD')) return ACTION_COLORS.SPREAD_WIDE
  if (action.startsWith('LOW_EDGE')) return ACTION_COLORS.LOW_EDGE
  if (action.startsWith('PRICED_IN')) return ACTION_COLORS.PRICED_IN
  if (action.startsWith('PRICE_BAND')) return ACTION_COLORS.PRICE_BAND
  return ACTION_COLORS[action] || 'bg-muted text-muted-foreground'
}

export function EventsPanel({ data }: { data?: TraderState }) {
  const [filter, setFilter] = useState('ALL')
  const events = [...(data?.events || [])].reverse()

  const actions = ['ALL', ...new Set(events.map(e => e.action.split('_')[0]))]
  const filtered = filter === 'ALL' ? events : events.filter(e => e.action.startsWith(filter))

  return (
    <Card>
      <CardContent className="p-0">
        <div className="flex items-center justify-between px-4 py-2 border-b border-border">
          <div className="flex items-center gap-2">
            <span className="text-xs font-semibold">Signals & Events</span>
            <Badge variant="outline" className="text-[9px] h-4">{filtered.length}</Badge>
          </div>
          <div className="flex gap-1">
            {actions.slice(0, 6).map(a => (
              <button
                key={a}
                onClick={() => setFilter(a)}
                className={`text-[8px] px-1.5 py-0.5 rounded transition-colors ${
                  filter === a ? 'bg-primary text-primary-foreground' : 'bg-muted text-muted-foreground hover:bg-muted/80'
                }`}
              >
                {a}
              </button>
            ))}
          </div>
        </div>
        <ScrollArea className="max-h-[400px]">
          <div className="font-mono text-[10px] p-2 space-y-px">
            {filtered.length === 0 && (
              <div className="text-center py-6 text-xs text-muted-foreground">No events yet</div>
            )}
            {filtered.map((ev, i) => (
              <div key={i} className="flex items-center gap-2 py-0.5 px-1 rounded-sm hover:bg-muted/30">
                <span className="text-muted-foreground/60 shrink-0 w-14">{ev.time}</span>
                <span className={`font-bold w-10 shrink-0 ${TYPE_COLORS[ev.etype] || 'text-muted-foreground'}`}>
                  {ev.etype.toUpperCase()}
                </span>
                <span className="text-muted-foreground/60 shrink-0 w-16">
                  G{ev.game} [{ev.clock}]
                </span>
                <span className="text-muted-foreground shrink-0 w-20 truncate">{ev.team}</span>
                <Badge variant="outline" className={`text-[7px] h-3 px-1 shrink-0 ${getActionColor(ev.action)}`}>
                  {ev.action.split('_').slice(0, 2).join('_')}
                </Badge>
                <span className="text-muted-foreground/50 shrink-0">{(ev.mid * 100).toFixed(1)}¢</span>
                {ev.signal_reason && (
                  <span className="text-green-400/70 truncate" title={ev.signal_reason}>{ev.signal_reason}</span>
                )}
              </div>
            ))}
          </div>
        </ScrollArea>
      </CardContent>
    </Card>
  )
}
