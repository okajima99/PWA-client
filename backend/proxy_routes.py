"""Anthropic API のリバースプロキシ。
rate-limit ヘッダを観測したいので、 SDK を直結せず自プロセスを経由させる。

旧: response 全 buffer → return Response(content=resp.content) の単一往復。
   SSE / 大レスポンスで全部読み終わるまで client に何も流れず latency 悪化。
新: httpx.AsyncClient.send(req, stream=True) で chunk を素通し → StreamingResponse。
   rate-limit ヘッダは応答開始時の resp.headers で確定するので変化なし。
"""
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

import http_client
from config import ANTHROPIC_API_BASE
from state import update_shared_from_headers

router = APIRouter()

_SKIP_HEADERS = {"transfer-encoding", "connection", "keep-alive", "content-encoding"}


@router.api_route(
    "/proxy/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
)
async def anthropic_proxy(path: str, request: Request):
    target_url = f"{ANTHROPIC_API_BASE}/{path}"
    if request.query_params:
        target_url += f"?{request.query_params}"

    body = await request.body()
    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in ("host", "content-length")
    }

    client = http_client.get()
    req = client.build_request(
        method=request.method,
        url=target_url,
        headers=headers,
        content=body,
    )
    resp = await client.send(req, stream=True)
    update_shared_from_headers(resp.headers)

    response_headers = {
        k: v for k, v in resp.headers.items()
        if k.lower() not in _SKIP_HEADERS
    }

    async def passthrough():
        try:
            async for chunk in resp.aiter_raw():
                yield chunk
        finally:
            await resp.aclose()

    return StreamingResponse(
        passthrough(),
        status_code=resp.status_code,
        headers=response_headers,
    )
