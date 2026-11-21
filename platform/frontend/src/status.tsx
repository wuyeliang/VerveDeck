import { useTranslation } from 'react-i18next'

/**
 * 全站统一状态文案：把后端状态枚举（queued/running/succeeded/failed/ready/...）
 * 映射为 status.<枚举> 的 i18n 文案（6 语言），未收录的新状态回退显示原值（不空白）。
 * 用法：const st = useStatusLabel() ... <Tag>{st(v)}</Tag>
 */
export function useStatusLabel() {
  const { t } = useTranslation()
  return (v?: string | null) => (v ? t(`status.${v}`, { defaultValue: v }) : '-')
}
