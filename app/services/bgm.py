import math
import os
import subprocess
import tempfile
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

from loguru import logger

from app.utils import file_security, utils

# Server-side upload size limit for background music files.
MAX_BGM_UPLOAD_BYTES = 30 * 1024 * 1024
_COPY_CHUNK_BYTES = 1024 * 1024
_INTERNAL_UPLOAD_PREFIX = ".bgm-upload-"
_WINDOWS_INVALID_FILENAME_CHARS = frozenset('<>:"|?*')
_WINDOWS_RESERVED_FILENAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)
# Supported background audio extensions decoded via FFmpeg / MoviePy.
SUPPORTED_BGM_EXTENSIONS = (
    ".mp3",
    ".m4a",
    ".aac",
    ".wav",
    ".flac",
    ".ogg",
    ".opus",
    ".wma",
)

class BgmUploadError(ValueError):
    """Indicates that an uploaded file does not satisfy security or format requirements."""

class BgmServiceError(RuntimeError):
    """Indicates backend failure during FFmpeg validation or persistent storage."""

def should_use_bgm(bgm_type: str | None, bgm_volume: float | None) -> bool:
    """
    Determine whether the current task requires background music processing.
    """
    if not str(bgm_type or "").strip():
        return False
    try:
        normalized_volume = float(bgm_volume or 0)
    except (TypeError, ValueError):
        return False
    return math.isfinite(normalized_volume) and normalized_volume > 0

def uploaded_bgm_dir(create: bool = True) -> str:
    """
    Return persistence directory for uploaded background music.
    """
    return utils.storage_dir("bgm", create=create)

def _remove_staged_file(file_path: str) -> None:
    """Clean up staging files on error."""
    if not file_path or not os.path.exists(file_path):
        return
    try:
        os.remove(file_path)
    except OSError as exc:
        logger.warning(
            f"failed to remove staged background music: path={file_path}, "
            f"error={str(exc)}"
        )

def sanitize_upload_filename(filename: str) -> str:
    """Extract sanitized filename and validate against illegal characters and extensions."""
    safe_name = (filename or "").replace("\\", "/").split("/")[-1].strip()
    if (
        not safe_name
        or safe_name in {".", ".."}
        or len(safe_name) > 255
        or any(ord(character) < 32 for character in safe_name)
        or any(character in _WINDOWS_INVALID_FILENAME_CHARS for character in safe_name)
        or safe_name.lower().startswith(_INTERNAL_UPLOAD_PREFIX)
    ):
        raise BgmUploadError("invalid background music filename")

    # Reject Windows reserved device names.
    windows_basename = safe_name.split(".", 1)[0].rstrip(" .").upper()
    if windows_basename in _WINDOWS_RESERVED_FILENAMES:
        raise BgmUploadError("invalid background music filename")
    if Path(safe_name).suffix.lower() not in SUPPORTED_BGM_EXTENSIONS:
        supported_formats = ", ".join(
            extension.removeprefix(".").upper()
            for extension in SUPPORTED_BGM_EXTENSIONS
        )
        raise BgmUploadError(
            f"unsupported background music format; supported formats: {supported_formats}"
        )
    return safe_name

def _validate_audio(file_path: str, timeout_seconds: int = 30) -> None:
    """
    仅使用项目当前配置的 FFmpeg 验证文件包含可完整解码的音频流。

    项目允许 imageio-ffmpeg 提供便携 FFmpeg，该安装方式不保证同时存在
    FFprobe，因此不能新增独立二进制依赖。`-map 0:a:0` 会在没有音频流时失败，
    `-xerror` 会把解码错误提升为失败；完整解码还能拦截加密文件或随机数据偶然
    命中音频帧头的误判。文件可以包含专辑封面等附加流，但只校验第一条音频流。
    """
    try:
        decoded = subprocess.run(
            [
                utils.get_ffmpeg_binary(),
                "-nostdin",
                "-v",
                "error",
                "-xerror",
                "-i",
                file_path,
                "-map",
                "0:a:0",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise BgmServiceError("FFmpeg background music validation timed out") from exc
    except OSError as exc:
        raise BgmServiceError("failed to run FFmpeg for background music validation") from exc
    if decoded.returncode != 0:
        raise BgmUploadError("uploaded file must contain a decodable audio stream")

def validate_audio_file(file_path: str, timeout_seconds: int = 120) -> None:
    """
    校验磁盘上的音频文件可由项目 FFmpeg 完整解码。

    上传预检通常只需 30 秒；Sonilo 生成的配乐最长可达 6 分钟，因此对外提供
    可调整超时的复用入口。服务只依赖 FFmpeg，不要求系统额外安装 FFprobe。
    """
    if not os.path.isfile(file_path) or os.path.getsize(file_path) <= 0:
        raise BgmUploadError("background music file is empty or missing")
    _validate_audio(file_path, timeout_seconds=timeout_seconds)

def _stage_bgm_upload(filename: str, source: BinaryIO) -> tuple[str, str, int]:
    """
    将上传流写入同目录临时文件，并返回安全文件名、临时路径和字节数。

    WebUI 的上传预检和最终持久化必须使用完全相同的分块读取、大小限制与文件名
    规则，否则可能出现界面显示可用、点击生成后却被服务端拒绝的状态分裂。
    临时文件由调用方在完成音频探测后删除或原子替换。
    """
    safe_name = sanitize_upload_filename(filename)
    try:
        target_dir = uploaded_bgm_dir(create=True)
    except OSError as exc:
        raise BgmServiceError("failed to prepare background music storage") from exc
    temp_path = ""
    total_bytes = 0

    try:
        try:
            source.seek(0)
        except (AttributeError, OSError) as exc:
            raise BgmUploadError("background music upload is not seekable") from exc

        # Preserve original extension for demuxer format detection; staged in target directory for atomic rename.
        
        descriptor, temp_path = tempfile.mkstemp(
            prefix=_INTERNAL_UPLOAD_PREFIX,
            suffix=Path(safe_name).suffix.lower(),
            dir=target_dir,
        )
        with os.fdopen(descriptor, "wb") as output:
            while True:
                chunk = source.read(_COPY_CHUNK_BYTES)
                if not chunk:
                    break
                if not isinstance(chunk, (bytes, bytearray, memoryview)):
                    raise BgmUploadError("background music upload must be binary")
                total_bytes += len(chunk)
                if total_bytes > MAX_BGM_UPLOAD_BYTES:
                    raise BgmUploadError("background music file exceeds the 30 MB limit")
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())

        if total_bytes == 0:
            raise BgmUploadError("background music file is empty")
        return safe_name, temp_path, total_bytes
    except Exception as exc:
        _remove_staged_file(temp_path)
        if isinstance(exc, BgmUploadError):
            raise
        if isinstance(exc, OSError):
            raise BgmServiceError("failed to stage background music upload") from exc
        raise
    finally:
        # Rewind file pointer after validation so player reads full content.
        
        try:
            source.seek(0)
        except (AttributeError, OSError):
            pass

def validate_bgm_upload(filename: str, source: BinaryIO) -> str:
    """Fully validate uploaded audio without persisting to disk."""
    safe_name, temp_path, total_bytes = _stage_bgm_upload(filename, source)
    try:
        _validate_audio(temp_path)
        logger.debug(
            f"background music upload validated: name={safe_name}, "
            f"size={total_bytes} bytes"
        )
        return safe_name
    finally:
        _remove_staged_file(temp_path)

def save_bgm_upload(filename: str, source: BinaryIO) -> str:
    """
    Save uploaded background music using chunked writes and atomic rename.
    """
    safe_name, temp_path, total_bytes = _stage_bgm_upload(filename, source)
    stored_name = f"{uuid4().hex}{Path(safe_name).suffix.lower()}"
    target_path = os.path.join(os.path.dirname(temp_path), stored_name)

    try:
        _validate_audio(temp_path)
        try:
            os.replace(temp_path, target_path)
        except OSError as exc:
            raise BgmServiceError("failed to persist background music upload") from exc
        temp_path = ""
        logger.info(
            f"background music uploaded: original_name={safe_name}, "
            f"stored_name={stored_name}, size={total_bytes} bytes"
        )
        return stored_name
    finally:
        _remove_staged_file(temp_path)

def list_bgm_files() -> list[str]:
    """List available background music from uploaded storage and built-in resources."""
    files_by_name: dict[str, str] = {}
    for directory in (utils.song_dir(), uploaded_bgm_dir(create=True)):
        if not os.path.isdir(directory):
            continue
        for name in sorted(os.listdir(directory), key=str.lower):
            # Exclude staging files that have not completed validation.
            if name.startswith(_INTERNAL_UPLOAD_PREFIX):
                continue
            if Path(name).suffix.lower() not in SUPPORTED_BGM_EXTENSIONS:
                continue
            file_path = os.path.join(directory, name)
            try:
                # Verify paths to prevent symlink traversal outside allowed directories.
                resolved_path = file_security.resolve_path_within_directory(
                    directory, file_path
                )
            except ValueError as exc:
                logger.warning(
                    f"skip unsafe background music file: name={name}, error={str(exc)}"
                )
                continue
            files_by_name[name] = resolved_path
    return [files_by_name[name] for name in sorted(files_by_name, key=str.lower)]

def resolve_bgm_file(unsafe_path: str) -> str:
    """
    Resolve BGM path within uploaded or built-in audio directories.
    """
    if (
        not unsafe_path
        or Path(unsafe_path).suffix.lower() not in SUPPORTED_BGM_EXTENSIONS
    ):
        raise ValueError("unsupported background music path")

    candidates = [unsafe_path]
    if not os.path.isabs(unsafe_path):
        candidates.append(os.path.join(utils.root_dir(), unsafe_path))

    last_error = ValueError("background music file does not exist")
    for directory in (uploaded_bgm_dir(create=True), utils.song_dir()):
        for candidate in candidates:
            try:
                return file_security.resolve_path_within_directory(directory, candidate)
            except ValueError as exc:
                last_error = exc
    raise ValueError(str(last_error)) from last_error
