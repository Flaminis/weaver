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
  match_events: EventData[]
  llf_connected: boolean
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
  signal_dir: string | null
  signal_size: number | null
  signal_reason: string | null
  signal_impact: number | null
  signal_confidence: number | null
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
  consecutive_losses: number
  matches: Record<string, MatchData>
  positions: PositionData[]
  trades: TradeData[]
  events: EventData[]
}
