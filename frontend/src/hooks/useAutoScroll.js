import { useState, useRef, useEffect, useLayoutEffect, useCallback } from 'react'

// column-reverse 配置で「最新が見えてる」 と判定する scrollTop 上限 (= px)。 viewport 高さに
// 比例しない固定値だが、 数 px の指の振動を許容する目的で 30px。 ユーザがメッセージを
// 戻し読みする時は最低でも 1 段スクロール (= 数十 px) するので識別可能。
const AT_BOTTOM_TOP_THRESHOLD_PX = 30
// onScroll の showScrollBtn 更新の throttle 間隔。 60fps で十分、 過剰更新を抑える。
const SCROLL_THROTTLE_MS = 150
// scrollToBottom 実行後、 自前 scroll を「ユーザ操作」 と誤検知させないための猶予時間。
// この間の onScroll は無視する。 短いと render 遅延中の onScroll を拾い、 長いと
// ユーザ反応に対する反映が遅れる。
const PROGRAMMATIC_SCROLL_GUARD_MS = 200

// 起動時に最新メッセージが見える」 を最速化するため、 .messages を
// flex-direction: column-reverse にして「scrollTop=0 = 最新表示の状態」 にする。
// この hook はその前提で書かれている:
//   - isAtBottom = (scrollTop ≈ 0)、 = 「最新が見えてる」 の判定
//   - scrollToBottom = scrollTop = 0
//   - 上スクロール (scrollTop > 0) = 古いメッセージ閲覧
//   - 新着メッセージ自動追従は column-reverse が flex で底辺に push してくれる、
//     scrollTop=0 のままなら何もしなくても見える
//   - scrollTop > 0 (= 古いメッセージ閲覧中) に新着が来たら hasNew=true で赤丸表示
//
// 起動 / タブ切替時は useLayoutEffect で paint 前に scrollTop=0 を強制 (= 前 session
// の scroll 位置が残るのを防ぐ)。
export function useAutoScroll({ messages, activeSession }) {
  const [showScrollBtn, setShowScrollBtn] = useState(false)
  const [hasNew, setHasNew] = useState(false)
  const isAtBottomRef = useRef(true)
  const scrollerDomRef = useRef(null)
  const scrollThrottleRef = useRef(0)
  const msgLengthRef = useRef({})
  const programmaticScrollRef = useRef(false)
  const scrollEndTimerRef = useRef(null)
  const sid = activeSession?.id

  // 同期: scrollTop=0 (= column-reverse の底辺 = 最新が見える状態)
  const scrollToBottomSync = useCallback(() => {
    const el = scrollerDomRef.current
    if (!el) return
    isAtBottomRef.current = true
    el.scrollTop = 0
  }, [])

  // 公開: 「↓ 最新へ」 ボタン or send 直後に呼ぶ用
  const scrollToBottom = useCallback(() => {
    const el = scrollerDomRef.current
    if (!el) return
    programmaticScrollRef.current = true
    isAtBottomRef.current = true
    setHasNew(false)
    el.scrollTop = 0
    clearTimeout(scrollEndTimerRef.current)
    scrollEndTimerRef.current = setTimeout(() => {
      programmaticScrollRef.current = false
    }, PROGRAMMATIC_SCROLL_GUARD_MS)
  }, [])

  // 起動 / タブ切替: paint 前に scrollTop=0 を強制 (= 前 session の scroll 残留防止)
  useLayoutEffect(() => {
    if (!sid) return
    isAtBottomRef.current = true
    setShowScrollBtn(false)
    setHasNew(false)
    msgLengthRef.current[sid] = (messages[sid] || []).length
    scrollToBottomSync()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sid])

  // 新着メッセージ:
  //   isAtBottom (scrollTop≈0) なら column-reverse の flex が自動で底辺に push、 何もしなくてOK。
  //   scrollTop>0 (= 古いメッセージ閲覧中) なら hasNew=true で赤丸表示。
  useEffect(() => {
    if (!sid) return
    const cur = messages[sid] || []
    const currentLen = cur.length
    const prevLen = msgLengthRef.current[sid] || 0
    msgLengthRef.current[sid] = currentLen

    if (currentLen > prevLen && !isAtBottomRef.current) {
      setHasNew(true)
    }
  }, [messages, sid])

  // 画面回転 / キーボード表示等のレイアウト変化時は最新位置に戻す (isAtBottom 中のみ)
  useEffect(() => {
    const onResize = () => {
      if (isAtBottomRef.current) scrollToBottomSync()
    }
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [scrollToBottomSync])

  const onScroll = useCallback(() => {
    if (programmaticScrollRef.current) return
    const el = scrollerDomRef.current
    if (!el) return
    // column-reverse: scrollTop が 0 に近い = 最新が見えてる
    const atBottom = el.scrollTop <= AT_BOTTOM_TOP_THRESHOLD_PX
    isAtBottomRef.current = atBottom
    if (atBottom) setHasNew(false)
    const now = Date.now()
    if (now - scrollThrottleRef.current >= SCROLL_THROTTLE_MS) {
      scrollThrottleRef.current = now
      setShowScrollBtn(!atBottom)
    }
  }, [])

  return {
    scrollerDomRef,
    isAtBottomRef,
    showScrollBtn,
    hasNew,
    scrollToBottom,
    onScroll,
  }
}
