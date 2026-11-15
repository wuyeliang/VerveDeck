import { Modal, message } from 'antd'
import { useTranslation } from 'react-i18next'
import { api } from './api'

/**
 * 批量删除（多选删除）：确认弹窗 → 调后端 batch-delete → 部分结果提示。
 * 用法：
 *   const [sel, setSel] = useState<number[]>([])
 *   const batchDel = useBatchDelete('/api/datasets/batch-delete', () => { setSel([]); load() })
 *   <Table rowSelection={{ selectedRowKeys: sel, onChange: (k: any) => setSel(k) }} ... />
 *   {sel.length > 0 && <Button danger onClick={() => batchDel(sel)}>{t('common.batchDelete')}（{sel.length}）</Button>}
 *
 * 第三个参数 buildBody 可自定义请求体（默认 { ids }），如存储对象：
 *   useBatchDelete('/api/storage/objects/batch-delete', load, (ids) => ({ names: ids, backend: backendId }))
 */
export function useBatchDelete(url: string, load: () => void, buildBody?: (ids: (number | string)[]) => any) {
  const { t } = useTranslation()
  return (ids: (number | string)[]) => {
    if (!ids.length) return
    const body = buildBody ? buildBody(ids) : { ids }
    Modal.confirm({
      title: `${t('common.batchDelete')}（${ids.length}）`,
      content: t('common.batchDelConfirm'),
      okText: t('common.delete'),
      cancelText: t('common.cancel'),
      okButtonProps: { danger: true },
      onOk: async () => {
        try {
          const r = await api.post(url, body)
          const failed: any[] = r.data.failed || []
          const n = (r.data.deleted ?? []).length
          if (failed.length) {
            const first = failed[0]
            const label = first.id != null ? '#' + first.id + ' ' : (first.name ? String(first.name) + ' ' : '')
            message.warning(`${t('common.batchDone')}: ${n}/${ids.length}（${label}${first.error}）`)
          } else {
            message.success(`${t('common.batchDone')}（${n}）`)
          }
          load()
        } catch (e: any) {
          message.error(e?.response?.data?.detail || e?.message || t('common.batchDelConfirm'))
          load()
        }
      },
    })
  }
}
