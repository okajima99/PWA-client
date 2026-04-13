import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

// ~/... または /Users/... で始まるパスを cpc:// リンクに変換
const PATH_RE = /(~\/[^\s`"')\]]+|\/Users\/[^\s`"')\]]+)/g

function preprocessPaths(text) {
  return text.replace(PATH_RE, (match) => `[${match}](cpc://${match})`)
}

export default function MessageRenderer({ text, onOpenFile, markdown }) {
  if (!markdown) {
    // Markdownオフ: パスだけリンク化してプレーンテキスト表示
    const parts = []
    let last = 0
    let match
    PATH_RE.lastIndex = 0
    while ((match = PATH_RE.exec(text)) !== null) {
      if (match.index > last) parts.push(text.slice(last, match.index))
      const p = match[0]
      parts.push(
        <span key={match.index} className="file-link" onClick={() => onOpenFile(p)}>{p}</span>
      )
      last = match.index + p.length
    }
    if (last < text.length) parts.push(text.slice(last))
    return <span style={{ whiteSpace: 'pre-wrap' }}>{parts}</span>
  }

  // Markdownオン
  const processed = preprocessPaths(text)

  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      components={{
        a({ href, children }) {
          if (href?.startsWith('cpc://')) {
            const path = href.slice('cpc://'.length)
            return (
              <span className="file-link" onClick={() => onOpenFile(path)}>
                {children}
              </span>
            )
          }
          return <a href={href} target="_blank" rel="noreferrer">{children}</a>
        },
        // コードブロックのスタイル
        code({ inline, children }) {
          if (inline) return <code className="inline-code">{children}</code>
          return <pre className="md-code"><code>{children}</code></pre>
        },
      }}
    >
      {processed}
    </ReactMarkdown>
  )
}
