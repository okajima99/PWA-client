// ⋯ メニュー → Model & Effort の切替ダイアログ。 App.jsx から純粋プレゼンテーショナルに
// 切り出したもの。 選択肢は公式 CLI が受け入れる短縮形 + effort 階層。

// バージョン固定で選ばせる (= エイリアスでなくプレーンなフル ID = 標準モデル ID なので
// 認識ミスがない最も堅牢な指定)。 ctx は各モデルの context window (= 公式の値)。
// `[1m]` は付けない: Opus は Max で自動 1M なので不要、 Sonnet の 1M は credits 課金なので
// 勝手に有効化しない (= 金銭ルール)、 さらに `[1m]` 付きフル ID は statusline で生表示され
// `/model` がデフォルトにも `[1m]` を書き戻して汚すため。
const MODEL_OPTIONS = [
  { value: 'claude-opus-4-8', label: 'Opus 4.8', ctx: '1M' },
  { value: 'claude-opus-4-7', label: 'Opus 4.7', ctx: '1M' },
  { value: 'claude-sonnet-4-6', label: 'Sonnet 4.6', ctx: '1M' },
  { value: 'claude-haiku-4-5', label: 'Haiku 4.5', ctx: '200k' },
]
const EFFORT_OPTIONS = [
  { value: 'low', label: 'Low' },
  { value: 'medium', label: 'Medium' },
  { value: 'high', label: 'High' },
  { value: 'xhigh', label: 'Extra High' },
  { value: 'max', label: 'Max' },
  { value: 'auto', label: 'Auto' },
  { value: 'ultracode', label: 'Ultracode ⚠ (heavy)' },
]

// open=false なら描画しない。 model / effort は現在の選択 (= override ?? default)、
// fast は /fast トグルの現在状態、 disabled は推論中フラグ、
// onPick({model}|{effort}|{fast}) で patch、 onClose で閉じる。
export default function ModelEffortPicker({ open, model, effort, fast, disabled, onPick, onClose }) {
  if (!open) return null
  return (
    <div className="picker-overlay" onClick={onClose}>
      <div className="picker-dialog" onClick={e => e.stopPropagation()}>
        <div className="picker-title">Model &amp; Effort</div>
        {disabled && (
          <div className="picker-notice">推論中は変更できません</div>
        )}
        <div className="picker-section">
          <div className="picker-section-label">Model</div>
          {MODEL_OPTIONS.map(opt => (
            <button
              key={opt.value}
              className={`picker-option ${model === opt.value ? 'active' : ''}`}
              onClick={() => onPick({ model: opt.value })}
              disabled={disabled}
            >
              <span>{opt.label}</span>
              <span className="picker-meta">
                <span className="picker-ctx">{opt.ctx}</span>
                {model === opt.value && <span className="picker-check">✓</span>}
              </span>
            </button>
          ))}
        </div>
        <div className="picker-section">
          <div className="picker-section-label">Effort</div>
          {EFFORT_OPTIONS.map(opt => (
            <button
              key={opt.value}
              className={`picker-option ${effort === opt.value ? 'active' : ''}`}
              onClick={() => onPick({ effort: opt.value })}
              disabled={disabled}
            >
              <span>{opt.label}</span>
              {effort === opt.value && <span className="picker-check">✓</span>}
            </button>
          ))}
        </div>
        <div className="picker-section">
          <div className="picker-section-label">Speed</div>
          <button
            className={`picker-option ${!fast ? 'active' : ''}`}
            onClick={() => onPick({ fast: false })}
            disabled={disabled}
          >
            <span>Normal</span>
            {!fast && <span className="picker-check">✓</span>}
          </button>
          <button
            className={`picker-option ${fast ? 'active' : ''}`}
            onClick={() => onPick({ fast: true })}
            disabled={disabled}
          >
            <span>Fast (2.5×, 1/3 price)</span>
            {fast && <span className="picker-check">✓</span>}
          </button>
        </div>
        <button className="picker-close" onClick={onClose}>Close</button>
      </div>
    </div>
  )
}
