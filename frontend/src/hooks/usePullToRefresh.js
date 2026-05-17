import { useEffect, useRef, useState } from 'react'

// pull-to-refresh: messages-container (= column-reverse、 scrollTop=0 が最新) の上端から
// 下方向にスワイプしたら onRefresh() を発火する。
//
// 仕様:
//   - scrollTop === 0 (= 最新表示) の状態で touchstart → 下方向に PULL_THRESHOLD_PX 以上
//     ドラッグ → 指を離した瞬間に onRefresh()
//   - 引っ張ってる間は pullDistance を返す (= UI 側でローダー位置 / 進捗表示用)
//   - 閾値未満で離したら何もしない
//   - touchmove はネイティブ scroll を妨げない (= preventDefault しない、 OS の bounce と共存)
//   - 既に refresh 実行中は重複発火しない (= isRefreshing で gate)
//
// 戻り値: { pullDistance, isRefreshing, setIsRefreshing }
//   pullDistance: 引っ張られてる px 量 (= 0..PULL_MAX_PX、 視覚反映に使う)
//   isRefreshing: onRefresh 走行中フラグ (= スピナー表示用)、 onRefresh の Promise 完了で false に
//   setIsRefreshing: 呼出側で手動 reset したい場合の setter

const PULL_THRESHOLD_PX = 70   // この距離以上引っ張ったら refresh trigger
const PULL_MAX_PX = 120        // pullDistance のクランプ上限 (= UI が伸びすぎないように)
const PULL_RESISTANCE = 0.5    // 指の移動量 × この比率で pullDistance に反映 (= ゴム感)

export function usePullToRefresh(scrollerRef, onRefresh) {
  const [pullDistance, setPullDistance] = useState(0)
  const [isRefreshing, setIsRefreshing] = useState(false)
  // refs: state 更新を介さずに gesture を追跡 (= touchmove の高頻度で setState 連発しない、
  // pullDistance のみ最終 setState で反映)。
  const startYRef = useRef(null)
  const activeRef = useRef(false)
  const isRefreshingRef = useRef(false)

  useEffect(() => { isRefreshingRef.current = isRefreshing }, [isRefreshing])

  useEffect(() => {
    const el = scrollerRef.current
    if (!el) return

    const onTouchStart = (e) => {
      // 既に refresh 中、 or 最新表示位置じゃない (= 上スクロール中) なら無視
      if (isRefreshingRef.current || el.scrollTop !== 0) return
      const t = e.touches && e.touches[0]
      if (!t) return
      startYRef.current = t.clientY
      activeRef.current = true
    }

    const onTouchMove = (e) => {
      if (!activeRef.current || startYRef.current == null) return
      const t = e.touches && e.touches[0]
      if (!t) return
      const dy = t.clientY - startYRef.current
      // 下方向 (= dy > 0) のみ反応、 上方向は通常スクロール扱い
      if (dy <= 0) {
        if (pullDistance !== 0) setPullDistance(0)
        return
      }
      // ゴム感のため抵抗をかけて、 PULL_MAX_PX にクランプ
      const pulled = Math.min(dy * PULL_RESISTANCE, PULL_MAX_PX)
      setPullDistance(pulled)
    }

    const onTouchEnd = () => {
      if (!activeRef.current) return
      activeRef.current = false
      startYRef.current = null
      // 閾値超えてたら onRefresh、 さもなくば pullDistance リセットだけ
      if (pullDistance >= PULL_THRESHOLD_PX && !isRefreshingRef.current && onRefresh) {
        setIsRefreshing(true)
        Promise.resolve(onRefresh())
          .catch(() => { /* ignore */ })
          .finally(() => {
            setIsRefreshing(false)
            setPullDistance(0)
          })
      } else {
        setPullDistance(0)
      }
    }

    const onTouchCancel = () => {
      activeRef.current = false
      startYRef.current = null
      setPullDistance(0)
    }

    el.addEventListener('touchstart', onTouchStart, { passive: true })
    el.addEventListener('touchmove', onTouchMove, { passive: true })
    el.addEventListener('touchend', onTouchEnd, { passive: true })
    el.addEventListener('touchcancel', onTouchCancel, { passive: true })
    return () => {
      el.removeEventListener('touchstart', onTouchStart)
      el.removeEventListener('touchmove', onTouchMove)
      el.removeEventListener('touchend', onTouchEnd)
      el.removeEventListener('touchcancel', onTouchCancel)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scrollerRef, onRefresh, pullDistance])

  return { pullDistance, isRefreshing, pullThreshold: PULL_THRESHOLD_PX }
}
