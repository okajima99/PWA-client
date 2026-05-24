"""チャットの添付ファイル保存 (uploads/tmp への退避 + セッション単位の追跡)。

PTY 経路では保存したファイルのパスを tmux send-keys で claude に渡し、 claude が
Read で読む。 ここはファイルの保存と uuid 命名だけを担当する。
"""
import logging
import mimetypes
import uuid
from pathlib import Path

from fastapi import UploadFile

from config import UPLOADS_TMP
from state import session_tmp_files

logger = logging.getLogger(__name__)


async def save_to_tmp(files: list[UploadFile], session_id: str) -> list[dict]:
    """アップロードされたファイルを uploads/tmp に保存、 セッションごとに追跡。"""
    UPLOADS_TMP.mkdir(parents=True, exist_ok=True)
    saved = []
    for f in files:
        if not f.size:
            continue
        ext = Path(f.filename or "file").suffix or ""
        dest = UPLOADS_TMP / f"{uuid.uuid4().hex}{ext}"
        data = await f.read()
        dest.write_bytes(data)
        session_tmp_files.setdefault(session_id, []).append(dest)
        saved.append({
            "name": f.filename or dest.name,
            "path": str(dest),
            "mime": f.content_type or mimetypes.guess_type(f.filename or "")[0] or "",
        })
    return saved
