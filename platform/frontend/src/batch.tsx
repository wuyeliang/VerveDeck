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
 */
export function useBatchDelete(url: string, load: () => void) {
  const { t } = useTranslation()
  return (ids: number[]) => {
    if (!ids.length) return
    Modal.confirm({
      title: `${t('common.batchDelete')}（${ids.length}）`,
      content: t('common.batchDelConfirm'),
      okText: t('common.delete'),
      cancelText: t('common.cancel'),
      okButtonProps: { danger: true },
      onOk: async () => {
        try {
          const r = await api.post(url, { ids })
          const failed: any[] = r.data.failed || []
          const n = (r.data.deleted ?? []).length
          if (failed.length) {
            message.warning(`${t('common.batchDone')}: ${n}/${ids.length}（${failed[0].id ? '#' + failed[0].id + ' ' : ''}${failed[0].error}）`)
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
