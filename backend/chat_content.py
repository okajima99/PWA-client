"""チャットの添付ファイル保存と Anthropic content 配列の組み立て。

chat_routes.py から責務分離 (= chat_routes は CRUD + SSE に専念、 ここは添付ファイルの
取り扱い専門)。 画像は base64 image content + パス text、 テキストファイルは fenced code
として content 配列に積む。
"""
import base64
import logging
import mimetypes
import uuid
from pathlib import Path

from fastapi import UploadFile

from config import SUPPORTED_IMAGE_TYPES, UPLOADS_TMP
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


def build_content(message: str, saved_files: list[dict]) -> list:
    """Anthropic API の content 配列を組み立てる。 画像は base64 image、 テキストは
    fenced code、 最後にユーザのメッセージを text で。"""
    content = []
    for sf in saved_files:
        mime = sf["mime"]
        path_obj = Path(sf["path"])
        if mime in SUPPORTED_IMAGE_TYPES:
            b64 = base64.b64encode(path_obj.read_bytes()).decode()
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": mime, "data": b64},
            })
            content.append({"type": "text", "text": f"[添付画像のパス: {sf['path']}]"})
        else:
            try:
                text_content = path_obj.read_text(errors="replace")
                content.append({
                    "type": "text",
                    "text": f"[添付ファイル: {sf['path']} ({sf['name']})]\n```\n{text_content}\n```",
                })
            except Exception:
                logger.debug("attachment text read failed for %s", sf.get("path"), exc_info=True)
    if message:
        content.append({"type": "text", "text": message})
    # 全件 read 失敗 + message 空 = content が空のまま SDK に渡ると Anthropic API が
    # 400 を返す。 添付があった事実だけでも text として残す (= 「ファイル N 件添付」)。
    if not content and saved_files:
        names = ", ".join(sf["name"] for sf in saved_files)
        content.append({"type": "text", "text": f"[添付ファイル {len(saved_files)} 件: {names}]"})
    return content
