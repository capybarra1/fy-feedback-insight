"""Start the personal dashboard on localhost only."""

import argparse
import os
import socket
import threading
import webbrowser

import uvicorn

from api import create_app


def main() -> None:
    parser = argparse.ArgumentParser(description="FIREFLY 本地反馈分析")
    parser.add_argument("--port", type=int, default=18765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", args.port))
        except OSError:
            print(
                f"端口 {args.port} 已被占用。如工具已启动，请打开 http://127.0.0.1:{args.port}；否则换一个端口。"
            )
            return
    url = f"http://127.0.0.1:{args.port}"
    print(f"FIREFLY 反馈分析：{url}\n保持此窗口开启。按 Ctrl+C 停止。")
    if not args.no_browser:
        threading.Timer(1.2, webbrowser.open, args=(url,)).start()
    uvicorn.run(create_app(), host="127.0.0.1", port=args.port, access_log=False)


if __name__ == "__main__":
    main()
