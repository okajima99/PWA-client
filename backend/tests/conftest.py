"""pytest 共通 setup。

- `backend/` を sys.path に注入することで、 test ファイル側で
  `from usage import _parse_reset` のように直 import できるようにする。
- `isolated_state` fixture: state.py の module-level dict を test 内で安全に
  mutate するための snapshot / restore 仕組み。 第一弾の pure 関数 test では
  実質出番ないが、 register_session 等 global state を触る test 群で必須になる。
"""
import copy
import pathlib
import sys

import pytest

BACKEND = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))


# state.py の module-level に存在する dict 群。 push.client_states は別 module
# なので、 第二弾で push 専用 fixture を切る時に分離する。
_STATE_GLOBALS = (
    "agent_status",
    "shared_status",
    "sessions_meta",
    "stream_states",
)


@pytest.fixture
def isolated_state():
    """state.py の global dict を deepcopy で snapshot → test 退場時に復元。
    test 間で global state が漏れて偽の pass/fail を起こさないための保険。"""
    import state

    snapshots = {name: copy.deepcopy(getattr(state, name)) for name in _STATE_GLOBALS}
    yield state
    for name, snap in snapshots.items():
        live = getattr(state, name)
        live.clear()
        live.update(snap)
