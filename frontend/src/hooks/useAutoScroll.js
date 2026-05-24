import { useState, useRef, useEffect, useLayoutEffect, useCallback } from 'react'

// 「最新が見えてる」 と判定するボトム余白 (= px)。 数 px の指の振動を許容する目的で 30px。
// ユーザがメッセージを戻し読みする時は最低でも 1 段スクロール (= 数十 px) するので識別可能。
const AT_BOTTOM_THRESHOLD_PX = 30
// scrollToBottom 実行後、 自前 scroll を「ユーザ操作」 と誤検知させないための猶予時間。
// この間の onScroll は無視する。 短いと render 遅延中の onScroll を拾い、 長いと
// ユーザ反応に対する反映が遅れる。
const PROGRAMMATIC_SCROLL_GUARD_MS = 200

// 通常 column (古い→新しい が DOM 上→下) で、 JS で底辺へ scroll する古典構成。
//
// 旧実装は flex-direction: column-reverse のトリックを使っていたが、 iOS Safari WebKit で
// column-reverse + overflow:auto の scrollTop 解釈が壊れていて (= 視覚順序は反転、 数値は
// 通常 column 仕様) 、 「↓ボタンが下端で出る」「details が上に展開」「scroll 末尾追従が
// 異常に強い」 等の連鎖症状を起こしていた (= 2026-05-19 修正、 WebKit #225278 系列の bug
// と整合)。 通常 column に戻すことで全て解消する。
//
//   - isAtBottom = (scrollHeight - scrollTop - clientHeight ≤ 30)、 = 「最新が見えてる」
//   - scrollToBottom = scrollTop を scrollHeight 相当に上げる
//   - 上スクロール (scrollTop が小さくなる) = 古いメッセージ閲覧
//   - 新着メッセージ追従は isAtBottom 中のみ JS で再 scroll、 そうでなければ hasNew=true
//
// 起動 / タブ切替時は useLayoutEffect で paint 前に底へ flush (= 前 session の scroll 残留防止)。
export function useAutoScroll({ messages, activeSession }) {
  const [showScrollBtn, setShowScrollBtn] = useState(false)
  const [hasNew, setHasNew] = useState(false)
  const isAtBottomRef = useRef(true)
  const scrollerDomRef = useRef(null)
  const msgLengthRef = useRef({})
  const programmaticScrollRef = useRef(false)
  const scrollEndTimerRef = useRef(null)
  const sid = activeSession?.id

  // 同期: 最下端 (= 最新が見える状態) に移動
  const scrollToBottomSync = useCallback(() => {
    const el = scrollerDomRef.current
    if (!el) return
    isAtBottomRef.current = true
    el.scrollTop = el.scrollHeight
  }, [])

  // sid 切替 / 初期マウント後の遅延 layout 追従用。 ユーザが既に上スクロールしてれば
  // (= isAtBottomRef=false) 何もしない、 末尾追従中だけ底辺へ寄せ直す。
  const scrollToBottomIfFollowing = useCallback(() => {
    const el = scrollerDomRef.current
    if (!el || !isAtBottomRef.current) return
    el.scrollTop = el.scrollHeight
  }, [])

  // 公開: 「↓ 最新へ」 ボタン or send 直後に呼ぶ用
  const scrollToBottom = useCallback(() => {
    const el = scrollerDomRef.current
    if (!el) return
    programmaticScrollRef.current = true
    isAtBottomRef.current = true
    setHasNew(false)
    el.scrollTop = el.scrollHeight
    clearTimeout(scrollEndTimerRef.current)
    scrollEndTimerRef.current = setTimeout(() => {
      programmaticScrollRef.current = false
    }, PROGRAMMATIC_SCROLL_GUARD_MS)
  }, [])

  // 起動 / タブ切替: paint 前に底へ flush (= 前 session の scroll 残留防止)。
  // 加えて、 JSONL 初回 replay (= 数百〜2000 行が EventSource で順次到着) や
  // localStorage 復元後の画像 / Markdown / コードブロックの遅延 layout で
  // scrollHeight が paint 後にも伸び続ける。 複数 timing で末尾追従中の場合だけ
  // 底辺へ寄せ直して、 「タブ切替したけど最下部じゃない」 を取り逃さない。
  // ユーザが意図的に上スクロールしたら scrollToBottomIfFollowing 側で no-op になる。
  useLayoutEffect(() => {
    if (!sid) return
    isAtBottomRef.current = true
    setShowScrollBtn(false)
    setHasNew(false)
    msgLengthRef.current[sid] = (messages[sid] || []).length
    scrollToBottomSync()
    const ids = [50, 150, 400, 1000, 2500].map(ms =>
      setTimeout(scrollToBottomIfFollowing, ms)
    )
    return () => ids.forEach(clearTimeout)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sid])

  // 新着メッセージ:
  //   isAtBottom 中なら底に追従、 上スクロール中 (= 古いメッセージ閲覧) なら hasNew=true で赤丸表示。
  //   通常 column では新着で要素が下に伸びるだけ、 scroll 位置は変わらないので明示追従が必要。
  useEffect(() => {
    if (!sid) return
    const cur = messages[sid] || []
    const currentLen = cur.length
    const prevLen = msgLengthRef.current[sid] || 0
    msgLengthRef.current[sid] = currentLen

    if (currentLen > prevLen) {
      if (isAtBottomRef.current) {
        scrollToBottomSync()
      } else {
        setHasNew(true)
      }
    }
  }, [messages, sid, scrollToBottomSync])

  // 画面回転 / キーボード表示等のレイアウト変化時は最新位置に戻す (isAtBottom 中のみ)
  useEffect(() => {
    const onResize = () => {
      if (isAtBottomRef.current) scrollToBottomSync()
    }
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [scrollToBottomSync])

  // scroll 容器の子要素 layout が遅延確定する (= Markdown / コードブロック / 画像 / details
  // 展開等) ケースに追従するための ResizeObserver。 isAtBottom 中なら scrollHeight が伸びる
  // たびに底辺へ送り直す。 初回マウント / タブ切替の「scrollHeight が後から伸びて底辺まで
  // 届かない」 問題と、 SSE で長文 AM が伸び続けるケースを同時に解消する。
  useEffect(() => {
    const el = scrollerDomRef.current
    if (!el || typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(() => {
      if (isAtBottomRef.current) scrollToBottomSync()
    })
    ro.observe(el)
    // 子要素のサイズ変化も拾う (= 直接の resize でない場合)
    for (const child of el.children) ro.observe(child)
    // 子の追加 / 削除に追従するため、 MutationObserver で children を監視
    const mo = new MutationObserver((muts) => {
      for (const m of muts) {
        for (const n of m.addedNodes) if (n.nodeType === 1) ro.observe(n)
      }
    })
    mo.observe(el, { childList: true })
    return () => { ro.disconnect(); mo.disconnect() }
  }, [scrollToBottomSync, sid])

  const onScroll = useCallback(() => {
    if (programmaticScrollRef.current) return
    const el = scrollerDomRef.current
    if (!el) return
    // 通常 column: 底辺 = scrollTop が scrollHeight - clientHeight に近い
    const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight
    const atBottom = distanceFromBottom <= AT_BOTTOM_THRESHOLD_PX
    isAtBottomRef.current = atBottom
    if (atBottom) setHasNew(false)
    // 同値時は React が re-render を bailout するので、 毎回 set で OK。
    setShowScrollBtn(!atBottom)
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
