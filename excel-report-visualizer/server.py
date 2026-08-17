from __future__ import annotations

import argparse
import json
import os
import tempfile
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parent
DRAFT_DIR = ROOT / ".local"
DRAFT_PATH = DRAFT_DIR / "report-state.json"
MAX_DRAFT_BYTES = 128 * 1024 * 1024
DATA_KEYS = (
    "acquisition",
    "funnel",
    "active",
    "retention",
    "commercial",
    "modes",
    "behavior",
)


def validate_draft(payload: object) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("草稿必须是 JSON 对象。")
    if payload.get("version") != 1:
        raise ValueError("草稿版本不受支持。")

    for key in ("period", "header", "viewState", "data"):
        if not isinstance(payload.get(key), dict):
            raise ValueError(f"草稿字段 {key} 格式不正确。")
    for key in ("definitions", "conclusions"):
        if not isinstance(payload.get(key), list):
            raise ValueError(f"草稿字段 {key} 格式不正确。")
    for key in ("createdAt", "sourceFile"):
        if not isinstance(payload.get(key), str):
            raise ValueError(f"草稿字段 {key} 格式不正确。")

    if any(key not in payload["data"] for key in DATA_KEYS):
        raise ValueError("草稿数据模块不完整。")
    if any(
        payload["data"][key] is not None
        and not isinstance(payload["data"][key], dict)
        for key in DATA_KEYS
    ):
        raise ValueError("草稿数据模块格式不正确。")
    return payload


def write_draft(payload: dict) -> None:
    DRAFT_DIR.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix="report-state.", suffix=".tmp", dir=DRAFT_DIR
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, DRAFT_PATH)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


class ReportHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def _is_local_host(self) -> bool:
        host = self.headers.get("Host", "").lower().split(":", 1)[0]
        return host in {"127.0.0.1", "localhost"}

    def _request_path(self) -> str:
        return unquote(urlsplit(self.path).path)

    def _send_json(self, status: HTTPStatus, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _reject_nonlocal_host(self) -> bool:
        if self._is_local_host():
            return False
        self._send_json(HTTPStatus.FORBIDDEN, {"error": "仅允许本机访问。"})
        return True

    def _is_private_static_path(self) -> bool:
        return ".local" in PurePosixPath(self._request_path()).parts

    def do_GET(self) -> None:
        if self._reject_nonlocal_host():
            return
        if self._request_path() == "/api/draft":
            self._get_draft()
            return
        if self._is_private_static_path():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        super().do_GET()

    def do_HEAD(self) -> None:
        if self._reject_nonlocal_host():
            return
        if self._request_path() == "/api/draft" or self._is_private_static_path():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        super().do_HEAD()

    def do_PUT(self) -> None:
        if self._reject_nonlocal_host():
            return
        if self._request_path() != "/api/draft":
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        try:
            content_length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._send_json(HTTPStatus.LENGTH_REQUIRED, {"error": "缺少有效的内容长度。"})
            return
        if content_length <= 0:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "草稿内容不能为空。"})
            return
        if content_length > MAX_DRAFT_BYTES:
            self._send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "草稿超过 128 MB。"})
            return

        try:
            raw_payload = self.rfile.read(content_length)
            payload = validate_draft(json.loads(raw_payload.decode("utf-8")))
            write_draft(payload)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        except OSError as error:
            self.log_error("draft write failed: %s", error)
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "草稿写入失败。"})
            return

        self.send_response(HTTPStatus.NO_CONTENT)
        self.end_headers()

    def _get_draft(self) -> None:
        if not DRAFT_PATH.exists():
            self.send_response(HTTPStatus.NO_CONTENT)
            self.end_headers()
            return
        try:
            raw_payload = DRAFT_PATH.read_bytes()
            validate_draft(json.loads(raw_payload.decode("utf-8")))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            self.log_error("draft read failed: %s", error)
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "本地草稿无法读取。"})
            return

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw_payload)))
        self.end_headers()
        self.wfile.write(raw_payload)


def main() -> None:
    parser = argparse.ArgumentParser(description="Excel 日报周报可视化本地服务")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), ReportHandler)
    server.daemon_threads = True
    print(f"Excel 日报周报可视化：http://127.0.0.1:{args.port}", flush=True)
    print("按 Ctrl+C 停止服务。", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
