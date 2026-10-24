from __future__ import annotations

import base64
import binascii
import json
import mimetypes
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
from flask import Flask, jsonify, render_template, request, send_file
from werkzeug.utils import secure_filename

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
IMAGE_DIR = DATA_DIR / "images"
SETTINGS_FILE = DATA_DIR / "settings.json"
LIBRARY_FILE = DATA_DIR / "library.json"
EXPIRY_SECONDS = 72 * 60 * 60
MAX_REFERENCE_BYTES = 20 * 1024 * 1024

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_REFERENCE_BYTES + 2 * 1024 * 1024
DATA_DIR.mkdir(exist_ok=True)
IMAGE_DIR.mkdir(parents=True, exist_ok=True)
state_lock = threading.RLock()

DEFAULT_SETTINGS = {
    "base_url": "",
    "api_key": "",
    "model": "",
    "api_mode": "images",
    "endpoint_path": "/images/generations",
    "size": "1024x1024",
    "convert_embedded": True,
}


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def settings():
    values = DEFAULT_SETTINGS | read_json(SETTINGS_FILE, {})
    if values.get("api_mode") not in ("images", "chat"):
        values["api_mode"] = "images"
    return values


def save_settings(values: dict) -> None:
    with state_lock:
        SETTINGS_FILE.write_text(json.dumps(values, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            SETTINGS_FILE.chmod(0o600)
        except OSError:
            pass


def library() -> list[dict]:
    records = read_json(LIBRARY_FILE, [])
    return records if isinstance(records, list) else []


def save_library(records: list[dict]) -> None:
    with state_lock:
        LIBRARY_FILE.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")


def cleanup_expired() -> int:
    now = time.time()
    with state_lock:
        records = library()
        kept = []
        removed = 0
        for item in records:
            path = IMAGE_DIR / item.get("file", "")
            created = float(item.get("created_at", now))
            expired = not item.get("downloaded") and now - created >= EXPIRY_SECONDS
            if expired:
                try:
                    path.unlink(missing_ok=True)
                finally:
                    removed += 1
            elif path.is_file():
                kept.append(item)
            else:
                removed += 1
        if len(kept) != len(records):
            save_library(kept)
        return removed


def cleanup_worker():
    while True:
        time.sleep(60 * 30)
        cleanup_expired()


threading.Thread(target=cleanup_worker, daemon=True, name="image-expiry-cleanup").start()


def guess_extension(mime: str, data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png", "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg", "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp", "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif", "image/gif"
    extension = mimetypes.guess_extension(mime or "") or ".png"
    if extension == ".jpe":
        extension = ".jpg"
    return extension, mime or "application/octet-stream"


def persist_image(data: bytes, mime: str, prompt: str, model: str) -> dict:
    extension, mime = guess_extension(mime, data)
    if not mime.startswith("image/"):
        raise ValueError("响应内容不是可识别的图片。")
    image_id = uuid.uuid4().hex
    filename = image_id + extension
    (IMAGE_DIR / filename).write_bytes(data)
    item = {
        "id": image_id,
        "file": filename,
        "mime": mime,
        "prompt": prompt[:1000],
        "model": model,
        "created_at": time.time(),
        "downloaded": False,
    }
    with state_lock:
        records = library()
        records.insert(0, item)
        save_library(records)
    return public_item(item)


def public_item(item: dict) -> dict:
    return {
        "id": item["id"],
        "mime": item.get("mime", "image/png"),
        "prompt": item.get("prompt", ""),
        "model": item.get("model", ""),
        "created_at": item.get("created_at", 0),
        "downloaded": bool(item.get("downloaded")),
        "url": f"/api/images/{item['id']}/file",
        "download_url": f"/api/images/{item['id']}/download",
    }


def identify_image(raw: bytes) -> tuple[str, bytes]:
    raw = raw.strip()
    try:
        decoded = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("Base64 内容无效或不完整。")
    if not decoded:
        raise ValueError("响应中没有图片数据。")
    ext, mime = guess_extension("", decoded)
    return mime, decoded


def extract_embedded_image(text: str) -> tuple[str, bytes] | None:
    data_match = re.search(r"data:(image/[A-Za-z0-9.+-]+);base64,([A-Za-z0-9+/=_\-\s]+)", text)
    if data_match:
        mime = data_match.group(1).lower()
        payload = re.sub(r"\s+", "", data_match.group(2)).rstrip(")]}>'\"`.,")
        try:
            decoded = base64.b64decode(payload + "=" * (-len(payload) % 4), validate=False)
            if decoded:
                return mime, decoded
        except (binascii.Error, ValueError):
            pass
    compact = re.sub(r"\s+", "", text.strip().strip("`"))
    if len(compact) > 128 and re.fullmatch(r"[A-Za-z0-9+/=_-]+", compact):
        try:
            decoded = base64.b64decode(compact + "=" * (-len(compact) % 4), validate=False)
            if decoded and guess_extension("", decoded)[1].startswith("image/"):
                return guess_extension("", decoded)[1], decoded
        except (binascii.Error, ValueError):
            pass
    return None


def response_text(payload) -> str:
    if isinstance(payload, str):
        return payload
    if isinstance(payload, list):
        parts = []
        for chunk in payload:
            if isinstance(chunk, str):
                parts.append(chunk)
            elif isinstance(chunk, dict):
                if isinstance(chunk.get("text"), str):
                    parts.append(chunk["text"])
                image_url = chunk.get("image_url")
                if isinstance(image_url, dict) and isinstance(image_url.get("url"), str):
                    parts.append(image_url["url"])
        return "\n".join(parts)
    return ""


def call_provider(cfg: dict, prompt: str, reference_file):
    base = cfg["base_url"].strip().rstrip("/")
    if not base:
        raise ValueError("请先填写 API URL。")
    if not cfg.get("api_key"):
        raise ValueError("请先填写 API Key。")
    if not cfg.get("model", "").strip():
        raise ValueError("请先填写模型名称。")
    mode = cfg.get("api_mode", "images")
    path = cfg.get("endpoint_path", "/images/generations").strip()
    if not path.startswith("/"):
        path = "/" + path
    if reference_file and mode == "images" and path.endswith("/generations"):
        path = path[:-len("generations")] + "edits"
    url = base + path
    headers = {"Authorization": "Bearer " + cfg["api_key"], "Accept": "application/json, image/*, text/plain"}
    timeout = 300

    try:
        if mode == "chat":
            content = [{"type": "text", "text": prompt}]
            if reference_file:
                ref_bytes = reference_file.read()
                reference_file.stream.seek(0)
                ref_mime = reference_file.mimetype or "image/png"
                ref_data = base64.b64encode(ref_bytes).decode("ascii")
                content.append({"type": "image_url", "image_url": {"url": f"data:{ref_mime};base64,{ref_data}"}})
            payload = {"model": cfg["model"], "messages": [{"role": "user", "content": content}], "stream": False}
            response = requests.post(url, headers=headers, json=payload, timeout=timeout)
        else:
            payload = {"model": cfg["model"], "prompt": prompt, "size": cfg.get("size", "1024x1024"), "n": 1, "response_format": "b64_json"}
            if reference_file:
                response = requests.post(url, headers=headers, data=payload, files={"image": (secure_filename(reference_file.filename or "reference.png"), reference_file.stream, reference_file.mimetype or "application/octet-stream")}, timeout=timeout)
            else:
                response = requests.post(url, headers=headers, json=payload, timeout=timeout)
    except requests.RequestException as exc:
        raise RuntimeError(f"连接 API 失败：{exc}") from exc

    if not response.ok:
        message = response.text[:1500]
        raise RuntimeError(f"API 返回 HTTP {response.status_code}：{message}")
    content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
    if content_type.startswith("image/"):
        return [(content_type, response.content)], ""

    try:
        body = response.json()
    except ValueError:
        body = response.text
    found = []
    raw_text = ""
    if isinstance(body, dict):
        data_items = body.get("data")
        if isinstance(data_items, list):
            for item in data_items:
                if not isinstance(item, dict):
                    continue
                b64 = item.get("b64_json") or item.get("base64")
                if isinstance(b64, str):
                    mime, decoded = identify_image(b64.encode("ascii", errors="ignore"))
                    found.append((mime, decoded))
                elif isinstance(item.get("url"), str):
                    image_url = item["url"]
                    if image_url.startswith("https://") or image_url.startswith("http://"):
                        try:
                            fetched = requests.get(image_url, timeout=timeout)
                            fetched.raise_for_status()
                            mime = fetched.headers.get("Content-Type", "image/png").split(";", 1)[0]
                            found.append((mime, fetched.content))
                        except requests.RequestException as exc:
                            raw_text = f"图片 URL 下载失败：{exc}"
        choices = body.get("choices")
        if isinstance(choices, list) and choices:
            message = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
            raw_text = response_text(message.get("content"))
            if not raw_text:
                raw_text = response_text(choices[0].get("text", ""))
        if not found and not raw_text:
            raw_text = json.dumps(body, ensure_ascii=False)
    else:
        raw_text = response_text(body)

    if not found and raw_text and cfg.get("convert_embedded", True):
        extracted = extract_embedded_image(raw_text)
        if extracted:
            found.append(extracted)
    return found, raw_text


@app.get("/")
def home():
    cleanup_expired()
    return render_template("index.html")


@app.get("/api/settings")
def get_settings():
    cfg = settings()
    cfg.pop("api_key", None)
    cfg["api_key_set"] = bool(settings().get("api_key"))
    return jsonify(cfg)


@app.post("/api/settings")
def update_settings():
    incoming = request.get_json(silent=True) or {}
    cfg = settings()
    for key in ("base_url", "model", "endpoint_path", "size"):
        if key in incoming:
            cfg[key] = str(incoming[key]).strip()
    if incoming.get("api_mode") in ("images", "chat"):
        cfg["api_mode"] = incoming["api_mode"]
    if "convert_embedded" in incoming:
        cfg["convert_embedded"] = bool(incoming["convert_embedded"])
    if incoming.get("api_key"):
        cfg["api_key"] = str(incoming["api_key"])
    if incoming.get("clear_api_key"):
        cfg["api_key"] = ""
    save_settings(cfg)
    safe = cfg.copy()
    safe.pop("api_key", None)
    safe["api_key_set"] = bool(cfg.get("api_key"))
    return jsonify(safe)


@app.get("/api/images")
def list_images():
    cleanup_expired()
    return jsonify([public_item(item) for item in library()])


@app.post("/api/generate")
def generate():
    cleanup_expired()
    prompt = request.form.get("prompt", "").strip()
    if not prompt:
        return jsonify(error="请先写下提示词。"), 400
    reference = request.files.get("reference")
    if reference and reference.filename:
        reference.stream.seek(0, 2)
        size = reference.stream.tell()
        reference.stream.seek(0)
        if size > MAX_REFERENCE_BYTES:
            return jsonify(error="参考图不能超过 20 MB。"), 413
    cfg = settings()
    if "convert_embedded" in request.form:
        cfg["convert_embedded"] = request.form.get("convert_embedded") == "true"
    try:
        images, text = call_provider(cfg, prompt, reference if reference and reference.filename else None)
        if images:
            results = [persist_image(data, mime, prompt, cfg["model"]) for mime, data in images]
            return jsonify(images=results, response_text=text)
        return jsonify(images=[], response_text=text, error="接口没有返回可识别的图片。请检查接口模式，或开启 Markdown/Base64 转图。"), 422
    except (ValueError, RuntimeError, requests.RequestException) as exc:
        return jsonify(error=str(exc)), 502


@app.get("/api/images/<image_id>/file")
def get_image(image_id):
    item = next((record for record in library() if record.get("id") == image_id), None)
    if not item:
        return jsonify(error="图片不存在或已清理。"), 404
    return send_file(IMAGE_DIR / item["file"], mimetype=item.get("mime", "image/png"), conditional=True)


@app.get("/api/images/<image_id>/download")
def download_image(image_id):
    with state_lock:
        records = library()
        item = next((record for record in records if record.get("id") == image_id), None)
        if not item:
            return jsonify(error="图片不存在或已清理。"), 404
        path = IMAGE_DIR / item["file"]
        if not path.is_file():
            return jsonify(error="图片文件不存在。"), 404
        item["downloaded"] = True
        save_library(records)
    return send_file(path, as_attachment=True, download_name=f"image-{image_id[:8]}{path.suffix}", mimetype=item.get("mime", "application/octet-stream"))


@app.delete("/api/images/<image_id>")
def delete_image(image_id):
    with state_lock:
        records = library()
        item = next((record for record in records if record.get("id") == image_id), None)
        if not item:
            return jsonify(error="图片不存在。"), 404
        (IMAGE_DIR / item["file"]).unlink(missing_ok=True)
        save_library([record for record in records if record.get("id") != image_id])
    return jsonify(ok=True)


@app.errorhandler(413)
def too_large(_error):
    return jsonify(error="上传内容过大。参考图上限为 20 MB。"), 413


if __name__ == "__main__":
    print("Local Image Studio: http://127.0.0.1:7860")
    app.run(host="127.0.0.1", port=7860, debug=False)
