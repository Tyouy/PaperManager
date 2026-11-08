#!/usr/bin/env python3
"""Launch the local PDF naming UI. Only listens on the loopback interface."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

try:
    from paper_renamer.extract import analyze_pdf
except ImportError as exc:
    if exc.name in {"pymupdf", "fitz"}:
        print("请先双击「启动程序.command」，或运行：python3 -m pip install -r requirements.txt")
        raise SystemExit(1)
    raise
from paper_renamer.metadata import validate_ai_config
from paper_renamer.rename import RenameRequest, apply_renames, undo_renames
from paper_renamer.library import Library
from paper_renamer.duplicates import HISTORY, inspect_duplicates, remove_duplicates, restore_duplicates

BASE = Path(__file__).resolve().parent
JOBS: dict[str, dict] = {}
JOB_LOCK = threading.Lock()
OPERATION_LOCK = threading.Lock()
LIBRARY = Library(BASE / ".paper-library.sqlite3")
INSTANCE_TOKEN = uuid.uuid4().hex


def folder_path(value) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("请先选择文件夹")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError("请输入文件夹的完整绝对路径")
    path = path.resolve(strict=True)
    if not path.is_dir():
        raise ValueError("所选路径不是文件夹")
    return path


def list_pdfs(root: Path, recursive: bool) -> list[Path]:
    found = []
    if recursive:
        def onerror(error):
            raise ValueError("无法读取子文件夹：" + str(error.filename))
        for parent, directories, files in os.walk(root, followlinks=False, onerror=onerror):
            directories[:] = sorted(d for d in directories if d not in {".git", ".venv", ".paper-renamer-history", HISTORY}
                                    and not (Path(parent) / d).is_symlink())
            for name in sorted(files):
                path = Path(parent) / name
                if path.suffix.casefold() == ".pdf" and not path.is_symlink() and path.is_file():
                    found.append(path)
    else:
        found = sorted((path for path in root.iterdir() if path.suffix.casefold() == ".pdf"
                        and not path.is_symlink() and path.is_file()), key=lambda p: p.name.casefold())
    return found


def validate_scan_eligibility(root: Path, requests: list[RenameRequest]) -> None:
    """Authorize names only from the latest successful scan of this exact folder.

    Caller holds OPERATION_LOCK so a new scan cannot change classification while
    this batch is being validated or applied. Client-supplied classification flags
    never grant permission to rename a document.
    """
    with JOB_LOCK:
        latest = next((job for job in reversed(list(JOBS.values()))
                       if job.get("folder") == str(root) and job.get("kind", "rename") == "rename"), None)
        if latest is None or latest.get("status") != "done":
            raise ValueError("请先完成所选文件夹的扫描，再重命名文件")
        records = {row.get("source"): row for row in latest.get("results", [])}
        for request in requests:
            source = Path(request.source)
            if ".." in source.parts:
                raise ValueError("文件路径不能包含上级目录")
            if not source.is_absolute():
                source = root / source
            row = records.get(str(source))
            if row is None:
                raise ValueError("文件未包含在最新扫描结果中，请重新扫描")
            if row.get("is_research_paper") is False or row.get("status") == "skipped":
                raise ValueError("此 PDF 已识别为非论文，已取消重命名资格：" + source.name)
            if row.get("rename_eligible") is False or row.get("status") == "error":
                raise ValueError("此 PDF 不具备重命名资格：" + source.name)
            scanned = row.get("fingerprint")
            supplied = request.fingerprint
            if not isinstance(scanned, dict) or not isinstance(supplied, dict):
                raise ValueError("扫描指纹缺失，请重新扫描")
            expected = {"size": scanned.get("size"), "mtime_ns": int(scanned.get("mtime_ns", -1)),
                        "sha256": scanned.get("sha256")}
            if any(supplied.get(key) != value for key, value in expected.items()):
                raise ValueError("文件指纹与最新扫描不符，请重新扫描")


def scan_worker(job_id: str, root: Path, recursive: bool, metadata: bool, ai: dict):
    try:
        paths = list_pdfs(root, recursive)
        with JOB_LOCK:
            JOBS[job_id]["progress"]["total"] = len(paths)
        for index, path in enumerate(paths):
            with JOB_LOCK:
                JOBS[job_id]["progress"]["current"] = str(path.relative_to(root))
            result = analyze_pdf(path, root, metadata, ai)
            if result.get("fingerprint"):
                # Nanosecond timestamps exceed JavaScript's exact integer range.
                result["fingerprint"]["mtime_ns"] = str(result["fingerprint"]["mtime_ns"])
            with JOB_LOCK:
                JOBS[job_id]["results"].append(result)
                JOBS[job_id]["progress"]["done"] = index + 1
        with JOB_LOCK:
            JOBS[job_id]["status"] = "done"
            JOBS[job_id]["progress"]["current"] = ""
    except Exception as exc:
        with JOB_LOCK:
            JOBS[job_id].update(status="error", error=str(exc)[:500])
    finally:
        ai.clear()
        OPERATION_LOCK.release()


def pick_folder() -> str:
    if sys.platform == "darwin":
        result = subprocess.run(["osascript", "-e", "activate", "-e", 'POSIX path of (choose folder with prompt "选择需要整理的 PDF 文件夹")'],
                                capture_output=True, text=True, timeout=180)
        if result.returncode:
            if "-128" in result.stderr:
                return ""
            raise ValueError("无法打开系统选择窗口，请在页面中粘贴文件夹完整路径")
        return result.stdout.strip()
    if sys.platform.startswith("linux") and shutil.which("zenity"):
        result = subprocess.run(["zenity", "--file-selection", "--directory", "--title=选择 PDF 文件夹"],
                                capture_output=True, text=True, timeout=180)
        return result.stdout.strip() if result.returncode == 0 else ""
    code = 'import tkinter as t; from tkinter import filedialog; r=t.Tk(); r.withdraw(); r.attributes("-topmost", True); print(filedialog.askdirectory(title="选择 PDF 文件夹")); r.destroy()'
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=180)
    if result.returncode:
        raise ValueError("系统文件夹选择不可用，请粘贴文件夹完整路径")
    return result.stdout.strip()


def library_worker(job_id: str, root: Path, metadata: bool, deduplicate: bool = True):
    try:
        paths = list_pdfs(root, True)
        with JOB_LOCK:
            JOBS[job_id]["progress"]["total"] = len(paths)
        def progress(done, current, reused):
            with JOB_LOCK:
                JOBS[job_id]["progress"].update(done=done, current=current, reused=reused)
        LIBRARY.index(root, paths, metadata, progress)
        with JOB_LOCK:
            JOBS[job_id]["progress"].update(phase="duplicates", done=0, current="正在核对全文与重复文献")
        def duplicate_progress(done, current):
            with JOB_LOCK:
                JOBS[job_id]["progress"].update(done=done, current=current)
        report = inspect_duplicates(root, LIBRARY.records(root), duplicate_progress)
        report["removal"] = remove_duplicates(root, report) if deduplicate else {"moved": [], "errors": [], "batch_id": ""}
        # Keep the last undo batch accessible when an update finds no new copies.
        previous = LIBRARY.duplicate_report(root)
        report["undo_batch"] = report["removal"]["batch_id"] or previous.get("undo_batch", "")
        LIBRARY.save_duplicate_report(root, report)
        with JOB_LOCK:
            JOBS[job_id]["status"] = "done"
            JOBS[job_id]["duplicates"] = {"removed": len(report["removal"]["moved"]),
                                         "suspected": len(report["suspected"]),
                                         "errors": len(report["failures"]) + len(report["removal"]["errors"])}
    except Exception as exc:
        with JOB_LOCK:
            JOBS[job_id].update(status="error", error=str(exc)[:500])
    finally:
        OPERATION_LOCK.release()


def open_document(path: Path):
    if sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    elif sys.platform == "win32":
        os.startfile(str(path))
    else:
        subprocess.Popen(["xdg-open", str(path)])


class Handler(BaseHTTPRequestHandler):
    server_version = "PaperRenamer/1.0"

    def log_message(self, format, *args):
        # Do not log PDF paths, body data, or API keys.
        pass

    def trusted_request(self) -> bool:
        allowed = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
        if self.headers.get("Host", "") not in allowed:
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in {"http://" + host for host in allowed}:
            return False
        return self.headers.get("Sec-Fetch-Site", "") != "cross-site"

    def respond(self, status: int, value=None, content=None, mime="application/json; charset=utf-8"):
        data = content if content is not None else json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        if not self.trusted_request():
            self.respond(403, {"error": "拒绝非本机页面请求"})
            return
        path = urlsplit(self.path).path
        if path == "/api/health":
            self.respond(200, {"application": "paperwise", "instance": INSTANCE_TOKEN})
            return
        if path.startswith("/api/jobs/"):
            job_id = path.rsplit("/", 1)[-1]
            with JOB_LOCK:
                job = JOBS.get(job_id)
                if job:
                    # Serialize inside the lock to avoid concurrent list mutation.
                    self.respond(200, job)
                else:
                    self.respond(404, {"error": "扫描记录不存在，请重新扫描"})
            return
        assets = {"/": ("index.html", "text/html; charset=utf-8"),
                  "/index.html": ("index.html", "text/html; charset=utf-8"),
                  "/styles.css": ("styles.css", "text/css; charset=utf-8"),
                  "/app.js": ("app.js", "application/javascript; charset=utf-8"),
                  "/library.js": ("library.js", "application/javascript; charset=utf-8")}
        if path in assets:
            filename, mime = assets[path]
            self.respond(200, content=(BASE / "web" / filename).read_bytes(), mime=mime)
        else:
            self.respond(404, {"error": "页面不存在"})

    def do_POST(self):
        if not self.trusted_request() or self.headers.get("X-Paper-Renamer") != "local":
            self.respond(403, {"error": "请求来源校验失败，请从本地程序页面操作"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or length > 8_000_000:
                raise ValueError("请求内容过大")
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("请求格式不正确")
            path = urlsplit(self.path).path
            if path == "/api/pick-folder":
                self.respond(200, {"path": pick_folder()})
            elif path == "/api/shutdown":
                if body.get("instance") != INSTANCE_TOKEN:
                    raise ValueError("启动实例已变化，请重新打开文献检索")
                self.respond(200, {"stopped": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            elif path == "/api/library/search":
                root = folder_path(body.get("folder"))
                self.respond(200, LIBRARY.search(root, body))
            elif path == "/api/library/open":
                root = folder_path(body.get("folder"))
                open_document(LIBRARY.document(root, body.get("source")))
                self.respond(200, {"opened": True})
            elif path == "/api/library/duplicates":
                root = folder_path(body.get("folder"))
                self.respond(200, LIBRARY.duplicate_report(root))
            elif path == "/api/library/restore-duplicates":
                root = folder_path(body.get("folder"))
                if not OPERATION_LOCK.acquire(blocking=False):
                    raise ValueError("正在扫描或修改文件，请等待当前操作完成")
                try:
                    report = LIBRARY.duplicate_report(root)
                    batch = report.get("undo_batch", "")
                    result = restore_duplicates(root, batch)
                    if not result["errors"]:
                        report["undo_batch"] = ""
                    report["restored"] = result
                    # Reinsert restored paths on the next index; do not replay old deletions.
                    report["removal"]["moved"] = []
                    LIBRARY.save_duplicate_report(root, report)
                finally:
                    OPERATION_LOCK.release()
                self.respond(200, result)
            elif path == "/api/library/index":
                root = folder_path(body.get("folder"))
                if not OPERATION_LOCK.acquire(blocking=False):
                    raise ValueError("正在扫描或修改文件，请等待当前操作完成")
                job_id = uuid.uuid4().hex
                with JOB_LOCK:
                    completed = [key for key, value in JOBS.items() if value["status"] != "running"]
                    for key in completed[:-9]:
                        del JOBS[key]
                    JOBS[job_id] = {"status": "running", "kind": "library", "folder": str(root),
                                    "progress": {"done": 0, "total": 0, "current": "", "reused": 0}}
                threading.Thread(target=library_worker, args=(job_id, root, bool(body.get("metadata", False)),
                                 bool(body.get("deduplicate", True))),
                                 daemon=True).start()
                self.respond(200, {"job_id": job_id})
            elif path == "/api/scan":
                root = folder_path(body.get("folder"))
                ai = validate_ai_config(body.get("ai") or {})
                if not OPERATION_LOCK.acquire(blocking=False):
                    raise ValueError("正在扫描或修改文件，请等待当前操作完成")
                job_id = uuid.uuid4().hex
                with JOB_LOCK:
                    completed = [key for key, value in JOBS.items() if value["status"] != "running"]
                    for key in completed[:-9]:
                        del JOBS[key]
                    JOBS[job_id] = {"status": "running", "folder": str(root),
                                    "progress": {"done": 0, "total": 0, "current": ""}, "results": []}
                threading.Thread(target=scan_worker, args=(job_id, root, bool(body.get("recursive", True)),
                                  bool(body.get("metadata", True)), ai), daemon=True).start()
                self.respond(200, {"job_id": job_id})
            elif path == "/api/rename":
                root = folder_path(body.get("folder"))
                items = body.get("items")
                if not isinstance(items, list) or not items or len(items) > 10000:
                    raise ValueError("请选择需要重命名的文件（单批最多 10000 个）")
                requests = []
                for item in items:
                    if not isinstance(item, dict) or not isinstance(item.get("source"), str) or not isinstance(item.get("filename"), str):
                        raise ValueError("文件命名请求格式不正确")
                    fp = item.get("fingerprint")
                    if not isinstance(fp, dict) or not all(k in fp for k in ("size", "mtime_ns", "sha256")):
                        raise ValueError("文件指纹缺失，请重新扫描后操作")
                    fp = dict(fp)
                    if not isinstance(fp["mtime_ns"], str) or not fp["mtime_ns"].isdigit():
                        raise ValueError("文件时间指纹格式不正确，请重新扫描")
                    fp["mtime_ns"] = int(fp["mtime_ns"])
                    requests.append(RenameRequest(item["source"], item["filename"], fp))
                if not OPERATION_LOCK.acquire(blocking=False):
                    raise ValueError("当前有其他操作正在进行，请稍后再试")
                try:
                    validate_scan_eligibility(root, requests)
                    result = apply_renames(root, requests, root / ".paper-renamer-history")
                finally:
                    OPERATION_LOCK.release()
                self.respond(200, result)
            elif path == "/api/undo":
                root = folder_path(body.get("folder"))
                log_id = body.get("log_id")
                if not isinstance(log_id, str):
                    raise ValueError("撤销记录格式不正确")
                if not OPERATION_LOCK.acquire(blocking=False):
                    raise ValueError("当前有其他操作正在进行，请稍后再试")
                try:
                    result = undo_renames(root, log_id, root / ".paper-renamer-history")
                finally:
                    OPERATION_LOCK.release()
                self.respond(200, result)
            else:
                self.respond(404, {"error": "接口不存在"})
        except (ValueError, TypeError, FileNotFoundError, NotADirectoryError, PermissionError) as exc:
            self.respond(400, {"error": str(exc)[:400]})
        except subprocess.TimeoutExpired:
            self.respond(408, {"error": "文件夹选择已超时，请重试或输入路径"})
        except Exception:
            self.respond(500, {"error": "操作未完成，请检查文件权限，或关闭程序后重试"})


def main():
    parser = argparse.ArgumentParser(description="本地 PDF 论文批量重命名工具")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--library", action="store_true", help="启动时直接打开文献检索")
    parser.add_argument("--restore-duplicates", nargs=2, metavar=("FOLDER", "BATCH_ID"), help="恢复去重移除的副本")
    parser.add_argument("--undo", nargs=2, metavar=("FOLDER", "LOG_ID"), help="根据持久化日志撤销历史批次")
    args = parser.parse_args()
    if args.restore_duplicates:
        root = folder_path(args.restore_duplicates[0])
        print(json.dumps(restore_duplicates(root, args.restore_duplicates[1]), ensure_ascii=False, indent=2))
        return
    if args.undo:
        root = folder_path(args.undo[0])
        print(json.dumps(undo_renames(root, args.undo[1], root / ".paper-renamer-history"), ensure_ascii=False, indent=2))
        return
    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError:
        # A second launch should still work; never attach to an unknown existing service.
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    url = f"http://127.0.0.1:{server.server_port}" + ("/#library" if args.library else "")
    state_file = os.environ.get("PAPERWISE_DESKTOP_STATE")
    if state_file:
        Path(state_file).write_text(json.dumps({"port": server.server_port, "instance": INSTANCE_TOKEN}), encoding="utf-8")
    print(f"\n论文整理与文献检索已启动：{url}\n关闭程序：在此窗口按 Control+C。\n", flush=True)
    if not args.no_browser:
        threading.Timer(.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n程序已关闭。")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
