export interface BookLevel {
  p: number
  s: number
}

export interface MatchData {
  match_id: number
  name: string
  team_a: string
  team_b: string
  team_a_id: number
  team_b_id: number
  has_market: boolean
  market_question: string
  token_a: string
  token_b: string
  games: GameData[]
  series_score: Record<string, number>
  mid: number
  bid: number
  ask: number
  spread: number
  has_book: boolean
  book_bids: BookLevel[]
  book_asks: BookLevel[]
  price_history: [number, number][]
  active: boolean
  finished_at: number
  league: string
  status: string
  event_count: number
  total_markets: number
  match_events: EventData[]
  llf_connected: boolean
  llf_status: string
  llf_last_msg_age: number
  llf_last_msg_type: string
  llf_msg_count: number
  /** Unix seconds when LLF last applied games[] (kills, objs, clock). Dashboard derives live age. */
  llf_scoreboard_updated_at?: number
  gamma: {
    title: string
    live: boolean
    score: string
    period: string
    volume: number
    liquidity: number
    open_interest: number
    start_time: string
    end_date: string
    description: string
    resolution_source: string
    competitive: number
    league: string
    league_tier: string
    context: string
    teams: { name: string; image?: string; slug?: string }[]
    icon: string
  }
}

export interface GameData {
  id: number
  position: number
  status: string
  timer?: { timer: number; paused: boolean; issued_at: string }
  teams: GameTeam[]
  draft?: { picks: DraftPick[] }
}

export interface GameTeam {
  id: number
  side: string | null
  kills: number
  towers: number
  drakes: number
  nashors: number
  inhibitors: number
}

export interface DraftPick {
  role: string
  team_id: number
  champion_slug: string
}

export interface PositionData {
  match_id: number
  match_name: string
  direction: string
  entry_price: number
  size: number
  cost_usd: number
  current_price: number
  unrealized_pnl: number
  age_sec: number
  signal_reason: string
  closed: boolean
  exit_pnl: number
  sell_order_id: string
  exit_story?: string | null
}

export interface TradeData {
  ts: number
  match: string
  direction: string
  entry: number
  exit: number
  size: number
  pnl: number
  hold_sec: number
  reason: string
}

export interface EventData {
  ts: number
  time: string
  match: string
  match_id: number
  etype: string
  team: string
  game: number
  clock: string
  desc: string
  action: string
  /** How the entry resolved after a TRADE signal: dry_run vs CLOB vs gated, etc. */
  trade_exec?: string | null
  gate_reason?: string | null
  order_error?: string | null
  /** Multi-line timeline: intent → order → fill/sell/reconcile; human-readable. */
  exec_story?: string | null
  clob_order_id?: string | null
  fill_reported_shares?: number | null
  /** Book reference px used for sizing (best ask for buy_a, etc.) */
  attempt_ref_px?: number | null
  /** FAK limit sent to CLOB (ref + 1¢ tick) */
  attempt_limit_price?: number | null
  signal_dir: string | null
  signal_size: number | null
  signal_reason: string | null
  signal_impact: number | null
  signal_confidence: number | null
  p_fair?: number | null
  edge?: number | null
  pre_event_mid?: number | null
  mid: number
  bid: number
  ask: number
  spread: number
  buy_price_a: number
  buy_price_b: number
  recent_move_2s: number
  holding: string | null
  market_type: string
  book_snapshot: {
    bids: { p: number; s: number }[]
    asks: { p: number; s: number }[]
  }
}

export interface PolyWsHealth {
  connected: boolean
  subscriptions: number
  active_books: number
  last_msg_age: number
}

export interface LlfWsHealth {
  active: number
  max: number
  matches: string[]
}

export interface TraderState {
  ts: number
  dry_run: boolean
  uptime_sec: number
  capital: number
  bankroll: number
  daily_pnl: number
  total_trades: number
  win_rate: number
  exposure: number
  circuit_active: boolean
  circuit_seconds_left: number
  circuit_reason: string
  consecutive_losses: number
  poly_ws: PolyWsHealth
  llf_ws: LlfWsHealth
  matches: Record<string, MatchData>
  positions: PositionData[]
  trades: TradeData[]
  events: EventData[]
}
