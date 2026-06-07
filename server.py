r"""
./server.py - AI Canvas V2 本地服务

用法:
  cd v2
  venv\Scripts\python server.py

访问地址: http://localhost:8777

主要目录位于 v2/ 下:
  user/Canvas Project/  - 画布项目
  user/shortcuts.json   - 快捷键配置
  user/settings.json    - 应用设置
  user/config.json      - API Key 配置
  data/uploads/         - 上传文件

"""

import http.server
import socketserver
import os
import json
import threading
import subprocess
import time
import mimetypes
import sys
import urllib.request
import urllib.error
import urllib.parse
from urllib.parse import unquote
import base64
import re
import random
import hashlib
import datetime
import hmac
import ipaddress
import shutil
import tempfile

CURRENT_DIR = os.path.abspath(os.path.dirname(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

from backend.services.hot_update_service import HotUpdateService
from backend.services.http_route_dispatcher import HttpRouteDispatcher
from backend.services.config_route_service import ConfigRouteService
from backend.services.json_file_route_service import JsonFileRouteService
from backend.services.library_file_route_service import LibraryFileRouteService
from backend.services.media_file_route_service import MediaFileRouteService
from backend.services.local_media_processing_route_service import LocalMediaProcessingRouteService
from backend.services.remote_proxy_route_service import RemoteProxyRouteService
from backend.services.subscription_gate_service import SubscriptionGateService
from backend.services.subscription_gate_manifest import (
    get_runninghub_subscription_workflow_ids,
    get_subscription_gate_model_id_by_key,
    get_subscription_gate_model_ids,
    get_subscription_gate_model_name_map,
    normalize_subscription_gate_model_id,
)
from backend.services.subscription_client import SubscriptionRemoteClient
from backend.services.dreamina_cli_service import DreaminaCliService
from backend.services.dreamina_route_service import DreaminaRouteService

mimetypes.add_type("text/javascript; charset=utf-8", ".js")
mimetypes.add_type("text/javascript; charset=utf-8", ".mjs")
mimetypes.add_type("text/css; charset=utf-8", ".css")

STATIC_VIDEO_CACHE_EXTS = {
    ".mp4",
    ".webm",
    ".mov",
    ".m4v",
    ".avi",
    ".mkv",
    ".mpeg",
    ".mpg",
}
DERIVED_MEDIA_CACHE_CONTROL = "public, max-age=604800, immutable"
STATIC_VIDEO_CACHE_CONTROL = "public, max-age=86400"
NO_STORE_CACHE_CONTROL = "no-store, no-cache, must-revalidate, max-age=0"
SMART_CLIP_MIN_SEGMENTS = 2
SMART_CLIP_MAX_SEGMENTS = 25
SMART_CLIP_DEFAULT_SEGMENTS = 20
SMART_CLIP_FPS_OPTIONS = (16, 24, 30)
SMART_CLIP_DEFAULT_FPS = 24
SMART_CLIP_OUTPUT_MODE_SEGMENTS = "videoSegments"
SMART_CLIP_OUTPUT_MODE_KEYFRAMES = "keyframes"
SMART_CLIP_DEFAULT_OUTPUT_MODE = SMART_CLIP_OUTPUT_MODE_SEGMENTS
DERIVED_STATIC_MEDIA_PREFIXES = (
    "/data/uploads/_derived/",
    "/data/assets/_derived/",
    "/data/assets/derived/",
    "/output/_derived/",
    "/output/VideoThumbs/",
)


def _normalize_request_path(request_path):
    try:
        raw_path = urllib.parse.urlsplit(str(request_path or "")).path
        return urllib.parse.unquote(raw_path).replace("\\", "/")
    except Exception:
        return ""


def _is_cacheable_derived_media_request(request_path):
    decoded_path = _normalize_request_path(request_path)
    return any(decoded_path.startswith(prefix) for prefix in DERIVED_STATIC_MEDIA_PREFIXES)


def _is_cacheable_static_video_request(request_path):
    decoded_path = _normalize_request_path(request_path)
    if not decoded_path:
        return False
    if not (
        decoded_path.startswith("/output/")
        or decoded_path.startswith("/data/uploads/")
        or decoded_path.startswith("/data/assets/")
    ):
        return False
    _, ext = os.path.splitext(decoded_path)
    return ext.lower() in STATIC_VIDEO_CACHE_EXTS


def _resolve_static_cache_control(request_path):
    if _is_cacheable_derived_media_request(request_path):
        return DERIVED_MEDIA_CACHE_CONTROL
    if _is_cacheable_static_video_request(request_path):
        return STATIC_VIDEO_CACHE_CONTROL
    return NO_STORE_CACHE_CONTROL

def _get_int_env(name, default, min_value=None):
    try:
        value = int(str(os.environ.get(name, default)).strip())
    except Exception:
        return default
    if min_value is not None and value < min_value:
        return default
    return value

def _get_bool_env(name, default=False):
    raw = os.environ.get(name)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() in ("1", "true", "yes", "on")

def _split_env_list(name):
    raw = str(os.environ.get(name, "") or "").strip()
    if not raw:
        return []
    return [item.strip() for item in re.split(r"[\s,]+", raw) if item.strip()]

def _get_path_env(name, fallback):
    raw = str(os.environ.get(name, "") or "").strip()
    if not raw:
        return os.path.abspath(fallback)
    return os.path.abspath(os.path.expandvars(os.path.expanduser(raw)))

def _get_optional_path_env(name):
    raw = str(os.environ.get(name, "") or "").strip()
    if not raw:
        return ""
    return os.path.abspath(os.path.expandvars(os.path.expanduser(raw)))

def _get_optional_path_env_list(base_name, max_items=4):
    values = []
    seen = set()
    for index in range(1, max_items + 1):
        name = base_name if index == 1 else f"{base_name}_{index}"
        value = _get_optional_path_env(name)
        if not value:
            continue
        key = os.path.normcase(os.path.abspath(value))
        if key in seen:
            continue
        seen.add(key)
        values.append(value)
    return values

def _get_executable_env(name, fallback):
    raw = str(os.environ.get(name, "") or "").strip()
    if not raw:
        return fallback
    return os.path.abspath(os.path.expandvars(os.path.expanduser(raw)))

def _normalize_origin(origin):
    raw = str(origin or "").strip().rstrip("/")
    if not raw or raw == "null":
        return ""
    try:
        parsed = urllib.parse.urlparse(raw)
    except Exception:
        return ""
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        return ""
    try:
        if parsed.port is not None and (parsed.port < 1 or parsed.port > 65535):
            return ""
    except ValueError:
        return ""
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"

PORT      = _get_int_env("AICANVAS_PORT", 8777, 1)
BIND_HOST = (os.environ.get("AIC_BIND_HOST", "127.0.0.1") or "").strip() or "127.0.0.1"
LAN_MODE  = _get_bool_env("AIC_LAN_MODE") or _get_bool_env("AIC_ENABLE_LAN")
ALLOWED_ORIGINS = tuple(
    origin for origin in (_normalize_origin(item) for item in _split_env_list("AIC_ALLOWED_ORIGINS")) if origin
)
LOCAL_ACCESS_TOKEN = str(os.environ.get("AIC_LOCAL_TOKEN", "") or "").strip()
DIRECTORY = os.path.abspath(os.path.dirname(__file__))   # v2/ 绝对路径
# --- 版本号 ---
# 从 index.html 读取版本号
import re

def get_version_from_index_html():
    """从 index.html 读取应用版本号。"""
    index_path = os.path.join(DIRECTORY, "index.html")
    try:
        with open(index_path, 'r', encoding='utf-8') as f:
            content = f.read()
        # 匹配 <meta name="app-version" content="V0.0.7">
        match = re.search(r'<meta name="app-version" content="([^"]+)"', content)
        if match:
            return match.group(1)
    except Exception:
        pass
    return "V0.0.7"  # 默认版本

LOCAL_VERSION   = get_version_from_index_html()  # 从 index.html 读取版本号
_gen_seq_lock   = threading.Lock()
_smart_clip_jobs = {}
_smart_clip_lock = threading.Lock()
_file_save_migration_jobs = {}
_file_save_migration_lock = threading.Lock()

def _normalize_smart_clip_max_segments(value):
    try:
        max_segments = int(value)
    except Exception:
        max_segments = SMART_CLIP_DEFAULT_SEGMENTS
    return max(SMART_CLIP_MIN_SEGMENTS, min(SMART_CLIP_MAX_SEGMENTS, max_segments))

def _normalize_smart_clip_fps(value):
    try:
        fps = int(round(float(value)))
    except Exception:
        fps = SMART_CLIP_DEFAULT_FPS
    return fps if fps in SMART_CLIP_FPS_OPTIONS else SMART_CLIP_DEFAULT_FPS

def _normalize_smart_clip_output_mode(value):
    raw = str(value or "").strip()
    if raw == SMART_CLIP_OUTPUT_MODE_KEYFRAMES:
        return SMART_CLIP_OUTPUT_MODE_KEYFRAMES
    return SMART_CLIP_DEFAULT_OUTPUT_MODE

# --- 可配置的数据目录，默认位于 v2/ 下 ---
DEFAULT_USER_DIR = _get_path_env("AIC_USER_DIR", os.path.join(DIRECTORY, "user"))
DEFAULT_CANVAS_DIR = _get_path_env("AIC_CANVAS_DIR", os.path.join(DEFAULT_USER_DIR, "Canvas Project"))
DEFAULT_OUTPUT_DIR = _get_path_env("AIC_OUTPUT_DIR", os.path.join(DIRECTORY, "output"))
DEFAULT_DATA_DIR = _get_path_env("AIC_DATA_DIR", os.path.join(DIRECTORY, "data"))
DEFAULT_UPLOADS_DIR = _get_path_env("AIC_UPLOADS_DIR", os.path.join(DEFAULT_DATA_DIR, "uploads"))
DEFAULT_ASSETS_DIR = _get_path_env("AIC_ASSETS_DIR", os.path.join(DEFAULT_DATA_DIR, "assets"))
DEFAULT_WORKFLOWS_DIR = _get_path_env("AIC_WORKFLOWS_DIR", os.path.join(DEFAULT_DATA_DIR, "workflows"))
LEGACY_DEFAULT_CANVAS_DIRS = _get_optional_path_env_list("AIC_LEGACY_CANVAS_DIR")
LEGACY_DEFAULT_OUTPUT_DIRS = _get_optional_path_env_list("AIC_LEGACY_OUTPUT_DIR")
LEGACY_DEFAULT_DATA_DIRS = _get_optional_path_env_list("AIC_LEGACY_DATA_DIR")
LEGACY_DEFAULT_UPLOADS_DIRS = _get_optional_path_env_list("AIC_LEGACY_UPLOADS_DIR")
LEGACY_DEFAULT_CANVAS_DIR = LEGACY_DEFAULT_CANVAS_DIRS[0] if LEGACY_DEFAULT_CANVAS_DIRS else ""
LEGACY_DEFAULT_OUTPUT_DIR = LEGACY_DEFAULT_OUTPUT_DIRS[0] if LEGACY_DEFAULT_OUTPUT_DIRS else ""
LEGACY_DEFAULT_DATA_DIR = LEGACY_DEFAULT_DATA_DIRS[0] if LEGACY_DEFAULT_DATA_DIRS else ""
LEGACY_DEFAULT_UPLOADS_DIR = LEGACY_DEFAULT_UPLOADS_DIRS[0] if LEGACY_DEFAULT_UPLOADS_DIRS else ""
FFMPEG_EXE = _get_executable_env("AIC_FFMPEG_EXE", "ffmpeg")
FFPROBE_EXE = _get_executable_env("AIC_FFPROBE_EXE", "ffprobe")
SYSTEM_FILE_SAVE_PATHS_ENABLED = bool(str(os.environ.get("AIC_USER_DIR", "") or "").strip())

USER_DIR       = DEFAULT_USER_DIR
CANVAS_DIR     = DEFAULT_CANVAS_DIR
DATA_DIR       = DEFAULT_DATA_DIR
ASSETS_DIR     = DEFAULT_ASSETS_DIR
ASSET_THUMBS_DIR = os.path.join(ASSETS_DIR, "thumbs")
WORKFLOWS_DIR  = DEFAULT_WORKFLOWS_DIR
WORKFLOW_THUMBS_DIR = os.path.join(ASSETS_DIR, "workflows", "thumbs")
UPLOADS_DIR    = DEFAULT_UPLOADS_DIR
OUTPUT_DIR     = DEFAULT_OUTPUT_DIR
CONFIG_FILE    = os.path.join(USER_DIR, "config.json")
SETTINGS_FILE  = os.path.join(USER_DIR, "settings.json")
GEN_SEQ_STATE_FILE = os.path.join(OUTPUT_DIR, ".gen_seq_state.json")
MAX_UPLOAD_BYTES = _get_int_env("AIC_UPLOAD_MAX_BYTES", 100 * 1024 * 1024, 1)
IMAGE_DERIVATIVE_DISPLAY_MAX_EDGE = 1280
IMAGE_DERIVATIVE_THUMB_MAX_EDGE = 320
IMAGE_DERIVATIVE_DISPLAY_QUALITY = 78
IMAGE_DERIVATIVE_THUMB_QUALITY = 70
IMAGE_DERIVATIVE_ROOT_DIRNAME = "_derived"

DREAMINA_VIDEO_VIP_MODEL_ID = get_subscription_gate_model_id_by_key("dreaminaVideoVip")
VIDEO_VIP_MODEL_IDS = get_subscription_gate_model_ids()
VIDEO_VIP_WORKFLOW_IDS = get_runninghub_subscription_workflow_ids()
VIDEO_VIP_MODEL_NAME_MAP = get_subscription_gate_model_name_map()
SUB_STATUS_NONE = "none"
SUB_STATUS_ACTIVE = "active"
SUB_STATUS_EXPIRED = "expired"
SUB_ERROR_INVALID_ARGUMENTS = "INVALID_ARGUMENTS"
SUB_ERROR_INVALID_CDKEY = "INVALID_CDKEY"
SUB_ERROR_CDKEY_ALREADY_USED = "CDKEY_ALREADY_USED"
SUB_ERROR_REQUIRED = "SUBSCRIPTION_REQUIRED"
SUB_ERROR_MODEL_NOT_ENTITLED = "SUBSCRIPTION_MODEL_NOT_ENTITLED"
SUB_MESSAGE_V54_REQUIRED = "该模型为 VIP 模型，请先激活 CDKEY/订阅"
DEFAULT_SUB_CONTACT_TEXT = os.environ.get(
    "AIC_SUB_CONTACT_TEXT",
    "联系管理员获取授权码",
).strip() or "联系管理员获取授权码"
DEFAULT_SUB_CONTACT_WECHAT = os.environ.get(
    "AIC_SUB_CONTACT_WECHAT",
    "yumengashuo",
).strip() or "yumengashuo"
DEFAULT_SUB_CONTACT_IMAGE_URL = "https://api.ashuoai.com/static/contact/wechat.png"
DEFAULT_SUB_CONTACT_URL = os.environ.get(
    "AIC_SUB_CONTACT_URL",
    DEFAULT_SUB_CONTACT_IMAGE_URL,
).strip()
OFFICIAL_SUBSCRIPTION_API_BASE = "https://api.ashuoai.com"


def _get_system_state_dir():
    app_folder = "AI-CanvasPro"
    if sys.platform.startswith("win"):
        base_dir = (
            os.environ.get("LOCALAPPDATA")
            or os.environ.get("APPDATA")
            or os.path.expanduser("~")
        )
        return os.path.join(base_dir, app_folder)
    if sys.platform == "darwin":
        return os.path.join(
            os.path.expanduser("~/Library/Application Support"),
            app_folder,
        )
    base_dir = (
        os.environ.get("XDG_STATE_HOME")
        or os.path.expanduser("~/.local/state")
    )
    return os.path.join(base_dir, app_folder)


SYSTEM_STATE_DIR = _get_system_state_dir()
SYSTEM_SETTINGS_FILE = os.path.join(SYSTEM_STATE_DIR, "settings.json")
DEVICE_IDENTITY_FILENAME = "device-identity.json"
SUBSCRIPTION_AUTHORIZATION_ID_KEYS = ("installId", "install_id", "deviceId", "device_id")


def _read_json_file(path, default=None):
    fallback = {} if default is None else default
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else fallback
    except Exception:
        return fallback


def _write_json_file(path, data):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _normalize_storage_dir(raw, fallback):
    value = str(raw or "").strip()
    if not value:
        return os.path.abspath(fallback)
    value = os.path.expandvars(os.path.expanduser(value))
    return os.path.abspath(value)

def _same_storage_path(a, b):
    if not a or not b:
        return False
    try:
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))
    except Exception:
        return False

def _unique_storage_paths(paths):
    values = []
    for path_value in paths or ():
        if not path_value:
            continue
        if any(_same_storage_path(path_value, existing) for existing in values):
            continue
        values.append(os.path.abspath(path_value))
    return values

def _legacy_default_candidates(primary, additional=()):
    return _unique_storage_paths((primary, *(additional or ())))

def _replace_legacy_default_path(raw, legacy_defaults, next_default):
    for legacy_default in legacy_defaults or ():
        if raw and _same_storage_path(raw, legacy_default):
            return os.path.abspath(next_default)
    return raw

def _migrate_legacy_default_file_save_paths(paths):
    if not isinstance(paths, dict):
        return paths
    migrated = dict(paths)
    replacements = (
        ("canvasDir", _legacy_default_candidates(LEGACY_DEFAULT_CANVAS_DIR, LEGACY_DEFAULT_CANVAS_DIRS), DEFAULT_CANVAS_DIR),
        ("outputDir", _legacy_default_candidates(LEGACY_DEFAULT_OUTPUT_DIR, LEGACY_DEFAULT_OUTPUT_DIRS), DEFAULT_OUTPUT_DIR),
        ("dataDir", _legacy_default_candidates(LEGACY_DEFAULT_DATA_DIR, LEGACY_DEFAULT_DATA_DIRS), DEFAULT_DATA_DIR),
        ("tempDir", _legacy_default_candidates(LEGACY_DEFAULT_UPLOADS_DIR, LEGACY_DEFAULT_UPLOADS_DIRS), DEFAULT_UPLOADS_DIR),
    )
    changed = False
    for key, legacy_default, next_default in replacements:
        raw = migrated.get(key)
        next_value = _replace_legacy_default_path(raw, legacy_default, next_default)
        if next_value != raw:
            migrated[key] = next_value
            changed = True
    return migrated if changed else paths

def _is_path_inside_system_temp(path_value):
    if not path_value:
        return False
    try:
        candidate = os.path.normcase(os.path.abspath(path_value))
        temp_root = os.path.normcase(os.path.abspath(tempfile.gettempdir()))
        return os.path.commonpath([candidate, temp_root]) == temp_root
    except Exception:
        return False

def _is_path_policy_test_residue(path_value):
    if not _is_path_inside_system_temp(path_value):
        return False
    normalized = os.path.normcase(os.path.abspath(path_value)).replace("\\", "/")
    return "/aicanvas-path-policy-test/" in normalized or normalized.endswith("/aicanvas-path-policy-test")

def _should_ignore_system_file_save_paths(paths):
    if not SYSTEM_FILE_SAVE_PATHS_ENABLED or not isinstance(paths, dict):
        return False
    values = (
        paths.get("canvasDir"),
        paths.get("outputDir"),
        paths.get("dataDir"),
        paths.get("tempDir"),
    )
    return any(_is_path_policy_test_residue(value) for value in values)


def _has_file_save_paths(settings):
    return isinstance(settings, dict) and isinstance(settings.get("fileSavePaths"), dict)


def _is_user_managed_file_save_paths(settings):
    if not isinstance(settings, dict):
        return False
    meta = settings.get("fileSavePathsMeta")
    if not isinstance(meta, dict):
        return False
    return str(meta.get("source") or "").strip().lower() == "user"


def _read_system_file_save_paths():
    if not SYSTEM_FILE_SAVE_PATHS_ENABLED:
        return None
    system_settings = _read_json_file(SYSTEM_SETTINGS_FILE, {})
    paths = system_settings.get("fileSavePaths") if isinstance(system_settings, dict) else None
    if not isinstance(paths, dict) or _should_ignore_system_file_save_paths(paths):
        return None
    return paths


def _clear_system_file_save_paths():
    if not SYSTEM_FILE_SAVE_PATHS_ENABLED:
        return
    try:
        system_settings = _read_json_file(SYSTEM_SETTINGS_FILE, {})
        if "fileSavePaths" not in system_settings:
            return
        next_system_settings = dict(system_settings)
        next_system_settings.pop("fileSavePaths", None)
        _write_json_file(SYSTEM_SETTINGS_FILE, next_system_settings)
    except Exception:
        pass


def _migrate_system_file_save_paths_to_user_settings(local_settings, paths):
    if not isinstance(paths, dict) or _has_file_save_paths(local_settings):
        return
    try:
        next_settings = dict(local_settings) if isinstance(local_settings, dict) else {}
        next_settings["fileSavePaths"] = _normalize_file_save_paths_for_policy(paths)
        _write_json_file(SETTINGS_FILE, next_settings)
        _clear_system_file_save_paths()
    except Exception:
        pass


def _persist_local_file_save_paths_if_needed(local_settings, paths):
    if not SYSTEM_FILE_SAVE_PATHS_ENABLED or not _has_file_save_paths(local_settings):
        return
    if not isinstance(paths, dict):
        return
    try:
        next_settings = dict(local_settings) if isinstance(local_settings, dict) else {}
        next_settings["fileSavePaths"] = _normalize_file_save_paths_for_policy(
            paths,
            migrate_legacy_defaults=not _is_user_managed_file_save_paths(local_settings),
        )
        _write_json_file(SETTINGS_FILE, next_settings)
    except Exception:
        pass


def _infer_data_dir_from_temp_dir(temp_dir):
    raw = str(temp_dir or "").strip()
    if not raw:
        return ""
    normalized = os.path.abspath(raw)
    if os.path.basename(normalized).lower() == "uploads":
        return os.path.dirname(normalized)
    return normalized


def _file_save_paths_from_settings(settings, migrate_legacy_defaults=True):
    src = settings.get("fileSavePaths") if isinstance(settings, dict) else {}
    if not isinstance(src, dict):
        src = {}
    if migrate_legacy_defaults:
        src = _migrate_legacy_default_file_save_paths(src)
    raw_data_dir = src.get("dataDir")
    raw_temp_dir = src.get("tempDir")
    has_data_dir = bool(str(raw_data_dir or "").strip())
    data_dir = _normalize_storage_dir(
        raw_data_dir or _infer_data_dir_from_temp_dir(raw_temp_dir),
        DEFAULT_DATA_DIR,
    )
    temp_dir = (
        os.path.join(data_dir, "uploads")
        if has_data_dir
        else _normalize_storage_dir(raw_temp_dir, os.path.join(data_dir, "uploads"))
    )
    return {
        "userDir": _normalize_storage_dir(src.get("userDir"), DEFAULT_USER_DIR),
        "canvasDir": _normalize_storage_dir(src.get("canvasDir"), CANVAS_DIR),
        "outputDir": _normalize_storage_dir(src.get("outputDir"), DEFAULT_OUTPUT_DIR),
        "dataDir": data_dir,
        "tempDir": os.path.abspath(temp_dir),
    }


def _normalize_file_save_paths_for_policy(paths, migrate_legacy_defaults=True):
    normalized = _file_save_paths_from_settings(
        {"fileSavePaths": paths},
        migrate_legacy_defaults=migrate_legacy_defaults,
    )
    normalized["userDir"] = os.path.abspath(USER_DIR)
    return normalized


def _current_file_save_paths():
    return {
        "userDir": os.path.abspath(USER_DIR),
        "canvasDir": os.path.abspath(CANVAS_DIR),
        "outputDir": os.path.abspath(OUTPUT_DIR),
        "dataDir": os.path.abspath(DATA_DIR),
        "tempDir": os.path.abspath(UPLOADS_DIR),
    }


def _is_path_inside(candidate, root):
    try:
        candidate_abs = os.path.normcase(os.path.abspath(candidate))
        root_abs = os.path.normcase(os.path.abspath(root))
        return os.path.commonpath([candidate_abs, root_abs]) == root_abs
    except Exception:
        return False


def _is_same_or_nested_path(a, b):
    aa = os.path.normcase(os.path.abspath(a))
    bb = os.path.normcase(os.path.abspath(b))
    return aa == bb or _is_path_inside(aa, bb) or _is_path_inside(bb, aa)


def _validate_file_save_paths(paths, migrate_legacy_defaults=True):
    normalized = _normalize_file_save_paths_for_policy(
        paths,
        migrate_legacy_defaults=migrate_legacy_defaults,
    )
    for label, p in (
        ("用户设置保存路径", normalized["userDir"]),
        ("画布项目保存路径", normalized["canvasDir"]),
        ("输出文件保存路径", normalized["outputDir"]),
        ("数据文件保存路径", normalized["dataDir"]),
    ):
        if not os.path.isabs(p):
            raise ValueError(f"{label}必须是绝对路径")
        if os.path.exists(p) and not os.path.isdir(p):
            raise ValueError(f"{label}不能指向文件")

    pairs = (
        ("用户设置保存路径", normalized["userDir"], "输出文件保存路径", normalized["outputDir"]),
        ("用户设置保存路径", normalized["userDir"], "数据文件保存路径", normalized["dataDir"]),
        ("画布项目保存路径", normalized["canvasDir"], "数据文件保存路径", normalized["dataDir"]),
        ("输出文件保存路径", normalized["outputDir"], "数据文件保存路径", normalized["dataDir"]),
    )
    for left_label, left, right_label, right in pairs:
        if _is_same_or_nested_path(left, right):
            raise ValueError(f"{left_label}和{right_label}不能相同或互相包含")
    return normalized


def _remove_empty_dirs(root_dir):
    root_dir = os.path.abspath(root_dir)
    if not os.path.isdir(root_dir):
        return
    for current_root, _, files in os.walk(root_dir, topdown=False):
        if files:
            continue
        try:
            if not os.listdir(current_root):
                os.rmdir(current_root)
        except Exception:
            pass


def _move_missing_tree(src, dst):
    src = os.path.abspath(src)
    dst = os.path.abspath(dst)
    if not os.path.isdir(src):
        return
    os.makedirs(dst, exist_ok=True)
    for root, dirs, files in os.walk(src):
        rel_root = os.path.relpath(root, src)
        target_root = dst if rel_root == "." else os.path.join(dst, rel_root)
        os.makedirs(target_root, exist_ok=True)
        for dirname in dirs:
            os.makedirs(os.path.join(target_root, dirname), exist_ok=True)
        for filename in files:
            src_file = os.path.join(root, filename)
            dst_file = os.path.join(target_root, filename)
            if os.path.exists(dst_file):
                continue
            should_stop = False
            try:
                shutil.move(src_file, dst_file)
            except Exception:
                pass
    _remove_empty_dirs(src)


def _is_using_default_file_save_paths(paths):
    if not isinstance(paths, dict):
        return False
    return (
        _same_storage_path(paths.get("canvasDir"), DEFAULT_CANVAS_DIR)
        and _same_storage_path(paths.get("outputDir"), DEFAULT_OUTPUT_DIR)
        and _same_storage_path(paths.get("dataDir"), DEFAULT_DATA_DIR)
        and _same_storage_path(paths.get("tempDir"), DEFAULT_UPLOADS_DIR)
    )


def _path_at(paths, index):
    return paths[index] if index < len(paths) else ""


def _legacy_default_file_save_path_sets():
    canvas_dirs = _legacy_default_candidates(LEGACY_DEFAULT_CANVAS_DIR, LEGACY_DEFAULT_CANVAS_DIRS)
    output_dirs = _legacy_default_candidates(LEGACY_DEFAULT_OUTPUT_DIR, LEGACY_DEFAULT_OUTPUT_DIRS)
    data_dirs = _legacy_default_candidates(LEGACY_DEFAULT_DATA_DIR, LEGACY_DEFAULT_DATA_DIRS)
    uploads_dirs = _legacy_default_candidates(LEGACY_DEFAULT_UPLOADS_DIR, LEGACY_DEFAULT_UPLOADS_DIRS)
    total = max(len(canvas_dirs), len(output_dirs), len(data_dirs), len(uploads_dirs), 0)
    legacy_sets = []
    for index in range(total):
        data_dir = _path_at(data_dirs, index)
        uploads_dir = _path_at(uploads_dirs, index) or (os.path.join(data_dir, "uploads") if data_dir else "")
        paths = {
            "userDir": DEFAULT_USER_DIR,
            "canvasDir": _path_at(canvas_dirs, index),
            "outputDir": _path_at(output_dirs, index),
            "dataDir": data_dir,
            "tempDir": uploads_dir,
        }
        if any(paths.get(key) for key in ("canvasDir", "outputDir", "dataDir", "tempDir")):
            legacy_sets.append(paths)
    return legacy_sets


def _move_legacy_default_tree(src, dst):
    if not src or not dst:
        return False
    if _same_storage_path(src, dst) or _is_same_or_nested_path(src, dst):
        return False
    if not os.path.isdir(src):
        return False
    _move_missing_tree(src, dst)
    return True


def _migrate_legacy_default_files_to_current(paths=None):
    if not SYSTEM_FILE_SAVE_PATHS_ENABLED:
        return False
    current = paths if isinstance(paths, dict) else _current_file_save_paths()
    if not _is_using_default_file_save_paths(current):
        return False
    moved = False
    for legacy_paths in _legacy_default_file_save_path_sets():
        moved = _move_legacy_default_tree(legacy_paths.get("canvasDir"), current.get("canvasDir")) or moved
        moved = _move_legacy_default_tree(legacy_paths.get("outputDir"), current.get("outputDir")) or moved
        moved_data = _move_legacy_default_tree(legacy_paths.get("dataDir"), current.get("dataDir"))
        moved = moved_data or moved
        if not moved_data:
            moved = _move_legacy_default_tree(legacy_paths.get("tempDir"), current.get("tempDir")) or moved
    return moved


def _new_file_save_migration_job_id():
    ts = int(time.time() * 1000)
    return f"file-save-migration-{ts}-{random.randint(1000, 9999)}"


def _snapshot_file_save_migration_job(job_id):
    with _file_save_migration_lock:
        job = _file_save_migration_jobs.get(job_id)
        return dict(job) if isinstance(job, dict) else None


def _update_file_save_migration_job(job_id, **kwargs):
    with _file_save_migration_lock:
        job = _file_save_migration_jobs.get(job_id)
        if not job:
            return
        job.update(kwargs)
        job["updatedAt"] = time.time()


def _file_save_migration_public_job(job):
    if not isinstance(job, dict):
        return None
    public = dict(job)
    errors = public.get("errors")
    if isinstance(errors, list):
        public["errors"] = errors[:20]
    return public


def _count_file_save_migration_files(src):
    src = os.path.abspath(src)
    if not os.path.isdir(src):
        return 0
    total = 0
    for _, _, files in os.walk(src):
        total += len(files)
    return total


def _build_file_save_migration_steps(previous, normalized):
    return [
        {
            "key": "canvasDir",
            "label": "画布项目保存路径",
            "src": os.path.abspath(previous["canvasDir"]),
            "dst": os.path.abspath(normalized["canvasDir"]),
        },
        {
            "key": "outputDir",
            "label": "输出文件保存路径",
            "src": os.path.abspath(previous["outputDir"]),
            "dst": os.path.abspath(normalized["outputDir"]),
        },
        {
            "key": "tempDir",
            "label": "上传文件保存路径",
            "src": os.path.abspath(previous["tempDir"]),
            "dst": os.path.abspath(normalized["tempDir"]),
        },
        {
            "key": "assetsDir",
            "label": "资产库保存路径",
            "src": os.path.abspath(os.path.join(previous["dataDir"], "assets")),
            "dst": os.path.abspath(os.path.join(normalized["dataDir"], "assets")),
        },
        {
            "key": "workflowsDir",
            "label": "工作流保存路径",
            "src": os.path.abspath(os.path.join(previous["dataDir"], "workflows")),
            "dst": os.path.abspath(os.path.join(normalized["dataDir"], "workflows")),
        },
    ]


def _move_missing_tree_with_file_save_progress(job_id, step):
    src = os.path.abspath(step["src"])
    dst = os.path.abspath(step["dst"])
    label = str(step.get("label") or "")
    if _same_storage_path(src, dst) or not os.path.isdir(src):
        return

    os.makedirs(dst, exist_ok=True)
    for root, dirs, files in os.walk(src):
        rel_root = os.path.relpath(root, src)
        target_root = dst if rel_root == "." else os.path.join(dst, rel_root)
        os.makedirs(target_root, exist_ok=True)
        for dirname in dirs:
            os.makedirs(os.path.join(target_root, dirname), exist_ok=True)
        for filename in files:
            src_file = os.path.join(root, filename)
            dst_file = os.path.join(target_root, filename)
            rel_file = filename if rel_root == "." else os.path.join(rel_root, filename)
            current_file = rel_file.replace("\\", "/")
            with _file_save_migration_lock:
                job = _file_save_migration_jobs.get(job_id)
                if not job:
                    return
                job["stage"] = f"正在迁移{label}"
                job["currentBucket"] = step.get("key") or ""
                job["currentFile"] = current_file
                job["updatedAt"] = time.time()

            if os.path.exists(dst_file):
                with _file_save_migration_lock:
                    job = _file_save_migration_jobs.get(job_id)
                    if not job:
                        return
                    job["skippedCount"] = int(job.get("skippedCount") or 0) + 1
                    job["processedFiles"] = int(job.get("processedFiles") or 0) + 1
                    total_files = max(1, int(job.get("totalFiles") or 0))
                    job["progress"] = min(94, 6 + int((job["processedFiles"] / total_files) * 88))
                    job["updatedAt"] = time.time()
                continue

            try:
                shutil.move(src_file, dst_file)
                with _file_save_migration_lock:
                    job = _file_save_migration_jobs.get(job_id)
                    if not job:
                        return
                    job["copiedCount"] = int(job.get("copiedCount") or 0) + 1
                    job["copiedBytes"] = int(job.get("copiedBytes") or 0) + int(os.path.getsize(dst_file) or 0)
            except Exception as exc:
                with _file_save_migration_lock:
                    job = _file_save_migration_jobs.get(job_id)
                    if not job:
                        return
                    job["failedCount"] = int(job.get("failedCount") or 0) + 1
                    errors = job.get("errors")
                    if not isinstance(errors, list):
                        errors = []
                        job["errors"] = errors
                    if len(errors) < 20:
                        errors.append(
                            {
                                "bucket": step.get("key") or "",
                                "path": current_file,
                                "error": str(exc),
                            }
                        )
            finally:
                with _file_save_migration_lock:
                    job = _file_save_migration_jobs.get(job_id)
                    if not job:
                        should_stop = True
                    else:
                        job["processedFiles"] = int(job.get("processedFiles") or 0) + 1
                        total_files = max(1, int(job.get("totalFiles") or 0))
                        job["progress"] = min(94, 6 + int((job["processedFiles"] / total_files) * 88))
                        job["updatedAt"] = time.time()
            if should_stop:
                return
    _remove_empty_dirs(src)


def _run_file_save_migration_job(job_id, settings_payload, normalized, previous):
    try:
        steps = _build_file_save_migration_steps(previous, normalized)
        _update_file_save_migration_job(
            job_id,
            status="planning",
            stage="正在检查旧目录",
            progress=2,
        )
        for p in normalized.values():
            os.makedirs(p, exist_ok=True)

        total_files = 0
        step_summaries = []
        for step in steps:
            count = 0
            if not _same_storage_path(step["src"], step["dst"]):
                count = _count_file_save_migration_files(step["src"])
            total_files += count
            step_summaries.append(
                {
                    "key": step["key"],
                    "label": step["label"],
                    "source": step["src"],
                    "target": step["dst"],
                    "fileCount": count,
                }
            )

        _update_file_save_migration_job(
            job_id,
            status="moving",
            stage="正在迁移文件",
            progress=6 if total_files else 88,
            totalFiles=total_files,
            steps=step_summaries,
        )

        for step in steps:
            _move_missing_tree_with_file_save_progress(job_id, step)

        _update_file_save_migration_job(
            job_id,
            status="applying",
            stage="正在应用新的保存位置",
            progress=96,
            currentFile="",
            currentBucket="",
        )
        payload = dict(settings_payload) if isinstance(settings_payload, dict) else {}
        payload["fileSavePaths"] = normalized
        _write_user_settings(payload, migrate=False)
        applied_paths = _current_file_save_paths()
        _update_file_save_migration_job(
            job_id,
            status="done",
            stage="迁移完成",
            progress=100,
            settings=_read_user_settings(),
            targetPaths=applied_paths,
            completedAt=time.time(),
        )
    except Exception as exc:
        _update_file_save_migration_job(
            job_id,
            status="error",
            stage="迁移失败",
            error=str(exc),
            progress=100,
            completedAt=time.time(),
        )


def _start_file_save_migration(data):
    payload = dict(data) if isinstance(data, dict) else {}
    settings_payload = payload.get("settings")
    if not isinstance(settings_payload, dict):
        settings_payload = dict(payload)
    path_payload = payload.get("fileSavePaths")
    if not isinstance(path_payload, dict):
        path_payload = settings_payload.get("fileSavePaths")
    if not isinstance(path_payload, dict):
        raise ValueError("Missing fileSavePaths")

    normalized = _validate_file_save_paths(path_payload, migrate_legacy_defaults=False)
    previous = _current_file_save_paths()

    job_id = _new_file_save_migration_job_id()
    job = {
        "success": True,
        "jobId": job_id,
        "status": "pending",
        "stage": "准备迁移文件",
        "progress": 0,
        "previousPaths": previous,
        "targetPaths": normalized,
        "totalFiles": 0,
        "processedFiles": 0,
        "copiedCount": 0,
        "skippedCount": 0,
        "failedCount": 0,
        "copiedBytes": 0,
        "currentBucket": "",
        "currentFile": "",
        "errors": [],
        "startedAt": time.time(),
        "updatedAt": time.time(),
    }
    with _file_save_migration_lock:
        for active_job in _file_save_migration_jobs.values():
            if str(active_job.get("status") or "") in ("pending", "planning", "moving", "copying", "applying"):
                raise RuntimeError("文件迁移正在进行中，请等待当前迁移完成")
        _file_save_migration_jobs[job_id] = job

    thread = threading.Thread(
        target=_run_file_save_migration_job,
        args=(job_id, settings_payload, normalized, previous),
        daemon=True,
        name=f"FileSaveMigration-{job_id}",
    )
    thread.start()
    return _file_save_migration_public_job(job)


def _get_file_save_migration_status(job_id):
    job_id = str(job_id or "").strip()
    if not job_id:
        raise ValueError("Missing jobId")
    job = _snapshot_file_save_migration_job(job_id)
    if not job:
        raise FileNotFoundError("Migration job not found")
    return _file_save_migration_public_job(job)


def _refresh_storage_globals(paths):
    global USER_DIR, CANVAS_DIR, DATA_DIR, UPLOADS_DIR, ASSETS_DIR, ASSET_THUMBS_DIR
    global WORKFLOWS_DIR, WORKFLOW_THUMBS_DIR, OUTPUT_DIR, CONFIG_FILE, SETTINGS_FILE
    global GEN_SEQ_STATE_FILE, DREAMINA_CLI_SERVICE, DREAMINA_ROUTE_SERVICE
    USER_DIR = os.path.abspath(paths["userDir"])
    CANVAS_DIR = os.path.abspath(paths.get("canvasDir") or DEFAULT_CANVAS_DIR)
    DATA_DIR = os.path.abspath(paths["dataDir"])
    UPLOADS_DIR = os.path.abspath(paths.get("tempDir") or os.path.join(DATA_DIR, "uploads"))
    ASSETS_DIR = os.path.join(DATA_DIR, "assets")
    ASSET_THUMBS_DIR = os.path.join(ASSETS_DIR, "thumbs")
    WORKFLOWS_DIR = os.path.join(DATA_DIR, "workflows")
    WORKFLOW_THUMBS_DIR = os.path.join(ASSETS_DIR, "workflows", "thumbs")
    OUTPUT_DIR = os.path.abspath(paths["outputDir"])
    CONFIG_FILE = os.path.join(USER_DIR, "config.json")
    SETTINGS_FILE = os.path.join(USER_DIR, "settings.json")
    GEN_SEQ_STATE_FILE = os.path.join(OUTPUT_DIR, ".gen_seq_state.json")
    try:
        DREAMINA_CLI_SERVICE = DreaminaCliService(
            CONFIG_FILE,
            output_root_dir=OUTPUT_DIR,
            output_dir_getter=lambda: OUTPUT_DIR,
            uploads_dir_getter=lambda: UPLOADS_DIR,
            assets_dir_getter=lambda: ASSETS_DIR,
        )
        DREAMINA_ROUTE_SERVICE = DreaminaRouteService(
            cli_service=DREAMINA_CLI_SERVICE,
            subscription_gate_service=SUBSCRIPTION_GATE_SERVICE,
            video_required_model_id=DREAMINA_VIDEO_VIP_MODEL_ID,
        )
    except NameError:
        pass


def _ensure_storage_dirs():
    os.makedirs(USER_DIR, exist_ok=True)
    os.makedirs(CANVAS_DIR, exist_ok=True)
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(UPLOADS_DIR, exist_ok=True)
    os.makedirs(ASSETS_DIR, exist_ok=True)
    os.makedirs(ASSET_THUMBS_DIR, exist_ok=True)
    os.makedirs(WORKFLOWS_DIR, exist_ok=True)
    os.makedirs(WORKFLOW_THUMBS_DIR, exist_ok=True)
    os.makedirs(OUTPUT_DIR, exist_ok=True)


def _apply_file_save_paths(paths, migrate=False, migrate_legacy_defaults=True):
    normalized = _validate_file_save_paths(
        paths,
        migrate_legacy_defaults=migrate_legacy_defaults,
    )
    previous = _current_file_save_paths()
    for p in normalized.values():
        os.makedirs(p, exist_ok=True)
    if migrate:
        for step in _build_file_save_migration_steps(previous, normalized):
            _move_missing_tree(step["src"], step["dst"])
    _refresh_storage_globals(normalized)
    _ensure_storage_dirs()
    return _current_file_save_paths()


def _is_enabled_env(name):
    try:
        value = str(os.environ.get(name, "") or "").strip().lower()
    except Exception:
        return False
    return value in ("1", "true", "yes", "on")

def _resolve_subscription_api_base():
    allow_override = (
        _is_enabled_env("AIC_ALLOW_SUBSCRIPTION_API_OVERRIDE")
        or _is_enabled_env("AIC_DEV_MODE")
    )
    raw_override = (os.environ.get("AIC_SUBSCRIPTION_API_BASE", "") or "").strip()
    if allow_override and raw_override:
        return raw_override.rstrip("/"), True
    return OFFICIAL_SUBSCRIPTION_API_BASE, False

SUBSCRIPTION_API_BASE, SUBSCRIPTION_API_BASE_OVERRIDDEN = _resolve_subscription_api_base()
try:
    SUBSCRIPTION_TIMEOUT_SECONDS = int(
        (os.environ.get("AIC_SUBSCRIPTION_TIMEOUT_SEC", "5") or "5").strip()
    )
except Exception:
    SUBSCRIPTION_TIMEOUT_SECONDS = 5

SUBSCRIPTION_CLIENT = SubscriptionRemoteClient(
    api_base_url=SUBSCRIPTION_API_BASE,
    timeout_seconds=SUBSCRIPTION_TIMEOUT_SECONDS,
    status_active=SUB_STATUS_ACTIVE,
    err_required=SUB_ERROR_REQUIRED,
    required_message=SUB_MESSAGE_V54_REQUIRED,
    contact_text=DEFAULT_SUB_CONTACT_TEXT,
    contact_url=DEFAULT_SUB_CONTACT_URL,
    contact_wechat=DEFAULT_SUB_CONTACT_WECHAT,
)
SUBSCRIPTION_GATE_SERVICE = SubscriptionGateService(
    client=SUBSCRIPTION_CLIENT,
    status_active=SUB_STATUS_ACTIVE,
    status_none=SUB_STATUS_NONE,
    error_model_not_entitled=SUB_ERROR_MODEL_NOT_ENTITLED,
    model_name_map=VIDEO_VIP_MODEL_NAME_MAP,
    model_id_normalizer=normalize_subscription_gate_model_id,
    success_logger=lambda decision: print("[subscription][vip_gate] first VIP verification passed"),
)
os.makedirs(SYSTEM_STATE_DIR, exist_ok=True)
_startup_system_settings = _read_json_file(SYSTEM_SETTINGS_FILE, {})
_startup_local_settings = _read_json_file(os.path.join(DEFAULT_USER_DIR, "settings.json"), {})
_startup_settings = dict(_startup_local_settings)
_startup_system_file_save_paths = _read_system_file_save_paths()
if not _has_file_save_paths(_startup_settings) and _startup_system_file_save_paths:
    _startup_settings["fileSavePaths"] = _startup_system_file_save_paths
try:
    _startup_applied_file_save_paths = _apply_file_save_paths(
        _normalize_file_save_paths_for_policy(
            _startup_settings.get("fileSavePaths"),
            migrate_legacy_defaults=not _is_user_managed_file_save_paths(_startup_settings),
        ),
        migrate=False,
        migrate_legacy_defaults=False,
    )
    _migrate_legacy_default_files_to_current(_startup_applied_file_save_paths)
    _persist_local_file_save_paths_if_needed(
        _startup_local_settings,
        _startup_applied_file_save_paths,
    )
    if not _has_file_save_paths(_startup_local_settings) and _startup_system_file_save_paths:
        _migrate_system_file_save_paths_to_user_settings(
            _startup_local_settings,
            _startup_applied_file_save_paths,
        )
except Exception:
    _apply_file_save_paths(
        {
            "userDir": DEFAULT_USER_DIR,
            "outputDir": DEFAULT_OUTPUT_DIR,
            "dataDir": DEFAULT_DATA_DIR,
        },
        migrate=False,
    )
DREAMINA_CLI_SERVICE = DreaminaCliService(
    CONFIG_FILE,
    output_root_dir=OUTPUT_DIR,
    output_dir_getter=lambda: OUTPUT_DIR,
    uploads_dir_getter=lambda: UPLOADS_DIR,
    assets_dir_getter=lambda: ASSETS_DIR,
)
DREAMINA_ROUTE_SERVICE = DreaminaRouteService(
    cli_service=DREAMINA_CLI_SERVICE,
    subscription_gate_service=SUBSCRIPTION_GATE_SERVICE,
    video_required_model_id=DREAMINA_VIDEO_VIP_MODEL_ID,
)
# 确保目录存在
os.makedirs(ASSETS_DIR,  exist_ok=True)
os.makedirs(ASSET_THUMBS_DIR, exist_ok=True)
os.makedirs(WORKFLOWS_DIR, exist_ok=True)
os.makedirs(WORKFLOW_THUMBS_DIR, exist_ok=True)


def _read_user_settings():
    local_settings = _read_json_file(SETTINGS_FILE, {})
    system_settings = _read_json_file(SYSTEM_SETTINGS_FILE, {})

    system_install_id = str(system_settings.get("installId") or "").strip()
    local_install_id = str(local_settings.get("installId") or "").strip()
    local_file_save_paths = (
        local_settings.get("fileSavePaths")
        if isinstance(local_settings.get("fileSavePaths"), dict)
        else None
    )
    system_file_save_paths = _read_system_file_save_paths()

    # 兼容旧版本：首次读到仓库内 settings.json 的 installId 时自动迁移到系统目录。
    if not system_install_id and local_install_id:
        system_settings = dict(system_settings)
        system_settings["installId"] = local_install_id
        try:
            _write_json_file(SYSTEM_SETTINGS_FILE, system_settings)
        except Exception:
            pass
        system_install_id = local_install_id

    merged = dict(local_settings)
    if system_install_id:
        merged["installId"] = system_install_id
    if local_file_save_paths:
        merged["fileSavePaths"] = _normalize_file_save_paths_for_policy(
            local_file_save_paths,
            migrate_legacy_defaults=not _is_user_managed_file_save_paths(local_settings),
        )
    elif system_file_save_paths:
        normalized_paths = _normalize_file_save_paths_for_policy(system_file_save_paths)
        merged["fileSavePaths"] = normalized_paths
        _migrate_system_file_save_paths_to_user_settings(local_settings, normalized_paths)
    else:
        merged["fileSavePaths"] = _current_file_save_paths()
    return merged


def _write_user_settings(data, migrate=True):
    payload = dict(data) if isinstance(data, dict) else {}
    if isinstance(payload.get("fileSavePaths"), dict):
        applied_paths = _apply_file_save_paths(
            payload["fileSavePaths"],
            migrate=bool(migrate),
            migrate_legacy_defaults=False,
        )
        payload["fileSavePaths"] = applied_paths
        meta = payload.get("fileSavePathsMeta") if isinstance(payload.get("fileSavePathsMeta"), dict) else {}
        payload["fileSavePathsMeta"] = {
            **meta,
            "source": "user",
            "updatedAt": meta.get("updatedAt") or time.time(),
        }
    elif "fileSavePaths" not in payload:
        payload["fileSavePaths"] = _current_file_save_paths()
    _write_json_file(SETTINGS_FILE, payload)

    install_id = str(payload.get("installId") or "").strip()
    system_settings = _read_json_file(SYSTEM_SETTINGS_FILE, {})
    next_system_settings = dict(system_settings)
    if install_id:
        next_system_settings["installId"] = install_id
    next_system_settings.pop("fileSavePaths", None)
    _write_json_file(SYSTEM_SETTINGS_FILE, next_system_settings)


def _subscription_user_data_root():
    try:
        return os.path.dirname(os.path.abspath(USER_DIR))
    except Exception:
        return ""


def _subscription_device_identity_paths():
    paths = [
        os.path.join(_subscription_user_data_root(), DEVICE_IDENTITY_FILENAME),
        os.path.join(SYSTEM_STATE_DIR, DEVICE_IDENTITY_FILENAME),
    ]
    seen = set()
    unique = []
    for path_value in paths:
        path_text = str(path_value or "").strip()
        if not path_text:
            continue
        normalized = os.path.abspath(path_text)
        if normalized in seen:
            continue
        seen.add(normalized)
        unique.append(normalized)
    return unique


def _subscription_settings_authorization_paths():
    paths = [
        SETTINGS_FILE,
        SYSTEM_SETTINGS_FILE,
        os.path.join(DIRECTORY, "user", "settings.json"),
    ]
    seen = set()
    unique = []
    for path_value in paths:
        path_text = str(path_value or "").strip()
        if not path_text:
            continue
        normalized = os.path.abspath(path_text)
        if normalized in seen:
            continue
        seen.add(normalized)
        unique.append(normalized)
    return unique


def _collect_subscription_authorization_ids_from_file(path):
    data = _read_json_file(path, {})
    if not isinstance(data, dict):
        return [], []
    install_ids = []
    device_ids = []
    for key in ("installId", "install_id"):
        value = str(data.get(key) or "").strip()
        if value and value not in install_ids:
            install_ids.append(value)
    for key in ("deviceId", "device_id"):
        value = str(data.get(key) or "").strip()
        if value and value not in device_ids:
            device_ids.append(value)
    return install_ids, device_ids


def _strip_subscription_authorization_keys(path):
    data = _read_json_file(path, {})
    if not isinstance(data, dict):
        return False
    next_data = dict(data)
    changed = False
    for key in SUBSCRIPTION_AUTHORIZATION_ID_KEYS:
        if key in next_data:
            next_data.pop(key, None)
            changed = True
    if changed:
        _write_json_file(path, next_data)
    return changed


def _clear_subscription_authorization():
    if not _is_dev_build():
        raise PermissionError("仅开发模式可清空授权")

    install_ids = []
    device_ids = []
    for path_value in [
        *_subscription_settings_authorization_paths(),
        *_subscription_device_identity_paths(),
    ]:
        next_install_ids, next_device_ids = _collect_subscription_authorization_ids_from_file(
            path_value,
        )
        install_ids.extend([item for item in next_install_ids if item not in install_ids])
        device_ids.extend([item for item in next_device_ids if item not in device_ids])

    cleared = []
    for label, path_value in (
        ("userSettings", SETTINGS_FILE),
        ("systemSettings", SYSTEM_SETTINGS_FILE),
        ("legacyUserSettings", os.path.join(DIRECTORY, "user", "settings.json")),
    ):
        if _strip_subscription_authorization_keys(path_value):
            cleared.append(label)

    for path_value in _subscription_device_identity_paths():
        try:
            if os.path.isfile(path_value):
                os.remove(path_value)
                cleared.append("deviceIdentity")
        except FileNotFoundError:
            pass

    for install_id in install_ids:
        targets = device_ids or [""]
        for device_id in targets:
            try:
                SUBSCRIPTION_GATE_SERVICE.clear_vip_allow_cache(install_id, device_id)
            except Exception:
                pass

    return {
        "success": True,
        "status": SUB_STATUS_NONE,
        "cleared": cleared,
    }


def _is_dev_build():
    return os.path.exists(os.path.join(DIRECTORY, ".dev"))

def _is_advanced_mode():
    return os.path.exists(os.path.join(DIRECTORY, ".Advanced"))

UPDATE_SERVICE = HotUpdateService(
    directory=DIRECTORY,
    local_version=LOCAL_VERSION,
    is_dev_build=_is_dev_build,
)

CONFIG_ROUTE_SERVICE = ConfigRouteService(config_file_getter=lambda: CONFIG_FILE)
JSON_FILE_ROUTE_SERVICE = JsonFileRouteService(
    canvas_dir_getter=lambda: CANVAS_DIR,
    assets_dir_getter=lambda: ASSETS_DIR,
    workflows_dir_getter=lambda: WORKFLOWS_DIR,
    user_dir_getter=lambda: USER_DIR,
    read_user_settings=_read_user_settings,
    write_user_settings=_write_user_settings,
    start_file_save_migration=_start_file_save_migration,
    get_file_save_migration_status=_get_file_save_migration_status,
    atomic_write_json=lambda path, data: _atomic_write_json(path, data),
    output_dir_getter=lambda: OUTPUT_DIR,
    uploads_dir_getter=lambda: UPLOADS_DIR,
)
LIBRARY_FILE_ROUTE_SERVICE = LibraryFileRouteService(
    user_dir_getter=lambda: USER_DIR,
    asset_thumbs_dir_getter=lambda: ASSET_THUMBS_DIR,
    workflow_thumbs_dir_getter=lambda: WORKFLOW_THUMBS_DIR,
    subscription_gate_service_getter=lambda: SUBSCRIPTION_GATE_SERVICE,
)

def _get_custom_ai_config():
    return CONFIG_ROUTE_SERVICE.get_custom_ai_config()


def _request_server_port(handler):
    try:
        return int(handler.server.server_address[1])
    except Exception:
        return int(PORT)


def _local_allowed_origins(handler):
    port = _request_server_port(handler)
    return {
        f"http://127.0.0.1:{port}",
        f"http://localhost:{port}",
        f"http://[::1]:{port}",
    }


def _is_allowed_origin(handler, origin):
    normalized = _normalize_origin(origin)
    if not normalized:
        return False
    return normalized in _local_allowed_origins(handler) or normalized in ALLOWED_ORIGINS


def _allowed_cors_origin(handler):
    origin = handler.headers.get("Origin", "")
    normalized = _normalize_origin(origin)
    if normalized and _is_allowed_origin(handler, normalized):
        return normalized
    return ""


def _send_cors_origin_header(handler):
    origin = _allowed_cors_origin(handler)
    if not origin:
        return
    handler.send_header("Access-Control-Allow-Origin", origin)
    handler.send_header("Vary", "Origin")


def _client_is_loopback(handler):
    try:
        host = str(handler.client_address[0] or "").strip()
    except Exception:
        return False
    if host in ("localhost",):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except Exception:
        return False


def _request_has_valid_local_token(handler):
    if not LOCAL_ACCESS_TOKEN:
        return False
    token = str(handler.headers.get("X-AIC-Local-Token", "") or "").strip()
    auth = str(handler.headers.get("Authorization", "") or "").strip()
    if not token and auth.lower().startswith("bearer "):
        token = auth[7:].strip()
    return bool(token) and hmac.compare_digest(token, LOCAL_ACCESS_TOKEN)


_SENSITIVE_API_PREFIXES = (
    "/api/config",
    "/api/projects",
    "/api/upload",
    "/api/v2/assets",
    "/api/v2/chat",
    "/api/v2/config",
    "/api/v2/dreamina",
    "/api/v2/grid_tiles",
    "/api/v2/images/derivatives",
    "/api/v2/matting",
    "/api/v2/projects",
    "/api/v2/proxy",
    "/api/v2/runninghubwf",
    "/api/v2/save_output",
    "/api/v2/save_output_from_url",
    "/api/v2/output-files",
    "/api/v2/subscription/activate",
    "/api/v2/subscription/authorization/clear",
    "/api/v2/update/apply",
    "/api/v2/user",
    "/api/v2/video",
    "/api/v2/workflows",
)


def _is_sensitive_api_path(path):
    clean_path = str(path or "").split("?", 1)[0].rstrip("/") or "/"
    return any(
        clean_path == prefix or clean_path.startswith(prefix + "/")
        for prefix in _SENSITIVE_API_PREFIXES
    )


def _request_passes_local_security(handler, path):
    if not _is_sensitive_api_path(path):
        return True
    if LOCAL_ACCESS_TOKEN:
        return _request_has_valid_local_token(handler)
    origin = handler.headers.get("Origin", "")
    if origin:
        return _is_allowed_origin(handler, origin) or _request_has_valid_local_token(handler)
    return _client_is_loopback(handler) or _request_has_valid_local_token(handler)


def _enforce_local_api_access(handler, path):
    if _request_passes_local_security(handler, path):
        return True
    _json_err(handler, 403, "Forbidden: request origin is not allowed")
    return False


def _extract_install_id_from_request(handler, payload=None):
    return SUBSCRIPTION_GATE_SERVICE.extract_install_id_from_request(handler, payload)


def _enforce_vip_subscription_gate(handler, payload=None, required_model_id=""):
    decision = SUBSCRIPTION_GATE_SERVICE.check_vip_subscription_gate(
        handler,
        payload,
        required_model_id=required_model_id,
    )
    if bool(decision.get("allowed")):
        return True
    _json_ok(handler, SUBSCRIPTION_GATE_SERVICE.build_subscription_denial_payload(decision))
    return False


def _json_ok(handler, data):
    body = json.dumps(data, ensure_ascii=False, indent=2).encode()
    handler.send_response(200)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    _send_cors_origin_header(handler)
    handler.end_headers()
    try:
        handler.wfile.write(body)
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
        pass

def _json_err(handler, code, msg):
    body = json.dumps({"error": msg}, ensure_ascii=False, indent=2).encode()
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    _send_cors_origin_header(handler)
    handler.end_headers()
    try:
        handler.wfile.write(body)
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
        pass


def _send_route_response(handler, response):
    if not isinstance(response, dict):
        raise ValueError("Route response must be a dict")
    kind = str(response.get("kind") or "").strip()
    if kind == "json_ok":
        _json_ok(handler, response.get("data"))
        return
    if kind == "json_err":
        _json_err(
            handler,
            int(response.get("code") or 500),
            response.get("message") or "Unknown error",
        )
        return
    if kind == "binary":
        body = response.get("body") or b""
        if isinstance(body, str):
            body = body.encode("utf-8")
        else:
            body = bytes(body)
        handler.send_response(int(response.get("status") or 200))
        handler.send_header(
            "Content-Type",
            str(response.get("contentType") or "application/octet-stream"),
        )
        headers = response.get("headers") if isinstance(response.get("headers"), dict) else {}
        for name, value in headers.items():
            if str(name).lower() == "access-control-allow-origin":
                continue
            handler.send_header(str(name), str(value))
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        try:
            handler.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        return
    raise ValueError(f"Unknown route response kind: {kind}")

def _read_body(handler, max_bytes=None):
    te = (handler.headers.get("Transfer-Encoding", "") or "").lower()
    if "chunked" in te:
        chunks = []
        total = 0
        while True:
            line = handler.rfile.readline()
            if not line:
                break
            size_hex = line.split(b";", 1)[0].strip()
            try:
                size = int(size_hex, 16)
            except Exception:
                break
            if size == 0:
                handler.rfile.readline()
                break
            chunk = handler.rfile.read(size)
            total += len(chunk)
            if max_bytes is not None and total > max_bytes:
                raise ValueError("REQUEST_BODY_TOO_LARGE")
            chunks.append(chunk)
            handler.rfile.read(2)
        return b"".join(chunks)
    length = int(handler.headers.get("Content-Length", 0))
    if max_bytes is not None and length > max_bytes:
        raise ValueError("REQUEST_BODY_TOO_LARGE")
    return handler.rfile.read(length) if length > 0 else b""


def _iter_sse_data_lines(response):
    try:
        iterator = response.iter_lines(decode_unicode=True)
    except TypeError:
        iterator = response.iter_lines()
    except Exception:
        iterator = []

    for raw_line in iterator:
        if isinstance(raw_line, bytes):
            line = raw_line.decode("utf-8", errors="replace")
        else:
            line = str(raw_line or "")
        line = line.strip()
        if not line.startswith("data:"):
            continue
        yield line[5:].strip()


def _extract_chat_completion_text_parts(payload):
    if not isinstance(payload, dict):
        return []
    parts = []
    choices = payload.get("choices")
    if not isinstance(choices, list):
        data = payload.get("data")
        choices = data.get("choices") if isinstance(data, dict) else []
    for choice in choices or []:
        if not isinstance(choice, dict):
            continue
        delta = choice.get("delta")
        if isinstance(delta, dict) and isinstance(delta.get("content"), str):
            parts.append(delta.get("content") or "")
        message = choice.get("message")
        if isinstance(message, dict) and isinstance(message.get("content"), str):
            parts.append(message.get("content") or "")
        if isinstance(choice.get("text"), str):
            parts.append(choice.get("text") or "")
    for key in ("text", "output", "content"):
        if isinstance(payload.get(key), str):
            parts.append(payload.get(key) or "")
    data = payload.get("data")
    if isinstance(data, dict):
        for key in ("text", "output", "content"):
            if isinstance(data.get(key), str):
                parts.append(data.get(key) or "")
    return parts


def _normalize_chat_completion_sse_response(response):
    text_parts = []
    last_payload = None
    finish_reason = None
    role = "assistant"

    for data_line in _iter_sse_data_lines(response):
        if not data_line:
            continue
        if data_line == "[DONE]":
            break
        try:
            payload = json.loads(data_line)
        except Exception:
            continue
        if isinstance(payload, dict):
            last_payload = payload
            choices = payload.get("choices")
            if not isinstance(choices, list):
                data = payload.get("data")
                choices = data.get("choices") if isinstance(data, dict) else []
            if choices and isinstance(choices[0], dict):
                finish_reason = choices[0].get("finish_reason") or finish_reason
                delta = choices[0].get("delta")
                message = choices[0].get("message")
                if isinstance(delta, dict) and isinstance(delta.get("role"), str):
                    role = delta.get("role") or role
                if isinstance(message, dict) and isinstance(message.get("role"), str):
                    role = message.get("role") or role
            text_parts.extend(_extract_chat_completion_text_parts(payload))

    content = "".join(text_parts)
    if content:
        normalized = {
            "id": last_payload.get("id", "") if isinstance(last_payload, dict) else "",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": role,
                        "content": content,
                    },
                    "finish_reason": finish_reason or "stop",
                }
            ],
        }
        if isinstance(last_payload, dict) and isinstance(last_payload.get("usage"), dict):
            normalized["usage"] = last_payload.get("usage")
        return json.dumps(normalized, ensure_ascii=False)

    if isinstance(last_payload, dict):
        return json.dumps(last_payload, ensure_ascii=False)
    return ""


MEDIA_FILE_ROUTE_SERVICE = MediaFileRouteService(
    directory=DIRECTORY,
    uploads_dir_getter=lambda: UPLOADS_DIR,
    user_dir_getter=lambda: USER_DIR,
    assets_dir_getter=lambda: ASSETS_DIR,
    output_dir_getter=lambda: OUTPUT_DIR,
    max_upload_bytes=MAX_UPLOAD_BYTES,
    next_output_filename=lambda ext: _next_gen_output_filename(ext),
    load_json_file=lambda path: _load_json_file(path),
    atomic_write_json=lambda path, data: _atomic_write_json(path, data),
    read_body=_read_body,
    ffprobe_getter=lambda: FFPROBE_EXE,
    image_derivative_display_max_edge=IMAGE_DERIVATIVE_DISPLAY_MAX_EDGE,
    image_derivative_thumb_max_edge=IMAGE_DERIVATIVE_THUMB_MAX_EDGE,
    image_derivative_display_quality=IMAGE_DERIVATIVE_DISPLAY_QUALITY,
    image_derivative_thumb_quality=IMAGE_DERIVATIVE_THUMB_QUALITY,
    image_derivative_root_dirname=IMAGE_DERIVATIVE_ROOT_DIRNAME,
)


LOCAL_MEDIA_PROCESSING_ROUTE_SERVICE = LocalMediaProcessingRouteService(
    output_dir_getter=lambda: OUTPUT_DIR,
    resolve_local_virtual_path=lambda src_path: _resolve_local_virtual_path(src_path),
    read_body=_read_body,
    ffmpeg_getter=lambda: FFMPEG_EXE,
    ffprobe_getter=lambda: FFPROBE_EXE,
)


REMOTE_PROXY_ROUTE_SERVICE = RemoteProxyRouteService(
    read_body=_read_body,
    subscription_gate_service_getter=lambda: SUBSCRIPTION_GATE_SERVICE,
    video_vip_workflow_ids=VIDEO_VIP_WORKFLOW_IDS,
)


def _smart_clip_new_job_id():
    ts = int(time.time() * 1000)
    return f"smartclip_{ts}_{random.randint(1000, 9999)}"

def _smart_clip_cleanup(max_age_sec=2 * 60 * 60):
    try:
        now = time.time()
    except Exception:
        now = 0.0
    with _smart_clip_lock:
        expired = []
        for jid, job in list(_smart_clip_jobs.items()):
            try:
                created = float(job.get("createdAt") or 0.0)
            except Exception:
                created = 0.0
            if now - created > max_age_sec:
                expired.append(jid)
        for jid in expired:
            _smart_clip_jobs.pop(jid, None)

def _smart_clip_update(job_id, **kwargs):
    with _smart_clip_lock:
        job = _smart_clip_jobs.get(job_id)
        if not job:
            return
        for k, v in kwargs.items():
            job[k] = v


HTTP_ROUTE_DISPATCHER = HttpRouteDispatcher(
    local_version=LOCAL_VERSION,
    is_dev_build=_is_dev_build,
    is_advanced_mode=_is_advanced_mode,
    subscription_client_getter=lambda: SUBSCRIPTION_CLIENT,
    subscription_gate_service_getter=lambda: SUBSCRIPTION_GATE_SERVICE,
    clear_subscription_authorization=_clear_subscription_authorization,
    config_route_service_getter=lambda: CONFIG_ROUTE_SERVICE,
    json_file_route_service_getter=lambda: JSON_FILE_ROUTE_SERVICE,
    library_file_route_service_getter=lambda: LIBRARY_FILE_ROUTE_SERVICE,
    media_file_route_service_getter=lambda: MEDIA_FILE_ROUTE_SERVICE,
    local_media_processing_route_service_getter=lambda: LOCAL_MEDIA_PROCESSING_ROUTE_SERVICE,
    remote_proxy_route_service_getter=lambda: REMOTE_PROXY_ROUTE_SERVICE,
    dreamina_route_service_getter=lambda: DREAMINA_ROUTE_SERVICE,
    update_service_getter=lambda: UPDATE_SERVICE,
    smart_clip_cleanup=_smart_clip_cleanup,
    smart_clip_jobs=_smart_clip_jobs,
    smart_clip_lock=_smart_clip_lock,
    sub_status_none=SUB_STATUS_NONE,
    sub_error_invalid_arguments=SUB_ERROR_INVALID_ARGUMENTS,
    default_sub_contact_text=DEFAULT_SUB_CONTACT_TEXT,
    default_sub_contact_url=DEFAULT_SUB_CONTACT_URL,
    default_sub_contact_wechat=DEFAULT_SUB_CONTACT_WECHAT,
    json_ok=_json_ok,
    json_err=_json_err,
    send_route_response=_send_route_response,
    read_body=_read_body,
)


def _run_smart_clip_job(job_id, local_src, options):
    try:
        try:
            from scenedetect import open_video, SceneManager
            from scenedetect.detectors import ContentDetector
        except Exception as e:
            _smart_clip_update(
                job_id,
                status="error",
                stage="import",
                error=f"缺少依赖 scenedetect/opencv: {str(e)}。请在 venv 中执行 pip install -r requirements.txt",
                progress=0.0,
            )
            return

        opt = options if isinstance(options, dict) else {}
        raw_mode = str(opt.get("mode") or "stable").strip().lower()
        mode_map = {"stable": "stable", "balanced": "balanced", "sensitive": "sensitive"}
        mode = mode_map.get(raw_mode, raw_mode)
        if mode not in ("stable", "balanced", "sensitive"):
            mode = "stable"
        max_segments = _normalize_smart_clip_max_segments(
            opt.get("maxSegments", SMART_CLIP_DEFAULT_SEGMENTS)
        )
        output_fps = _normalize_smart_clip_fps(
            opt.get("fps", opt.get("frameRate", SMART_CLIP_DEFAULT_FPS))
        )
        output_mode = _normalize_smart_clip_output_mode(
            opt.get("outputMode", opt.get("outputType", opt.get("resultType")))
        )
        output_keyframes = output_mode == SMART_CLIP_OUTPUT_MODE_KEYFRAMES

        try:
            black_luma_thr = float(opt.get("blackLuma", 16.0))
        except Exception:
            black_luma_thr = 16.0
        black_luma_thr = max(0.0, min(60.0, black_luma_thr))
        try:
            min_black_sec = float(opt.get("minBlackSec", 0.5))
        except Exception:
            min_black_sec = 0.5
        min_black_sec = max(0.1, min(10.0, min_black_sec))

        _smart_clip_update(job_id, status="running", stage="detect", progress=0.01)

        startupinfo = None
        if os.name == "nt":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

        def _ffprobe_duration_sec(p):
            try:
                cmd = [
                    FFPROBE_EXE,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=nw=1:nk=1",
                    p,
                ]
                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    startupinfo=startupinfo,
                )
                stdout, _ = process.communicate(timeout=20)
                if process.returncode != 0:
                    return 0.0
                txt = (stdout or b"").decode("utf-8", errors="ignore").strip()
                return float(txt) if txt else 0.0
            except Exception:
                return 0.0

        def _ffprobe_video_size(p):
            try:
                cmd = [
                    FFPROBE_EXE,
                    "-v",
                    "error",
                    "-select_streams",
                    "v:0",
                    "-show_entries",
                    "stream=width,height",
                    "-of",
                    "json",
                    p,
                ]
                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    startupinfo=startupinfo,
                )
                stdout, _ = process.communicate(timeout=20)
                if process.returncode != 0:
                    return 0, 0
                data = json.loads((stdout or b"{}").decode("utf-8", errors="ignore") or "{}")
                streams = data.get("streams") if isinstance(data, dict) else None
                stream = streams[0] if isinstance(streams, list) and streams else {}
                width = int(stream.get("width") or 0)
                height = int(stream.get("height") or 0)
                return max(0, width), max(0, height)
            except Exception:
                return 0, 0

        duration_sec = _ffprobe_duration_sec(local_src)
        if not duration_sec or duration_sec <= 0:
            duration_sec = 0.0
        fps_str = str(output_fps)

        def _run_detect_content_boundaries(threshold, min_scene_sec):
            try:
                scene_manager = SceneManager()
                video = open_video(local_src)
                try:
                    fps = float(getattr(video, "frame_rate", 0.0) or 0.0)
                except Exception:
                    fps = 0.0
                if not fps or fps <= 0:
                    fps = 30.0
                min_scene_len = max(1, int(round(float(min_scene_sec) * fps)))
                scene_manager.add_detector(
                    ContentDetector(
                        threshold=float(threshold), min_scene_len=int(min_scene_len)
                    )
                )
                scene_manager.detect_scenes(video, show_progress=False)
                scene_list = scene_manager.get_scene_list() or []
                boundaries = []
                for i, (start_tc, _end_tc) in enumerate(scene_list):
                    if i == 0:
                        continue
                    try:
                        t = float(start_tc.get_seconds())
                    except Exception:
                        continue
                    if t and t > 0:
                        boundaries.append(t)
                dur = duration_sec
                if not dur or dur <= 0:
                    try:
                        if scene_list:
                            dur = float(scene_list[-1][1].get_seconds())
                    except Exception:
                        dur = 0.0
                return boundaries, dur
            except Exception:
                return [], duration_sec

        black_intervals = []
        try:
            import cv2

            if duration_sec and duration_sec > 0:
                sample_fps = 2.0 if duration_sec <= 900 else 1.0
                step = 1.0 / sample_fps
                cap = cv2.VideoCapture(local_src)
                t = 0.0
                blk_start = None
                margin = 0.15
                while t <= duration_sec:
                    cap.set(cv2.CAP_PROP_POS_MSEC, int(round(t * 1000)))
                    ok, frame = cap.read()
                    if not ok or frame is None:
                        t += step
                        continue
                    try:
                        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                        mean_luma = float(gray.mean())
                    except Exception:
                        mean_luma = 999.0
                    is_black = mean_luma <= black_luma_thr
                    if is_black:
                        if blk_start is None:
                            blk_start = t
                    else:
                        if blk_start is not None:
                            blk_end = t
                            if blk_end - blk_start >= min_black_sec:
                                s = max(0.0, blk_start - margin)
                                e = min(duration_sec, blk_end + margin)
                                if e > s:
                                    black_intervals.append((s, e))
                            blk_start = None
                    t += step
                if blk_start is not None:
                    blk_end = duration_sec
                    if blk_end - blk_start >= min_black_sec:
                        s = max(0.0, blk_start - margin)
                        e = min(duration_sec, blk_end)
                        if e > s:
                            black_intervals.append((s, e))
                try:
                    cap.release()
                except Exception:
                    pass
        except Exception:
            black_intervals = []

        def _is_in_black(mid_t):
            for s, e in black_intervals:
                if mid_t >= s and mid_t <= e:
                    return True
            return False

        def _postprocess(boundaries, min_scene_sec, debounce_sec, strip_black):
            bds = []
            for t in boundaries or []:
                try:
                    bds.append(float(t))
                except Exception:
                    pass
            for s, e in black_intervals:
                bds.append(float(s))
                bds.append(float(e))
            bds = [t for t in bds if duration_sec and t > 0.0 and t < duration_sec]
            bds.sort()

            debounced = []
            prev = None
            for t in bds:
                if prev is None:
                    debounced.append(t)
                    prev = t
                    continue
                if t - prev < float(debounce_sec):
                    continue
                debounced.append(t)
                prev = t
            bds = debounced

            raw_segments = []
            cur = 0.0
            for t in bds:
                if t - cur >= 0.05:
                    raw_segments.append((cur, t))
                cur = t
            if duration_sec and duration_sec - cur >= 0.05:
                raw_segments.append((cur, duration_sec))

            segments2 = []
            for s, e in raw_segments:
                if not (e > s):
                    continue
                mid = (s + e) / 2.0
                if strip_black and _is_in_black(mid):
                    continue
                segments2.append([float(s), float(e)])

            i = 0
            while i < len(segments2):
                s, e = segments2[i]
                dur = e - s
                if dur < float(min_scene_sec) and len(segments2) > 1:
                    if i == 0:
                        ns, ne = segments2[i + 1]
                        segments2[i + 1] = [s, ne]
                        segments2.pop(i)
                        continue
                    ps, pe = segments2[i - 1]
                    segments2[i - 1] = [ps, e]
                    segments2.pop(i)
                    i = max(0, i - 1)
                    continue
                i += 1

            segments2 = [seg for seg in segments2 if (seg[1] - seg[0]) >= 0.2]

            def _merge_to_limit(segs, limit):
                out = [list(x) for x in (segs or [])]
                if limit <= 1:
                    return out
                while len(out) > int(limit):
                    shortest_i = 0
                    shortest_d = 999999.0
                    for i, (s, e) in enumerate(out):
                        d = float(e) - float(s)
                        if d < shortest_d:
                            shortest_d = d
                            shortest_i = i
                    if len(out) <= 1:
                        break
                    if shortest_i == 0:
                        out[1] = [out[0][0], out[1][1]]
                        out.pop(0)
                        continue
                    if shortest_i == len(out) - 1:
                        out[-2] = [out[-2][0], out[-1][1]]
                        out.pop(-1)
                        continue
                    left_d = out[shortest_i - 1][1] - out[shortest_i - 1][0]
                    right_d = out[shortest_i + 1][1] - out[shortest_i + 1][0]
                    if left_d <= right_d:
                        out[shortest_i - 1] = [out[shortest_i - 1][0], out[shortest_i][1]]
                        out.pop(shortest_i)
                    else:
                        out[shortest_i + 1] = [out[shortest_i][0], out[shortest_i + 1][1]]
                        out.pop(shortest_i)
                return out

            segments2 = _merge_to_limit(segments2, max_segments)
            return segments2

        def _equal_split(duration_sec, max_segments):
            if not duration_sec or duration_sec <= 0:
                return []
            desired = int(round(duration_sec / 3.0))
            desired = max(2, desired)
            desired = min(int(max_segments), desired)
            step = float(duration_sec) / float(desired)
            if step < 0.2:
                desired = max(2, min(int(max_segments), int(duration_sec / 0.2)))
                if desired <= 1:
                    return []
                step = float(duration_sec) / float(desired)
            out = []
            t = 0.0
            for i in range(desired):
                s = t
                e = float(duration_sec) if i == desired - 1 else min(float(duration_sec), s + step)
                if e - s >= 0.2:
                    out.append([s, e])
                t = e
                if t >= duration_sec:
                    break
            return out

        profiles = {
            "stable": {"threshold": 27.0, "min_scene_sec": 1.0, "debounce_sec": 0.3, "strip_black": True},
            "balanced": {"threshold": 23.0, "min_scene_sec": 0.6, "debounce_sec": 0.2, "strip_black": True},
            "sensitive": {"threshold": 18.0, "min_scene_sec": 0.25, "debounce_sec": 0.1, "strip_black": False},
        }
        chain = ["stable", "balanced", "sensitive"] if mode == "stable" else (["balanced", "sensitive"] if mode == "balanced" else ["sensitive"])

        segments2 = []
        for key in chain:
            prof = profiles[key]
            content_boundaries, dur2 = _run_detect_content_boundaries(prof["threshold"], prof["min_scene_sec"])
            if dur2 and dur2 > 0 and (not duration_sec or duration_sec <= 0):
                duration_sec = dur2
            segments2 = _postprocess(content_boundaries, prof["min_scene_sec"], prof["debounce_sec"], prof["strip_black"])
            if len(segments2) >= 2:
                break

        if len(segments2) <= 1:
            segments2 = _equal_split(duration_sec, max_segments)

        if len(segments2) <= 1:
            _smart_clip_update(
                job_id,
                status="done",
                stage="done",
                progress=1.0,
                segments=[],
                outputMode=output_mode,
            )
            return

        segments = []
        for i, (s, e) in enumerate(segments2):
            segments.append({"index": i + 1, "start": s, "end": e, "duration": e - s})

        _smart_clip_update(
            job_id,
            stage="frame" if output_keyframes else "cut",
            progress=0.05,
            total=len(segments),
            outputMode=output_mode,
        )

        out_dir_name = "SceneKeyframes" if output_keyframes else "SceneCuts"
        out_dir = os.path.join(OUTPUT_DIR, out_dir_name, job_id)
        os.makedirs(out_dir, exist_ok=True)

        out_segments = []
        total = len(segments)
        for idx, seg in enumerate(segments):
            s = float(seg["start"])
            e = float(seg["end"])
            dur = max(0.01, e - s)
            ms_s = int(round(s * 1000))
            ms_e = int(round(e * 1000))
            filename = (
                f"scene_{idx+1:03d}_{ms_s}.jpg"
                if output_keyframes
                else f"scene_{idx+1:03d}_{ms_s}-{ms_e}.mp4"
            )
            out_path = os.path.join(out_dir, filename)

            if output_keyframes:
                cmd = [
                    FFMPEG_EXE,
                    "-y",
                    "-ss",
                    str(s),
                    "-i",
                    local_src,
                    "-frames:v",
                    "1",
                    "-q:v",
                    "2",
                    out_path,
                ]
            else:
                cmd = [
                    FFMPEG_EXE,
                    "-y",
                    "-ss",
                    str(s),
                    "-i",
                    local_src,
                    "-t",
                    str(dur),
                    "-c:v",
                    "libx264",
                    "-preset",
                    "fast",
                    "-c:a",
                    "aac",
                    out_path,
                ]
                if fps_str:
                    cmd.insert(-1, "-r")
                    cmd.insert(-1, fps_str)

            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                startupinfo=startupinfo,
            )
            try:
                _, stderr = process.communicate(timeout=300)
            except subprocess.TimeoutExpired:
                process.kill()
                _smart_clip_update(
                    job_id,
                    status="error",
                    stage="frame" if output_keyframes else "cut",
                    error="FFmpeg process timeout",
                )
                return
            if process.returncode != 0:
                try:
                    err_text = (stderr or b"").decode("utf-8", errors="ignore").strip()
                except Exception:
                    err_text = ""
                _smart_clip_update(
                    job_id,
                    status="error",
                    stage="frame" if output_keyframes else "cut",
                    error=f"FFmpeg processing failed: {err_text or 'unknown error'}",
                )
                return

            rel = f"output/{out_dir_name}/{job_id}/{filename}"
            segment_result = {
                "index": idx + 1,
                "start": s,
                "end": e,
                "duration": dur,
                "fps": output_fps,
                "path": rel,
                "localPath": rel,
                "url": f"/{rel}",
            }
            if output_keyframes:
                width, height = _ffprobe_video_size(out_path)
                segment_result.update(
                    {
                        "fileName": filename,
                        "outputType": "image",
                        "width": width,
                        "height": height,
                    }
                )
            out_segments.append(segment_result)

            p = 0.05 + 0.95 * float(idx + 1) / float(total)
            _smart_clip_update(
                job_id,
                stage="frame" if output_keyframes else "cut",
                progress=min(0.999, p),
                doneCount=idx + 1,
                total=total,
                outputMode=output_mode,
            )

        _smart_clip_update(
            job_id,
            status="done",
            stage="done",
            progress=1.0,
            segments=out_segments,
            outputMode=output_mode,
        )
    except Exception as e:
        _smart_clip_update(job_id, status="error", stage="error", error=str(e))

def _load_json_file(p):
    try:
        if not os.path.exists(p):
            return {}
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def _atomic_write_json(p, data):
    tmp = p + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        raise

def _scan_max_gen_seq_for_date(date_str):
    try:
        pat = re.compile(r"^gen_" + re.escape(date_str) + r"_(\d+)\.[a-z0-9]{1,5}$")
        max_n = 0
        for root, _, files in os.walk(OUTPUT_DIR):
            for fn in files:
                m = pat.match(fn)
                if not m:
                    continue
                try:
                    n = int(m.group(1))
                    if n > max_n:
                        max_n = n
                except Exception:
                    continue
        return max_n
    except Exception:
        return 0

def _next_gen_output_filename(ext):
    date_str = datetime.datetime.now().strftime("%Y%m%d")
    with _gen_seq_lock:
        state = _load_json_file(GEN_SEQ_STATE_FILE)
        last = 0
        try:
            last = int(state.get(date_str) or 0)
        except Exception:
            last = 0
        if last <= 0:
            scanned = _scan_max_gen_seq_for_date(date_str)
            if scanned > last:
                last = scanned
        n = last + 1
        state[date_str] = n
        try:
            _atomic_write_json(GEN_SEQ_STATE_FILE, state)
        except Exception:
            pass
    seq = str(n).zfill(4)
    return f"gen_{date_str}_{seq}.{ext}"


def _normalize_posix_rel_path(path_value):
    return MediaFileRouteService._normalize_posix_rel_path(path_value)


def _join_virtual_local_path(root_prefix, rel_path):
    return MediaFileRouteService._join_virtual_local_path(root_prefix, rel_path)


def _resolve_virtual_media_root(local_path=None, abs_path=None):
    return MEDIA_FILE_ROUTE_SERVICE.resolve_virtual_media_root(local_path, abs_path)


def _collect_image_derivative_payload(abs_path, root_abs, root_prefix, rel_original_path):
    return MEDIA_FILE_ROUTE_SERVICE.collect_image_derivative_payload(
        abs_path,
        root_abs,
        root_prefix,
        rel_original_path,
    )


def _augment_saved_media_response(payload, abs_path, local_path):
    return MEDIA_FILE_ROUTE_SERVICE.augment_saved_media_response(payload, abs_path, local_path)


def _resolve_local_virtual_path(src_path):
    return MEDIA_FILE_ROUTE_SERVICE.resolve_local_virtual_path(src_path)


class Handler(http.server.SimpleHTTPRequestHandler):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DIRECTORY, **kwargs)

    def translate_path(self, path):
        raw_path = urllib.parse.urlsplit(path).path
        decoded_path = urllib.parse.unquote(raw_path).replace("\\", "/")
        virtual_roots = (
            ("/user/prompt/_thumbs/", os.path.join(USER_DIR, "prompt", "_thumbs")),
            ("/data/workflows/", WORKFLOWS_DIR),
        )
        media_path = _resolve_local_virtual_path(decoded_path)
        if media_path:
            return media_path
        for prefix, root_dir in virtual_roots:
            if decoded_path == prefix[:-1] or decoded_path.startswith(prefix):
                rel = decoded_path[len(prefix):].lstrip("/")
                rel = os.path.normpath(rel)
                if rel in ("", "."):
                    return os.path.abspath(root_dir)
                if rel.startswith(".."):
                    return os.path.abspath(root_dir)
                return os.path.abspath(os.path.join(root_dir, rel))
        return super().translate_path(path)

    # 屏蔽日志噪音（按霢注释掉）
    def log_message(self, fmt, *args):
        pass

    def send_head(self):
        path = self.translate_path(self.path)
        f = None
        if os.path.isdir(path):
            parts = urllib.parse.urlsplit(self.path)
            if not parts.path.endswith('/'):
                self.send_response(301)
                new_parts = (parts[0], parts[1], parts[2] + '/', parts[3], parts[4])
                new_url = urllib.parse.urlunsplit(new_parts)
                self.send_header("Location", new_url)
                self.end_headers()
                return None
            for index in ("index.html", "index.htm"):
                index_path = os.path.join(path, index)
                if os.path.exists(index_path):
                    path = index_path
                    break
            else:
                return self.list_directory(path)
        ctype = self.guess_type(path)
        try:
            f = open(path, 'rb')
        except OSError:
            self.send_error(404, "File not found")
            return None

        fs = os.fstat(f.fileno())
        size = fs.st_size
        range_header = self.headers.get("Range", "")
        self._range = None

        if range_header.startswith("bytes="):
            spec = range_header[6:].strip()
            if "," not in spec:
                start_s, dash, end_s = spec.partition("-")
                try:
                    if start_s == "":
                        suffix_len = int(end_s)
                        if suffix_len <= 0:
                            raise ValueError()
                        start = max(0, size - suffix_len)
                        end = size - 1
                    else:
                        start = int(start_s)
                        end = int(end_s) if end_s else size - 1
                    if start < 0 or start >= size:
                        raise ValueError()
                    end = min(end, size - 1)
                    if end < start:
                        raise ValueError()
                    self._range = (start, end)
                except Exception:
                    f.close()
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return None

        if self._range:
            start, end = self._range
            self.send_response(206)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Last-Modified", self.date_time_string(fs.st_mtime))
            self.end_headers()
            f.seek(start)
            return f

        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(size))
        self.send_header("Last-Modified", self.date_time_string(fs.st_mtime))
        self.end_headers()
        return f

    def copyfile(self, source, outputfile):
        rng = getattr(self, "_range", None)
        if not rng:
            return super().copyfile(source, outputfile)
        start, end = rng
        remaining = end - start + 1
        bufsize = 64 * 1024
        while remaining > 0:
            chunk = source.read(min(bufsize, remaining))
            if not chunk:
                break
            outputfile.write(chunk)
            remaining -= len(chunk)

    # ┢┢ OPTIONS 预检（CORS）─┢┢┢┢┢┢┢┢┢┢┢┢┢┢┢┢┢┢┢┢┢┢┢┢┢┢┢┢┢
    def do_OPTIONS(self):
        path = self.path.split("?")[0]
        if not _enforce_local_api_access(self, path):
            return
        self.send_response(204)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, PATCH, OPTIONS")
        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type, Authorization, X-AIC-Install-Id, X-AIC-Device-Id, X-AIC-Local-Token",
        )
        self.end_headers()

    # ════════════════════════════════════════════════════
    #  DELETE  /api/v2/projects/{filename}
    # ════════════════════════════════════════════════════
    def do_DELETE(self):
        path = self.path.split("?")[0]
        if not _enforce_local_api_access(self, path):
            return
        if HTTP_ROUTE_DISPATCHER.handle_delete(self, path):
            return

        _json_err(self, 400, "Invalid request")

    # ════════════════════════════════════════════════════
    #  PATCH  /api/v2/projects/{filename}  ?rename
    # ════════════════════════════════════════════════════
    def do_PATCH(self):
        path = self.path.split("?")[0]
        if not _enforce_local_api_access(self, path):
            return
        if HTTP_ROUTE_DISPATCHER.handle_patch(self, path):
            return

        _json_err(self, 400, "Invalid request")

    # ════════════════════════════════════════════════════
    #  GET
    # ════════════════════════════════════════════════════
    def do_GET(self):
        path = self.path.split("?")[0]
        if not _enforce_local_api_access(self, path):
            return

        if HTTP_ROUTE_DISPATCHER.handle_get(self, path):
            return

        # --- 其余静态资源交给 SimpleHTTPRequestHandler 处理 ---
        try:
            super().do_GET()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass

    def end_headers(self):
        # 避免重复响应头导致浏览器 CORS 拒绝（例如 "*, *"）
        header_buf = getattr(self, "_headers_buffer", []) or []
        has_cache_control = any(b"Cache-Control:" in h for h in header_buf)
        has_cors = any(b"Access-Control-Allow-Origin:" in h for h in header_buf)
        has_server_id = any(b"X-AICanvas-Server:" in h for h in header_buf)
        if not has_server_id:
            self.send_header("X-AICanvas-Server", "AI CanvasPro")
        if not has_cache_control:
            self.send_header(
                "Cache-Control",
                _resolve_static_cache_control(getattr(self, "path", "")),
            )
        if not has_cors:
            _send_cors_origin_header(self)
        super().end_headers()

    # ════════════════════════════════════════════════════
    #  POST
    # ════════════════════════════════════════════════════
    def do_POST(self):
        path = self.path.split("?")[0]
        if not _enforce_local_api_access(self, path):
            return

        if HTTP_ROUTE_DISPATCHER.handle_post(self, path):
            return

        if path == "/api/v2/proxy/apimart-upload":
            try:
                content_type_header = self.headers.get("Content-Type", "") or ""
                body = _read_body(self)
                filename = "upload.bin"
                file_content_type = "application/octet-stream"
                file_extension = ""
                api_key = ""
                api_url = "https://api.apimart.ai"
                permanent = False
                file_bytes = b""

                if content_type_header.startswith("multipart/form-data"):
                    match = re.search(r"boundary=([^;]+)", content_type_header)
                    boundary = (match.group(1).strip().strip('"') if match else "")
                    if not boundary:
                        _json_err(self, 400, "Missing multipart boundary"); return
                    boundary_bytes = ("--" + boundary).encode("utf-8", "ignore")
                    for part in body.split(boundary_bytes):
                        if b"Content-Disposition:" not in part:
                            continue
                        header_end = part.find(b"\r\n\r\n")
                        if header_end == -1:
                            continue
                        header_blob = part[:header_end].decode("utf-8", "ignore")
                        data_blob = part[header_end + 4 :]
                        if data_blob.endswith(b"\r\n"):
                            data_blob = data_blob[:-2]
                        if data_blob.endswith(b"--"):
                            data_blob = data_blob[:-2]
                        name_match = re.search(r'name="([^"]+)"', header_blob)
                        field_name = name_match.group(1) if name_match else ""
                        if field_name == "file":
                            file_bytes = data_blob
                            filename_match = re.search(r'filename="([^"]*)"', header_blob)
                            if filename_match and filename_match.group(1).strip():
                                filename = os.path.basename(filename_match.group(1).strip())
                            type_match = re.search(r"Content-Type:\s*([^\r\n;]+)", header_blob, flags=re.IGNORECASE)
                            if type_match and type_match.group(1).strip():
                                file_content_type = type_match.group(1).strip()
                        elif field_name in ("contentType", "fileExtension", "permanent", "apiKey", "apiUrl"):
                            value = data_blob.decode("utf-8", "ignore").strip()
                            if field_name == "contentType" and value:
                                file_content_type = value
                            elif field_name == "fileExtension" and value:
                                file_extension = value.lstrip(".")
                            elif field_name == "permanent":
                                permanent = value.lower() in ("1", "true", "yes", "on")
                            elif field_name == "apiKey":
                                api_key = re.sub(r"^Bearer\s+", "", value, flags=re.IGNORECASE).strip()
                            elif field_name == "apiUrl" and value:
                                api_url = re.sub(r"/v1/?$", "", value.rstrip("/"), flags=re.IGNORECASE)
                else:
                    file_bytes = body
                    file_content_type = content_type_header.split(";", 1)[0].strip() or file_content_type

                if not file_bytes:
                    _json_err(self, 400, "Missing upload file"); return
                if not file_extension:
                    file_extension = (os.path.splitext(filename)[1] or "").lstrip(".")
                if not file_extension:
                    file_extension = (mimetypes.guess_extension(file_content_type) or ".bin").lstrip(".")

                if api_key and file_content_type.lower().startswith("image/"):
                    normalized_api_url = re.sub(
                        r"/v1/?$",
                        "",
                        str(api_url or 'https://api.apimart.ai').strip().rstrip("/"),
                        flags=re.IGNORECASE,
                    )
                    upload_url = f"{normalized_api_url}/v1/uploads/images"
                    try:
                        import requests as _req
                        resp = _req.post(
                            upload_url,
                            headers={
                                "Authorization": f"Bearer {api_key}",
                                "User-Agent": "Mozilla/5.0",
                            },
                            files={
                                "file": (
                                    filename,
                                    file_bytes,
                                    file_content_type,
                                )
                            },
                            timeout=300,
                        )
                        content = resp.content
                        self.send_response(resp.status_code)
                        self.send_header(
                            "Content-Type",
                            resp.headers.get("Content-Type") or "application/json; charset=utf-8",
                        )
                        _send_cors_origin_header(self)
                        self.send_header("Content-Length", str(len(content)))
                        self.end_headers()
                        self.wfile.write(content)
                    except ImportError:
                        boundary = f"----AICanvasAPIMartUpload{random.randint(100000, 999999)}"
                        safe_filename = filename.replace('"', "_")
                        body_prefix = (
                            f"--{boundary}\r\n"
                            f'Content-Disposition: form-data; name="file"; filename="{safe_filename}"\r\n'
                            f"Content-Type: {file_content_type}\r\n\r\n"
                        ).encode("utf-8")
                        body_suffix = f"\r\n--{boundary}--\r\n".encode("utf-8")
                        req_body = body_prefix + file_bytes + body_suffix
                        req = urllib.request.Request(
                            upload_url,
                            data=req_body,
                            headers={
                                "Authorization": f"Bearer {api_key}",
                                "Content-Type": f"multipart/form-data; boundary={boundary}",
                                "User-Agent": "Mozilla/5.0",
                            },
                            method="POST",
                        )
                        try:
                            with urllib.request.urlopen(req, timeout=300) as resp:
                                content = resp.read()
                                status = resp.status
                                content_type = resp.headers.get("Content-Type") or "application/json; charset=utf-8"
                        except urllib.error.HTTPError as exc:
                            content = exc.read()
                            status = exc.code
                            content_type = exc.headers.get("Content-Type") or "application/json; charset=utf-8"
                        self.send_response(status)
                        self.send_header("Content-Type", content_type)
                        _send_cors_origin_header(self)
                        self.send_header("Content-Length", str(len(content)))
                        self.end_headers()
                        self.wfile.write(content)
                    except Exception as e:
                        _json_err(self, 500, f"APIMART official upload error: {repr(e)}")
                    return

                presign_payload = {
                    "contentType": file_content_type,
                    "fileExtension": file_extension,
                    "permanent": bool(permanent),
                }
                try:
                    import requests as _req
                    presign_resp = _req.post(
                        "https://apimart.ai/api/upload/presign",
                        json=presign_payload,
                        headers={
                            "Content-Type": "application/json",
                            "User-Agent": "Mozilla/5.0",
                        },
                        timeout=60,
                    )
                    presign_resp.raise_for_status()
                    presign_data = presign_resp.json()
                    presigned_url = str(presign_data.get("presignedUrl") or "")
                    cdn_url = str(presign_data.get("cdnUrl") or "")
                    if not presigned_url or not cdn_url:
                        raise RuntimeError("invalid presign response")
                    upload_resp = _req.put(
                        presigned_url,
                        data=file_bytes,
                        headers={"Content-Type": file_content_type},
                        timeout=300,
                    )
                    upload_resp.raise_for_status()
                except ImportError:
                    req_body = json.dumps(presign_payload).encode("utf-8")
                    req = urllib.request.Request(
                        "https://apimart.ai/api/upload/presign",
                        data=req_body,
                        headers={
                            "Content-Type": "application/json",
                            "User-Agent": "Mozilla/5.0",
                        },
                        method="POST",
                    )
                    with urllib.request.urlopen(req, timeout=60) as resp:
                        presign_data = json.loads(resp.read().decode("utf-8", errors="replace"))
                    presigned_url = str(presign_data.get("presignedUrl") or "")
                    cdn_url = str(presign_data.get("cdnUrl") or "")
                    if not presigned_url or not cdn_url:
                        raise RuntimeError("invalid presign response")
                    put_req = urllib.request.Request(
                        presigned_url,
                        data=file_bytes,
                        headers={"Content-Type": file_content_type},
                        method="PUT",
                    )
                    with urllib.request.urlopen(put_req, timeout=300):
                        pass

                _json_ok(
                    self,
                    {
                        "url": cdn_url,
                        "cdnUrl": cdn_url,
                        "content_type": file_content_type,
                        "bytes": len(file_bytes),
                    },
                )
            except Exception as e:
                _json_err(self, 500, f"APIMART upload proxy error: {repr(e)}")
            return

        # ┢┢ 文件上传 ┢┢
        if path.rstrip("/") == "/api/v2/video/smart_clip":
            body = _read_body(self)
            try:
                data = json.loads(body or b"{}")
            except Exception:
                _json_err(self, 400, "Invalid JSON")
                return

            src_path = (data.get("src") or "").strip()
            options = data.get("options") or {}
            if not isinstance(options, dict):
                options = {}
            output_mode = _normalize_smart_clip_output_mode(
                options.get("outputMode", options.get("outputType", options.get("resultType")))
            )

            if not src_path:
                _json_err(self, 400, "Missing src")
                return

            safe_src = src_path.lstrip("/")
            norm_src = os.path.normpath(safe_src)
            if norm_src.startswith("..") or norm_src.startswith("../") or norm_src.startswith("..\\"):
                _json_err(self, 400, "Invalid src path")
                return
            local_src = _resolve_local_virtual_path(src_path)

            if not local_src or not os.path.exists(local_src):
                _json_err(self, 404, "Source video not found")
                return

            job_id = _smart_clip_new_job_id()
            try:
                created_at = time.time()
            except Exception:
                created_at = 0.0

            with _smart_clip_lock:
                _smart_clip_jobs[job_id] = {
                    "success": True,
                    "jobId": job_id,
                    "status": "running",
                    "stage": "queued",
                    "progress": 0.0,
                    "segments": None,
                    "outputMode": output_mode,
                    "error": None,
                    "createdAt": created_at,
                }

            t = threading.Thread(
                target=_run_smart_clip_job,
                args=(job_id, local_src, options),
                daemon=True,
            )
            t.start()

            _json_ok(self, {"success": True, "jobId": job_id})
            return

        if path == "/api/v2/video/matting/run":
            body = _read_body(self)
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                _json_err(self, 400, "Invalid JSON"); return

            api_key = (data.get("apiKey") or "").strip()
            node_info_list = data.get("nodeInfoList")
            if not api_key or not isinstance(node_info_list, list):
                _json_err(self, 400, "Missing apiKey or nodeInfoList"); return

            app_id = str(data.get("appId") or "2042569732972355585").strip() or "2042569732972355585"
            instance_type = data.get("instanceType") or data.get("rhInstanceType") or ""
            instance_type = str(instance_type).strip().lower()
            if instance_type in ("24g", "default", "basic"):
                instance_type = "default"
            elif instance_type in ("48g", "plus", "pro"):
                instance_type = "plus"
            else:
                instance_type = "default"

            def _resolve_local_file(url_or_path: str):
                fp = _resolve_local_virtual_path(url_or_path)
                if fp and os.path.isfile(fp):
                    return fp
                return None

            def _guess_filename(raw: str, fallback_name: str):
                path_name = ""
                try:
                    path_name = os.path.basename(urllib.parse.urlparse(raw).path or "")
                except Exception:
                    path_name = ""
                candidate = path_name or fallback_name
                if "." not in os.path.basename(candidate):
                    fallback_ext = os.path.splitext(fallback_name)[1] or ".bin"
                    candidate = f"{candidate}{fallback_ext}"
                return candidate

            def _download_remote_bytes(url: str):
                try:
                    import requests as _req
                    resp = _req.get(url, timeout=120)
                    resp.raise_for_status()
                    return resp.content
                except ImportError:
                    with urllib.request.urlopen(url, timeout=120) as resp:
                        return resp.read()

            def _upload_to_runninghub(file_bytes: bytes, filename: str, content_type: str = "application/octet-stream"):
                upload_api_url = "https://www.runninghub.cn/openapi/v2/media/upload/binary"
                try:
                    import requests as _req
                    files = {"file": (filename, file_bytes, content_type or "application/octet-stream")}
                    resp = _req.post(
                        upload_api_url,
                        files=files,
                        headers={"Authorization": f"Bearer {api_key}"},
                        timeout=120,
                    )
                    resp.raise_for_status()
                    js = resp.json()
                    if js.get("code") != 0:
                        raise RuntimeError(js.get("message") or js.get("msg") or "upload failed")
                    u = (js.get("data") or {}).get("download_url") or ""
                    if not u:
                        raise RuntimeError("upload missing download_url")
                    return u
                except ImportError:
                    import uuid
                    import urllib.request
                    import urllib.error
                    boundary = "----WebKitFormBoundary" + uuid.uuid4().hex
                    head = (
                        f"--{boundary}\r\n"
                        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
                        f"Content-Type: {content_type or 'application/octet-stream'}\r\n\r\n"
                    ).encode("utf-8")
                    tail = f"\r\n--{boundary}--\r\n".encode("utf-8")
                    payload = head + file_bytes + tail
                    req = urllib.request.Request(upload_api_url, data=payload, method="POST")
                    req.add_header("Authorization", f"Bearer {api_key}")
                    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
                    req.add_header("Content-Length", str(len(payload)))
                    with urllib.request.urlopen(req, timeout=120) as resp:
                        rb = resp.read()
                    js = json.loads(rb.decode("utf-8", errors="replace"))
                    if js.get("code") != 0:
                        raise RuntimeError(js.get("message") or js.get("msg") or "upload failed")
                    u = (js.get("data") or {}).get("download_url") or ""
                    if not u:
                        raise RuntimeError("upload missing download_url")
                    return u

            def _materialize_media_url(raw_value: str, fallback_name: str, fallback_content_type: str):
                raw = str(raw_value or "").strip()
                if not raw:
                    raise RuntimeError("missing media fieldValue")
                if "runninghub.cn" in raw:
                    return raw

                local_file = _resolve_local_file(raw)
                if local_file:
                    with open(local_file, "rb") as f:
                        file_bytes = f.read()
                    filename = os.path.basename(local_file) or fallback_name
                    content_type = mimetypes.guess_type(filename)[0] or fallback_content_type
                    return _upload_to_runninghub(file_bytes, filename, content_type)

                if raw.startswith("data:"):
                    match = re.match(r"^data:([^;,]+)?;base64,(.*)$", raw, re.DOTALL)
                    if not match:
                        raise RuntimeError("invalid data url")
                    mime_type = (match.group(1) or fallback_content_type or "application/octet-stream").strip()
                    ext = mimetypes.guess_extension(mime_type) or os.path.splitext(fallback_name)[1] or ".bin"
                    filename_root = os.path.splitext(fallback_name)[0] or "upload"
                    filename = f"{filename_root}{ext}"
                    try:
                        file_bytes = base64.b64decode(match.group(2))
                    except Exception as exc:
                        raise RuntimeError("invalid base64 media payload") from exc
                    return _upload_to_runninghub(file_bytes, filename, mime_type)

                if raw.startswith("http://") or raw.startswith("https://"):
                    file_bytes = _download_remote_bytes(raw)
                    filename = _guess_filename(raw, fallback_name)
                    content_type = mimetypes.guess_type(filename)[0] or fallback_content_type
                    return _upload_to_runninghub(file_bytes, filename, content_type)

                raise RuntimeError("unsupported media url")

            try:
                source_video_item = None
                mask_item = None
                for item in node_info_list:
                    if not isinstance(item, dict):
                        continue
                    node_id = str(item.get("nodeId") or "")
                    field_name = str(item.get("fieldName") or "")
                    if node_id == "117" and field_name == "video":
                        source_video_item = item
                    elif node_id == "63" and field_name == "image":
                        mask_item = item

                if not source_video_item:
                    _json_err(self, 400, "Missing source video node 117/video"); return
                if not mask_item:
                    _json_err(self, 400, "Missing erase mask node 63/image"); return

                source_video_item["fieldValue"] = _materialize_media_url(
                    source_video_item.get("fieldValue"),
                    "input.mp4",
                    "video/mp4",
                )
                mask_item["fieldValue"] = _materialize_media_url(
                    mask_item.get("fieldValue"),
                    "erase-mask.png",
                    "image/png",
                )

                api_url = f"https://www.runninghub.cn/openapi/v2/run/ai-app/{app_id}"
                payload = {
                    "nodeInfoList": node_info_list,
                    "instanceType": instance_type,
                    "usePersonalQueue": "false",
                }
                request_headers = {
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                }

                try:
                    import requests as _req
                    resp = _req.post(
                        api_url,
                        json=payload,
                        headers=request_headers,
                        timeout=900,
                    )
                    self.send_response(resp.status_code)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    _send_cors_origin_header(self)
                    self.end_headers()
                    self.wfile.write(resp.content)
                except ImportError:
                    import urllib.request, urllib.error
                    req_body = json.dumps(payload).encode("utf-8")
                    req = urllib.request.Request(api_url, data=req_body, method="POST")
                    req.add_header("Authorization", f"Bearer {api_key}")
                    req.add_header("Content-Type", "application/json")
                    req.add_header("User-Agent", "Mozilla/5.0")
                    try:
                        with urllib.request.urlopen(req, timeout=900) as resp:
                            resp_data = resp.read()
                        self.send_response(resp.status)
                        self.send_header("Content-Type", "application/json; charset=utf-8")
                        _send_cors_origin_header(self)
                        self.end_headers()
                        self.wfile.write(resp_data)
                    except urllib.error.HTTPError as e:
                        self.send_response(e.code)
                        self.send_header("Content-Type", "application/json; charset=utf-8")
                        _send_cors_origin_header(self)
                        self.end_headers()
                        self.wfile.write(e.read())
            except Exception as e:
                _json_err(self, 500, f"Video matting proxy error: {repr(e)}")
            return

        # ┢┢ PPIO 图像生成代理 ┢┢
        if path == "/api/v2/proxy/image":
            body = _read_body(self)
            try:
                data = json.loads(body)
                api_url = data.pop("apiUrl", "").strip().rstrip("/")
                api_key = data.pop("apiKey", "").strip()
            except json.JSONDecodeError:
                _json_err(self, 400, "Invalid JSON"); return
            if not api_url or not api_key:
                _json_err(self, 400, "Missing apiUrl or apiKey"); return
            local_authorization_payload = dict(data) if isinstance(data, dict) else {}
            for key in SUBSCRIPTION_AUTHORIZATION_ID_KEYS:
                data.pop(key, None)
            def _extract_task_id_from_text(raw_text):
                text = str(raw_text or "")
                if not text:
                    return ""
                patterns = [
                    r'"task_id"\s*:\s*"([^"]+)"',
                    r'"taskId"\s*:\s*"([^"]+)"',
                    r'"data"\s*:\s*"([^"]{8,})"',
                    r'\btask[_-]?id\b\s*[:=]\s*["\']?([a-zA-Z0-9._:-]+)["\']?',
                ]
                for pattern in patterns:
                    match = re.search(pattern, text, flags=re.IGNORECASE)
                    if match:
                        value = str(match.group(1) or "").strip()
                        if value:
                            return value
                return ""
            workflow_match = re.search(
                r"/openapi/v2/run/ai-app/(\d+)$",
                api_url,
                flags=re.IGNORECASE,
            )
            workflow_id = workflow_match.group(1) if workflow_match else ""
            is_runninghub_query_endpoint = bool(
                re.search(r"/openapi/v2/query(?:$|[/?])", api_url, flags=re.IGNORECASE)
            )
            # 仅在“提交任务”类端点启用 task_id 快速探测；
            # 查询类端点和 GRSAI 新 JSON 端点必须透传完整响应，否则前端无法拿到最终出图 URL。
            is_grsai_query_endpoint = bool(
                re.search(
                    r"/v1/(?:draw/(?:result|query)|api/result)(?:$|[/?])",
                    api_url,
                    flags=re.IGNORECASE,
                )
            )
            is_grsai_generate_endpoint = bool(
                re.search(r"/v1/api/generate(?:$|[/?])", api_url, flags=re.IGNORECASE)
            )
            allow_task_probe_short_circuit = not (
                is_runninghub_query_endpoint
                or is_grsai_query_endpoint
                or is_grsai_generate_endpoint
            )
            if workflow_id in VIDEO_VIP_WORKFLOW_IDS:
                if not _enforce_vip_subscription_gate(
                    self,
                    local_authorization_payload,
                    required_model_id=f"runninghub/{workflow_id}",
                ):
                    return
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0",
                # 减少代理复用连接被远端提前关闭导致的偶发断链
                "Connection": "close",
            }
            try:
                import requests as _req
                retry_delays = (0.0, 0.3, 0.9)
                proxy_error_markers = (
                    "ProxyError",
                    "Unable to connect to proxy",
                    "RemoteDisconnected",
                    "MaxRetryError",
                )
                for attempt_idx, delay_sec in enumerate(retry_delays):
                    if delay_sec > 0:
                        time.sleep(delay_sec)
                    try:
                        resp = _req.post(
                            api_url,
                            json=data,
                            headers=headers,
                            timeout=900,
                            stream=True,
                        )
                        header_task_id = ""
                        for key in (
                            "x-task-id",
                            "x-taskid",
                            "task-id",
                            "taskid",
                            "x-job-id",
                            "job-id",
                        ):
                            value = str(resp.headers.get(key, "") or "").strip()
                            if value:
                                header_task_id = value
                                break
                        if header_task_id and allow_task_probe_short_circuit:
                            _json_ok(
                                self,
                                {
                                    "task_id": header_task_id,
                                    "status": "submitted",
                                    "source": "header",
                                },
                            )
                            try:
                                resp.close()
                            except Exception:
                                pass
                            return

                        chunks = []
                        bytes_read = 0
                        max_probe_bytes = 256 * 1024
                        found_task_id = ""
                        for chunk in resp.iter_content(chunk_size=4096):
                            if not chunk:
                                continue
                            chunks.append(chunk)
                            bytes_read += len(chunk)
                            if found_task_id:
                                continue
                            if bytes_read <= max_probe_bytes:
                                probe_text = b"".join(chunks).decode("utf-8", "ignore")
                                found_task_id = _extract_task_id_from_text(probe_text)
                                if found_task_id and allow_task_probe_short_circuit:
                                    _json_ok(
                                        self,
                                        {
                                            "task_id": found_task_id,
                                            "status": "submitted",
                                            "source": "body-probe",
                                        },
                                    )
                                    try:
                                        resp.close()
                                    except Exception:
                                        pass
                                    return

                        full_content = b"".join(chunks)
                        self.send_response(resp.status_code)
                        self.send_header("Content-Type", "application/json; charset=utf-8")
                        _send_cors_origin_header(self)
                        self.end_headers()
                        self.wfile.write(full_content)
                        return
                    except _req.exceptions.ProxyError:
                        if attempt_idx == len(retry_delays) - 1:
                            raise
                    except _req.exceptions.ConnectionError as e:
                        msg = repr(e)
                        is_proxy_chain_error = any(
                            marker in msg for marker in proxy_error_markers
                        )
                        if is_proxy_chain_error:
                            if attempt_idx == len(retry_delays) - 1:
                                raise
                            continue
                        raise
            except ImportError:
                import urllib.request, urllib.error
                req_body = json.dumps(data).encode("utf-8")
                req = urllib.request.Request(api_url, data=req_body, headers=headers, method="POST")
                retry_delays = (0.0, 0.3, 0.9)
                proxy_error_markers = (
                    "ProxyError",
                    "Unable to connect to proxy",
                    "RemoteDisconnected",
                    "MaxRetryError",
                )
                for attempt_idx, delay_sec in enumerate(retry_delays):
                    if delay_sec > 0:
                        time.sleep(delay_sec)
                    try:
                        with urllib.request.urlopen(req, timeout=900) as resp:
                            resp_data = resp.read()
                        self.send_response(resp.status)
                        self.send_header("Content-Type", "application/json; charset=utf-8")
                        _send_cors_origin_header(self)
                        self.end_headers()
                        self.wfile.write(resp_data)
                        return
                    except urllib.error.HTTPError as e:
                        self.send_response(e.code)
                        self.send_header("Content-Type", "application/json; charset=utf-8")
                        _send_cors_origin_header(self)
                        self.end_headers()
                        self.wfile.write(e.read())
                        return
                    except urllib.error.URLError as e:
                        msg = repr(e)
                        is_proxy_chain_error = any(
                            marker in msg for marker in proxy_error_markers
                        )
                        if is_proxy_chain_error:
                            if attempt_idx == len(retry_delays) - 1:
                                raise
                            continue
                        raise
            except Exception as e:
                _json_err(self, 500, f"Proxy error: {repr(e)}")
            return

        # ┢┢ 通用代理 forwarded ┢┢
        if path == "/api/v2/proxy/completions":
            body = _read_body(self)
            try:
                data = json.loads(body)
                api_url = data.pop("apiUrl", "").strip().rstrip("/")
                api_key = data.pop("apiKey", "").strip()
            except json.JSONDecodeError:
                _json_err(self, 400, "Invalid JSON"); return
            
            if not api_url or not api_key:
                global_cfg = _get_custom_ai_config()
                api_url = api_url or global_cfg["apiUrl"]
                api_key = api_key or global_cfg["apiKey"]

            if not api_url or not api_key:
                _json_err(self, 400, "Missing apiUrl or apiKey"); return
            
            # 兼容 Gemini 和 OpenAI 风格接口
            if (
                ":generateContent" in api_url
                or "/v1beta/models" in api_url
                or api_url.endswith("/chat/completions")
                or api_url.endswith("/responses")
            ):
                endpoint = api_url
            else:
                endpoint = f"{api_url}/chat/completions"
            
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                "Accept": "application/json"
            }
            
            try:
                import requests
                req_body = json.dumps(data)
                try:
                    resp = requests.post(endpoint, data=req_body, headers=headers, timeout=300, stream=True)
                    # 生成请求允许最长 300 秒，与 aiTextApi.js 保持一致
                except requests.exceptions.ConnectionError as ce:
                    _json_err(self, 502, f"连接 AI 服务失败: {str(ce)}")
                    return
                except requests.exceptions.Timeout as te:
                    _json_err(self, 504, f"AI 服务请求超时: {str(te)}")
                    return
                except requests.exceptions.RequestException as req_err:
                    _json_err(self, 502, f"AI 服务请求失败: {str(req_err)}")
                    return
                
                # 兼容返回 SSE 的服务，转换为普通 JSON
                resp_content_type = resp.headers.get('Content-Type', '')
                try:
                    if 'text/event-stream' in resp_content_type.lower():
                        resp_text = _normalize_chat_completion_sse_response(resp)
                    else:
                        resp_text = resp.text
                finally:
                    try:
                        resp.close()
                    except Exception:
                        pass
                if not resp_text:
                    resp_text = "{}"
                
                # 处理 text/event-stream 或以 data: 开头的响应
                is_sse = resp_text.strip().startswith('data:')
                if is_sse:
                    try:
                        # 取 SSE 最后一条有效 JSON
                        lines = [l.strip() for l in resp_text.split('\n') if l.strip().startswith('data:')]
                        if lines:
                            last_line = lines[-1].replace('data:', '').strip()
                            if last_line == '[DONE]':
                                # 找数第二个有效行
                                valid_lines = [l for l in lines if l.replace('data:', '').strip() != '[DONE]']
                                if valid_lines:
                                    json_str = valid_lines[-1].replace('data:', '').strip()
                                    json_data = json.loads(json_str)
                                    resp_text = json.dumps(json_data)
                            else:
                                json_data = json.loads(last_line)
                                resp_text = json.dumps(json_data)
                    except Exception:
                        # 解析失败时保留原始响应
                        pass
                
                self.send_response(resp.status_code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                _send_cors_origin_header(self)
                self.end_headers()
                self.wfile.write(resp_text.encode('utf-8'))
            except ImportError:
                # Fallback to urllib if requests is not installed
                import urllib.request
                req_body = json.dumps(data).encode("utf-8")
                req = urllib.request.Request(endpoint, data=req_body, headers=headers, method="POST")
                try:
                    with urllib.request.urlopen(req, timeout=120) as resp:
                        resp_data = resp.read()
                        resp_text = resp_data.decode('utf-8')
                    
                    # 兼容返回 SSE 的服务，转换为普通 JSON
                    if resp_text.strip().startswith('data:'):
                        try:
                            lines = [l.strip() for l in resp_text.split('\n') if l.strip().startswith('data:')]
                            if lines:
                                last_line = lines[-1].replace('data:', '').strip()
                                if last_line == '[DONE]':
                                    valid_lines = [l for l in lines if l.replace('data:', '').strip() != '[DONE]']
                                    if valid_lines:
                                        json_str = valid_lines[-1].replace('data:', '').strip()
                                        json_data = json.loads(json_str)
                                        resp_text = json.dumps(json_data)
                                else:
                                    json_data = json.loads(last_line)
                                    resp_text = json.dumps(json_data)
                        except Exception:
                            pass

                    self.send_response(resp.status)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    _send_cors_origin_header(self)
                    self.end_headers()
                    self.wfile.write(resp_text.encode('utf-8'))
                except urllib.error.HTTPError as e:
                    self.send_response(e.code)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    _send_cors_origin_header(self)
                    self.end_headers()
                    self.wfile.write(e.read())
            except Exception as e:
                _json_err(self, 500, repr(e))
            return

        # --- 自定义 AI 聊天接口，兼容 OpenAI 格式 ---
        if path == "/api/v2/chat":
            body = _read_body(self)
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                _json_err(self, 400, "Invalid JSON"); return
            api_url  = data.get("apiUrl", "").strip().rstrip("/")
            api_key  = data.get("apiKey", "").strip()
            model    = data.get("model", "")
            prompt   = data.get("prompt", "")
            # apiUrl/apiKey 未传时，回退到 config.json 中的自定义 AI 配置
            if not api_url or not api_key:
                global_cfg = _get_custom_ai_config()
                api_url = api_url or global_cfg["apiUrl"]
                api_key = api_key or global_cfg["apiKey"]
            if not api_url or not api_key or not model or not prompt:
                _json_err(self, 400, "Missing required fields: apiUrl, apiKey, model, prompt"); return
            
            # 若未指定完整端点，则默认拼接 /chat/completions
            endpoint = api_url if api_url.endswith("/chat/completions") else f"{api_url}/chat/completions"
            
            import urllib.request
            req_body = json.dumps({
                "model": model,
                "messages": [{"role": "user", "content": prompt}]
            }).encode("utf-8")
            req = urllib.request.Request(
                endpoint,
                data=req_body,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    resp_data = json.loads(resp.read().decode("utf-8"))

                content = resp_data["choices"][0]["message"]["content"]
                _json_ok(self, {"content": content})
            except urllib.error.HTTPError as e:
                err_body = e.read().decode("utf-8", errors="ignore")
                try: err_msg = json.loads(err_body).get("error", {}).get("message", err_body)
                except: err_msg = err_body
                _json_err(self, e.code, err_msg)
            except urllib.error.URLError as e:
                _json_err(self, 502, f"AI service connection failed: {getattr(e, 'reason', e)}")
            except Exception as e:
                _json_err(self, 500, str(e))
            return

        _json_err(self, 404, "Not found")


def _is_wildcard_bind_host(host):
    normalized = str(host or "").strip().lower()
    return normalized in ("0.0.0.0", "::", "[::]", "*")


def _parse_server_args(argv):
    port = PORT
    bind_host = BIND_HOST
    lan_mode = bool(LAN_MODE)
    positional = []
    for arg in argv:
        raw = str(arg or "").strip()
        if not raw:
            continue
        if raw == "--lan":
            lan_mode = True
            continue
        if raw.startswith("--host="):
            bind_host = raw.split("=", 1)[1].strip() or bind_host
            continue
        if raw.startswith("--port="):
            try:
                port = int(raw.split("=", 1)[1].strip())
            except Exception:
                port = PORT
            continue
        positional.append(raw)

    if positional:
        try:
            port = int(positional[0])
        except Exception:
            port = PORT
    if len(positional) > 1:
        bind_host = positional[1].strip() or bind_host

    return port, bind_host, lan_mode


def _resolve_bind_host(bind_host, lan_mode):
    host = str(bind_host or "").strip() or "127.0.0.1"
    if lan_mode and host in ("127.0.0.1", "localhost"):
        return "0.0.0.0", False
    if _is_wildcard_bind_host(host) and not lan_mode:
        return "127.0.0.1", True
    return host, False


def _display_urls(bind_host, port):
    host = str(bind_host or "").strip()
    if _is_wildcard_bind_host(host):
        hosts = ["127.0.0.1", "localhost"]
    elif host in ("127.0.0.1", "localhost"):
        hosts = ["127.0.0.1", "localhost"]
    else:
        hosts = [host]
    urls = []
    for item in hosts:
        url_host = f"[{item}]" if ":" in item and not item.startswith("[") else item
        urls.append(f"http://{url_host}:{port}/")
    return urls


# --- 启动 ---
def _is_benign_client_disconnect_error(error):
    current = error
    seen = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
            return True
        current = getattr(current, "__context__", None) or getattr(current, "__cause__", None)
    return False


class QuietThreadingTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        error = sys.exc_info()[1]
        if _is_benign_client_disconnect_error(error):
            return
        super().handle_error(request, client_address)


if __name__ == "__main__":
    # 后台启动自动更新检查
    _t = threading.Thread(target=UPDATE_SERVICE.update_check_loop, daemon=True, name='AutoUpdateChecker')
    _t.start()
    port, requested_bind_host, lan_mode = _parse_server_args(sys.argv[1:])
    bind_host, bind_host_was_restricted = _resolve_bind_host(requested_bind_host, lan_mode)
    with QuietThreadingTCPServer((bind_host, port), Handler) as httpd:
        print("=" * 56)
        if SUBSCRIPTION_API_BASE_OVERRIDDEN:
            print(f"[subscription] api base override enabled: {SUBSCRIPTION_API_BASE}")
        else:
            print("[subscription] api base = official")
        if bind_host_was_restricted:
            print("[security] 0.0.0.0 需要显式局域网模式，已回退到 127.0.0.1")
        if lan_mode:
            print("[security] 局域网模式已开启，请通过 AIC_ALLOWED_ORIGINS 配置可信 Origin")
        print("AI Canvas 服务已启动")
        for url in _display_urls(bind_host, port):
            print(url)
        print("按 Ctrl+C 停止服务")
        print("=" * 56)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n服务已停止。")
