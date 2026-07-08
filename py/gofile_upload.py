import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO

import requests
from requests_toolbelt import MultipartEncoder, MultipartEncoderMonitor

from downloader import TransferCancelled
from upload_common import format_size  # re-exported for backward compatibility


GOFILE_API_KEY = os.environ.get("GOFILE_API_KEY", "")
_PREFERRED_SERVER = (os.environ.get("GOFILE_PREFERRED_SERVER") or "").strip().lower()
_RUNTIME_PREFERRED_SERVER = ""


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default

# Regional upload proxies from https://gofile.io/api (also benchmarked by speedtest script).
REGIONAL_UPLOAD_HOSTS = (
    "upload.gofile.io",
    "upload-eu-par.gofile.io",
    "upload-na-phx.gofile.io",
    "upload-ap-sgp.gofile.io",
    "upload-ap-hkg.gofile.io",
    "upload-ap-tyo.gofile.io",
    "upload-sa-sao.gofile.io",
)


def _auth_headers():
    h = {}
    if GOFILE_API_KEY:
        h["Authorization"] = f"Bearer {GOFILE_API_KEY}"
    return h


def get_account_info():
    """Get account ID and root folder ID."""
    r = requests.get("https://api.gofile.io/accounts/getid", headers=_auth_headers())
    r.raise_for_status()
    data = r.json()
    print(f"[GOFILE] getid response: {data}", flush=True)
    if data.get("status") != "ok":
        raise RuntimeError(f"Failed to get account id: {data}")
    account_id = data["data"]["id"]

    r2 = requests.get(f"https://api.gofile.io/accounts/{account_id}", headers=_auth_headers())
    r2.raise_for_status()
    data2 = r2.json()
    print(f"[GOFILE] account response keys: {list(data2.get('data', {}).keys())}", flush=True)
    if data2.get("status") != "ok":
        raise RuntimeError(f"Failed to get account details: {data2}")
    return account_id, data2["data"]["rootFolder"]


def get_root_folder_id():
    _, root_id = get_account_info()
    return root_id


def folder_url(folder_id):
    return f"https://gofile.io/d/{folder_id}" if folder_id else "https://gofile.io"


def create_folder(parent_id, name):
    r = requests.post(
        "https://api.gofile.io/contents/createFolder",
        headers=_auth_headers(),
        json={"parentFolderId": parent_id, "folderName": name},
    )
    r.raise_for_status()
    data = r.json()
    if data.get("status") != "ok":
        raise RuntimeError(f"Failed to create folder: {data}")
    return data["data"]["id"]


def get_upload_servers():
    r = requests.get("https://api.gofile.io/servers", headers=_auth_headers())
    r.raise_for_status()
    data = r.json()
    if data.get("status") != "ok":
        raise RuntimeError(f"Failed to get server: {data}")
    servers = data["data"]["servers"]
    if not servers:
        raise RuntimeError("No upload servers available")
    return [s["name"] for s in servers if s.get("name")]


def upload_host(server: str) -> str:
    """Normalize API server name or full hostname to ``host.gofile.io``."""
    s = (server or "").strip().lower()
    if not s:
        return ""
    if s.endswith(".gofile.io"):
        return s
    if "." in s:
        return s
    return f"{s}.gofile.io"


def upload_url(server: str) -> str:
    return f"https://{upload_host(server)}/uploadfile"


def preferred_upload_server() -> str:
    # Prefer env pin first; allow runtime override (background scan) to adjust
    # without restarting the service.
    return _PREFERRED_SERVER or _RUNTIME_PREFERRED_SERVER


def set_runtime_preferred_server(server: str) -> None:
    """Set preferred server for this process (used by background scan)."""
    global _RUNTIME_PREFERRED_SERVER
    _RUNTIME_PREFERRED_SERVER = (server or "").strip().lower()


def order_upload_servers(servers: list[str]) -> list[str]:
    """Preferred server first; preserve API order for the rest."""
    pref = preferred_upload_server()
    seen: set[str] = set()
    ordered: list[str] = []

    def add(name: str) -> None:
        key = upload_host(name)
        if not key or key in seen:
            return
        seen.add(key)
        ordered.append(name.strip())

    if pref:
        add(pref)
    for name in servers:
        add(name)
    return ordered


def get_ordered_upload_servers(*, include_regional: bool = False) -> list[str]:
    """Upload servers from API, optionally merged with regional proxies, preferred first."""
    api = get_upload_servers()
    extra = list(REGIONAL_UPLOAD_HOSTS) if include_regional else []
    merged: list[str] = []
    seen: set[str] = set()
    for name in api + extra:
        key = upload_host(name)
        if key and key not in seen:
            seen.add(key)
            merged.append(name)
    return order_upload_servers(merged)


def _mbps(bytes_sent: int, elapsed: float) -> float:
    if elapsed <= 0 or bytes_sent <= 0:
        return 0.0
    return (bytes_sent * 8) / elapsed / 1_000_000


def probe_server(server: str, payload: bytes, *, connect_to: float, read_to: float) -> dict:
    """Probe a single GoFile upload server (creates a tiny guest upload)."""
    host = upload_host(server)
    url = upload_url(server)
    headers = _auth_headers()
    fields = {
        "file": ("apu_speedtest.bin", BytesIO(payload), "application/octet-stream"),
    }
    encoder = MultipartEncoder(fields=fields)
    headers["Content-Type"] = encoder.content_type

    started = time.monotonic()
    try:
        r = requests.post(url, data=encoder, headers=headers, timeout=(connect_to, read_to))
    except requests.RequestException as e:
        elapsed = time.monotonic() - started
        return {
            "host": host,
            "server": server,
            "ok": False,
            "error": str(e)[:140],
            "seconds": round(elapsed, 2),
        }

    elapsed = time.monotonic() - started
    if r.status_code >= 400:
        snippet = (r.text or "")[:120]
        return {
            "host": host,
            "server": server,
            "ok": False,
            "error": f"HTTP {r.status_code}: {snippet}",
            "seconds": round(elapsed, 2),
        }

    try:
        body = r.json()
    except ValueError:
        body = {}
    if body.get("status") != "ok":
        return {
            "host": host,
            "server": server,
            "ok": False,
            "error": str(body.get("status") or body)[:140],
            "seconds": round(elapsed, 2),
        }

    return {
        "host": host,
        "server": server,
        "ok": True,
        "mbps": _mbps(len(payload), elapsed),
        "mib_s": len(payload) / elapsed / (1024 * 1024),
        "seconds": round(elapsed, 2),
        "bytes": len(payload),
    }


def benchmark_servers(
    servers: list[str],
    *,
    payload: bytes,
    connect_to: float,
    read_to: float,
    workers: int,
) -> list[dict]:
    """Benchmark GoFile upload servers by uploading an in-memory probe."""
    # Dedupe by normalized host while preserving first label.
    seen: set[str] = set()
    labels: list[str] = []
    for s in servers:
        h = upload_host(s)
        if not h or h in seen:
            continue
        seen.add(h)
        labels.append(s.strip())

    if not labels:
        return []

    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futs = {
            pool.submit(probe_server, label, payload, connect_to=connect_to, read_to=read_to): label
            for label in labels
        }
        for fut in as_completed(futs):
            results.append(fut.result())

    ok = [r for r in results if r.get("ok")]
    fail = [r for r in results if not r.get("ok")]
    ok.sort(key=lambda r: r["mbps"], reverse=True)
    return [*ok, *fail]


def upload_file(filepath, folder_id=None, on_progress=None, should_cancel=None):
    """Upload a single file to GoFile with progress logging to stdout.

    on_progress(pct, uploaded, total, speed, eta_seconds) is called at most
    once per second if provided.

    should_cancel: optional callable; if it returns True, upload aborts (TransferCancelled).
    """
    filename = os.path.basename(filepath)
    filesize = os.path.getsize(filepath)
    servers = get_ordered_upload_servers()
    pref = preferred_upload_server()
    if pref:
        print(f"[UPLOAD] {filename} ({format_size(filesize)}) -> preferred {upload_host(pref)}", flush=True)
    else:
        print(
            f"[UPLOAD] {filename} ({format_size(filesize)}) -> trying {len(servers)} server(s)",
            flush=True,
        )

    last_log = [0.0]
    start_time = [time.time()]

    def progress_callback(monitor):
        if should_cancel and should_cancel():
            raise TransferCancelled("Upload cancelled")
        now = time.time()
        if now - last_log[0] < 1.0:
            return
        last_log[0] = now
        elapsed = now - start_time[0]
        uploaded = monitor.bytes_read
        speed = uploaded / elapsed if elapsed > 0 else 0
        pct = (uploaded / monitor.len) * 100 if monitor.len else 0
        remaining = ((monitor.len - uploaded) / speed) if speed > 0 else 0
        print(
            f"[UPLOAD] {filename}: {pct:5.1f}%  "
            f"{format_size(uploaded)}/{format_size(monitor.len)}  "
            f"{format_size(speed)}/s  "
            f"ETA {int(remaining)}s",
            flush=True,
        )
        if on_progress:
            on_progress(pct, uploaded, monitor.len, speed, remaining)

    last_err = None
    max_attempts = max(5, _env_int("GOFILE_UPLOAD_SERVER_ATTEMPTS", 5))
    # Upload nodes can occasionally throw transient 5xx. Try a few nodes before failing.
    for attempt, server in enumerate(servers[:max_attempts], start=1):
        if should_cancel and should_cancel():
            raise TransferCancelled("Upload cancelled")
        url = upload_url(server)
        print(f"[UPLOAD] attempt {attempt}: POST {url}", flush=True)
        try:
            with open(filepath, "rb") as fp:
                fields = {"file": (filename, fp, "application/octet-stream")}
                if folder_id:
                    fields["folderId"] = folder_id

                encoder = MultipartEncoder(fields=fields)
                monitor = MultipartEncoderMonitor(encoder, progress_callback)

                headers = _auth_headers()
                headers["Content-Type"] = monitor.content_type

                r = requests.post(url, data=monitor, headers=headers)

            if r.status_code >= 500:
                snippet = (r.text or "")[:300]
                last_err = RuntimeError(
                    f"GoFile server {server} returned HTTP {r.status_code}: {snippet}"
                )
                print(f"[UPLOAD] attempt {attempt} got {r.status_code}, trying next server", flush=True)
                time.sleep(min(2 * attempt, 8))
                continue

            r.raise_for_status()
            result = r.json()
            break
        except TransferCancelled:
            raise
        except requests.RequestException as e:
            last_err = e
            print(f"[UPLOAD] attempt {attempt} request error: {e}", flush=True)
            time.sleep(min(2 * attempt, 8))
    else:
        raise RuntimeError(f"Upload failed across GoFile servers: {last_err}")

    if on_progress:
        on_progress(100.0, filesize, filesize, 0, 0)

    if result.get("status") == "ok":
        dl = result["data"].get("downloadPage", "N/A")
        print(f"[UPLOAD] {filename} DONE -> {dl}", flush=True)
    else:
        print(f"[UPLOAD] {filename} FAILED: {result}", flush=True)

    return result


def upload_path(path, folder_id=None, on_progress=None, should_cancel=None):
    """Upload a file or recursively upload all files in a directory."""
    results = []
    if os.path.isfile(path):
        results.append(
            upload_file(path, folder_id, on_progress=on_progress, should_cancel=should_cancel)
        )
    elif os.path.isdir(path):
        for root, _dirs, files in os.walk(path):
            for fname in sorted(files):
                if should_cancel and should_cancel():
                    raise TransferCancelled("Upload cancelled")
                fpath = os.path.join(root, fname)
                results.append(
                    upload_file(fpath, folder_id, on_progress=on_progress, should_cancel=should_cancel)
                )
    else:
        raise FileNotFoundError(f"Path not found: {path}")
    return results
