from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import re
import tempfile
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, Field
from pypdf import PdfReader

from .config import ROOT, settings
from .db import DB
from .orchestrator import NovelStudio
from .providers import LLMError, ModelRouter

STATIC = ROOT / "app" / "static"
ASSET_ROOT = settings.data_dir / "assets"
ASSET_ROOT.mkdir(parents=True, exist_ok=True)

db = DB()
studio = NovelStudio(db)
model_router = ModelRouter(db)
app = FastAPI(title="墨境 · 长篇创作室", docs_url="/docs")
app.mount("/static", StaticFiles(directory=STATIC), name="static")


class ProjectCreate(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    genre: str = Field(default="长篇小说", min_length=1, max_length=80)
    premise: str = Field(default="", max_length=6000)
    target_chapters: int = Field(default=300, ge=1, le=10000)
    target_words_per_chapter: int = Field(default=2500, ge=500, le=12000)
    audience: str = Field(default="中文连载读者", max_length=240)
    style_notes: str = Field(default="", max_length=6000)


class ChatRequest(BaseModel):
    message: str = Field(default="", max_length=20000)
    mode: str = Field(default="auto", pattern="^(auto|discuss|plan|write|revise|audit)$")
    attachment_ids: list[str] = Field(default_factory=list, max_length=12)


class RunNoteRequest(BaseModel):
    message: str = Field(min_length=1, max_length=6000)


class PreferencesPatch(BaseModel):
    provider: str | None = None
    quality_threshold: float | None = Field(default=None, ge=60, le=100)
    max_revisions: int | None = Field(default=None, ge=0, le=6)
    agent_depth: str | None = Field(default=None, pattern="^(adaptive|deep|maximum)$")
    cross_provider_council: bool | None = None
    model_routes: dict[str, dict[str, Any]] | None = None


@app.exception_handler(KeyError)
async def not_found(_: Request, exc: KeyError):
    return JSONResponse({"detail": "没有找到这项内容，可能已经被删除。"}, status_code=404)


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
def health():
    with db.conn() as c:
        ok = c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    return {"ok": bool(ok), "product": "墨境长篇创作室"}


@app.get("/api/projects")
def projects():
    return db.list_projects()


@app.post("/api/projects")
def create_project(payload: ProjectCreate):
    return db.create_project(payload.model_dump())


def _project_payload(pid: str) -> dict[str, Any]:
    project = db.get_project(pid)
    dashboard = db.project_dashboard(pid)
    return {
        **project,
        **dashboard,
        "preferences": db.get_preferences(pid),
        "messages": db.recent_messages(pid, 160),
        "artifacts": db.project_artifacts_for_ui(pid, chapter_limit=100, other_limit=60),
        "characters": db.entity_latest(pid, 80),
        "threads": db.open_threads(pid, 100),
        "facts": db.canon(pid, 220),
        "assets": db.list_assets(pid, 100),
        "activity": db.recent_activity(pid, 12, 100),
        "unfinished_run": db.latest_resumable_run(pid),
    }


@app.get("/api/projects/{pid}")
def project(pid: str):
    return _project_payload(pid)


@app.patch("/api/projects/{pid}/preferences")
def preferences(pid: str, payload: PreferencesPatch):
    db.get_project(pid)
    patch = {k: v for k, v in payload.model_dump().items() if v is not None}
    if payload.model_routes is not None:
        patch["model_routes"] = payload.model_routes
    return db.update_preferences(pid, patch)


@app.get("/api/projects/{pid}/artifacts/{aid}")
def artifact(pid: str, aid: str):
    return db.get_artifact(pid, aid)


@app.get("/api/engines")
def engines():
    return [profile.__dict__ for profile in model_router.profiles()]


def _safe_name(name: str | None) -> str:
    value = Path(name or "reference").name.replace("\x00", "")
    value = re.sub(r"[\r\n]+", " ", value).strip()
    return value[:180] or "reference"


def _validate_upload(path: Path, filename: str, mime: str, size: int) -> tuple[str, dict[str, Any]]:
    if size <= 0:
        raise HTTPException(400, "这个文件是空的，请重新选择。")
    if size > settings.max_upload_mb * 1024 * 1024:
        raise HTTPException(413, f"单个参考资料不能超过 {settings.max_upload_mb}MB。")

    suffix = Path(filename).suffix.lower()
    image_suffixes = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
    text_suffixes = {".txt", ".md", ".csv", ".json"}
    if suffix in image_suffixes or mime.startswith("image/"):
        try:
            with Image.open(path) as image:
                width, height = image.size
                if width <= 0 or height <= 0 or width * height > 40_000_000:
                    raise HTTPException(400, "图片尺寸过大，请缩小到 4000–6000 像素以内再上传。")
                image.seek(0)
                image.convert("RGB").resize((1, 1)).load()
                fmt = str(image.format or "").lower()
        except HTTPException:
            raise
        except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError) as exc:
            raise HTTPException(400, "这张图片无法正常读取，请重新导出为 PNG、JPG 或 WebP。") from exc
        return "image", {"width": width, "height": height, "format": fmt}

    if suffix == ".pdf" or mime == "application/pdf":
        with path.open("rb") as handle:
            header = handle.read(5)
        if header != b"%PDF-":
            raise HTTPException(400, "这个文件并不是有效的 PDF。")
        try:
            reader = PdfReader(str(path))
            return "document", {"pages": len(reader.pages)}
        except Exception as exc:
            raise HTTPException(400, "这个 PDF 无法读取，请重新导出后上传。") from exc

    if suffix in text_suffixes or mime.startswith("text/") or mime == "application/json":
        try:
            sample = path.read_bytes()[:200_000]
            decoded = sample.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise HTTPException(400, "这个文本资料不是 UTF-8 编码，请转换编码后再上传。") from exc
        if suffix == ".json" or mime == "application/json":
            try:
                json.loads(path.read_text("utf-8-sig"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise HTTPException(400, "这个 JSON 资料格式不完整，请修正后再上传。") from exc
        return "document", {"format": suffix.lstrip(".") or "text", "preview_chars": len(decoded)}

    raise HTTPException(415, "目前支持图片、PDF、TXT、Markdown、CSV 和 JSON 参考资料。")


@app.post("/api/projects/{pid}/assets")
async def upload_asset(pid: str, file: UploadFile = File(...)):
    db.get_project(pid)
    filename = _safe_name(file.filename)
    guessed = mimetypes.guess_type(filename)[0]
    mime = file.content_type or guessed or "application/octet-stream"
    suffix = Path(filename).suffix.lower()[:12]
    asset_dir = ASSET_ROOT / pid
    asset_dir.mkdir(parents=True, exist_ok=True)

    fd, temp_name = tempfile.mkstemp(prefix="upload-", suffix=suffix, dir=asset_dir)
    os.close(fd)
    temp = Path(temp_name)
    size = 0
    final: Path | None = None
    try:
        with temp.open("wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > settings.max_upload_mb * 1024 * 1024:
                    raise HTTPException(413, f"单个参考资料不能超过 {settings.max_upload_mb}MB。")
                out.write(chunk)
        kind, meta = _validate_upload(temp, filename, mime, size)
        final = asset_dir / f"{uuid.uuid4().hex}{suffix}"
        temp.replace(final)
        asset = db.save_asset(pid, kind, filename, mime, size, str(final), meta)
        asset["url"] = f"/api/assets/{asset['id']}/content"
        return asset
    except Exception:
        temp.unlink(missing_ok=True)
        if final is not None:
            final.unlink(missing_ok=True)
        raise


@app.get("/api/assets/{aid}/content")
def asset_content(aid: str):
    asset = db.get_asset(aid)
    path = Path(asset["storage_path"])
    if not path.exists():
        raise HTTPException(404, "参考资料已经丢失，请重新上传。")
    return FileResponse(path, media_type=asset.get("mime_type") or "application/octet-stream", filename=asset.get("filename"))


def _customer_error(exc: Exception) -> str:
    text = str(exc)
    if isinstance(exc, LLMError):
        if "没有配置" in text:
            return "创作引擎还没有连接。请先在服务端配置至少一家模型服务。"
        if any(code in text for code in (" 429", "429:", "rate", "频繁")):
            return "当前创作服务比较繁忙，请稍后再试。"
        if any(code in text for code in (" 401", "401:", " 403", "403:")):
            return "创作引擎连接失败，请检查服务端密钥配置。"
        if "无法读取" in text or "无法解码" in text or "参考资料" in text:
            return text[:240]
        return "这次创作没有完成，运行记录已经保留。请重试或换一个已连接的创作引擎。"
    if isinstance(exc, ValueError):
        return text[:300]
    return "这次创作没有完成，运行记录已经保留。请重试。"


@app.post("/api/projects/{pid}/runs/{run_id}/note")
def add_run_note(pid: str, run_id: str, payload: RunNoteRequest):
    db.get_project(pid)
    try:
        note = db.add_run_author_note(run_id, pid, payload.message)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    db.add_message(pid, "user", payload.message, {"run_id": run_id, "live_note": True})
    return {"ok": True, "note": note, "message": "补充要求已收到，会在当前创作的下一个安全节点生效。"}


@app.post("/api/projects/{pid}/runs/{run_id}/resume/stream")
async def resume_run_stream(pid: str, run_id: str):
    db.get_project(pid)
    candidate = db.latest_resumable_run(pid)
    if not candidate or str(candidate.get("id")) != run_id:
        raise HTTPException(409, "这次创作当前不需要恢复；请刷新作品状态后再试。")

    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=256)
    channel = {"open": True, "error_seen": False}

    def enqueue(event: dict[str, Any]) -> None:
        if not channel["open"]:
            return
        event = dict(event)
        if event.get("type") == "error":
            channel["error_seen"] = True
            # Preserve actionable product-language validation errors; hide provider and
            # implementation details for everything else.
            if event.get("error_type") == "ValueError" and event.get("message"):
                event["message"] = str(event["message"])[:300]
            else:
                event["message"] = "这次创作没有完成，运行记录已经保留。"
            event.pop("error_type", None)
        try:
            queue.put_nowait(event)
            return
        except asyncio.QueueFull:
            if event.get("type") == "progress":
                return
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            pass

    async def callback(event: dict[str, Any]):
        enqueue(event)

    async def worker():
        try:
            await studio.resume(run_id, event_callback=callback)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # NovelStudio reports failures through the callback and then raises. Avoid
            # emitting the same error a second time from the HTTP wrapper.
            if not channel["error_seen"]:
                enqueue({"type": "error", "message": _customer_error(exc)})
        finally:
            enqueue({"type": "_end"})

    task = asyncio.create_task(worker())

    async def stream():
        try:
            while True:
                event = await queue.get()
                if event.get("type") == "_end":
                    break
                yield json.dumps(event, ensure_ascii=False, default=str) + "\n"
        finally:
            channel["open"] = False
            if task.done():
                try:
                    task.result()
                except Exception:
                    pass

    return StreamingResponse(stream(), media_type="application/x-ndjson", headers={"Cache-Control": "no-cache"})


@app.post("/api/projects/{pid}/chat/stream")
async def chat_stream(pid: str, payload: ChatRequest):
    db.get_project(pid)
    if not payload.message.strip() and not payload.attachment_ids:
        raise HTTPException(400, "请输入一句话，或者先添加参考资料。")

    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=256)
    channel = {"open": True, "error_seen": False}

    def enqueue(event: dict[str, Any]) -> None:
        # Streaming is only a view over a durable run. If the browser disconnects, never let
        # an abandoned response queue stall the underlying chapter mutation. Progress events
        # are lossy by design; final/error events displace the oldest queued progress item.
        if not channel["open"]:
            return
        event = dict(event)
        if event.get("type") == "error":
            channel["error_seen"] = True
            # Preserve actionable product-language validation errors; hide provider and
            # implementation details for everything else.
            if event.get("error_type") == "ValueError" and event.get("message"):
                event["message"] = str(event["message"])[:300]
            else:
                event["message"] = "这次创作没有完成，运行记录已经保留。"
            event.pop("error_type", None)
        try:
            queue.put_nowait(event)
            return
        except asyncio.QueueFull:
            if event.get("type") == "progress":
                return
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            pass

    async def callback(event: dict[str, Any]):
        enqueue(event)

    async def worker():
        try:
            await studio.chat(
                pid,
                payload.message,
                payload.mode,
                payload.attachment_ids,
                event_callback=callback,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # NovelStudio reports failures through the callback and then raises. Avoid
            # emitting the same error a second time from the HTTP wrapper.
            if not channel["error_seen"]:
                enqueue({"type": "error", "message": _customer_error(exc)})
        finally:
            enqueue({"type": "_end"})

    task = asyncio.create_task(worker())

    async def stream():
        try:
            while True:
                event = await queue.get()
                if event.get("type") == "_end":
                    break
                yield json.dumps(event, ensure_ascii=False, default=str) + "\n"
        finally:
            # Do not cancel a story mutation merely because the browser closed the stream.
            # Close only the response channel; the durable run keeps executing without any
            # queue backpressure and a refreshed project shows the committed result.
            channel["open"] = False
            if task.done():
                try:
                    task.result()
                except Exception:
                    pass

    return StreamingResponse(stream(), media_type="application/x-ndjson", headers={"Cache-Control": "no-cache"})
