import os
import time
import requests
from requests_toolbelt import MultipartEncoder, MultipartEncoderMonitor

from downloader import TransferCancelled
from upload_common import format_size  # re-exported for backward compatibility


GOFILE_API_KEY = os.environ.get("GOFILE_API_KEY", "")


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


def upload_file(filepath, folder_id=None, on_progress=None, should_cancel=None):
    """Upload a single file to GoFile with progress logging to stdout.

    on_progress(pct, uploaded, total, speed, eta_seconds) is called at most
    once per second if provided.

    should_cancel: optional callable; if it returns True, upload aborts (TransferCancelled).
    """
    filename = os.path.basename(filepath)
    filesize = os.path.getsize(filepath)
    servers = get_upload_servers()
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
    # Upload nodes can occasionally throw transient 5xx. Try a few nodes before failing.
    for attempt, server in enumerate(servers[:5], start=1):
        if should_cancel and should_cancel():
            raise TransferCancelled("Upload cancelled")
        url = f"https://{server}.gofile.io/uploadfile"
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
