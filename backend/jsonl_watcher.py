"""tmux session ↔ claude プロセス ↔ JSONL の紐付けを backend mem で確定保持する。

紐付けは **hook 駆動の確定経路のみ** (= 確率窓マッチは廃止)。 PWA spawn が tmux env に
`PWA_SID=ses_xxx` を注入し、 その claude が叩く hook は全イベントで `X-PWA-SID` header と
`transcript_path` を運ぶ。 hooks_router がそれを `confirm_bind` に渡すので、 「そのタブの
claude 自身が報告した transcript」 という 100% 確定の事実だけで pwa_sid → jsonl が決まる。

同 cwd で並行する他 claude (Claude Desktop App / ターミナル直叩き) は PWA_SID env を
持たず header が付かないので、 構造的に binding に混入しない。 birthtime / cwd による
推測は一切しない (= cross-contamination の発生源を排除)。

backend 再起動跨ぎ: confirmed binding を `logs/jsonl_bindings.json` に persist する。
再起動後は load し、 jsonl が rotate 済でも pwa_sid の所有権は保持して、 次の hook 1 発で
新 jsonl に再確定する (= self-heal)。
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
# binding を JSON で persist する file。 backend 再起動跨ぎで confirmed binding を
# 保持する。 file が破損 / 読めなければ空起動。
_PERSIST_PATH = Path(__file__).resolve().parent.parent / "logs" / "jsonl_bindings.json"


@dataclass
class _ClaudeBinding:
    tmux_sid: str
    claude_pid: int
    claude_cwd: str
    start_time: float
    jsonl_path: Optional[Path] = None
    # hook (= SessionStart / 任意イベント) の X-PWA-SID 経由で確定したか。
    confirmed: bool = False


# tmux_sid → binding
_bindings: dict[str, _ClaudeBinding] = {}
# tmux_sid → 確定 JSONL path (= hook 由来のみ)。 _bindings の jsonl_path が再 attach /
# restart の race で失われても、 こちらから self-heal で復元する。 PWA_SID 確定だけが
# 入るので Desktop Claude 等の cwd 一致プロセス混入は構造的に起きない。
_confirmed_paths: dict[str, Path] = {}


def _cwd_to_project_dirname(cwd: str) -> str:
    """claude Code の規約: パス中の `/` と `.` を `-` に置換 (先頭 `/` も `-`)。"""
    return cwd.replace("/", "-").replace(".", "-")


def _cwd_to_project_dir(cwd: str) -> Path:
    return _CLAUDE_PROJECTS / _cwd_to_project_dirname(cwd)


def register_pending(
    tmux_sid: str, claude_pid: int, claude_cwd: str, start_time: float,
) -> None:
    """spawn 直後に呼ぶ。 session を pending binding として登録する (= jsonl_path は未確定)。
    実際の jsonl 紐付けは、 この claude が最初の hook を叩いた時に `confirm_bind` が行う。
    既に confirmed なら claude_pid / start_time の更新だけ行い jsonl_path は保護する。
    """
    existing = _bindings.get(tmux_sid)
    if existing is not None and existing.confirmed:
        existing.claude_pid = claude_pid
        existing.start_time = start_time
        return
    _bindings[tmux_sid] = _ClaudeBinding(
        tmux_sid=tmux_sid,
        claude_pid=claude_pid,
        claude_cwd=claude_cwd,
        start_time=start_time,
    )


def unregister(tmux_sid: str) -> None:
    _confirmed_paths.pop(tmux_sid, None)
    if _bindings.pop(tmux_sid, None) is not None:
        _save_bindings()


def confirm_bind(pwa_sid: str, claude_sid: str, transcript_path: str) -> Optional[Path]:
    """SessionStart hook 経由で呼ばれる確定 binding 経路。
    PWA タブで起動した claude のみが PWA_SID env を持っているので、 hooks_router が
    X-PWA-SID header からこの pwa_sid を取得して呼ぶ。 確率窓マッチを介さず
    claude_sid / transcript_path を直接受け取るので race / 誤割当ゼロ。
    /clear 等で新 claude_sid に切り替わっても hook が再発火して上書きされる。
    """
    path = Path(transcript_path)
    # SessionStart hook は claude 起動直後 (= JSONL birth 前) に発火するので、 ファイル
    # 存在 check はしない。 jsonl_path は文字列で保持しておけば後の get_jsonl_for で
    # is_file() check して合えば読み出す。
    binding = _bindings.get(pwa_sid)
    if binding is None:
        # PWA spawn 直後で _register_claude_when_ready がまだ動いてない / 失敗してた
        # ケース。 hook 経由の情報だけで binding を作る。 claude_pid は後で必要なら
        # 解決すれば良い (= 現状の registry 利用箇所では未使用)。
        binding = _ClaudeBinding(
            tmux_sid=pwa_sid, claude_pid=0,
            claude_cwd=str(path.parent),  # project dir のまま (= cwd 逆引きは不要)
            start_time=time.time(),
        )
        _bindings[pwa_sid] = binding
    binding.jsonl_path = path
    binding.confirmed = True
    _confirmed_paths[pwa_sid] = path
    # race 救済: on_created の確率マッチが先に走って別 binding (= confirmed でない古いタブ)
    # に同 JSONL が bind されてた場合、 ここで剥がして二重所有を解消する。 同じ JSONL を
    # 2 タブで tail すると同 chat が複数タブに流れる cross-contamination 再発。
    for other in _bindings.values():
        if other is binding:
            continue
        if other.jsonl_path == path:
            logger.info(
                "jsonl_watcher confirm_bind detached stale binding: pwa_sid=%s -> None",
                other.tmux_sid,
            )
            other.jsonl_path = None
            # self-heal 経路でこの path に復帰させない (= 所有権は pwa_sid に移った)
            if _confirmed_paths.get(other.tmux_sid) == path:
                _confirmed_paths.pop(other.tmux_sid, None)
    logger.info(
        "jsonl_watcher confirm_bound pwa_sid=%s claude_sid=%s -> %s",
        pwa_sid, claude_sid, path.name,
    )
    _save_bindings()
    return path


def list_bindings() -> dict[str, dict]:
    """debug 用: 現在の全 binding を JSON-serializable な dict で返す。"""
    return {
        sid: {
            "claude_pid": b.claude_pid,
            "claude_cwd": b.claude_cwd,
            "start_time": b.start_time,
            "jsonl_path": str(b.jsonl_path) if b.jsonl_path else None,
            "confirmed": b.confirmed,
        }
        for sid, b in _bindings.items()
    }


def get_jsonl_for(tmux_sid: str) -> Optional[Path]:
    """確定済の JSONL path を返す。 未確定 / ファイル消失なら None。

    in-mem の _bindings が再 attach / restart の race で jsonl_path を失っても、
    SessionStart hook / persist 由来の確定 path (_confirmed_paths) が生きていれば
    そこから self-heal して in-mem を復元する。 _confirmed_paths は PWA_SID 確定だけが
    入るので、 同 cwd の Desktop Claude 等を誤って bind することはない。
    """
    binding = _bindings.get(tmux_sid)
    if binding is not None and binding.jsonl_path is not None and binding.jsonl_path.is_file():
        return binding.jsonl_path
    healed = _confirmed_paths.get(tmux_sid)
    if healed is not None and healed.is_file():
        if binding is None:
            binding = _ClaudeBinding(
                tmux_sid=tmux_sid, claude_pid=0,
                claude_cwd=str(healed.parent), start_time=time.time(),
            )
            _bindings[tmux_sid] = binding
        if binding.jsonl_path != healed or not binding.confirmed:
            binding.jsonl_path = healed
            binding.confirmed = True
            logger.info(
                "jsonl_watcher self-healed binding from confirmed path: sid=%s -> %s",
                tmux_sid, healed.name,
            )
        return healed
    return None


def _save_bindings() -> None:
    """confirmed binding だけ JSON で persist する。
    register_pending / on_created 経由の確率窓マッチは backend lifetime のみ有効 (= 再起動
    後の意味が薄い + ノイズになる) なので保存しない。 SessionStart hook 由来の確定値だけ
    残せば、 再起動後も該当 PWA タブが /clear せず chat 継続できる。
    atomic write (= tmp + os.replace) で書き込み中の crash でも壊れない。
    """
    try:
        _PERSIST_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            sid: {
                "tmux_sid": b.tmux_sid,
                "claude_pid": b.claude_pid,
                "claude_cwd": b.claude_cwd,
                "start_time": b.start_time,
                "jsonl_path": str(b.jsonl_path) if b.jsonl_path else None,
                "confirmed": b.confirmed,
            }
            for sid, b in _bindings.items() if b.confirmed
        }
        tmp = _PERSIST_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, _PERSIST_PATH)
    except OSError:
        logger.exception("jsonl_watcher _save_bindings failed")


def _load_bindings() -> None:
    """backend 起動時に persist file から confirmed binding を復元する。
    file が無い / 壊れてる / JSONL 実体が消えてる binding は無視。 復元後の binding は
    confirmed=True で乗るので、 確率窓マッチでの上書きから保護される。 該当 tmux session
    が既に死んでた場合、 binding は残るが get_jsonl_for で is_file() check が通れば
    chat tail は機能する (= claude プロセスが死んでても過去 JSONL は読める)。
    """
    if not _PERSIST_PATH.is_file():
        return
    try:
        data = json.loads(_PERSIST_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("jsonl_watcher _load_bindings: file corrupted, ignoring")
        return
    restored = 0
    for sid, d in data.items():
        jp = d.get("jsonl_path")
        if not jp:
            continue
        path = Path(jp)
        if not path.is_file():
            continue
        _bindings[sid] = _ClaudeBinding(
            tmux_sid=d.get("tmux_sid", sid),
            claude_pid=int(d.get("claude_pid") or 0),
            claude_cwd=d.get("claude_cwd", str(path.parent)),
            start_time=float(d.get("start_time") or time.time()),
            jsonl_path=path,
            confirmed=bool(d.get("confirmed", True)),
        )
        _confirmed_paths[sid] = path
        restored += 1
    if restored:
        logger.info("jsonl_watcher _load_bindings: restored %d binding(s)", restored)


def start_watcher() -> None:
    """起動時に persist 済の confirmed binding を復元する。 ファイル監視はしない
    (= 紐付けは hook 駆動の confirm_bind に一本化、 確率窓マッチを廃止したため)。"""
    _CLAUDE_PROJECTS.mkdir(parents=True, exist_ok=True)
    _load_bindings()
    logger.info("jsonl_watcher initialized (hook-driven bindings, %d restored)", len(_bindings))


def stop_watcher() -> None:
    """対称性のため残す no-op (= 監視 thread を持たないので停止対象なし)。"""
    return
