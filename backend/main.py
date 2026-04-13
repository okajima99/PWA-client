import json
import subprocess
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

HOME = Path.home()

# --- 設定読み込み ---
CONFIG_PATH = Path(__file__).parent / "config.json"
with open(CONFIG_PATH) as f:
    config = json.load(f)

AGENTS = config["agents"]
RATE_LIMITS_LOG = config["rate_limits_log"]

# --- アプリ初期化 ---
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- セッション管理（メモリ上） ---
# キー: config.jsonで定義されたagent名, 値: session_id または None
sessions: dict[str, str | None] = {name: None for name in AGENTS}


# --- リクエスト/レスポンス型定義 ---
class ChatRequest(BaseModel):
    message: str


class ChatResponse(BaseModel):
    result: str
    session_id: str


class StatusResponse(BaseModel):
    model: str
    five_hour_pct: float
    seven_day_pct: float
    context_pct: float
    five_hour_resets_at: int
    seven_day_resets_at: int


# --- エンドポイント ---

@app.post("/chat/{agent}", response_model=ChatResponse)
def chat(agent: str, req: ChatRequest):
    """メッセージを送信してエージェントの返答を返す"""
    if agent not in AGENTS:
        raise HTTPException(status_code=404, detail=f"Agent '{agent}' not found")

    cwd = AGENTS[agent]["cwd"]
    session_id = sessions[agent]

    cmd = ["claude"]
    if session_id:
        cmd += ["--resume", session_id]
    cmd += ["-p", req.message, "--output-format", "json"]

    result = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        raise HTTPException(status_code=500, detail=result.stderr)

    data = json.loads(result.stdout)
    sessions[agent] = data["session_id"]

    return ChatResponse(result=data["result"], session_id=data["session_id"])


@app.post("/session/{agent}/end")
def end_session(agent: str):
    """セッションを終了してsession_idをリセットする"""
    if agent not in AGENTS:
        raise HTTPException(status_code=404, detail=f"Agent '{agent}' not found")

    sessions[agent] = None
    return {"status": "ok", "agent": agent}


@app.get("/status/{agent}", response_model=StatusResponse)
def get_status(agent: str):
    """rate-limits.jsonlから最新のusage情報とエージェントのモデルを返す"""
    if agent not in AGENTS:
        raise HTTPException(status_code=404, detail=f"Agent '{agent}' not found")

    log_path = Path(RATE_LIMITS_LOG)
    if not log_path.exists():
        raise HTTPException(status_code=503, detail="rate-limits log not found")

    last_line = None
    with open(log_path) as f:
        for line in f:
            line = line.strip()
            if line:
                last_line = line

    if not last_line:
        raise HTTPException(status_code=503, detail="rate-limits log is empty")

    data = json.loads(last_line)
    return StatusResponse(
        model=AGENTS[agent].get("model", data["model"]),
        five_hour_pct=data["five_hour_pct"],
        seven_day_pct=data["seven_day_pct"],
        context_pct=data["context_pct"],
        five_hour_resets_at=data["five_hour_resets_at"],
        seven_day_resets_at=data["seven_day_resets_at"],
    )


def _resolve_safe(path_str: str) -> Path:
    """パスを解決してHOME配下かチェック。違う場合は403を返す"""
    resolved = Path(path_str.replace("~", str(HOME))).resolve()
    if not str(resolved).startswith(str(HOME)):
        raise HTTPException(status_code=403, detail="Access denied")
    return resolved


@app.get("/file")
def get_file(path: str = Query(...)):
    """ファイル内容をテキストで返す（HOME配下のみ）"""
    resolved = _resolve_safe(path)
    if not resolved.exists():
        raise HTTPException(status_code=404, detail="File not found")
    if not resolved.is_file():
        raise HTTPException(status_code=400, detail="Not a file")
    try:
        content = resolved.read_text(errors="replace")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"path": str(resolved), "content": content}


@app.get("/files/tree")
def get_tree(path: str = Query(default="~")):
    """ディレクトリ一覧を返す（HOME配下のみ）"""
    resolved = _resolve_safe(path)
    if not resolved.exists():
        raise HTTPException(status_code=404, detail="Directory not found")
    if not resolved.is_dir():
        raise HTTPException(status_code=400, detail="Not a directory")

    entries = []
    try:
        for entry in sorted(resolved.iterdir(), key=lambda e: (e.is_file(), e.name)):
            entries.append({
                "name": entry.name,
                "path": str(entry),
                "is_dir": entry.is_dir(),
            })
    except PermissionError:
        raise HTTPException(status_code=403, detail="Permission denied")
    return {"path": str(resolved), "entries": entries}
