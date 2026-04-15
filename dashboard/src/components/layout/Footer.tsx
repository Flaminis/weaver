import { useEffect, useState } from 'react'

export function Footer({ dataUpdatedAt, isError }: { dataUpdatedAt: number; isError: boolean }) {
  const [age, setAge] = useState(0)

  useEffect(() => {
    const iv = setInterval(() => {
      setAge(dataUpdatedAt > 0 ? (Date.now() - dataUpdatedAt) / 1000 : 999)
    }, 1000)
    return () => clearInterval(iv)
  }, [dataUpdatedAt])

  const status = isError || age > 15 ? 'error' : age > 8 ? 'stale' : 'ok'
  const colors = { ok: 'text-green-400', stale: 'text-yellow-400', error: 'text-red-400' }
  const labels = { ok: 'Connected', stale: 'Stale', error: 'Disconnected' }

  return (
    <footer className="flex items-center justify-between px-4 py-1.5 border-t border-border text-[10px] text-muted-foreground">
      <span>Oracle-LoL • LLF limit: 3 simultaneous</span>
      <span className={colors[status]}>
        {labels[status]} • {age < 999 ? `${age.toFixed(0)}s ago` : '—'}
      </span>
    </footer>
  )
}
