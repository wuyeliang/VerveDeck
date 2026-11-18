import React from 'react'
import { Tooltip } from 'antd'

/**
 * 推理模块共享 UI：状态圆点 + 色板 + 分区标题。
 * 状态一律「彩色圆点 + 文案」（不用胶囊 Tag），原因悬停查看；
 * 色板：运行=绿 · 创建/同步中=琥珀 · 失败=红 · 停止/关闭=灰。
 */
export type StatusTone = 'success' | 'warning' | 'error' | 'default' | 'processing'

export const STATUS_TONE: Record<string, StatusTone> = {
  running: 'success',
  // 尚未提供服务（创建中/待同步）= 琥珀，不用蓝：绿=在服、琥珀=未就绪、红=失败、灰=停止
  creating: 'warning',
  pending: 'warning',
  stopped: 'default',
  failed: 'error',
  error: 'error',
  synced: 'success',
  dead: 'error',
  alive: 'success',
}

const TONE_COLOR: Record<StatusTone, string> = {
  success: 'var(--ant-color-success)',
  warning: 'var(--ant-color-warning)',
  error: 'var(--ant-color-error)',
  default: 'var(--ant-color-text-quaternary)',
  processing: 'var(--ant-color-primary)',
}

export function StatusDot({ status, tone, tip }: { status?: string; tone?: StatusTone; tip?: string }) {
  const t = tone || STATUS_TONE[status || ''] || 'default'
  return (
    <Tooltip title={tip || undefined} mouseEnterDelay={0.3}>
      <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6, whiteSpace: 'nowrap', cursor: tip ? 'default' : undefined }}>
        <span style={{
          width: 8, height: 8, borderRadius: '50%', flex: 'none',
          background: TONE_COLOR[t],
          ...(t === 'processing' ? { boxShadow: `0 0 0 3px ${TONE_COLOR.processing}22` } : {}),
        }} />
        <span style={{ color: t === 'default' ? 'var(--ant-color-text-secondary)' : 'var(--ant-color-text)' }}>{status || '-'}</span>
      </span>
    </Tooltip>
  )
}

/** 表单分区标题：小号加粗 + 上间距（无装饰条） */
export function SectionTitle({ children }: { children: React.ReactNode }) {
  return (
    <div style={{
      fontSize: 13, fontWeight: 600, color: 'var(--ant-color-text)',
      margin: '2px 0 12px', padding: 0,
    }}>{children}</div>
  )
}
