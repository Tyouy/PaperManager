"""Mac desktop entrypoint: one background server and a direct library tab."""
from __future__ import annotations

import fcntl
import http.client
import json
import os
import sys
import time
import webbrowser
from pathlib import Path

BASE = Path(__file__).resolve().parent


def main():
    state = BASE / ".paperwise-desktop.json"
    if "--stop" in sys.argv:
        try:
            running = json.loads(state.read_text(encoding="utf-8"))
        except FileNotFoundError:
            print("文献检索未在后台运行。")
            return
        connection = http.client.HTTPConnection("127.0.0.1", int(running["port"]), timeout=3)
        try:
            connection.request("POST", "/api/shutdown", json.dumps({"instance": running["instance"]}),
                               {"Content-Type": "application/json", "X-Paper-Renamer": "local"})
            response = connection.getresponse()
            if response.status != 200:
                raise RuntimeError("后台实例已变化，未停止其他程序")
            response.read()
            print("文献检索后台服务已停止。")
        finally:
            connection.close()
        return
    with (BASE / ".paperwise-desktop.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # Authenticate the running instance before opening a remembered port.
            for _ in range(80):
                connection = None
                try:
                    running = json.loads(state.read_text(encoding="utf-8"))
                    port = int(running["port"])
                    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
                    connection.request("GET", "/api/health")
                    response = connection.getresponse()
                    health = json.loads(response.read(4096))
                    if response.status == 200 and health.get("instance") == running["instance"] and health.get("application") == "paperwise":
                        webbrowser.open(f"http://127.0.0.1:{port}/#library")
                        return
                except (OSError, ValueError, KeyError, http.client.HTTPException):
                    pass
                finally:
                    if connection:
                        connection.close()
                time.sleep(.1)
            raise RuntimeError("文献检索服务尚未就绪，请稍后重新打开桌面图标")
        os.environ["PAPERWISE_DESKTOP_STATE"] = str(state)
        import app
        port = 8765
        try:
            previous_port = int(json.loads(state.read_text(encoding="utf-8"))["port"])
            if 1024 <= previous_port <= 65535:
                port = previous_port
        except (OSError, ValueError, KeyError):
            pass
        sys.argv = [str(BASE / "app.py"), "--library", "--port", str(port)]
        try:
            app.main()
        finally:
            state.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
