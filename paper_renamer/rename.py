"""Conservative, recoverable PDF renaming with no-overwrite filesystem operations."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
import threading
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .dates import normalize_date


MAX_FILENAME_BYTES = 240
_LOCK = threading.RLock()
_FORBIDDEN = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", re.I)


@dataclass
class RenameRequest:
    source: str
    filename: str
    fingerprint: Optional[dict] = None


def _truncate_bytes(value: str, limit: int, preserve_trailing_periods: bool = False) -> str:
    trim_characters = " " if preserve_trailing_periods else " ."
    return value.encode("utf-8")[:max(0, limit)].decode("utf-8", errors="ignore").rstrip(trim_characters)


def sanitize_filename_component(value: str, max_bytes: int = 160, *, preserve_trailing_periods: bool = False) -> str:
    """Keep readable Unicode; interior journal fields may retain abbreviation dots."""
    value = unicodedata.normalize("NFC", str(value or ""))
    value = _FORBIDDEN.sub(" ", value)
    value = "".join(c for c in value if unicodedata.category(c) != "Cf")
    value = re.sub(r"\s+", " ", value).lstrip(" .")
    value = value.rstrip(" " if preserve_trailing_periods else " .")
    if _RESERVED.match(value):
        value = "_" + value
    return _truncate_bytes(value, max_bytes, preserve_trailing_periods)


def build_filename(surname: str, journal: str, title: str, publication_date: str = "") -> str:
    """Return year[-last-author]-journal-title.pdf, bounded by UTF-8 bytes."""
    published = normalize_date(publication_date)[:4] or "时间未知"
    surname = sanitize_filename_component(surname, 128)
    journal = sanitize_filename_component(journal, 64, preserve_trailing_periods=True)
    title = sanitize_filename_component(title, MAX_FILENAME_BYTES)
    if not all((journal, title)):
        raise ValueError("期刊简称和标题均不能为空；作者可留空。")
    prefix = "-".join(part for part in (published, surname, journal) if part)
    budget = MAX_FILENAME_BYTES - len((prefix + "-.pdf").encode("utf-8"))
    title = _truncate_bytes(title, budget)
    if not title:
        raise ValueError("标题无法生成有效文件名。")
    return "{}-{}.pdf".format(prefix, title)


def file_fingerprint(path: Path) -> dict:
    """Hash a regular file and reject files that changed during the read."""
    path = Path(path)
    if path.is_symlink():
        raise ValueError("不处理符号链接。")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    descriptor = os.open(str(path), flags)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("只允许处理普通文件。")
        digest = hashlib.sha256()
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(stream.fileno())
    if (before.st_size, before.st_mtime_ns, before.st_ino, before.st_dev) != (
        after.st_size, after.st_mtime_ns, after.st_ino, after.st_dev
    ):
        raise ValueError("文件正在修改，请重新扫描。")
    return {"size": after.st_size, "mtime_ns": after.st_mtime_ns, "sha256": digest.hexdigest()}


def _fingerprint_matches(actual: dict, expected: dict) -> bool:
    fields = [key for key in ("size", "mtime_ns", "sha256") if key in expected]
    return bool(fields) and all(actual.get(key) == expected[key] for key in fields)


def _root_path(root: Path) -> Path:
    root = Path(root).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError("所选路径不是文件夹。")
    return root


def _within_root(root: Path, value: str, require_file: bool = False) -> Path:
    """Do not resolve away symlinks: inspect every lexical component instead."""
    path = Path(value)
    if ".." in path.parts:
        raise ValueError("路径不能包含上级目录。")
    if not path.is_absolute():
        path = root / path
    try:
        relative = path.relative_to(root)
    except ValueError:
        raise ValueError("文件不在所选文件夹内。")
    if not relative.parts:
        raise ValueError("文件路径不能为空。")
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("出于文件安全，不处理符号链接或经过符号链接的路径。")
    if require_file and not path.is_file():
        raise ValueError("文件不存在或不是普通文件。")
    return path


def _log_directory(root: Path, log_dir: Path, create: bool) -> Path:
    path = _within_root(root, str(log_dir))
    if create:
        path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir():
        raise ValueError("找不到重命名历史文件夹。")
    return path


def _sync_directory(path: Path) -> None:
    try:
        descriptor = os.open(str(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        # Windows and some network filesystems cannot fsync directories.
        pass


def _save_journal(path: Path, journal: dict) -> None:
    if path.is_symlink():
        raise ValueError("历史记录不能是符号链接。")
    temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(journal, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(temporary), str(path))
        _sync_directory(path.parent)
    finally:
        if temporary.exists():
            temporary.unlink()


def _no_replace_move(source: Path, target: Path) -> None:
    """Hard-link creation is atomic and fails if target exists; never overwrite."""
    try:
        os.link(str(source), str(target), follow_symlinks=False)
    except OSError as error:
        unsupported_codes = {errno.EPERM, errno.ENOSYS, errno.EOPNOTSUPP, getattr(errno, "ENOTSUP", errno.EOPNOTSUPP)}
        if error.errno in unsupported_codes:
            raise OSError(
                error.errno,
                "当前磁盘不支持安全重命名所需的硬链接，或没有创建硬链接的权限。"
                "exFAT/FAT32 外置盘通常不支持此操作；请把文件夹复制到本机 APFS/HFS+ "
                "或其他支持硬链接的磁盘后重试，并检查文件夹权限。原文件未改动。",
            ) from error
        raise
    except NotImplementedError as error:
        raise OSError(
            errno.ENOSYS,
            "当前系统不支持安全重命名所需的硬链接，请换用支持硬链接的磁盘或系统。原文件未改动。",
        ) from error
    if target.is_symlink():
        # A source replaced by a symlink between validation and link creation
        # must never be moved. Unlink only the newly created destination link.
        target.unlink()
        raise ValueError("文件已变为符号链接，请重新扫描。")
    try:
        source.unlink()
    except OSError:
        # Keep the original if unlink was rejected; remove only our own link.
        if source.exists() and target.exists() and os.path.samefile(source, target):
            target.unlink()
        raise
    _sync_directory(source.parent)


def _validate_filename(filename: str) -> str:
    if not isinstance(filename, str) or not filename or filename in (".", ".."):
        raise ValueError("新文件名无效。")
    if _FORBIDDEN.search(filename) or filename != filename.strip(" ."):
        raise ValueError("文件名包含非法字符或路径分隔符。")
    if any(unicodedata.category(char) == "Cf" for char in filename):
        raise ValueError("文件名包含不可见控制字符。")
    if Path(filename).suffix.lower() != ".pdf":
        raise ValueError("新文件名必须以 .pdf 结尾。")
    if _RESERVED.match(filename) or len(filename.encode("utf-8")) > MAX_FILENAME_BYTES:
        raise ValueError("文件名为系统保留名称或过长，请缩短后重试。")
    return filename


def _candidate_path(source: Path, filename: str, number: int) -> Path:
    if number == 1:
        return source.with_name(filename)
    suffix = " ({}).pdf".format(number)
    stem = _truncate_bytes(filename[:-4], MAX_FILENAME_BYTES - len(suffix.encode("utf-8")))
    return source.with_name(stem + suffix)


def apply_renames(root: Path, requests: list[RenameRequest], log_dir: Path) -> dict:
    """Rename PDFs in place, recording recoverable steps before every mutation."""
    with _LOCK:
        root = _root_path(root)
        logs = _log_directory(root, log_dir, create=True)
        log_id = uuid.uuid4().hex
        journal = {
            "version": 1, "id": log_id, "root": str(root),
            "created_at": datetime.now(timezone.utc).isoformat(), "operations": [],
        }
        journal_path = logs / (log_id + ".json")
        _save_journal(journal_path, journal)
        result = {"renamed": [], "skipped": [], "errors": [], "log_id": log_id}
        seen = set()
        for request in requests:
            operation = None
            try:
                source = _within_root(root, request.source, require_file=True)
                if source.suffix.lower() != ".pdf":
                    raise ValueError("只允许重命名 PDF 文件。")
                if str(source) in seen:
                    result["skipped"].append({"source": str(source), "reason": "重复请求已跳过。"})
                    continue
                seen.add(str(source))
                filename = _validate_filename(request.filename)
                fingerprint = file_fingerprint(source)
                if request.fingerprint is not None and not _fingerprint_matches(fingerprint, request.fingerprint):
                    raise ValueError("文件在扫描后已修改，请重新扫描。")
                number = 1
                while True:
                    target = _candidate_path(source, filename, number)
                    if target == source or (
                        target.exists() and not target.is_symlink() and os.path.samefile(source, target)
                    ):
                        result["skipped"].append({"source": str(source), "reason": "文件名已符合要求。"})
                        break
                    if target.exists() or target.is_symlink():
                        number += 1
                        continue
                    operation = {
                        "source": str(source.relative_to(root)), "target": str(target.relative_to(root)),
                        "fingerprint": fingerprint, "status": "pending",
                    }
                    journal["operations"].append(operation)
                    _save_journal(journal_path, journal)
                    if not _fingerprint_matches(file_fingerprint(source), fingerprint):
                        raise ValueError("重命名前文件已修改，请重新扫描。")
                    try:
                        _no_replace_move(source, target)
                    except FileExistsError:
                        operation["status"] = "collision"
                        _save_journal(journal_path, journal)
                        number += 1
                        continue
                    result["renamed"].append({"source": str(source), "target": str(target)})
                    operation["status"] = "applied"
                    _save_journal(journal_path, journal)
                    break
            except (OSError, ValueError, TypeError) as error:
                result["errors"].append({"source": str(request.source), "error": str(error)})
                if operation and operation["status"] == "pending":
                    # Keep pending if the move succeeded but journal confirmation failed.
                    if source.exists():
                        operation["status"] = "failed"
                    operation["error"] = str(error)
                    try:
                        _save_journal(journal_path, journal)
                    except OSError:
                        pass
        return result


def undo_renames(root: Path, log_id: str, log_dir: Path) -> dict:
    """Undo a batch; refuse changed files, occupied original paths, or foreign logs."""
    with _LOCK:
        root = _root_path(root)
        if not isinstance(log_id, str) or not re.fullmatch(r"[0-9a-f]{32}", log_id):
            raise ValueError("无效的历史记录编号。")
        logs = _log_directory(root, log_dir, create=False)
        journal_path = _within_root(root, str(logs / (log_id + ".json")), require_file=True)
        if journal_path.stat().st_size > 32 * 1024 * 1024:
            raise ValueError("历史记录过大，无法安全读取。")
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        if journal.get("root") != str(root) or journal.get("id") != log_id or journal.get("version") != 1:
            raise ValueError("历史记录不属于当前文件夹或格式不受支持。")
        restored = []
        result = {"restored": restored, "renamed": restored, "skipped": [], "errors": [], "log_id": log_id}
        for operation in reversed(journal.get("operations", [])):
            if operation.get("status") not in ("applied", "pending", "undo_pending"):
                continue
            try:
                source = _within_root(root, operation["source"])
                target = _within_root(root, operation["target"])
                if source.parent != target.parent or source.suffix.lower() != ".pdf" or target.suffix.lower() != ".pdf":
                    raise ValueError("历史记录包含不受支持的路径。")
                if not target.exists():
                    if source.exists() and _fingerprint_matches(file_fingerprint(source), operation["fingerprint"]):
                        operation["status"] = "restored"
                        _save_journal(journal_path, journal)
                        result["skipped"].append({"source": str(source), "reason": "原文件已恢复或该操作未执行。"})
                        continue
                    raise ValueError("重命名后的文件不存在，无法撤销。")
                if not _fingerprint_matches(file_fingerprint(target), operation["fingerprint"]):
                    raise ValueError("文件在重命名后已修改，无法安全撤销。")
                if source.exists():
                    if operation["status"] in ("pending", "undo_pending") and os.path.samefile(source, target):
                        operation["status"] = "undo_pending"
                        _save_journal(journal_path, journal)
                        target.unlink()
                        _sync_directory(target.parent)
                    else:
                        raise ValueError("原文件名已被占用，未覆盖现有文件。")
                else:
                    operation["status"] = "undo_pending"
                    _save_journal(journal_path, journal)
                    _no_replace_move(target, source)
                restored.append({"source": str(target), "target": str(source)})
                operation["status"] = "restored"
                _save_journal(journal_path, journal)
            except (OSError, ValueError, KeyError, TypeError) as error:
                result["errors"].append({"source": str(operation.get("target", "")), "error": str(error)})
        return result
