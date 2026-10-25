from __future__ import annotations

import base64
import binascii
import json
import mimetypes
import re
import shutil
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
from io import BytesIO
from flask import Flask, jsonify, render_template, request, send_file
from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
IMAGE_DIR = DATA_DIR / "images"
REFERENCE_DIR = DATA_DIR / "reference_sets"
SETTINGS_FILE = DATA_DIR / "settings.json"
PROFILE_FILE = DATA_DIR / "profiles.json"
LIBRARY_FILE = DATA_DIR / "library.json"
EXPIRY_SECONDS = 7 * 24 * 60 * 60
MAX_REFERENCE_BYTES = 20 * 1024 * 1024
MAX_REFERENCE_COUNT = 16

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_REFERENCE_BYTES * MAX_REFERENCE_COUNT + 2 * 1024 * 1024
DATA_DIR.mkdir(exist_ok=True)
IMAGE_DIR.mkdir(parents=True, exist_ok=True)
REFERENCE_DIR.mkdir(parents=True, exist_ok=True)
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


def write_profiles(store: dict) -> None:
    with state_lock:
        temporary = PROFILE_FILE.with_suffix(".tmp")
        temporary.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(PROFILE_FILE)
        try:
            PROFILE_FILE.chmod(0o600)
        except OSError:
            pass


def profiles_store() -> dict:
    store = read_json(PROFILE_FILE, None)
    if isinstance(store, dict) and isinstance(store.get("profiles"), list) and store["profiles"]:
        ids = {profile.get("id") for profile in store["profiles"] if isinstance(profile, dict)}
        if store.get("active_profile_id") in ids:
            return store
        store["active_profile_id"] = next(iter(ids), None)
        write_profiles(store)
        return store

    # One-time migration: preserve the existing connection and secret as the first scheme.
    legacy = DEFAULT_SETTINGS | read_json(SETTINGS_FILE, {})
    profile = {"id": uuid.uuid4().hex, "name": "默认方案", "settings": legacy}
    store = {"active_profile_id": profile["id"], "profiles": [profile]}
    write_profiles(store)
    return store


def active_profile(store: dict | None = None) -> dict:
    store = store or profiles_store()
    return next((profile for profile in store["profiles"] if profile.get("id") == store.get("active_profile_id")), store["profiles"][0])


def public_profiles(store: dict | None = None) -> dict:
    store = store or profiles_store()
    return {
        "active_profile_id": store.get("active_profile_id"),
        "profiles": [{"id": profile["id"], "name": profile["name"]} for profile in store["profiles"]],
    }


def settings():
    profile = active_profile()
    values = DEFAULT_SETTINGS | profile.get("settings", {})
    if values.get("api_mode") not in ("images", "chat"):
        values["api_mode"] = "images"
    return values


def save_settings(values: dict) -> None:
    with state_lock:
        store = profiles_store()
        profile = active_profile(store)
        profile["settings"] = DEFAULT_SETTINGS | values
        write_profiles(store)
        # Keep the legacy local file in sync for recovery by older builds; it is git-ignored.
        SETTINGS_FILE.write_text(json.dumps(profile["settings"], ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            SETTINGS_FILE.chmod(0o600)
        except OSError:
            pass


def library() -> list[dict]:
    records = read_json(LIBRARY_FILE, [])
    return records if isinstance(records, list) else []


def save_library(records: list[dict]) -> None:
    with state_lock:
        temporary = LIBRARY_FILE.with_suffix(".tmp")
        temporary.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(LIBRARY_FILE)


def save_reference_set(reference_set_id: str, reference_files) -> None:
    if not reference_files:
        return
    folder = REFERENCE_DIR / reference_set_id
    folder.mkdir(parents=True, exist_ok=True)
    manifest = []
    for index, reference in enumerate(reference_files, start=1):
        reference.stream.seek(0)
        content = reference.read()
        reference.stream.seek(0)
        safe_name = secure_filename(reference.filename or "reference.png") or "reference.png"
        extension = Path(safe_name).suffix or ".png"
        filename = f"{index:02d}{extension.lower()}"
        (folder / filename).write_bytes(content)
        manifest.append({"file": filename, "name": safe_name, "mime": reference.mimetype or "image/png"})
    (folder / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def load_reference_set(reference_set_id: str | None):
    if not reference_set_id:
        return []
    folder = REFERENCE_DIR / reference_set_id
    manifest_path = folder / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError("原参考图文件不存在，无法按原条件重生成。")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("原参考图信息无法读取，无法按原条件重生成。") from exc
    references = []
    for item in manifest:
        file_path = folder / item["file"]
        if not file_path.is_file():
            raise ValueError("原参考图文件不完整，无法按原条件重生成。")
        references.append(FileStorage(stream=BytesIO(file_path.read_bytes()), filename=item["name"], content_type=item["mime"]))
    return references


def cleanup_reference_sets(records: list[dict]) -> None:
    used = {item.get("reference_set_id") for item in records if item.get("reference_set_id")}
    for folder in REFERENCE_DIR.iterdir():
        if folder.is_dir() and folder.name not in used:
            shutil.rmtree(folder, ignore_errors=True)


def provider_path(cfg: dict, has_references: bool) -> str:
    path = cfg.get("endpoint_path", "/images/generations").strip()
    if not path.startswith("/"):
        path = "/" + path
    if has_references and cfg.get("api_mode", "images") == "images" and path.endswith("/generations"):
        path = path[:-len("generations")] + "edits"
    return path


def generation_settings_snapshot(cfg: dict, profile_id: str, has_references: bool) -> dict:
    return {
        "profile_id": profile_id,
        "base_url": cfg.get("base_url", "").strip().rstrip("/"),
        "model": cfg.get("model", ""),
        "api_mode": cfg.get("api_mode", "images"),
        "endpoint_path": provider_path(cfg, has_references),
        "size": cfg.get("size", "1024x1024"),
        "convert_embedded": bool(cfg.get("convert_embedded", True)),
    }


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
        cleanup_reference_sets(kept)
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


def persist_image(data: bytes, mime: str, prompt: str, model: str, generation_settings: dict | None = None, reference_set_id: str | None = None) -> dict:
    extension, mime = guess_extension(mime, data)
    if not mime.startswith("image/"):
        raise ValueError("响应内容不是可识别的图片。")
    image_id = uuid.uuid4().hex
    filename = image_id + extension
    final_path = IMAGE_DIR / filename
    temporary_path = final_path.with_suffix(final_path.suffix + ".tmp")
    temporary_path.write_bytes(data)
    temporary_path.replace(final_path)
    item = {
        "id": image_id,
        "file": filename,
        "mime": mime,
        "prompt": prompt[:4000],
        "model": model,
        "created_at": time.time(),
        "downloaded": False,
        "generation_settings": generation_settings,
        "reference_set_id": reference_set_id,
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
        "has_saved_route": bool(item.get("generation_settings")),
        "url": f"/api/images/{item['id']}/file?v={int(float(item.get('updated_at', item.get('created_at', 0))) * 1000)}",
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


def call_provider(cfg: dict, prompt: str, reference_files, quantity: int = 1):
    base = cfg["base_url"].strip().rstrip("/")
    if not base:
        raise ValueError("请先填写 API URL。")
    if not cfg.get("api_key"):
        raise ValueError("请先填写 API Key。")
    if not cfg.get("model", "").strip():
        raise ValueError("请先填写模型名称。")
    mode = cfg.get("api_mode", "images")
    path = provider_path(cfg, bool(reference_files))
    url = base + path
    headers = {"Authorization": "Bearer " + cfg["api_key"], "Accept": "application/json, image/*, text/plain"}
    timeout = 300

    try:
        if mode == "chat":
            content = [{"type": "text", "text": prompt}]
            for reference_file in reference_files:
                ref_bytes = reference_file.read()
                reference_file.stream.seek(0)
                ref_mime = reference_file.mimetype or "image/png"
                ref_data = base64.b64encode(ref_bytes).decode("ascii")
                content.append({"type": "image_url", "image_url": {"url": f"data:{ref_mime};base64,{ref_data}"}})
            payload = {"model": cfg["model"], "messages": [{"role": "user", "content": content}], "stream": False, "n": quantity}
            response = requests.post(url, headers=headers, json=payload, timeout=timeout)
        else:
            payload = {"model": cfg["model"], "prompt": prompt, "size": cfg.get("size", "1024x1024"), "n": quantity, "response_format": "b64_json"}
            if reference_files:
                files = [("image", (secure_filename(reference_file.filename or "reference.png"), reference_file.stream, reference_file.mimetype or "application/octet-stream")) for reference_file in reference_files]
                response = requests.post(url, headers=headers, data=payload, files=files, timeout=timeout)
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
            choice_texts = []
            for choice in choices[:quantity]:
                if not isinstance(choice, dict):
                    continue
                message = choice.get("message", {})
                choice_text = response_text(message.get("content")) if isinstance(message, dict) else ""
                if not choice_text:
                    choice_text = response_text(choice.get("text", ""))
                if choice_text:
                    choice_texts.append(choice_text)
                    if cfg.get("convert_embedded", True):
                        extracted = extract_embedded_image(choice_text)
                        if extracted:
                            found.append(extracted)
            raw_text = "\n".join(choice_texts)
        if not found and not raw_text:
            raw_text = json.dumps(body, ensure_ascii=False)
    else:
        raw_text = response_text(body)

    if not found and raw_text and cfg.get("convert_embedded", True):
        extracted = extract_embedded_image(raw_text)
        if extracted:
            found.append(extracted)
    return found, raw_text


@app.after_request
def disable_api_cache(response):
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
    return response


@app.get("/")
def home():
    cleanup_expired()
    return render_template("index.html")


@app.get("/api/profiles")
def get_profiles():
    return jsonify(public_profiles())


@app.post("/api/profiles")
def create_profile():
    incoming = request.get_json(silent=True) or {}
    name = str(incoming.get("name", "")).strip()[:60]
    if not name:
        return jsonify(error="请给方案起个名字。"), 400
    store = profiles_store()
    if any(profile.get("name", "").casefold() == name.casefold() for profile in store["profiles"]):
        return jsonify(error="已经有同名方案，请换一个名字。"), 409
    current = DEFAULT_SETTINGS | active_profile(store).get("settings", {})
    supplied = incoming.get("settings") if isinstance(incoming.get("settings"), dict) else {}
    for key in ("base_url", "model", "api_mode", "endpoint_path", "size", "convert_embedded"):
        if key in supplied:
            current[key] = supplied[key]
    if supplied.get("api_key"):
        current["api_key"] = str(supplied["api_key"])
    profile = {"id": uuid.uuid4().hex, "name": name, "settings": current}
    store["profiles"].append(profile)
    store["active_profile_id"] = profile["id"]
    write_profiles(store)
    return jsonify(public_profiles(store)), 201


@app.post("/api/profiles/select")
def select_profile():
    incoming = request.get_json(silent=True) or {}
    profile_id = incoming.get("profile_id")
    store = profiles_store()
    if not any(profile.get("id") == profile_id for profile in store["profiles"]):
        return jsonify(error="找不到这个方案。"), 404
    store["active_profile_id"] = profile_id
    write_profiles(store)
    return jsonify(public_profiles(store))


@app.delete("/api/profiles/<profile_id>")
def delete_profile(profile_id):
    store = profiles_store()
    if len(store["profiles"]) <= 1:
        return jsonify(error="至少保留一套连接方案。"), 400
    removed = next((profile for profile in store["profiles"] if profile.get("id") == profile_id), None)
    if removed is None:
        return jsonify(error="找不到这个方案。"), 404
    store["profiles"] = [profile for profile in store["profiles"] if profile.get("id") != profile_id]
    if store.get("active_profile_id") == profile_id:
        store["active_profile_id"] = store["profiles"][0]["id"]
    write_profiles(store)
    return jsonify(public_profiles(store))


@app.get("/api/settings")
def get_settings():
    cfg = settings()
    cfg.pop("api_key", None)
    active = active_profile()
    cfg["api_key_set"] = bool(settings().get("api_key"))
    cfg["profile_id"] = active["id"]
    cfg["profile_name"] = active["name"]
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
    active = active_profile()
    safe["api_key_set"] = bool(cfg.get("api_key"))
    safe["profile_id"] = active["id"]
    safe["profile_name"] = active["name"]
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
    try:
        quantity = int(request.form.get("quantity", "1"))
    except (TypeError, ValueError):
        return jsonify(error="生成数量须为 1 到 5。"), 400
    if not 1 <= quantity <= 5:
        return jsonify(error="生成数量须为 1 到 5。"), 400
    references = [item for item in request.files.getlist("reference") if item and item.filename]
    if len(references) > MAX_REFERENCE_COUNT:
        return jsonify(error=f"参考图最多 {MAX_REFERENCE_COUNT} 张。"), 413
    for reference in references:
        reference.stream.seek(0, 2)
        size = reference.stream.tell()
        reference.stream.seek(0)
        if size > MAX_REFERENCE_BYTES:
            return jsonify(error=f"参考图 {reference.filename} 超过 20 MB。"), 413
    cfg = settings()
    if "convert_embedded" in request.form:
        cfg["convert_embedded"] = request.form.get("convert_embedded") == "true"
    profile_id = active_profile().get("id", "")
    saved_route = generation_settings_snapshot(cfg, profile_id, bool(references))
    reference_set_id = uuid.uuid4().hex if references else None
    try:
        images, text = call_provider(cfg, prompt, references, quantity)
        if images:
            if reference_set_id:
                save_reference_set(reference_set_id, references)
            results = [persist_image(data, mime, prompt, cfg["model"], saved_route, reference_set_id) for mime, data in images]
            return jsonify(images=results, response_text=text)
        return jsonify(images=[], response_text=text, error="接口没有返回可识别的图片。请检查接口模式，或开启 Markdown/Base64 转图。"), 422
    except (ValueError, RuntimeError, requests.RequestException) as exc:
        return jsonify(error=str(exc)), 502


@app.post("/api/images/<image_id>/regenerate")
def regenerate_image(image_id):
    with state_lock:
        item = next((record.copy() for record in library() if record.get("id") == image_id), None)
    if not item:
        return jsonify(error="图片不存在或已清理。"), 404
    prompt = item.get("prompt", "").strip()
    if not prompt:
        return jsonify(error="这张图片没有可用的历史提示词。"), 400

    snapshot = item.get("generation_settings")
    if snapshot:
        store = profiles_store()
        profile = next((entry for entry in store["profiles"] if entry.get("id") == snapshot.get("profile_id")), None)
        if not profile:
            return jsonify(error="原连接方案已删除，无法使用原方案重生成。"), 409
        cfg = DEFAULT_SETTINGS | profile.get("settings", {})
        for key in ("base_url", "model", "api_mode", "endpoint_path", "size", "convert_embedded"):
            if key in snapshot:
                cfg[key] = snapshot[key]
    else:
        # Legacy records predate route snapshots; the UI explicitly confirms this fallback.
        profile_id = active_profile().get("id", "")
        cfg = settings()

    try:
        references = load_reference_set(item.get("reference_set_id"))
        if not snapshot:
            snapshot = generation_settings_snapshot(cfg, profile_id, bool(references))
        images, text = call_provider(cfg, prompt, references, 1)
        if not images:
            return jsonify(error="接口没有返回可识别的图片。", response_text=text), 422
        mime, data = images[0]
        extension, mime = guess_extension(mime, data)
        if not mime.startswith("image/"):
            raise ValueError("响应内容不是可识别的图片。")
        new_filename = image_id + extension
        new_path = IMAGE_DIR / new_filename
        temporary_path = new_path.with_suffix(new_path.suffix + ".tmp")
        temporary_path.write_bytes(data)
        temporary_path.replace(new_path)
        with state_lock:
            records = library()
            current = next((record for record in records if record.get("id") == image_id), None)
            if current is None:
                new_path.unlink(missing_ok=True)
                return jsonify(error="图片已被删除，无法覆盖。"), 404
            old_path = IMAGE_DIR / current.get("file", "")
            current["file"] = new_filename
            current["mime"] = mime
            current["model"] = cfg["model"]
            current["created_at"] = time.time()
            current["updated_at"] = current["created_at"]
            current["downloaded"] = False
            current["generation_settings"] = snapshot or None
            save_library(records)
            if old_path != new_path:
                old_path.unlink(missing_ok=True)
            return jsonify(public_item(current))
    except (ValueError, RuntimeError, requests.RequestException, OSError) as exc:
        return jsonify(error=str(exc)), 502


@app.get("/api/images/<image_id>/file")
def get_image(image_id):
    item = next((record for record in library() if record.get("id") == image_id), None)
    if not item:
        return jsonify(error="图片不存在或已清理。"), 404
    return send_file(IMAGE_DIR / item["file"], mimetype=item.get("mime", "image/png"), conditional=True)


@app.get("/api/images/<image_id>/references")
def list_image_references(image_id):
    item = next((record for record in library() if record.get("id") == image_id), None)
    if not item:
        return jsonify(error="图片不存在或已清理。"), 404
    reference_set_id = item.get("reference_set_id")
    recorded = bool(reference_set_id or item.get("generation_settings"))
    if not reference_set_id:
        return jsonify(reference_recorded=recorded, references=[])
    folder = REFERENCE_DIR / reference_set_id
    manifest_path = folder / "manifest.json"
    if not manifest_path.is_file():
        return jsonify(error="参考图记录缺失。"), 404
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return jsonify(error="参考图记录无法读取。"), 500
    references = []
    for index, entry in enumerate(manifest):
        if not isinstance(entry, dict):
            continue
        filename = entry.get("file", "")
        if not filename or Path(filename).name != filename:
            continue
        reference_path = folder / filename
        if not reference_path.is_file():
            continue
        references.append({
            "index": index,
            "name": entry.get("name") or f"参考图 {index + 1}",
            "mime": entry.get("mime") or "image/png",
            "url": f"/api/images/{image_id}/references/{index}",
        })
    return jsonify(reference_recorded=True, references=references)


@app.get("/api/images/<image_id>/references/<int:index>")
def get_image_reference(image_id, index):
    item = next((record for record in library() if record.get("id") == image_id), None)
    if not item:
        return jsonify(error="图片不存在或已清理。"), 404
    reference_set_id = item.get("reference_set_id")
    if not reference_set_id:
        return jsonify(error="这张图片没有参考图。"), 404
    folder = REFERENCE_DIR / reference_set_id
    try:
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        entry = manifest[index]
        filename = entry.get("file", "")
        if not filename or Path(filename).name != filename:
            raise ValueError("Invalid reference filename")
        reference_path = folder / filename
        mime = entry.get("mime") or "image/png"
    except (OSError, ValueError, IndexError, TypeError, AttributeError):
        return jsonify(error="参考图文件不可用。"), 404
    if not reference_path.is_file():
        return jsonify(error="参考图文件不存在。"), 404
    return send_file(reference_path, mimetype=mime, conditional=True)


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
        remaining = [record for record in records if record.get("id") != image_id]
        save_library(remaining)
        cleanup_reference_sets(remaining)
    return jsonify(ok=True)


@app.errorhandler(413)
def too_large(_error):
    return jsonify(error="上传内容过大。参考图上限为 20 MB。"), 413


if __name__ == "__main__":
    print("Local Image Studio: http://127.0.0.1:7860")
    app.run(host="127.0.0.1", port=7860, debug=False)
