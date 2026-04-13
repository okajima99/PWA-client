import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

const PATH_RE = /(~\/[^\s`"')\]]+|\/Users\/[^\s`"')\]]+)/g

function preprocessPaths(text) {
  return text.replace(PATH_RE, (match) =>
    `[${match}](cpc://${encodeURIComponent(match)})`
  )
}

export default function MessageRenderer({ text, onOpenFile, markdown }) {
  if (!markdown) {
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

  const processed = preprocessPaths(text)

  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      components={{
        a({ href, children }) {
          if (href?.startsWith('cpc://')) {
            const path = decodeURIComponent(href.slice('cpc://'.length))
            return (
              <span className="file-link" onClick={() => onOpenFile(path)}>
                {children}
              </span>
            )
          }
          return <a href={href} target="_blank" rel="noreferrer">{children}</a>
        },
        pre({ children }) {
          return <pre className="md-code">{children}</pre>
        },
        code({ className, children }) {
          if (!className) {
            // インラインコード: パスだったらリンク化
            const content = String(children).trim()
            if (/^(~\/|\/Users\/)/.test(content)) {
              return (
                <span className="file-link" onClick={() => onOpenFile(content)}>
                  {children}
                </span>
              )
            }
            return <code className="inline-code">{children}</code>
          }
          return <code className={className}>{children}</code>
        },
      }}
    >
      {processed}
    </ReactMarkdown>
  )
}
