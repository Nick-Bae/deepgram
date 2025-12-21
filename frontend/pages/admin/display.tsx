'use client'

import { useCallback, useEffect, useState } from 'react'
import type { CSSProperties } from 'react'
import { useTranslationSocket } from '../../utils/useTranslationSocket'

const MIN_SPEED = 0.6
const MAX_SPEED = 1.6
const STEP = 0.1
const LS_KEY = 'display_speed_factor'

const clamp = (value: number) => Math.max(MIN_SPEED, Math.min(MAX_SPEED, value))

export default function AdminDisplaySpeed() {
  const { connected, sendDisplayConfig } = useTranslationSocket({ isProducer: true })
  const [speed, setSpeed] = useState(1)

  useEffect(() => {
    if (typeof window === 'undefined') return
    const raw = window.localStorage.getItem(LS_KEY)
    const parsed = raw ? Number(raw) : 1
    if (Number.isFinite(parsed)) {
      setSpeed(clamp(parsed))
    }
  }, [])

  useEffect(() => {
    if (!connected) return
    sendDisplayConfig(speed)
  }, [connected, sendDisplayConfig, speed])

  const applySpeed = useCallback((next: number) => {
    const clamped = clamp(Number(next.toFixed(2)))
    setSpeed(clamped)
    if (typeof window !== 'undefined') {
      window.localStorage.setItem(LS_KEY, String(clamped))
    }
    sendDisplayConfig(clamped)
  }, [sendDisplayConfig])

  return (
    <div style={styles.page}>
      <h1 style={styles.title}>Display Speed</h1>
      <p style={styles.subtitle}>
        Control how long each translated line lingers on the public display.
      </p>
      <div style={styles.card}>
        <div style={styles.row}>
          <span style={styles.label}>Connection</span>
          <span style={{ ...styles.value, color: connected ? '#0f766e' : '#b45309' }}>
            {connected ? 'Connected' : 'Disconnected'}
          </span>
        </div>
        <div style={styles.row}>
          <span style={styles.label}>Current speed</span>
          <span style={styles.value}>{speed.toFixed(2)}x</span>
        </div>
        <div style={styles.controls}>
          <button style={styles.button} onClick={() => applySpeed(speed - STEP)}>
            Slower
          </button>
          <button style={styles.buttonPrimary} onClick={() => applySpeed(1)}>
            Reset 1.00x
          </button>
          <button style={styles.button} onClick={() => applySpeed(speed + STEP)}>
            Faster
          </button>
        </div>
        <p style={styles.note}>
          Lower = faster turnover (less lag), higher = more readable. Range {MIN_SPEED.toFixed(1)}–{MAX_SPEED.toFixed(1)}x.
        </p>
      </div>
    </div>
  )
}

const styles: Record<string, CSSProperties> = {
  page: {
    padding: '32px',
    fontFamily: "Inter, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
    background: '#f5f6f8',
    minHeight: '100vh',
  },
  title: {
    fontSize: '26px',
    fontWeight: 700,
    marginBottom: '8px',
  },
  subtitle: {
    color: '#4b5563',
    marginBottom: '20px',
  },
  card: {
    background: 'white',
    borderRadius: 14,
    padding: '20px 22px',
    boxShadow: '0 10px 30px rgba(15,23,42,0.08)',
    maxWidth: 520,
  },
  row: {
    display: 'flex',
    justifyContent: 'space-between',
    alignItems: 'center',
    marginBottom: '12px',
  },
  label: {
    color: '#6b7280',
    fontSize: '14px',
    fontWeight: 600,
    textTransform: 'uppercase',
    letterSpacing: '0.08em',
  },
  value: {
    fontSize: '18px',
    fontWeight: 700,
    color: '#111827',
  },
  controls: {
    display: 'flex',
    gap: '10px',
    marginTop: '14px',
  },
  button: {
    border: '1px solid #d1d5db',
    borderRadius: 10,
    padding: '10px 14px',
    background: 'white',
    fontWeight: 600,
    cursor: 'pointer',
  },
  buttonPrimary: {
    border: '1px solid #0f766e',
    borderRadius: 10,
    padding: '10px 16px',
    background: '#0f766e',
    color: 'white',
    fontWeight: 700,
    cursor: 'pointer',
  },
  note: {
    marginTop: '14px',
    fontSize: '13px',
    color: '#6b7280',
  },
}
