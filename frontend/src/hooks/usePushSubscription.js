/**
 * Web Push の購読状態 + 有効/無効トグル。
 *
 * pushAvailable は環境で固定 (= iOS は 16.4+ かつ standalone 必須等の制約)。
 * pushEnabled は localStorage に永続化された希望状態 (= 実体は SW の subscription)。
 * トグル中 (pushBusy) は連打防止。
 */
import { useState } from 'react'
import {
  enablePush,
  disablePush,
  isPushSupported,
  isStandalone,
  isPushEnabledLocally,
} from '../utils/push.js'

export function usePushSubscription({ onCloseMenu } = {}) {
  const [pushEnabled, setPushEnabled] = useState(() => isPushEnabledLocally())
  const [pushBusy, setPushBusy] = useState(false)
  const pushAvailable = isPushSupported() && isStandalone()

  const handleTogglePush = async () => {
    if (pushBusy) return
    setPushBusy(true)
    onCloseMenu?.()
    try {
      if (pushEnabled) {
        await disablePush()
        setPushEnabled(false)
      } else {
        await enablePush()
        setPushEnabled(true)
      }
    } catch (e) {
      alert(e?.message || '通知設定の変更に失敗しました')
    } finally {
      setPushBusy(false)
    }
  }

  return { pushEnabled, pushBusy, pushAvailable, handleTogglePush }
}
