"""User-triggered session management. GET never probes the external platform."""
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field
from typing import Literal
from app.services.buff_session import check_session, session_status

router = APIRouter()


def _require_intent(request: Request):
    if request.headers.get('x-aetherswap-intent') != 'buff-session':
        raise HTTPException(403, '缺少明确的 BUFF 会话操作意图')
    origin = request.headers.get('origin')
    expected_origin = f'{request.url.scheme}://{request.url.netloc}'
    if request.headers.get('sec-fetch-site') == 'cross-site' or (origin and origin.rstrip('/') != expected_origin):
        raise HTTPException(403, '拒绝跨来源的会话操作')


@router.get('/api/auth/buff/session')
def api_buff_session(response: Response):
    response.headers['Cache-Control'] = 'no-store'
    return {'ok': True, 'session': session_status()}


@router.post('/api/auth/buff/session/check')
def api_buff_session_check(request: Request):
    _require_intent(request)
    return check_session()


class SessionImportBody(BaseModel):
    cookies: str = Field(max_length=32768)
    user_agent: str = Field(default='', max_length=1024)
    source: Literal['manual', 'extension'] = 'manual'
    recovery: bool = False


@router.post('/api/auth/buff/session/import')
def api_buff_session_import(body: SessionImportBody, request: Request):
    _require_intent(request)
    from app.services.buff_session import import_session
    return import_session(body.cookies, body.user_agent, body.source, body.recovery)


@router.get('/api/auth/buff/extension.zip')
def api_buff_extension_download():
    from io import BytesIO
    from pathlib import Path
    from zipfile import ZipFile, ZIP_DEFLATED
    from fastapi.responses import Response
    root = Path(__file__).resolve().parents[2] / 'extensions' / 'buff-session'
    if not (root / 'manifest.json').is_file():
        raise HTTPException(503, '扩展安装包尚不可用')
    buffer = BytesIO()
    with ZipFile(buffer, 'w', ZIP_DEFLATED) as archive:
        for path in sorted(root.rglob('*')):
            if path.is_symlink() or not path.is_file() or path.suffix not in {'.json', '.js', '.html', '.css', '.png', '.svg', '.md'}:
                continue
            if not path.resolve().is_relative_to(root.resolve()):
                continue
            archive.write(path, 'buff-session/' + path.relative_to(root).as_posix())
    return Response(buffer.getvalue(), media_type='application/zip', headers={
        'Content-Disposition': 'attachment; filename="aetherswap-buff-session.zip"',
        'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
    })
