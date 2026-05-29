"""maintenance.restart_sunshine_if_bloated の判定分岐 + footprint parser の regression。

外部コマンド (pgrep / footprint / os.kill) は monkeypatch でスタブし、
「配信中はスキップ」「閾値未満はスキップ」「肥大化 + idle のみ kill」を固定する。
"""
import signal

import maintenance as m


def test_footprint_parse_units():
    assert m._FOOTPRINT_RE.search("phys_footprint: 30 GB").groups() == ("30", "GB")
    assert m._FOOTPRINT_RE.search("    phys_footprint: 38 MB").groups() == ("38", "MB")


def _patch(monkeypatch, *, sunshine_pid, streamer_pid, footprint_bytes, killed):
    def fake_pgrep(pattern, *, exact=False):
        if "sunshine" in pattern:
            return sunshine_pid
        if "streamer" in pattern:
            return streamer_pid
        return None

    monkeypatch.setattr(m, "_pgrep_one", fake_pgrep)
    monkeypatch.setattr(m, "_phys_footprint_bytes", lambda pid: footprint_bytes)
    monkeypatch.setattr(m.os, "kill", lambda pid, sig: killed.append((pid, sig)))


def test_skip_when_sunshine_absent(monkeypatch):
    killed = []
    _patch(monkeypatch, sunshine_pid=None, streamer_pid=None,
           footprint_bytes=99 * 1024**3, killed=killed)
    assert m.restart_sunshine_if_bloated() is False
    assert killed == []


def test_skip_when_streaming(monkeypatch):
    """配信中 (= streamer 在席) は肥大化していても使用中ペアを壊さないため触らない。"""
    killed = []
    _patch(monkeypatch, sunshine_pid=100, streamer_pid=200,
           footprint_bytes=99 * 1024**3, killed=killed)
    assert m.restart_sunshine_if_bloated() is False
    assert killed == []


def test_skip_when_under_threshold(monkeypatch):
    killed = []
    _patch(monkeypatch, sunshine_pid=100, streamer_pid=None,
           footprint_bytes=m.SUNSHINE_FOOTPRINT_MAX_BYTES - 1, killed=killed)
    assert m.restart_sunshine_if_bloated() is False
    assert killed == []


def test_kill_when_bloated_and_idle(monkeypatch):
    killed = []
    _patch(monkeypatch, sunshine_pid=100, streamer_pid=None,
           footprint_bytes=m.SUNSHINE_FOOTPRINT_MAX_BYTES + 1, killed=killed)
    assert m.restart_sunshine_if_bloated() is True
    assert killed == [(100, signal.SIGKILL)]


def test_skip_when_footprint_unavailable(monkeypatch):
    killed = []
    _patch(monkeypatch, sunshine_pid=100, streamer_pid=None,
           footprint_bytes=None, killed=killed)
    assert m.restart_sunshine_if_bloated() is False
    assert killed == []
