import { useState, useRef, useEffect, useCallback } from 'react'

export function useAutoScroll({ messages, activeAgent }) {
  const [showScrollBtn, setShowScrollBtn] = useState(false)
  const [hasNew, setHasNew] = useState(false)
  const isAtBottomRef = useRef(true)
  const scrollerDomRef = useRef(null)
  const scrollThrottleRef = useRef(0)
  const msgLengthRef = useRef({ agent_a: 0, agent_b: 0 })
  const programmaticScrollRef = useRef(false)
  const scrollEndTimerRef = useRef(null)

  // スクロールは CSS `scroll-behavior: smooth` に委ねる。scrollTo() 経由で呼べば自動的にぬるっと動く
  // onScrollはsmoothアニメ中も発火するので、programmaticScrollRefで一定時間ガードしてユーザー操作誤認を防ぐ
  const scrollToBottom = useCallback(() => {
    const el = scrollerDomRef.current
    if (!el) return
    programmaticScrollRef.current = true
    isAtBottomRef.current = true
    setHasNew(false)
    el.scrollTo({ top: el.scrollHeight })
    clearTimeout(scrollEndTimerRef.current)
    scrollEndTimerRef.current = setTimeout(() => {
      programmaticScrollRef.current = false
    }, 600)
  }, [])

  // 新着メッセージ時の自動スクロール（タブ切り替えは別のuseEffect）
  useEffect(() => {
    const currentLen = messages[activeAgent].length
    const prevLen = msgLengthRef.current[activeAgent]
    msgLengthRef.current[activeAgent] = currentLen

    if (currentLen > prevLen) {
      // 新規アイテム追加: 最下部にいれば追従、そうでなければ未読通知
      if (isAtBottomRef.current) {
        requestAnimationFrame(() => { requestAnimationFrame(() => { scrollToBottom() }) })
      } else {
        setHasNew(true)
      }
    } else if (isAtBottomRef.current) {
      // ストリーミング中の内容更新（アイテム数変化なし）: CSS smoothで追従
      scrollToBottom()
    }
  }, [messages, activeAgent, scrollToBottom])

  // タブ切り替え時は常に最下部へ
  useEffect(() => {
    isAtBottomRef.current = true
    setShowScrollBtn(false)
    setHasNew(false)
    msgLengthRef.current[activeAgent] = messages[activeAgent].length

    const el = scrollerDomRef.current
    if (!el) return
    // 画像onload・markdownハイライト等で後から高さが増えるケースに備え、
    // 500msの窓で scrollHeight 変化を追う（最下部にいる間だけ追従）
    let cancelled = false
    let lastHeight = -1
    const deadline = Date.now() + 500
    const tick = () => {
      if (cancelled) return
      const cur = el.scrollHeight
      if (cur !== lastHeight && isAtBottomRef.current) {
        lastHeight = cur
        scrollToBottom()
      }
      if (Date.now() < deadline) requestAnimationFrame(tick)
    }
    requestAnimationFrame(tick)
    return () => { cancelled = true }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeAgent])

  // 画面回転時：最下部にいた場合は追従
  useEffect(() => {
    const onResize = () => {
      if (isAtBottomRef.current) scrollToBottom()
    }
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [scrollToBottom])

  const onScroll = useCallback(() => {
    if (programmaticScrollRef.current) return
    const el = scrollerDomRef.current
    if (!el) return
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 30
    isAtBottomRef.current = atBottom
    if (atBottom) setHasNew(false)
    // setShowScrollBtnはre-renderを誘発するためthrottle（150ms）
    const now = Date.now()
    if (now - scrollThrottleRef.current >= 150) {
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
