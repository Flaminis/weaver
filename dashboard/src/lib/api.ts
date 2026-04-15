import { useQuery } from '@tanstack/react-query'
import type { TraderState } from './types'

const API_BASE = import.meta.env.VITE_API_URL || (typeof window !== 'undefined' ? window.location.origin : 'http://localhost:8422')

async function fetchState(): Promise<TraderState> {
  const res = await fetch(`${API_BASE}/api/state`)
  if (!res.ok) throw new Error(`${res.status}`)
  return res.json()
}

export function useTraderState() {
  return useQuery<TraderState>({
    queryKey: ['trader-state'],
    queryFn: fetchState,
    refetchInterval: 2000,
    staleTime: 1500,
    retry: 2,
  })
}
