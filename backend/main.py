import subprocess
import json
import os
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional

app = FastAPI()

# 開発中はすべてのオリジンを許可（本番はTailscaleのIPに絞る）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST"],
    allow_headers=["*"],
)

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")

def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None  # Noneなら新規セッション、あれば継続


@app.post("/chat")
def chat(req: ChatRequest):
    config = load_config()
    workdir = os.path.expanduser(config["agent_workdir"])

    if not os.path.isdir(workdir):
        raise HTTPException(status_code=500, detail=f"agent_workdir not found: {workdir}")

    cmd = ["claude", "-p", req.message, "--output-format", "json", "--dangerously-skip-permissions"]

    # session_idがあれば既存セッションを継続、なければ新規セッション起動
    if req.session_id:
        cmd += ["--resume", req.session_id]

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=120,
        cwd=workdir,
    )

    if result.returncode != 0:
        raise HTTPException(status_code=500, detail=result.stderr)

    parsed = json.loads(result.stdout)

    return {
        "reply": parsed["result"],
        "session_id": parsed["session_id"],
    }
