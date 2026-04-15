import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Header } from '@/components/layout/Header'
import { StatsRow } from '@/components/layout/StatsRow'
import { Footer } from '@/components/layout/Footer'
import { MatchGrid } from '@/components/match/MatchCard'
import { PositionsTable } from '@/components/positions/PositionsTable'
import { EventsPanel } from '@/components/events/EventsPanel'
import { useTraderState } from '@/lib/api'

const queryClient = new QueryClient()

function Dashboard() {
  const { data, dataUpdatedAt, isError } = useTraderState()

  const openPos = data?.positions.filter(p => !p.closed).length || 0
  const eventCount = data?.events.length || 0
  const tradeCount = data?.trades.length || 0

  return (
    <div className="h-screen flex flex-col bg-background text-foreground overflow-hidden">
      <Header data={data} />
      <StatsRow data={data} />

      <div className="flex-1 min-h-0 overflow-y-auto">
        <MatchGrid data={data} />

        <div className="px-4 mt-3 pb-2">
          <Tabs defaultValue="positions">
            <TabsList className="mb-2">
              <TabsTrigger value="positions" className="text-[10px]">
                Positions {openPos > 0 && `(${openPos})`}
              </TabsTrigger>
              <TabsTrigger value="events" className="text-[10px]">
                Events {eventCount > 0 && `(${eventCount})`}
              </TabsTrigger>
              <TabsTrigger value="trades" className="text-[10px]">
                History {tradeCount > 0 && `(${tradeCount})`}
              </TabsTrigger>
            </TabsList>
            <TabsContent value="positions">
              <PositionsTable data={data} />
            </TabsContent>
            <TabsContent value="events">
              <EventsPanel data={data} />
            </TabsContent>
            <TabsContent value="trades">
              <PositionsTable data={data} />
            </TabsContent>
          </Tabs>
        </div>
      </div>

      <Footer dataUpdatedAt={dataUpdatedAt} isError={isError} />
    </div>
  )
}

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <Dashboard />
    </QueryClientProvider>
  )
}
