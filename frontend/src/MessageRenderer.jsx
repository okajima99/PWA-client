// チャットメッセージ内のファイルパスを検出してタップ可能なリンクに変換する
// ~/... または /Users/... で始まるパスが対象
const PATH_RE = /(~\/[^\s`"']+|\/Users\/[^\s`"']+)/g

export default function MessageRenderer({ text, onOpenFile }) {
  const parts = []
  let last = 0
  let match

  PATH_RE.lastIndex = 0
  while ((match = PATH_RE.exec(text)) !== null) {
    if (match.index > last) {
      parts.push(text.slice(last, match.index))
    }
    const filePath = match[0]
    parts.push(
      <span
        key={match.index}
        className="file-link"
        onClick={() => onOpenFile(filePath)}
      >
        {filePath}
      </span>
    )
    last = match.index + filePath.length
  }
  if (last < text.length) {
    parts.push(text.slice(last))
  }

  return <span style={{ whiteSpace: 'pre-wrap' }}>{parts}</span>
}
