// 告警规则 / 告警事件两页共享：指标与事件词表、级别标签、规则分页大小
import React from 'react'
import { Tag } from 'antd'

/** 阈值型指标（Prometheus 评估；与后端 /api/admin/alerts/rules 的 metric 词表一致） */
export const METRICS = ['host_cpu', 'host_mem', 'host_disk', 'gpu_util', 'gpu_temp', 'gpu_mem',
  'gpu_mem_temp', 'gpu_power', 'gpu_xid', 'gpu_ecc_dbe', 'kv_cache', 'infer_waiting',
  'infer_latency_p95', 'infer_down', 'system_up']

/** 事件型告警码（平台故障收敛点 trigger_event） */
export const EVENTS = ['node_offline', 'node_online', 'train_failed', 'eval_failed', 'quant_failed', 'infer_down']

/** 规则表每页条数（服务端分页，支撑 2000+ 规则） */
export const RULE_PAGE = 50

export const mLabel = (t: any, m: string) => ({
  host_cpu: t('admin.mHostCpu'), host_mem: t('admin.mHostMem'), host_disk: t('admin.mHostDisk'),
  gpu_util: t('admin.mGpuUtil'), gpu_temp: t('admin.mGpuTemp'), gpu_mem: t('admin.mGpuMem'),
  gpu_mem_temp: t('admin.mGpuMemTemp'), gpu_power: t('admin.mGpuPower'),
  gpu_xid: t('admin.mGpuXid'), gpu_ecc_dbe: t('admin.mGpuEccDbe'),
  kv_cache: t('admin.mKvCache'), infer_waiting: t('admin.mInferWaiting'),
  infer_latency_p95: t('admin.mInferLatency'), infer_down: t('admin.mInferDown'),
  system_up: t('admin.mSystemUp'),
}[m] || m)

export const eLabel = (t: any, e: string) => ({
  node_offline: t('admin.evNodeOffline'), node_online: t('admin.evNodeOnline'),
  train_failed: t('admin.evTrainFailed'), eval_failed: t('admin.evEvalFailed'),
  quant_failed: t('admin.evQuantFailed'), infer_down: t('admin.evInferDown'),
}[e] || e)

export const sevTag = (t: any, v: string) =>
  v === 'critical' ? <Tag color="error">{t('admin.sevCritical')}</Tag> : <Tag color="warning">{t('admin.sevWarning')}</Tag>
