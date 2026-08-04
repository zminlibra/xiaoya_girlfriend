"""
Tiny server for the speech-to-speech demo.

The demo used to ship as a `sdk: static` Space, but the web-search tool needs a
search key the browser must NOT see. A static Space has no runtime process, so it
can't hold a secret the front-end uses. This server fixes that: it serves the
unchanged front-end AND exposes a same-origin `/api/search` proxy that holds the
Serper key server-side (see docs/adr/0001).

Everything lives in one container; the speech-to-speech backend stays a separate,
load-balanced service the browser talks to over WebSocket as before. The load
balancer's address is a secret too (like the Serper key): the browser never sees
it. `/api/session` proxies the session handshake server-side so only the
per-session compute URL the LB hands back (which the browser must dial) is exposed.

On the deployed Space the server also meters conversation time by HF login tier
(anonymous / signed-in / PRO) — see `limiter.py` and `auth.py`. That whole feature
is off unless BOTH `LOAD_BALANCER_URL` and `SPACE_ID` are set, so it runs only on
the live Space, never locally (even with the LB exported for testing).

`SPEECH_TO_SPEECH_URL` overrides everything: when set, the LB logic above is
disabled entirely (no session proxy, no queue, no metering, no sign-in) and the
browser connects directly to that URL, shown read-only in Settings.

Endpoints:
  GET  /api/config           -> { search, lb, allowDirect, s2sUrl, rtc, iceServers, auth }
  GET  /api/me               -> login + tier + remaining budget (LB mode only)
  POST /api/search           -> { results, answer }  Google via Serper.dev
  POST /api/calls            -> proxies the WebRTC SDP offer to <s2s>/v1/realtime/calls
  POST /api/session          -> proxies <LB>/session: a grant, or a queue ticket
  GET  /api/queue/{id}       -> proxies <LB>/queue/{id}: position, or a grant on claim
  DELETE /api/queue/{id}     -> leave the queue (explicit "Leave queue" button)
  POST /api/queue/end        -> leave the queue (sendBeacon on teardown)
  POST /api/session/heartbeat-> extend the reservation; { expired }
  POST /api/session/end      -> reconcile + refund (sendBeacon on teardown)
  /*                         -> static files (index.html, main.js, ...)

When every compute slot is busy the load balancer hands back a queue ticket
instead of a grant; the browser polls /api/queue/{id} until it reaches the front
and a slot frees. Waiting reserves nothing — the daily budget is only reserved at
the moment a slot is actually claimed (a grant), never while queued.
"""

import asyncio
import json
import logging
import os
import time
from urllib.parse import urlsplit, urlunsplit

import auth
import httpx
import limiter
from fastapi import FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

logger = logging.getLogger("s2s.search")

SERPER_KEY = os.environ.get("SERPER_API_KEY", "").strip()
# Speech-to-speech load balancer URL. When set, the browser POSTs /api/session
# (which proxies <lb>/session here, server-side) and connects to the URL the LB
# returns (the original flow). The LB address itself is never sent to the browser.
# When empty, the user may instead set a direct s2s server URL in Settings and the
# browser connects to it straight (no load balancer).
LOAD_BALANCER_URL = os.environ.get("LOAD_BALANCER_URL", "").strip()
# Direct s2s server URL pinned by the deploy. Takes priority over the load
# balancer: when set, ALL LB logic is disabled (no /api/session proxy, no queue,
# no limiter, no sign-in) and the browser connects to this URL directly. Unlike
# the LB address it is NOT a secret — /api/config sends it to the client, which
# shows it read-only in Settings.
SPEECH_TO_SPEECH_URL = os.environ.get("SPEECH_TO_SPEECH_URL", "").strip()
if SPEECH_TO_SPEECH_URL:
    LOAD_BALANCER_URL = ""
# HF injects SPACE_ID ("owner/space") into every Space runtime; it's absent
# locally and on a plain `docker run`. We meter conversation time ONLY on the
# deployed Space — i.e. when BOTH the LB is configured AND we're on a Space.
# Off-Space (local dev, even with the LB exported) the app still proxies the LB,
# but nothing is metered: no budget, no reservations, no sign-in gating.
SPACE_ID = os.environ.get("SPACE_ID", "").strip()
LIMITER_ENABLED = bool(LOAD_BALANCER_URL) and bool(SPACE_ID)


def _parse_ice_servers(raw: str) -> list:
    """ICE servers for the browser's RTCPeerConnection, from RTC_ICE_SERVERS.

    Accepts a JSON list of RTCIceServer dicts (same format as the s2s
    server's SPEECH_TO_SPEECH_ICE_SERVERS, e.g.
    ``[{"urls": "turn:t.example.com", "username": "u", "credential": "c"}]``),
    a single such dict, or a plain comma-separated list of STUN/TURN URLs.
    Empty when unset — host candidates only, which is fine for local use."""
    raw = raw.strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return [data]
    except ValueError:
        pass
    return [{"urls": u.strip()} for u in raw.split(",") if u.strip()]


RTC_ICE_SERVERS = _parse_ice_servers(os.environ.get("RTC_ICE_SERVERS", ""))
DEFAULT_STARTUP_GREETING = (
    "Start the conversation now with a brief, spontaneous greeting in character. "
    "Keep it to one sentence, invite the user in naturally, and vary the wording each time."
)
# Exposed to the browser through /api/config. Set an empty value to disable the
# automatic greeting without changing the client bundle.
STARTUP_GREETING = os.environ.get("STARTUP_GREETING", DEFAULT_STARTUP_GREETING).strip()


def _webrtc_calls_url(s2s_url: str) -> str:
    """Derive the WebRTC handshake URL from the pinned realtime URL.

    ``ws://host:port/v1/realtime`` -> ``http://host:port/v1/realtime/calls``
    (ws->http, wss->https; a bare host gets the default /v1/realtime path,
    mirroring the client's buildDirectWsUrl normalisation)."""
    s = s2s_url.strip()
    if not s.startswith(("ws://", "wss://", "http://", "https://")):
        s = "http://" + s
    parts = urlsplit(s)
    scheme = {"ws": "http", "wss": "https"}.get(parts.scheme, parts.scheme)
    path = parts.path if parts.path not in ("", "/") else "/v1/realtime"
    return urlunsplit((scheme, parts.netloc, path.rstrip("/") + "/calls", parts.query, ""))


SERPER_URL = "https://google.serper.dev/search"
# Cap results so the tool output stays small enough to feed back to the model.
MAX_RESULTS = 5
HERE = os.path.dirname(os.path.abspath(__file__))
LB_USER_AGENT = "speech-to-speech-demo"

app = FastAPI(title="s2s-demo")

# Wire HF OAuth before the app serves (no-op unless the OAuth env is present).
# Sign-in only matters when we're metering (prod Space), so gate it on that.
AUTH_ENABLED = LIMITER_ENABLED and auth.attach(app)


@app.on_event("startup")
async def _startup():
    """Stand up the usage DB and a periodic sweeper — metered (prod Space) only."""
    if not LIMITER_ENABLED:
        return
    limiter.init()
    asyncio.create_task(_sweeper())


async def _sweeper():
    while True:
        await asyncio.sleep(limiter.REAP_AFTER_SEC)
        try:
            await asyncio.to_thread(limiter.sweep)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("usage sweep failed: %r", exc)


class SearchRequest(BaseModel):
    query: str
    # Optional user-supplied key (fallback when the deploy has no server key).
    # Used for this request only; never stored.
    key: str | None = None


# GPT-SoVITS 代理（前端同源调用，避免跨域问题）
GSV_BASE = "http://127.0.0.1:9880"


@app.api_route("/api/gsv/{action:path}", methods=["GET", "POST"])
async def gsv_proxy(action: str, request: Request):
    """把 /api/gsv/* 转发到本地 GPT-SoVITS API (9880)。"""
    target = f"{GSV_BASE}/{action}"
    params = dict(request.query_params)
    body = await request.body()
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.request(
            request.method,
            target,
            params=params,
            content=body if body else None,
            headers={"Content-Type": request.headers.get("content-type", "application/json")},
        )
    return Response(content=resp.content, media_type=resp.headers.get("content-type", "application/json"))


# ---------- Agent 工具端点：给小雅提供真实电脑操作能力 ----------
import base64 as _b64
import subprocess as _subprocess
import webbrowser as _webbrowser
import shutil as _shutil

# 系统关键目录写保护（读取允许，写入/复制/移动拒绝，防止误改系统）
_SYSTEM_PROTECTED = [
    r"C:\Windows",
    r"C:\Program Files",
    r"C:\Program Files (x86)",
    r"C:\ProgramData",
]


def _is_system_protected(path: str) -> bool:
    """判断路径是否位于系统关键目录。"""
    p = os.path.normcase(os.path.abspath(path))
    for d in _SYSTEM_PROTECTED:
        dd = os.path.normcase(os.path.abspath(d))
        try:
            if os.path.commonpath([p, dd]) == dd:
                return True
        except ValueError:
            continue
    return False
# 可打开应用的 whitelist：name -> exe 路径
_AGENT_ALLOWED_APPS = {
    "notepad": r"C:\Windows\System32\notepad.exe",
    "calc": r"C:\Windows\System32\calc.exe",
    "mspaint": r"C:\Windows\System32\mspaint.exe",
    "explorer": r"C:\Windows\explorer.exe",
    "chrome": r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    "edge": r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
}
_MAX_READ_SIZE = 1_000_000  # 文件读取上限 1MB


def _is_local(request: Request) -> bool:
    """Agent 工具仅允许本机调用（防止局域网内被滥用启动程序/读写文件）。"""
    host = request.client.host if request.client else ""
    return host in ("127.0.0.1", "::1", "localhost")


def _agent_resolve_path(raw: str) -> str | None:
    """把路径规范化为绝对路径。已放开到整个电脑（系统关键目录的写操作另有保护）。"""
    if not raw:
        return None
    return os.path.normcase(os.path.abspath(os.path.expanduser(raw)))


class AgentFileRequest(BaseModel):
    action: str = "read"   # read | write | copy | move
    path: str              # 源路径（copy/move 时）
    content: str = ""
    dest: str = ""         # copy/move 的目标路径或目录


def _limit_text(content: str, max_len: int = 8000) -> str:
    if len(content) > max_len:
        return content[:max_len] + "\n…(内容过长已截断)"
    return content


def _read_docx(path: str) -> dict:
    """提取 Word .docx 文本（段落 + 表格）。"""
    from docx import Document
    doc = Document(path)
    parts = [p.text for p in doc.paragraphs if p.text.strip()]
    for table in doc.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells]
            if any(cells):
                parts.append(" | ".join(cells))
    content = _limit_text("\n".join(parts))
    return {"ok": True, "content": content or "(未提取到文字)", "size": len(content)}


def _read_xlsx(path: str) -> dict:
    """提取 Excel .xlsx 文本（各工作表单元格）。"""
    import openpyxl
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    parts = []
    try:
        for ws in wb.worksheets:
            parts.append(f"【工作表：{ws.title}】")
            for row in ws.iter_rows(values_only=True):
                vals = [str(v) for v in row if v is not None and str(v).strip()]
                if vals:
                    parts.append(" | ".join(vals))
    finally:
        wb.close()
    content = _limit_text("\n".join(parts))
    return {"ok": True, "content": content or "(未提取到文字)", "size": len(content)}


def _read_pptx(path: str) -> dict:
    """提取 PPT .pptx 文本（各页文本框）。"""
    from pptx import Presentation
    prs = Presentation(path)
    parts = []
    for slide in prs.slides:
        for shape in slide.shapes:
            if hasattr(shape, "text") and shape.text.strip():
                parts.append(shape.text)
    content = _limit_text("\n".join(parts))
    return {"ok": True, "content": content or "(未提取到文字)", "size": len(content)}


def _read_office(path: str, ext: str) -> dict:
    """按扩展名提取 Office 文档文本。"""
    try:
        if ext == ".docx":
            return _read_docx(path)
        if ext == ".xlsx":
            return _read_xlsx(path)
        if ext == ".pptx":
            return _read_pptx(path)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"文档解析失败: {e}")


def _read_legacy_office(path: str) -> dict:
    """旧版 .doc/.xls：用本机 Word/Excel COM 转换提取（需本机安装 Office/WPS）。"""
    try:
        import win32com.client
        ext = os.path.splitext(path)[1].lower()
        if ext == ".doc":
            app = win32com.client.Dispatch("Word.Application")
            app.Visible = False
            doc = app.Documents.Open(path, ReadOnly=True)
            text = doc.Content.Text
            doc.Close(False)
            app.Quit()
        elif ext == ".xls":
            app = win32com.client.Dispatch("Excel.Application")
            app.Visible = False
            wb = app.Workbooks.Open(path, ReadOnly=True)
            parts = []
            for ws in wb.Worksheets:
                used = ws.UsedRange
                for r in range(1, min(used.Rows.Count, 200) + 1):
                    vals = []
                    for c in range(1, min(used.Columns.Count, 40) + 1):
                        v = used.Cells(r, c).Value
                        if v is not None:
                            vals.append(str(v))
                    if vals:
                        parts.append(" | ".join(vals))
            wb.Close(False)
            app.Quit()
            text = "\n".join(parts)
        else:
            raise ValueError(f"不支持的旧版格式: {ext}")
        content = _limit_text(text)
        return {"ok": True, "content": content or "(未提取到文字)", "size": len(content)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"旧版文档(.doc/.xls)读取失败，需本机安装 Word/Excel: {e}")


_ocr_engine = None


def _get_ocr():
    """懒加载 RapidOCR（中文 OCR）。"""
    global _ocr_engine
    if _ocr_engine is None:
        from rapidocr_onnxruntime import RapidOCR
        _ocr_engine = RapidOCR()
    return _ocr_engine


def _ocr_image(path: str) -> dict:
    """OCR 识别图片中的文字（png/jpg 等）。"""
    try:
        with _memory_lock:
            ocr = _get_ocr()
            result, _ = ocr(path)
        text = "\n".join(item[1] for item in result) if result else ""
        content = _limit_text(text)
        return {"ok": True, "content": content or "(图片中未识别到文字)", "size": len(content)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"OCR 失败: {e}")


def _ocr_pdf(path: str) -> dict:
    """扫描版 PDF：渲染每页为图片再 OCR。"""
    try:
        import io
        import fitz
        with _memory_lock:
            ocr = _get_ocr()
            doc = fitz.open(path)
            texts = []
            for page in doc:
                pix = page.get_pixmap(dpi=200)
                result, _ = ocr(io.BytesIO(pix.tobytes("png")))
                if result:
                    texts.append("\n".join(item[1] for item in result))
            doc.close()
        content = _limit_text("\n".join(texts))
        return {"ok": True, "content": content or "(PDF 无法 OCR 提取文字)", "size": len(content)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"PDF OCR 失败: {e}")


_whisper_model = None


def _get_whisper():
    """懒加载 faster-whisper 模型（medium，复用 HF 缓存）。"""
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel
        _whisper_model = WhisperModel("medium", device="cuda", compute_type="float16")
    return _whisper_model


def _transcribe_audio(path: str) -> dict:
    """用 faster-whisper 转写音频文件为文字。"""
    try:
        with _memory_lock:  # 与记忆模块共用一把锁避免 GPU 并发
            model = _get_whisper()
            segments, _info = model.transcribe(path, beam_size=5)
            parts = [s.text.strip() for s in segments]
            text = " ".join(p for p in parts if p)
        content = _limit_text(text)
        return {"ok": True, "content": content or "(未识别到语音内容)", "size": len(content)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"音频转写失败: {e}")


def _read_pdf(path: str):
    """用 PyMuPDF 提取 PDF 文本（含中文），限 8KB。"""
    try:
        import fitz
    except ImportError:
        raise HTTPException(status_code=500, detail="PDF 支持未安装（pymupdf）。")
    try:
        doc = fitz.open(path)
        parts = []
        for page in doc:
            parts.append(page.get_text())
        doc.close()
        content = "\n".join(parts).strip()
        if not content:
            # 无文字层 → 扫描版 PDF，走 OCR
            return _ocr_pdf(path)
        content = _limit_text(content)
        return {"ok": True, "content": content or "(无法提取文本，可能是扫描版/图片型 PDF)", "size": len(content)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"PDF 读取失败: {e}")


@app.post("/api/agent/file")
async def agent_file(req: AgentFileRequest, request: Request):
    """读/写文件（限白名单目录，读上限 1MB；PDF 走文本提取）。仅限本机调用。"""
    if not _is_local(request):
        raise HTTPException(status_code=403, detail="Agent 工具仅允许本机访问。")
    p = _agent_resolve_path(req.path)
    if not p:
        raise HTTPException(status_code=403, detail="路径不在允许范围内（Documents/Desktop/Downloads）。")
    if req.action == "read":
        if not os.path.isfile(p):
            raise HTTPException(status_code=404, detail="文件不存在。")
        ext = os.path.splitext(p)[1].lower()
        if ext == ".pdf":
            return _read_pdf(p)
        if ext in (".docx", ".xlsx", ".pptx"):
            return _read_office(p, ext)
        if ext in (".doc", ".xls"):
            return _read_legacy_office(p)
        if ext in (".mp3", ".wav", ".m4a", ".flac", ".ogg", ".aac", ".opus", ".wma", ".amr"):
            return _transcribe_audio(p)
        if ext in (".png", ".jpg", ".jpeg", ".bmp", ".webp", ".gif", ".tiff", ".tif"):
            return _ocr_image(p)
        size = os.path.getsize(p)
        if size > _MAX_READ_SIZE:
            raise HTTPException(status_code=413, detail=f"文件过大（{size} 字节 > 1MB），拒绝读取。")
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                content = f.read()
            return {"ok": True, "content": content, "size": len(content)}
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"读取失败: {e}")
    if req.action == "write":
        if _is_system_protected(p):
            raise HTTPException(status_code=403, detail="系统关键目录（Windows/Program Files/ProgramData）禁止写入。")
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                f.write(req.content or "")
            return {"ok": True, "path": p, "bytes": len(req.content or "")}
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"写入失败: {e}")
    if req.action in ("copy", "move"):
        # 复制/移动文件到任意目录（系统关键目录写保护）
        src = p
        if not os.path.exists(src):
            raise HTTPException(status_code=404, detail="源文件不存在。")
        dst_raw = (req.dest or "").strip()
        if not dst_raw:
            raise HTTPException(status_code=400, detail="缺少目标路径(dest)。")
        dst = _agent_resolve_path(dst_raw)
        if not dst:
            raise HTTPException(status_code=403, detail="目标路径无效。")
        if _is_system_protected(dst):
            raise HTTPException(status_code=403, detail="系统关键目录（Windows/Program Files/ProgramData）禁止写入。")
        if req.action == "move" and _is_system_protected(src):
            raise HTTPException(status_code=403, detail="系统关键目录内的文件禁止移动。")
        if os.path.isdir(dst):
            dst = os.path.join(dst, os.path.basename(src))
        else:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
        try:
            if req.action == "copy":
                _shutil.copy2(src, dst)
            else:
                _shutil.move(src, dst)
            return {"ok": True, "from": src, "to": dst, "action": req.action}
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"{'复制' if req.action == 'copy' else '移动'}失败: {e}")
    raise HTTPException(status_code=400, detail="action 必须是 read/write/copy/move。")


def _resolve_lnk(lnk_path: str) -> str | None:
    """解析 .lnk 快捷方式的目标可执行文件。"""
    try:
        esc = lnk_path.replace("'", "''")
        script = (
            f"$sh = New-Object -ComObject WScript.Shell; "
            f"$lnk = $sh.CreateShortcut('{esc}'); "
            f"Write-Output $lnk.TargetPath"
        )
        r = _subprocess.run(["powershell", "-NoProfile", "-Command", script],
                            capture_output=True, text=True, timeout=10,
                            encoding="mbcs", errors="replace")
        target = (r.stdout or "").strip()
        if target and os.path.isfile(target):
            return target
    except Exception:
        pass
    return None


def _resolve_app_exe(name: str) -> str | None:
    """用 Everything 把应用名解析成可执行文件路径。
    中文应用名（如"微信"）exe 常是英文名，先搜 .lnk 快捷方式解析目标，再直接匹配 .exe。
    先查 _APP_ALIASES 别名表（如"电报"→telegram），再用解析后的名字搜索。"""
    if not os.path.isfile(_EVERYTHING_ES):
        return None
    qname = _APP_ALIASES.get(name.strip().lower(), name.strip())
    if not qname:
        return None
    for query in (f"{qname}.lnk", f"{qname}*.lnk", f"{qname}.exe", f"{qname}*.exe"):
        res = _es_search(query, limit=6)
        if not res:
            continue
        for p in res:
            pl = p.lower()
            if pl.endswith(".lnk"):
                target = _resolve_lnk(p)
                # 跳过卸载程序（如"卸载微信.lnk" -> Uninstall.exe）
                if target and "uninstall" not in target.lower() and "卸载" not in target:
                    return target
            if pl.endswith(".exe") and os.path.isfile(p) and "uninstall" not in pl:
                return p
    return None


def _launch_exe(exe: str) -> None:
    """启动可执行文件；若需要管理员权限（WinError 740）则回退 ShellExecute 触发 UAC 让用户确认。"""
    try:
        _subprocess.Popen([exe])
    except Exception:
        # WinError 740 请求的操作需要提升：用 ShellExecute 读取 manifest，弹 UAC 由用户确认提权
        os.startfile(exe)


class AgentAppRequest(BaseModel):
    name: str   # 应用名（PATH 查找）或可执行文件绝对路径


@app.post("/api/agent/openapp")
async def agent_openapp(req: AgentAppRequest, request: Request):
    """打开本机任意应用/文件/文件夹。仅限本机调用。
    - 可执行文件(.exe/.bat/.cmd/.com/.lnk)或 PATH 应用名：直接启动
    - 其他文件(如 .txt/.png/.mp4)或文件夹：用系统关联程序打开（如 txt -> 记事本）
    """
    if not _is_local(request):
        raise HTTPException(status_code=403, detail="Agent 工具仅允许本机访问。")
    key = (req.name or "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="缺少应用名/路径。")
    print(f"[openapp] name={key!r}", flush=True)  # 调试：记录小雅传的应用名
    # 1) 白名单别名
    exe = _AGENT_ALLOWED_APPS.get(key.lower())
    if exe:
        try:
            _launch_exe(exe)
            return {"ok": True, "app": key}
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"启动失败: {e}")
    # 2) 绝对路径：可执行直接启动，否则用关联程序打开（文档/图片/文件夹等）
    if os.path.exists(key):
        try:
            low = key.lower()
            if os.path.isfile(key) and low.endswith((".exe", ".bat", ".cmd", ".com", ".lnk")):
                _launch_exe(key)
            else:
                os.startfile(key)  # 用系统关联程序打开
            return {"ok": True, "app": key}
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"打开失败: {e}")
    # 3) PATH 应用名
    exe = _shutil.which(key)
    if exe:
        try:
            _launch_exe(exe)
            return {"ok": True, "app": key}
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"启动失败: {e}")
    # 4) 用 Everything 解析应用名（搜 .exe 或解析开始菜单 .lnk 快捷方式）
    exe = _resolve_app_exe(key)
    if exe:
        try:
            _launch_exe(exe)
            return {"ok": True, "app": key, "path": exe}
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"启动失败: {e}")
    raise HTTPException(status_code=400, detail=f"找不到应用/文件: {key}")


# 常见应用"中文全称 → 进程名"别名表（关闭时兜底，避免模型传变体名如"腾讯QQ"找不到）
_APP_ALIASES = {
    "腾讯qq": "qq", "qq聊天": "qq", "企鹅": "qq",
    "微信": "weixin", "wechat": "weixin", "微信电脑版": "weixin",
    "网易云音乐": "cloudmusic", "网易云": "cloudmusic",
    "steam": "steam", "steam平台": "steam",
    "钉钉": "dingtalk",
    "哔哩哔哩": "bilibili", "b站": "bilibili",
    "企业微信": "wexwork",
    "飞书": "feishu",
    "电报": "telegram", "telegram": "telegram",
    "记事本": "notepad", "计算器": "calc", "画图": "mspaint",
}


class AgentCloseRequest(BaseModel):
    name: str   # 进程名（如 notepad）或可执行文件名


@app.post("/api/agent/closeapp")
async def agent_closeapp(req: AgentCloseRequest, request: Request):
    """关闭本机运行中的应用或文档窗口。解析顺序：进程名 taskkill → Everything 解析应用名 taskkill → 窗口标题匹配。仅限本机调用。"""
    if not _is_local(request):
        raise HTTPException(status_code=403, detail="Agent 工具仅允许本机访问。")
    name = (req.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="缺少应用名/文件名。")
    print(f"[closeapp] name={name!r}", flush=True)  # 调试：记录小雅传的参数
    # Docker Desktop 专用：守护进程 com.docker.backend 会自动重启主程序，必须整栈按序杀
    if "docker" in name.lower():
        killed: list[str] = []
        for proc in ("com.docker.backend.exe", "Docker Desktop.exe",
                     "docker-sandbox.exe", "com.docker.build.exe"):
            try:
                r = _subprocess.run(["taskkill", "/IM", proc, "/T", "/F"],
                                    capture_output=True, text=True, timeout=20,
                                    encoding="mbcs", errors="replace")
                if r.returncode == 0:
                    killed.append(proc)
            except Exception:
                continue
        return {"ok": True, "app": name, "killed": "+".join(killed) or "docker-stack"}
    base = os.path.basename(name)
    stem = os.path.splitext(base)[0]
    try:
        # 收集候选进程名：别名映射 + 原始名 + Everything 解析出的真实 exe 名（中文应用名→英文进程名）
        proc_names: list[str] = []
        alias = _APP_ALIASES.get(name.lower().strip())
        if alias:
            ap = alias if alias.endswith(".exe") else alias + ".exe"
            proc_names.append(ap.lower())
        proc = base.lower()
        if not proc.endswith(".exe"):
            proc += ".exe"
        proc_names.append(proc)
        resolved = _resolve_app_exe(name)
        if resolved:
            rp = os.path.basename(resolved).lower()
            if rp not in proc_names:
                proc_names.append(rp)
        # 1) 按进程名强制结束（/T 杀进程树，多进程应用如 QQ 一次清干净）
        for pn in proc_names:
            r = _subprocess.run(["taskkill", "/IM", pn, "/T", "/F"],
                                capture_output=True, text=True, timeout=20,
                                encoding="mbcs", errors="replace")
            if r.returncode == 0:
                return {"ok": True, "app": name, "killed": pn}
        # 1.5) 进程名模糊匹配兜底：双向包含（ProcessName 含 name，或 name 含 ProcessName），
        #      如 'qq'/'QQ'/'qq聊天'/'腾讯QQ' 都能匹配到 QQ 进程
        low = name.lower()
        if len(low) >= 2:
            esc = low.replace("'", "''")
            script = (
                f"$hits = Get-Process | Where-Object {{ "
                f"$_.ProcessName -like '*{esc}*' -or '{esc}' -like ('*' + $_.ProcessName + '*') }}; "
                f"if ($hits) {{ $hits | Stop-Process -Force; Write-Output ('killed ' + $hits.Count) }} "
                f"else {{ Write-Output 'none' }}"
            )
            ps = _subprocess.run(["powershell", "-NoProfile", "-Command", script],
                                 capture_output=True, text=True, timeout=20,
                                 encoding="mbcs", errors="replace")
            out = (ps.stdout or "").strip().lower()
            if out.startswith("killed"):
                return {"ok": True, "app": name, "killed": f"by-proc:{low}"}
        # 2) 按窗口标题匹配：完整文件名（含扩展名）优先，再去扩展名
        for candidate in (base, stem):
            if not candidate:
                continue
            esc = candidate.replace("'", "''")
            script = (
                f"$hits = Get-Process | Where-Object {{ $_.MainWindowTitle -like '*{esc}*' }}; "
                f"if ($hits) {{ $hits | Stop-Process -Force; Write-Output ('killed ' + $hits.Count) }} "
                f"else {{ Write-Output 'none' }}"
            )
            ps = _subprocess.run(["powershell", "-NoProfile", "-Command", script],
                                 capture_output=True, text=True, timeout=20,
                                 encoding="mbcs", errors="replace")
            out = (ps.stdout or "").strip().lower()
            if out.startswith("killed"):
                return {"ok": True, "app": name, "killed": f"by-title:{candidate}"}
        # 3) 都没匹配到
        return {"ok": False, "app": name, "killed": "none-found"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"关闭失败: {e}")


class AgentBrowserRequest(BaseModel):
    url: str


@app.post("/api/agent/browser")
async def agent_browser(req: AgentBrowserRequest, request: Request):
    """用默认浏览器打开 http/https URL。仅限本机调用。"""
    if not _is_local(request):
        raise HTTPException(status_code=403, detail="Agent 工具仅允许本机访问。")
    url = (req.url or "").strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="只允许 http/https URL。")
    try:
        _webbrowser.open(url, new=2)
        return {"ok": True, "url": url}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"打开失败: {e}")


# ---------- 本机文件/程序搜索 ----------
import re as _re

# Everything 命令行版 es.exe：若存在则优先用它做全盘秒搜（需 Everything 正在运行）
_EVERYTHING_ES = r"C:\Users\Administrator\Desktop\Everything\es.exe"

_AGENT_SEARCH_ROOTS = [
    r"C:\Users\Administrator\Documents",
    r"C:\Users\Administrator\Desktop",
    r"C:\Users\Administrator\Downloads",
]
# 递归搜索时跳过的目录（避免进 AppData/node_modules 等慢目录）
_AGENT_SKIP_DIRS = {
    "appdata", "node_modules", ".git", ".venv", "venv", "cache", "temp", "tmp",
    "program files", "program files (x86)", "windows", "$recycle.bin",
    "system volume information", ".cache", "dist", "build",
}
_AGENT_PROGRAM_ROOTS = [
    r"C:\Users\Administrator\AppData\Roaming\Microsoft\Windows\Start Menu\Programs",
    r"C:\ProgramData\Microsoft\Windows\Start Menu\Programs",
    r"C:\Program Files",
    r"C:\Program Files (x86)",
]
_SEARCH_MAX = 40


class AgentSearchRequest(BaseModel):
    query: str
    scope: str = "all"   # file | program | all


def _es_search(q: str, limit: int = _SEARCH_MAX) -> list[str] | None:
    """用 Everything es.exe 做全盘文件名搜索；不可用时返回 None（调用方回退 os.walk）。"""
    if not os.path.isfile(_EVERYTHING_ES):
        return None
    try:
        # es.exe 输出是 ANSI/GBK（无 -utf8 开关），用 mbcs 解码；中文参数经 Windows API 传入不受影响
        r = _subprocess.run(
            [_EVERYTHING_ES, "-n", str(limit), q],
            capture_output=True, text=True, timeout=15,
            encoding="mbcs", errors="replace",
        )
        if r.returncode != 0:
            return None
        out = (r.stdout or "").strip()
        return out.splitlines() if out else []
    except Exception:
        return None


def _search_files(q: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for root in _AGENT_SEARCH_ROOTS:
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [
                d for d in dirnames
                if d.lower() not in _AGENT_SKIP_DIRS and not d.startswith(".")
            ]
            depth = dirpath[len(root):].count(os.sep)
            if depth > 4:
                dirnames[:] = []
                continue
            for fn in filenames:
                if q in fn.lower():
                    p = os.path.join(dirpath, fn)
                    if p not in seen:
                        seen.add(p)
                        found.append(p)
                        if len(found) >= _SEARCH_MAX:
                            return found
    return found


def _search_programs(q: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    # 1) PATH 下的 .exe
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if not d or not os.path.isdir(d):
            continue
        try:
            for fn in os.listdir(d):
                low = fn.lower()
                if low.endswith(".exe") and q in low and low not in seen:
                    seen.add(low)
                    found.append(os.path.join(d, fn))
                    if len(found) >= _SEARCH_MAX:
                        return found
        except OSError:
            continue
    # 2) 开始菜单快捷方式（.lnk/.url）
    for root in _AGENT_PROGRAM_ROOTS[:2]:
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            if dirpath[len(root):].count(os.sep) > 2:
                dirnames[:] = []
                continue
            for fn in filenames:
                if fn.lower().endswith((".lnk", ".url")) and q in fn.lower():
                    found.append(os.path.join(dirpath, fn))
                    if len(found) >= _SEARCH_MAX:
                        return found
    # 3) 常见安装目录下的程序目录名
    for root in _AGENT_PROGRAM_ROOTS[2:]:
        if not os.path.isdir(root):
            continue
        try:
            for fn in os.listdir(root):
                if q in fn.lower() and os.path.isdir(os.path.join(root, fn)):
                    found.append(os.path.join(root, fn))
                    if len(found) >= _SEARCH_MAX:
                        return found
        except OSError:
            continue
    return found


@app.post("/api/agent/search")
async def agent_search(req: AgentSearchRequest, request: Request):
    """按文件名关键词搜索本机文件或已安装程序。仅限本机调用。"""
    if not _is_local(request):
        raise HTTPException(status_code=403, detail="Agent 工具仅允许本机访问。")
    q = (req.query or "").strip().lower()
    if not q:
        raise HTTPException(status_code=400, detail="缺少搜索关键词。")
    scope = (req.scope or "all").lower()
    files: list[str] = []
    programs: list[str] = []
    if scope in ("file", "all"):
        # 优先 Everything 全盘秒搜；es.exe 不可用则回退目录遍历
        es = _es_search(q)
        files = es if es is not None else _search_files(q)
    if scope in ("program", "all"):
        programs = _search_programs(q)
    return {"query": req.query, "files": files, "programs": programs}


# ---------- 网页正文抓取 ----------
class AgentFetchUrlRequest(BaseModel):
    url: str


@app.post("/api/agent/fetchurl")
async def agent_fetchurl(req: AgentFetchUrlRequest, request: Request):
    """抓取网页并返回可读文本。仅限本机调用。"""
    if not _is_local(request):
        raise HTTPException(status_code=403, detail="Agent 工具仅允许本机访问。")
    url = (req.url or "").strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="只允许 http/https URL。")
    try:
        async with httpx.AsyncClient(
            timeout=15.0, follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
        ) as client:
            resp = await client.get(url)
        resp.encoding = resp.encoding or "utf-8"
        html = resp.text
        html = _re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
        text = _re.sub(r"(?is)<[^>]+>", " ", html)
        text = _re.sub(r"\s+", " ", text).strip()
        if len(text) > 8000:
            text = text[:8000] + "\n…(内容过长已截断)"
        return {"ok": True, "url": url, "text": text or "(页面无可读文本)"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"抓取失败: {e}")


# ---------- 文件内容搜索（grep） ----------
_AGENT_TEXT_EXTS = {
    ".txt", ".md", ".py", ".js", ".jsx", ".ts", ".tsx", ".html", ".htm", ".css",
    ".json", ".yaml", ".yml", ".xml", ".csv", ".log", ".ini", ".cfg", ".conf",
    ".sh", ".bat", ".toml", ".rst", ".tex", ".sql", ".c", ".h", ".cpp", ".java",
}


class AgentGrepRequest(BaseModel):
    query: str
    dir: str = ""     # 可选：限定搜索目录（必须在白名单根下）
    name: str = ""    # 可选：文件名过滤


@app.post("/api/agent/grep")
async def agent_grep(req: AgentGrepRequest, request: Request):
    """在文本文件内容中搜索关键词。仅限本机调用。"""
    if not _is_local(request):
        raise HTTPException(status_code=403, detail="Agent 工具仅允许本机访问。")
    q = (req.query or "").strip().lower()
    if not q:
        raise HTTPException(status_code=400, detail="缺少搜索关键词。")
    roots: list[str] = []
    if req.dir:
        p = os.path.normcase(os.path.abspath(req.dir))
        if not any(
            os.path.commonpath([p, os.path.normcase(os.path.abspath(d))]) == os.path.normcase(os.path.abspath(d))
            for d in _AGENT_SEARCH_ROOTS if os.path.isdir(d)
        ):
            raise HTTPException(status_code=403, detail="目录不在允许范围内。")
        roots = [p]
    else:
        roots = [d for d in _AGENT_SEARCH_ROOTS if os.path.isdir(d)]
    found: list[str] = []
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [
                d for d in dirnames
                if d.lower() not in _AGENT_SKIP_DIRS and not d.startswith(".")
            ]
            if dirpath[len(root):].count(os.sep) > 4:
                dirnames[:] = []
                continue
            for fn in filenames:
                if req.name and req.name.lower() not in fn.lower():
                    continue
                ext = os.path.splitext(fn)[1].lower()
                if ext not in _AGENT_TEXT_EXTS:
                    continue
                fp = os.path.join(dirpath, fn)
                try:
                    if os.path.getsize(fp) > 500_000:
                        continue
                    with open(fp, encoding="utf-8", errors="ignore") as f:
                        for lineno, line in enumerate(f, 1):
                            if q in line.lower():
                                found.append(f"{fp}:{lineno}: {line.strip()[:150]}")
                                break
                except Exception:
                    continue
                if len(found) >= _SEARCH_MAX:
                    return {"query": req.query, "matches": found}
    return {"query": req.query, "matches": found}


# ---------- 只读命令执行 ----------
_ALLOWED_READONLY_COMMANDS = {
    "tasklist", "netstat", "ipconfig", "systeminfo", "ver", "whoami", "hostname",
    "path", "dir", "ping", "where", "echo", "vol", "date", "time", "getmac",
    "route", "driverquery", "chcp",
}
_FORBIDDEN_CMD_CHARS = set(";&|<>`")


class AgentCommandRequest(BaseModel):
    command: str


@app.post("/api/agent/runcommand")
async def agent_runcommand(req: AgentCommandRequest, request: Request):
    """执行白名单内的只读诊断命令。仅限本机调用。"""
    if not _is_local(request):
        raise HTTPException(status_code=403, detail="Agent 工具仅允许本机访问。")
    cmd = (req.command or "").strip()
    if not cmd:
        raise HTTPException(status_code=400, detail="缺少命令。")
    if any(c in _FORBIDDEN_CMD_CHARS for c in cmd):
        raise HTTPException(status_code=400, detail="命令含不允许的字符（;&|<>`）。")
    parts = cmd.split()
    if not parts or parts[0].lower() not in _ALLOWED_READONLY_COMMANDS:
        raise HTTPException(
            status_code=400,
            detail=f"只允许只读命令: {', '.join(sorted(_ALLOWED_READONLY_COMMANDS))}",
        )
    try:
        argv = ["cmd", "/c", cmd] if parts[0].lower() == "dir" else cmd.split()
        # cmd 系统命令输出是 ANSI/GBK，用 mbcs 解码避免乱码
        r = _subprocess.run(argv, capture_output=True, text=True, timeout=20,
                            encoding="mbcs", errors="replace")
        out = (r.stdout or "").strip() or (r.stderr or "").strip()
        if len(out) > 6000:
            out = out[:6000] + "\n…(输出过长已截断)"
        return {"ok": True, "output": out or "(无输出)"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"命令执行失败: {e}")


# ---------- 剪贴板读写 ----------
class AgentClipboardRequest(BaseModel):
    action: str = "read"   # read | write
    content: str = ""


@app.post("/api/agent/clipboard")
async def agent_clipboard(req: AgentClipboardRequest, request: Request):
    """读/写系统剪贴板。仅限本机调用。"""
    if not _is_local(request):
        raise HTTPException(status_code=403, detail="Agent 工具仅允许本机访问。")
    if req.action == "read":
        try:
            script = "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; Get-Clipboard"
            r = _subprocess.run(["powershell", "-NoProfile", "-Command", script],
                                capture_output=True, text=True, timeout=15,
                                encoding="utf-8", errors="replace")
            text = (r.stdout or "").strip()
            return {"ok": True, "content": text or "(剪贴板为空)"}
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"读取剪贴板失败: {e}")
    if req.action == "write":
        try:
            b64 = _b64.b64encode((req.content or "").encode("utf-8")).decode("ascii")
            script = (
                f"Set-Clipboard -Value "
                f"([System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String('{b64}')))"
            )
            _subprocess.run(["powershell", "-NoProfile", "-Command", script],
                            capture_output=True, text=True, timeout=15,
                            encoding="utf-8", errors="replace")
            return {"ok": True, "bytes": len(req.content or "")}
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"写入剪贴板失败: {e}")
    raise HTTPException(status_code=400, detail="action 必须是 read 或 write。")


# ---------- 系统信息 ----------
@app.post("/api/agent/systeminfo")
async def agent_systeminfo(request: Request):
    """返回 CPU/内存/磁盘/进程数等系统状态。仅限本机调用。"""
    if not _is_local(request):
        raise HTTPException(status_code=403, detail="Agent 工具仅允许本机访问。")
    script = (
        "$cpu = Get-CimInstance Win32_Processor | Select-Object -First 1; "
        "$os = Get-CimInstance Win32_OperatingSystem; "
        "$mt = [math]::Round($os.TotalVisibleMemorySize/1MB,1); "
        "$mf = [math]::Round($os.FreePhysicalMemory/1MB,1); "
        "$disks = Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3' | ForEach-Object { "
        "  '{0}: 共{1}GB 空闲{2}GB' -f $_.DeviceID, [math]::Round($_.Size/1GB,1), [math]::Round($_.FreeSpace/1GB,1) }; "
        "$procs = (Get-Process).Count; "
        "Write-Output ('CPU: {0} | 核{1} | 负载{2}%' -f $cpu.Name, $cpu.NumberOfCores, $cpu.LoadPercentage); "
        "Write-Output ('内存: 总{0}GB 空闲{1}GB' -f $mt, $mf); "
        "Write-Output ('磁盘: ' + ($disks -join '; ')); "
        "Write-Output ('进程数: {0}' -f $procs)"
    )
    try:
        r = _subprocess.run(["powershell", "-NoProfile", "-Command", script],
                            capture_output=True, text=True, timeout=25,
                            encoding="utf-8", errors="replace")
        out = (r.stdout or "").strip()
        return {"ok": True, "info": out or "(无输出)"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取系统信息失败: {e}")


# ---------- 向量记忆（P2：Qdrant local + bge 中文 embedding）----------
import hashlib as _hashlib
import threading as _threading

_MEMORY_DIR = r"C:\Users\Administrator\.xiaoya_memory"
_MEMORY_COLLECTION = "xiaoya_memory"
_MEMORY_MODEL = r"C:\Users\Administrator\models\bge-small-zh-v1.5"
_MEMORY_DIM = 512

_memory_lock = _threading.Lock()
_memory_client = None
_memory_encoder = None


def _mem_point_id(text: str) -> int:
    """用 md5 生成稳定 point id（去重：同文本 upsert 覆盖）。"""
    return int(_hashlib.md5(text.encode("utf-8")).hexdigest()[:16], 16)


def _get_memory_client():
    global _memory_client
    if _memory_client is None:
        from qdrant_client import QdrantClient
        from qdrant_client.models import Distance, VectorParams
        _memory_client = QdrantClient(path=_MEMORY_DIR)
        cols = [c.name for c in _memory_client.get_collections().collections]
        if _MEMORY_COLLECTION not in cols:
            _memory_client.create_collection(
                collection_name=_MEMORY_COLLECTION,
                vectors_config=VectorParams(size=_MEMORY_DIM, distance=Distance.COSINE),
            )
    return _memory_client


def _get_memory_encoder():
    global _memory_encoder
    if _memory_encoder is None:
        import torch
        from sentence_transformers import SentenceTransformer
        device = "cuda" if torch.cuda.is_available() else "cpu"
        _memory_encoder = SentenceTransformer(_MEMORY_MODEL, device=device)
    return _memory_encoder


def _memory_init():
    _get_memory_client()
    _get_memory_encoder()


class MemoryAddRequest(BaseModel):
    text: str | list[str]


class MemorySearchRequest(BaseModel):
    query: str
    limit: int = 8


class MemoryDelRequest(BaseModel):
    text: str


@app.post("/api/memory/add")
async def memory_add(req: MemoryAddRequest, request: Request):
    """把记忆文本向量化存入本地 Qdrant。仅限本机调用。"""
    if not _is_local(request):
        raise HTTPException(status_code=403, detail="仅允许本机访问。")
    texts = [req.text] if isinstance(req.text, str) else (req.text or [])
    texts = [t.strip() for t in texts if t and t.strip()]
    if not texts:
        return {"ok": True, "added": 0}
    with _memory_lock:
        _memory_init()
        from qdrant_client.models import PointStruct
        enc = _get_memory_encoder()
        vecs = enc.encode(texts, normalize_embeddings=True)
        client = _get_memory_client()
        points = [
            PointStruct(id=_mem_point_id(t), vector=v.tolist(), payload={"text": t})
            for t, v in zip(texts, vecs)
        ]
        client.upsert(_MEMORY_COLLECTION, points=points)
        return {"ok": True, "added": len(points)}


@app.post("/api/memory/search")
async def memory_search(req: MemorySearchRequest, request: Request):
    """按语义检索与 query 最相关的记忆。仅限本机调用。"""
    if not _is_local(request):
        raise HTTPException(status_code=403, detail="仅允许本机访问。")
    q = (req.query or "").strip()
    if not q:
        return {"ok": True, "memories": []}
    with _memory_lock:
        _memory_init()
        enc = _get_memory_encoder()
        qv = enc.encode([q], normalize_embeddings=True)[0]
        client = _get_memory_client()
        res = client.query_points(_MEMORY_COLLECTION, query=qv.tolist(), limit=max(1, req.limit))
        memories = [p.payload["text"] for p in res.points if p.payload]
        return {"ok": True, "memories": memories}


@app.get("/api/memory/list")
async def memory_list(request: Request):
    """列出所有记忆。仅限本机调用。"""
    if not _is_local(request):
        raise HTTPException(status_code=403, detail="仅允许本机访问。")
    with _memory_lock:
        _memory_init()
        client = _get_memory_client()
        res = client.scroll(_MEMORY_COLLECTION, limit=5000)
        items = [p.payload["text"] for p in res[0] if p.payload]
        return {"ok": True, "memories": items}


@app.post("/api/memory/del")
async def memory_del(req: MemoryDelRequest, request: Request):
    """删除一条记忆（按文本精确匹配）。仅限本机调用。"""
    if not _is_local(request):
        raise HTTPException(status_code=403, detail="仅允许本机访问。")
    text = (req.text or "").strip()
    if not text:
        return {"ok": True}
    with _memory_lock:
        _memory_init()
        from qdrant_client.models import FieldCondition, Filter, MatchValue
        client = _get_memory_client()
        client.delete(
            collection_name=_MEMORY_COLLECTION,
            points_selector=Filter(must=[FieldCondition(key="text", match=MatchValue(value=text))]),
        )
        return {"ok": True}


@app.post("/api/memory/clear")
async def memory_clear(request: Request):
    """清空所有记忆。仅限本机调用。"""
    if not _is_local(request):
        raise HTTPException(status_code=403, detail="仅允许本机访问。")
    with _memory_lock:
        _memory_init()
        from qdrant_client.models import Distance, VectorParams
        client = _get_memory_client()
        client.delete_collection(_MEMORY_COLLECTION)
        client.create_collection(
            collection_name=_MEMORY_COLLECTION,
            vectors_config=VectorParams(size=_MEMORY_DIM, distance=Distance.COSINE),
        )
        return {"ok": True}


# ---------- 文件上传（聊天框传文档/录音/图片给小雅解析）----------
_UPLOAD_DIR = r"C:\Users\Administrator\Documents\s2s\uploads"
_ALLOWED_UPLOAD_EXTS = {
    ".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".txt", ".md", ".csv", ".json",
    ".mp3", ".wav", ".m4a", ".flac", ".ogg", ".aac", ".opus", ".wma", ".amr",
    ".png", ".jpg", ".jpeg", ".bmp", ".webp", ".gif", ".tiff", ".tif",
}
_MAX_UPLOAD = 50 * 1024 * 1024  # 50MB


@app.post("/api/upload")
async def upload_file(request: Request, file: UploadFile = File(...)):
    """接收用户上传的文件，保存到 uploads 目录，返回路径供 file 工具读取。仅限本机调用。"""
    if not _is_local(request):
        raise HTTPException(status_code=403, detail="仅允许本机访问。")
    filename = os.path.basename(file.filename or "upload")
    ext = os.path.splitext(filename)[1].lower()
    if ext not in _ALLOWED_UPLOAD_EXTS:
        raise HTTPException(status_code=400, detail=f"不支持的文件类型: {ext or '(无扩展名)'}")
    content = await file.read()
    if len(content) > _MAX_UPLOAD:
        raise HTTPException(status_code=413, detail="文件过大（>50MB）")
    os.makedirs(_UPLOAD_DIR, exist_ok=True)
    stem, e = os.path.splitext(filename)
    dest = os.path.join(_UPLOAD_DIR, f"{stem}_{int(time.time())}{e}")
    with open(dest, "wb") as f:
        f.write(content)
    return {"ok": True, "path": dest, "filename": filename, "size": len(content)}


# ---------- 文档生成 docgen（美观 Word/PDF）----------
_DOC_PRIMARY = "8B5CF6"   # 主色紫
_DOC_ACCENT = "EC4899"    # 强调粉
_DOC_DARK = "2D3748"      # 正文深蓝灰


def _parse_blocks(content: str) -> list[tuple[str, str]]:
    """简化 markdown：## 标题 / - 列表 / 普通段落。"""
    blocks: list[tuple[str, str]] = []
    for line in (content or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("### "):
            blocks.append(("h3", line[4:].strip()))
        elif line.startswith("## "):
            blocks.append(("h2", line[3:].strip()))
        elif line.startswith("# "):
            blocks.append(("h1", line[2:].strip()))
        elif line.startswith("- ") or line.startswith("* "):
            blocks.append(("li", line[2:].strip()))
        else:
            blocks.append(("para", line))
    return blocks


def _set_run_font(run, name="微软雅黑"):
    from docx.oxml.ns import qn
    run.font.name = name
    try:
        run._element.rPr.rFonts.set(qn("w:eastAsia"), name)
    except Exception:
        pass


def _gen_docx(title: str, subtitle: str, blocks, out_path: str) -> str:
    """用 python-docx 生成美观 Word 文档。"""
    from docx import Document
    from docx.shared import Pt, RGBColor, Cm
    from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    primary = RGBColor.from_string(_DOC_PRIMARY)
    accent = RGBColor.from_string(_DOC_ACCENT)
    dark = RGBColor.from_string(_DOC_DARK)

    doc = Document()
    normal = doc.styles["Normal"]
    normal.font.name = "微软雅黑"
    normal.font.size = Pt(11)
    normal.font.color.rgb = dark
    try:
        normal.element.rPr.rFonts.set(qn("w:eastAsia"), "微软雅黑")
    except Exception:
        pass

    # 标题
    t = doc.add_paragraph()
    t.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = t.add_run(title or "文档")
    r.font.size = Pt(24); r.bold = True; r.font.color.rgb = primary
    _set_run_font(r)

    # 副标题
    if subtitle:
        st = doc.add_paragraph()
        st.alignment = WD_ALIGN_PARAGRAPH.CENTER
        sr = st.add_run(subtitle)
        sr.font.size = Pt(12); sr.italic = True; sr.font.color.rgb = accent
        _set_run_font(sr)

    # 分隔线
    p = doc.add_paragraph()
    pPr = p._p.get_or_add_pPr()
    pBdr = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single"); bottom.set(qn("w:sz"), "8"); bottom.set(qn("w:color"), _DOC_PRIMARY)
    pBdr.append(bottom)
    pPr.append(pBdr)

    # 内容
    for typ, text in blocks:
        if typ in ("h1", "h2", "h3"):
            hp = doc.add_paragraph()
            hp.paragraph_format.space_before = Pt(12)
            hp.paragraph_format.space_after = Pt(4)
            hr = hp.add_run(text)
            hr.bold = True
            hr.font.color.rgb = primary if typ != "h3" else accent
            hr.font.size = Pt({ "h1": 18, "h2": 15, "h3": 13 }[typ])
            _set_run_font(hr)
        elif typ == "li":
            lp = doc.add_paragraph(style="List Bullet")
            lp.paragraph_format.space_after = Pt(2)
            lr = lp.add_run(text)
            lr.font.size = Pt(11)
            _set_run_font(lr)
        else:
            pp = doc.add_paragraph()
            pp.paragraph_format.line_spacing_rule = WD_LINE_SPACING.ONE_POINT_FIVE
            pp.paragraph_format.space_after = Pt(6)
            pr = pp.add_run(text)
            pr.font.size = Pt(11)
            _set_run_font(pr)

    # 页脚页码
    footer = doc.sections[0].footer
    fp = footer.paragraphs[0]
    fp.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = fp.add_run()
    fldChar1 = OxmlElement("w:fldChar"); fldChar1.set(qn("w:fldCharType"), "begin")
    instrText = OxmlElement("w:instrText"); instrText.set(qn("xml:space"), "preserve"); instrText.text = "PAGE"
    fldChar2 = OxmlElement("w:fldChar"); fldChar2.set(qn("w:fldCharType"), "end")
    run._r.append(fldChar1); run._r.append(instrText); run._r.append(fldChar2)
    run.font.size = Pt(9); run.font.color.rgb = RGBColor.from_string("94A3B8")

    doc.save(out_path)
    return out_path


def _gen_pdf(title: str, subtitle: str, blocks, out_path: str) -> str:
    """用 reportlab 生成美观 PDF（中文黑体）。"""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib.colors import HexColor
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, ListFlowable, ListItem, HRFlowable

    fontfile = r"C:\Windows\Fonts\simhei.ttf"
    pdfmetrics.registerFont(TTFont("CnFont", fontfile))

    primary = HexColor("#" + _DOC_PRIMARY)
    accent = HexColor("#" + _DOC_ACCENT)
    dark = HexColor("#" + _DOC_DARK)

    def make_style(name, **kw):
        base = dict(fontName="CnFont", textColor=dark)
        base.update(kw)
        return ParagraphStyle(name, **base)

    s_title = make_style("title", fontSize=24, leading=30, textColor=primary, alignment=1, spaceAfter=6)
    s_sub = make_style("sub", fontSize=12, leading=16, textColor=accent, alignment=1, spaceAfter=10)
    s_h1 = make_style("h1", fontSize=18, leading=24, textColor=primary, spaceBefore=14, spaceAfter=6)
    s_h2 = make_style("h2", fontSize=15, leading=20, textColor=primary, spaceBefore=12, spaceAfter=4)
    s_h3 = make_style("h3", fontSize=13, leading=18, textColor=accent, spaceBefore=10, spaceAfter=4)
    s_para = make_style("para", fontSize=11, leading=18, spaceAfter=6)
    s_li = make_style("li", fontSize=11, leading=18, spaceAfter=2)

    def page_cb(canvas, doc_):
        canvas.saveState()
        canvas.setFont("CnFont", 9)
        canvas.setFillColor(HexColor("#94A3B8"))
        canvas.drawCentredString(A4[0] / 2, 12 * mm, f"- {canvas.getPageNumber()} -")
        canvas.restoreState()

    doc = SimpleDocTemplate(out_path, pagesize=A4,
                            leftMargin=25 * mm, rightMargin=25 * mm,
                            topMargin=20 * mm, bottomMargin=20 * mm)
    story = [Paragraph(title or "文档", s_title)]
    if subtitle:
        story.append(Paragraph(subtitle, s_sub))
    story.append(HRFlowable(width="100%", thickness=1.2, color=primary, spaceBefore=2, spaceAfter=12))
    for typ, text in blocks:
        if typ == "h1":
            story.append(Paragraph(text, s_h1))
        elif typ == "h2":
            story.append(Paragraph(text, s_h2))
        elif typ == "h3":
            story.append(Paragraph(text, s_h3))
        elif typ == "li":
            story.append(ListFlowable([ListItem(Paragraph(text, s_li), leftIndent=10)], bulletType="bullet", start="•"))
        else:
            story.append(Paragraph(text, s_para))
    doc.build(story, onFirstPage=page_cb, onLaterPages=page_cb)
    return out_path


class DocGenRequest(BaseModel):
    format: str = "docx"   # docx | pdf
    title: str = ""
    subtitle: str = ""
    content: str = ""
    filename: str = ""


@app.post("/api/agent/docgen")
async def agent_docgen(req: DocGenRequest, request: Request):
    """生成排版美观的 Word/PDF 文档，保存到 uploads 目录。仅限本机调用。"""
    if not _is_local(request):
        raise HTTPException(status_code=403, detail="仅允许本机访问。")
    fmt = (req.format or "docx").lower()
    if fmt not in ("docx", "pdf"):
        raise HTTPException(status_code=400, detail="format 必须是 docx 或 pdf。")
    blocks = _parse_blocks(req.content)
    os.makedirs(_UPLOAD_DIR, exist_ok=True)
    name = (req.filename or req.title or "文档").strip() or "文档"
    safe = "".join(ch for ch in name if ch not in '\\/:*?"<>|').strip() or "文档"
    out = os.path.join(_UPLOAD_DIR, f"{safe}_{int(time.time())}.{fmt}")
    try:
        if fmt == "docx":
            _gen_docx(req.title, req.subtitle, blocks, out)
        else:
            _gen_pdf(req.title, req.subtitle, blocks, out)
        return {"ok": True, "path": out, "format": fmt}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"文档生成失败: {e}")


# ---------- LLM 代理（前端调 DeepSeek，key 在服务端环境变量）----------
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "").strip()


@app.post("/api/llm")
async def llm_proxy(request: Request):
    """把前端请求代理到 DeepSeek chat/completions（key 不落前端/代码）。仅限本机调用。"""
    if not _is_local(request):
        raise HTTPException(status_code=403, detail="仅允许本机访问。")
    body = await request.json()
    url = "https://api.deepseek.com/v1/chat/completions"
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {DEEPSEEK_API_KEY}"}
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(url, json=body, headers=headers)
        return Response(content=resp.content, media_type="application/json")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"LLM 代理失败: {e}")


@app.get("/api/config")
def config():
    """Client bootstrap: whether web search is available, whether the deploy runs
    behind a load balancer (so the browser uses the /api/session proxy + limiter),
    whether HF sign-in is available, and whether the user may instead set a direct
    s2s server URL. The LB address itself is intentionally NOT included."""
    return {
        "search": bool(SERPER_KEY),
        "lb": bool(LOAD_BALANCER_URL),
        "allowDirect": not LOAD_BALANCER_URL,
        # Deploy-pinned direct s2s URL (empty when unset). Not a secret: the
        # browser dials it itself, and Settings shows it locked.
        "s2sUrl": SPEECH_TO_SPEECH_URL,
        # WebRTC transport availability: the /api/calls proxy only forwards to
        # the env-pinned URL (never a client-supplied one), so the toggle is
        # offered exactly when that URL exists.
        "rtc": bool(SPEECH_TO_SPEECH_URL),
        "iceServers": RTC_ICE_SERVERS,
        "startupGreeting": STARTUP_GREETING,
        "auth": AUTH_ENABLED,
    }


@app.get("/api/me")
async def me(request: Request):
    """Login state, tier, and remaining daily budget. Only meaningful in LB mode;
    sets the anonymous tracking cookie when first seen."""
    if not LIMITER_ENABLED:
        return {"enabled": False}
    view = auth.user_view(request)
    tier, keys, set_cookie = auth.resolve_identity(request)
    unlimited = limiter.budget_for(tier) is None
    rem = None if unlimited else await asyncio.to_thread(limiter.remaining, keys, tier)
    out = {
        "enabled": True,
        "auth": AUTH_ENABLED,
        **view,
        "remainingSec": rem,
        "limitSec": limiter.budget_for(tier),
        "loginUrl": auth.OAUTH_LOGIN_PATH if AUTH_ENABLED else None,
        "logoutUrl": auth.OAUTH_LOGOUT_PATH if AUTH_ENABLED else None,
    }
    resp = JSONResponse(out)
    if set_cookie:
        auth.set_anon_cookie(resp, set_cookie)
    return resp


@app.post("/api/search")
async def search(req: SearchRequest):
    """Proxy a Google search via Serper.dev. The key stays on the server unless
    the user brought their own (then theirs is used for this request only)."""
    query = (req.query or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="Empty query.")

    key = (req.key or "").strip() or SERPER_KEY
    if not key:
        # No server key and the user didn't supply one — search is unavailable.
        raise HTTPException(status_code=503, detail="Search is not configured.")

    headers = {"X-API-KEY": key, "Content-Type": "application/json"}
    payload = {"q": query, "num": MAX_RESULTS}
    try:
        async with httpx.AsyncClient(timeout=12.0) as http:
            resp = await http.post(SERPER_URL, headers=headers, json=payload)
    except httpx.RequestError as exc:
        logger.warning("Serper unreachable: %r", exc)
        raise HTTPException(status_code=502, detail="Search provider unreachable.")

    if resp.status_code != 200:
        # Serper's error body carries the real reason (e.g. "Not enough
        # credits") and contains no key, so it's safe to log and relay.
        body = resp.text[:300]
        logger.warning("Serper error %s: %s", resp.status_code, body)
        msg = None
        try:
            msg = resp.json().get("message")
        except Exception:
            pass
        detail = f"Search provider error ({resp.status_code})"
        if msg:
            detail += f": {msg}"
        raise HTTPException(status_code=502, detail=detail)

    data = resp.json()
    results = []
    for item in (data.get("organic") or [])[:MAX_RESULTS]:
        results.append(
            {
                "title": item.get("title", ""),
                "snippet": item.get("snippet", ""),
                "url": item.get("link", ""),
            }
        )

    # A direct answer when Google has one — saves the model a hop.
    box = data.get("answerBox") or {}
    answer = box.get("answer") or box.get("snippet") or None
    if not answer:
        kg = data.get("knowledgeGraph") or {}
        answer = kg.get("description") or None

    return JSONResponse({"query": query, "answer": answer, "results": results})


@app.post("/api/calls")
async def calls(request: Request):
    """Proxy the WebRTC SDP handshake to the pinned s2s server.

    The browser can't POST /v1/realtime/calls cross-origin (the s2s server has
    no CORS middleware, and an application/sdp POST is preflighted), so it
    posts the offer here and we forward it server-side. Only the signaling hop
    goes through this proxy — the negotiated audio/data-channel media flows
    directly between the browser and the s2s server.

    Deliberately forwards ONLY to SPEECH_TO_SPEECH_URL: honouring a
    client-supplied target would make this an open proxy (SSRF). No env pin,
    no WebRTC — the client keeps such setups on the WebSocket transport."""
    if not SPEECH_TO_SPEECH_URL:
        raise HTTPException(status_code=404, detail="Not found.")

    offer = await request.body()
    url = _webrtc_calls_url(SPEECH_TO_SPEECH_URL)
    try:
        # Generous timeout: the s2s server waits for its own ICE gathering
        # (up to ~5 s) before returning the answer.
        async with httpx.AsyncClient(timeout=15.0) as http:
            resp = await http.post(url, headers={"Content-Type": "application/sdp"}, content=offer)
    except httpx.RequestError as exc:
        logger.warning("s2s calls endpoint unreachable: %r", exc)
        raise HTTPException(status_code=502, detail="Speech service unreachable.")

    # Relay the answer (or the error body) as-is; keep the Location header the
    # s2s server sets on success (the call id, per the OpenAI GA contract).
    headers = {}
    if "location" in resp.headers:
        headers["Location"] = resp.headers["location"]
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type", "application/sdp"),
        headers=headers,
    )


@app.post("/api/session")
async def session(request: Request):
    """Proxy the session handshake to the load balancer, keeping its URL secret,
    and meter conversation time by tier.

    The browser POSTs here (same-origin); we resolve the caller's tier, refuse if
    today's budget is already spent (402), otherwise POST <LOAD_BALANCER_URL>/session
    and relay the JSON back. The LB body carries a per-session `connect_url`
    (compute host + short-lived token) the browser must dial directly — that one
    URL is unavoidably exposed, but the stable load-balancer address is not. On a
    successful grant we reserve the first time chunk against the day's budget."""
    if not LOAD_BALANCER_URL:
        # No LB configured — this deploy is direct-mode only; the browser should
        # never call this. 404 so it's indistinguishable from a missing route.
        raise HTTPException(status_code=404, detail="Not found.")

    tier, keys, set_cookie = auth.resolve_identity(request)
    # Metering runs only on the deployed Space; off-Space the LB still proxies but
    # nothing is tracked. Within metering, unlimited tiers (pro, org) aren't either.
    tracked = LIMITER_ENABLED and limiter.budget_for(tier) is not None

    # Refuse before troubling the LB if the day's budget is already gone. Done
    # here (at enqueue) so we never put a user who can't talk into the queue.
    if tracked:
        rem = await asyncio.to_thread(limiter.remaining, keys, tier)
        if rem is not None and rem <= 0:
            resp = JSONResponse(
                {"tier": tier, "reason": "limit", "remainingSec": 0}, status_code=402
            )
            if set_cookie:
                auth.set_anon_cookie(resp, set_cookie)
            return resp

    url = f"{LOAD_BALANCER_URL.rstrip('/')}/session"
    try:
        async with httpx.AsyncClient(timeout=15.0) as http:
            lb = await http.post(
                url,
                headers=_load_balancer_headers(request),
                content="{}",
            )
    except httpx.RequestError as exc:
        logger.warning("Load balancer unreachable: %r", exc)
        raise HTTPException(status_code=502, detail="Speech service unreachable.")

    # The queue is full: the LB replies 503 {state:"at_capacity"}. Relay it as-is
    # so the client shows a soft "try again shortly", not a hard error.
    if lb.status_code == 503:
        body = _safe_json(lb)
        if body.get("state") == "at_capacity":
            resp = JSONResponse({"state": "at_capacity"}, status_code=503)
            if set_cookie:
                auth.set_anon_cookie(resp, set_cookie)
            return resp

    if lb.status_code != 200:
        # The LB's error body may name the reason (e.g. capacity); it carries no
        # secret, so relay a trimmed copy.
        logger.warning("Session handshake failed %s: %s", lb.status_code, lb.text[:300])
        raise HTTPException(status_code=502, detail=f"Session handshake failed ({lb.status_code}).")

    data = lb.json()

    # Busy pool: the LB queued us. Relay the ticket untouched — crucially with NO
    # reservation, so waiting in line never costs the day's budget.
    if data.get("state") == "queued":
        data["tier"] = tier
        resp = JSONResponse(data)
        if set_cookie:
            auth.set_anon_cookie(resp, set_cookie)
        return resp

    # A slot was free: reserve the first chunk now and return the grant.
    return await _finalize_grant(data, keys, tier, tracked, set_cookie)


def _load_balancer_headers(request: Request) -> dict[str, str]:
    """Headers for the server-to-server session allocation request.

    The dedicated authorization header matches the Reachy Mini client and lets
    the load balancer validate and attribute an optional HF user token without
    exposing it to browser JavaScript. Anonymous visitors send no credential.
    """
    headers = {
        "Content-Type": "application/json",
        "User-Agent": LB_USER_AGENT,
    }
    token = auth.current_access_token(request)
    if token:
        headers["X-Reachy-Mini-Authorization"] = f"Bearer {token}"
    return headers


@app.get("/api/queue/{queue_id}")
async def queue_status(queue_id: str, request: Request):
    """Poll a waiting ticket: relay the position, or — when the head of the line
    claims a freed slot — reserve the budget now and return the grant. Re-checks the
    daily budget at claim, since a multi-minute wait could have spent it elsewhere."""
    if not LOAD_BALANCER_URL:
        raise HTTPException(status_code=404, detail="Not found.")

    tier, keys, set_cookie = auth.resolve_identity(request)
    tracked = LIMITER_ENABLED and limiter.budget_for(tier) is not None

    url = f"{LOAD_BALANCER_URL.rstrip('/')}/queue/{queue_id}"
    try:
        async with httpx.AsyncClient(timeout=15.0) as http:
            lb = await http.get(url)
    except httpx.RequestError as exc:
        logger.warning("Load balancer unreachable: %r", exc)
        raise HTTPException(status_code=502, detail="Speech service unreachable.")

    if lb.status_code == 404:
        # Ticket unknown/expired (reaped after we stopped polling). Tell the client
        # to start over rather than spin.
        resp = JSONResponse({"state": "expired"}, status_code=404)
        if set_cookie:
            auth.set_anon_cookie(resp, set_cookie)
        return resp

    if lb.status_code != 200:
        logger.warning("Queue poll failed %s: %s", lb.status_code, lb.text[:300])
        raise HTTPException(status_code=502, detail=f"Queue poll failed ({lb.status_code}).")

    data = lb.json()

    if data.get("state") == "queued":
        data["tier"] = tier
        resp = JSONResponse(data)
        if set_cookie:
            auth.set_anon_cookie(resp, set_cookie)
        return resp

    # Claimed a slot. Re-check the budget: it may have been spent in another tab
    # during the wait. If so, refuse — the just-claimed slot is now a pending
    # session on the LB and its pending-timeout reaper reclaims it shortly.
    if tracked:
        rem = await asyncio.to_thread(limiter.remaining, keys, tier)
        if rem is not None and rem <= 0:
            resp = JSONResponse(
                {"tier": tier, "reason": "limit", "remainingSec": 0}, status_code=402
            )
            if set_cookie:
                auth.set_anon_cookie(resp, set_cookie)
            return resp

    return await _finalize_grant(data, keys, tier, tracked, set_cookie)


@app.delete("/api/queue/{queue_id}")
async def queue_leave(queue_id: str):
    """Leave the queue from the explicit 'Leave queue' button (a real fetch)."""
    if not LOAD_BALANCER_URL:
        raise HTTPException(status_code=404, detail="Not found.")
    await _lb_leave(queue_id)
    return {"ok": True}


@app.post("/api/queue/end")
async def queue_end(request: Request):
    """Leave the queue on teardown/tab-close (navigator.sendBeacon, which can only
    POST). Body: { queueId }. Best-effort; the LB reaps the ticket on TTL anyway."""
    if not LOAD_BALANCER_URL:
        raise HTTPException(status_code=404, detail="Not found.")
    qid = await _queue_id(request)
    if qid:
        await _lb_leave(qid)
    return {"ok": True}


async def _finalize_grant(data, keys, tier, tracked, set_cookie):
    """Shared grant tail (fast path or queue claim): reserve the first chunk, attach
    the metering fields the client needs, and set the anon cookie."""
    remaining = None
    if tracked and data.get("session_id"):
        await asyncio.to_thread(limiter.begin, data["session_id"], keys, tier)
        remaining = await asyncio.to_thread(limiter.remaining, keys, tier)

    data.update({
        "tier": tier,
        "limited": tracked,
        "remainingSec": remaining,
        "heartbeatSec": limiter.HEARTBEAT_SEC,
    })
    resp = JSONResponse(data)
    if set_cookie:
        auth.set_anon_cookie(resp, set_cookie)
    return resp


async def _lb_leave(queue_id: str) -> None:
    """Best-effort: tell the LB to drop a waiting ticket."""
    url = f"{LOAD_BALANCER_URL.rstrip('/')}/queue/{queue_id}"
    try:
        async with httpx.AsyncClient(timeout=5.0) as http:
            await http.delete(url)
    except httpx.RequestError as exc:
        logger.warning("Queue leave failed: %r", exc)


def _safe_json(response) -> dict:
    try:
        body = response.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


async def _queue_id(request: Request) -> str:
    """Pull `queueId` from a JSON body, tolerating sendBeacon's blob posts."""
    try:
        data = await request.json()
    except Exception:
        return ""
    return (data or {}).get("queueId", "") if isinstance(data, dict) else ""


async def _session_id(request: Request) -> str:
    """Pull `sessionId` from a JSON body, tolerating sendBeacon's blob posts."""
    try:
        data = await request.json()
    except Exception:
        return ""
    return (data or {}).get("sessionId", "") if isinstance(data, dict) else ""


@app.post("/api/session/heartbeat")
async def session_heartbeat(request: Request):
    """Extend the live reservation one chunk at a time. `expired` once the day's
    budget is spent — the client then tears down."""
    if not LIMITER_ENABLED:
        raise HTTPException(status_code=404, detail="Not found.")
    sid = await _session_id(request)
    alive = bool(sid) and await asyncio.to_thread(limiter.heartbeat, sid)
    return {"expired": not alive}


@app.post("/api/session/end")
async def session_end(request: Request):
    """Clean teardown: reconcile to real elapsed time and refund the unused
    chunk. Sent via navigator.sendBeacon, so it must succeed without a response."""
    if not LIMITER_ENABLED:
        raise HTTPException(status_code=404, detail="Not found.")
    sid = await _session_id(request)
    if sid:
        await asyncio.to_thread(limiter.end, sid)
    return {"ok": True}


# Static front-end. Registered last so the /api routes win. `html=True` serves
# index.html at "/". The repo is public anyway, so serving the dir is fine.
app.mount("/", StaticFiles(directory=HERE, html=True), name="static")
