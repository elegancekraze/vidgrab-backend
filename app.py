import os
import glob
import base64
import shutil
import tempfile

import yt_dlp
from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask

API_TOKEN = os.environ.get("API_TOKEN", "").strip()
DEFAULT_COOKIES_B64 = os.environ.get("COOKIES_B64", "").strip()
UPSTREAM_PROXY = os.environ.get("UPSTREAM_PROXY", "").strip()
YOUTUBE_PROXY = os.environ.get("YOUTUBE_PROXY", "").strip()
IMPERSONATE = os.environ.get("IMPERSONATE", "").strip()

app = FastAPI(title="ytdlp-backend", version="1.0.0")


def check_auth(request: Request) -> None:
    if API_TOKEN and request.headers.get("x-auth") != API_TOKEN:
        raise HTTPException(status_code=401, detail="unauthorized")


def cookie_file(request: Request) -> str | None:
    b64 = request.headers.get("x-cookies-b64") or DEFAULT_COOKIES_B64
    if not b64:
        return None
    try:
        data = base64.b64decode(b64)
    except Exception:
        raise HTTPException(status_code=400, detail="bad x-cookies-b64")
    f = tempfile.NamedTemporaryFile(delete=False, suffix=".txt")
    f.write(data)
    f.close()
    return f.name


def proxy_for(url: str) -> str:
    u = (url or "").lower()
    if ("youtube.com" in u or "youtu.be" in u or "youtube-nocookie.com" in u) and YOUTUBE_PROXY:
        return YOUTUBE_PROXY
    return UPSTREAM_PROXY


def base_opts(request: Request, url: str = "") -> dict:
    cp = cookie_file(request)
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "nocheckcertificate": True,
        "geo_bypass": True,
    }
    if cp:
        opts["cookiefile"] = cp
    px = proxy_for(url)
    if px:
        opts["proxy"] = px
    if IMPERSONATE:
        opts["impersonate"] = IMPERSONATE
    return opts


@app.get("/")
def root():
    return {"ok": True, "service": "ytdlp-backend", "auth": bool(API_TOKEN)}


@app.get("/api/info")
def info(request: Request, url: str = Query(...)):
    check_auth(request)
    opts = base_opts(request, url)
    opts["skip_download"] = True
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            data = ydl.extract_info(url, download=False)
    except Exception as e:
        return JSONResponse(status_code=502, content={"ok": False, "error": str(e)})

    fmts = []
    for f in data.get("formats") or []:
        if f.get("vcodec") in (None, "none") and f.get("acodec") in (None, "none"):
            continue
        fmts.append(
            {
                "format_id": f.get("format_id"),
                "ext": f.get("ext"),
                "height": f.get("height"),
                "fps": f.get("fps"),
                "vcodec": f.get("vcodec"),
                "acodec": f.get("acodec"),
                "filesize": f.get("filesize") or f.get("filesize_approx"),
                "note": f.get("format_note"),
            }
        )
    fmts.sort(key=lambda x: (x.get("height") or 0), reverse=True)

    return {
        "ok": True,
        "title": data.get("title"),
        "uploader": data.get("uploader"),
        "duration": data.get("duration"),
        "thumbnail": data.get("thumbnail"),
        "extractor": data.get("extractor"),
        "is_live": data.get("is_live"),
        "formats": fmts[:60],
    }


@app.get("/api/download")
def download(
    request: Request,
    url: str = Query(...),
    fmt: str = Query("bestvideo*+bestaudio/best"),
    merge: str = Query("mp4"),
):
    check_auth(request)
    tmp = tempfile.mkdtemp(prefix="ydlp_")
    opts = base_opts(request, url)
    opts.update(
        {
            "format": fmt,
            "outtmpl": os.path.join(tmp, "%(title).80s.%(ext)s"),
            "merge_output_format": merge,
        }
    )
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except Exception as e:
        shutil.rmtree(tmp, ignore_errors=True)
        return JSONResponse(status_code=502, content={"ok": False, "error": str(e)})

    files = [f for f in glob.glob(os.path.join(tmp, "*")) if os.path.isfile(f)]
    if not files:
        shutil.rmtree(tmp, ignore_errors=True)
        return JSONResponse(status_code=502, content={"ok": False, "error": "no output file"})

    path = max(files, key=os.path.getsize)
    ext = os.path.splitext(path)[1].lstrip(".") or "bin"
    media = "video/mp4" if ext == "mp4" else f"video/{ext}"
    return FileResponse(
        path,
        filename=os.path.basename(path),
        media_type=media,
        background=BackgroundTask(shutil.rmtree, tmp, True),
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "7860")))
