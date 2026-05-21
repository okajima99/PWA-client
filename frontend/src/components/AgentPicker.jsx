/**
 * Session picker — `/api/agents` を fetch して、 ユーザがどの session で
 * terminal を開きたいか選ばせる。 picker は URL に query param なしで開いた時に
 * 表示される、 選択すると `?terminal=<id>` に navigate して Terminal を mount する。
 *
 * AGENTS config (= backend/config.json) に登録された agent (= cwd / model / display_name)
 * と、 素の zsh を開く "Plain shell" エントリが並ぶ。 tmux 永続化のおかげで、 同じ id を
 * 何度選んでも前回の続きに戻る。
 */
import { useEffect, useState } from 'react';

export default function AgentPicker() {
  const [agents, setAgents] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    fetch('/api/agents')
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then((data) => {
        if (cancelled) return;
        setAgents(data.agents || []);
      })
      .catch((err) => {
        if (cancelled) return;
        setError(String(err));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const open = (sessionId) => {
    const url = new URL(window.location.href);
    url.searchParams.set('terminal', sessionId);
    window.location.assign(url.toString());
  };

  return (
    <div
      style={{
        position: 'fixed',
        inset: 0,
        background: '#0e0f12',
        color: '#e6e6e6',
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        justifyContent: 'center',
        padding: '24px',
        fontFamily: 'SF Mono, Menlo, monospace',
        gap: '16px',
      }}
    >
      <div style={{ fontSize: '20px', opacity: 0.8 }}>Open a terminal session</div>
      {error && (
        <div style={{ color: '#f48771', fontSize: '13px' }}>
          failed to load agents: {error}
        </div>
      )}
      {agents === null && !error && (
        <div style={{ opacity: 0.6, fontSize: '13px' }}>loading…</div>
      )}
      {agents && (
        <div
          style={{
            display: 'flex',
            flexDirection: 'column',
            gap: '12px',
            width: '100%',
            maxWidth: '320px',
          }}
        >
          {agents.map((a) => (
            <button
              key={a.id}
              type="button"
              onClick={() => open(a.id)}
              style={{
                background: '#1f2228',
                color: '#e6e6e6',
                border: '1px solid #3b3f4a',
                borderRadius: '10px',
                padding: '14px 18px',
                fontSize: '16px',
                fontFamily: 'inherit',
                cursor: 'pointer',
                textAlign: 'left',
              }}
            >
              {a.display_name}
              <div style={{ opacity: 0.5, fontSize: '11px', marginTop: '2px' }}>
                {a.id}
              </div>
            </button>
          ))}
        </div>
      )}
      <div
        style={{
          marginTop: '24px',
          fontSize: '11px',
          opacity: 0.4,
          textAlign: 'center',
        }}
      >
        tmux で永続化、 同じ session を再選択すると前回の続きに戻る
      </div>
    </div>
  );
}
