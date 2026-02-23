# sync_core.py
import io
import os
import shutil
import tempfile
import zipfile
import requests
import urllib3
from typing import Callable, Any

# Suppress SSL warnings for corporate environments with custom CA certificates
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

API_BASE = "https://api.github.com"

class SyncError(Exception):
    pass

def _headers(token: str | None) -> dict:
    h = {"Accept": "application/vnd.github+json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h

def _check_response(resp: requests.Response, context: str):
    if resp.status_code == 401:
        raise SyncError("Authentifizierung fehlgeschlagen (401): Token ungültig oder abgelaufen.")
    if resp.status_code == 403:
        body = resp.json()
        msg = body.get("message", "")
        raise SyncError(f"Zugriff verweigert (403): {msg}")
    if resp.status_code == 404:
        raise SyncError(f"Nicht gefunden (404): {context}")
    if not resp.ok:
        raise SyncError(f"API-Anfrage fehlgeschlagen ({resp.status_code}): {context}\n{resp.text}")

def get_branches(repo: str, token: str | None) -> list[str]:
    """Fetch list of branches for a remote repository (handles pagination)."""
    branches = []
    url = f"{API_BASE}/repos/{repo}/branches?per_page=100"
    try:
        while url:
            resp = requests.get(url, headers=_headers(token), timeout=30, verify=False)
            _check_response(resp, f"Branches für Repository '{repo}' konnten nicht abgerufen werden")
            branches.extend(b["name"] for b in resp.json())
            # Follow pagination via Link header
            url = None
            link_header = resp.headers.get("Link", "")
            for part in link_header.split(","):
                if 'rel="next"' in part:
                    url = part.split(";")[0].strip().strip("<>")
        return branches
    except SyncError:
        raise
    except requests.RequestException as e:
        raise SyncError(f"Netzwerkfehler: {e}")

def get_latest_commit_sha(repo: str, branch: str, token: str | None) -> str:
    """Fetch the latest commit SHA for a branch."""
    url = f"{API_BASE}/repos/{repo}/commits/{branch}"
    try:
        resp = requests.get(url, headers=_headers(token), timeout=30, verify=False)
        _check_response(resp, f"Commit-Verlauf für Repository '{repo}' konnte nicht abgerufen werden")
        return resp.json()["sha"]
    except requests.RequestException as e:
         raise SyncError(f"Netzwerkfehler: {e}")

def download_zipball(repo: str, branch: str, token: str | None, progress_cb: Callable[[float, bool], None] | None) -> bytes:
    """Download the branch as a ZIP archive and return raw bytes."""
    url = f"{API_BASE}/repos/{repo}/zipball/{branch}"
    resp = requests.get(url, headers=_headers(token), timeout=120, stream=True, verify=False)
    _check_response(resp, f"Branch '{branch}' existiert nicht im Repository '{repo}'")

    chunks = []
    downloaded = 0
    # Estimate size? It's not provided by zipball header typically, so we just track downloaded MB
    for chunk in resp.iter_content(chunk_size=1024 * 256):
        chunks.append(chunk)
        downloaded += len(chunk)
        mb = downloaded / (1024 * 1024)
        if progress_cb:
            progress_cb(mb, False)

    if progress_cb:
        progress_cb(downloaded / (1024 * 1024), True)
    return b"".join(chunks)

def extract_zip_to_temp(zip_bytes: bytes, sub_dir: str | None) -> str:
    tmp_dir = tempfile.mkdtemp(prefix="github_sync_")

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        zf.extractall(tmp_dir)

    entries = os.listdir(tmp_dir)
    if len(entries) == 1 and os.path.isdir(os.path.join(tmp_dir, entries[0])):
        content_root = os.path.join(tmp_dir, entries[0])
    else:
        content_root = tmp_dir

    if sub_dir:
        sub_path = os.path.join(content_root, sub_dir.replace("/", os.sep))
        if not os.path.isdir(sub_path):
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise SyncError(f"Unterverzeichnis '{sub_dir}' existiert nicht im Remote-Repository.")
        content_root = sub_path

    return content_root

def collect_files(root: str) -> set[str]:
    paths: set[str] = set()
    for dirpath, _, filenames in os.walk(root):
        for fname in filenames:
            abs_path = os.path.join(dirpath, fname)
            rel = os.path.relpath(abs_path, root).replace(os.sep, "/")
            paths.add(rel)
    return paths

def _is_binary(data: bytes) -> bool:
    return b"\x00" in data[:8192]

def files_identical(path_a: str, path_b: str) -> bool:
    try:
        with open(path_a, "rb") as fa:
            data_a = fa.read()
        with open(path_b, "rb") as fb:
            data_b = fb.read()
    except (OSError, PermissionError):
        return False

    if data_a == data_b:
        return True

    if not _is_binary(data_a) and not _is_binary(data_b):
        return data_a.replace(b"\r\n", b"\n") == data_b.replace(b"\r\n", b"\n")

    return False

def remove_empty_dirs(root: str):
    for dirpath, _, _ in os.walk(root, topdown=False):
        if dirpath == root:
            continue
        if not os.listdir(dirpath):
            try:
                os.rmdir(dirpath)
            except OSError:
                pass

def calculate_changes(source_root: str, local_dir: str) -> tuple[int, list[str]]:
    """Calculates how many files differ between source and local."""
    remote_files = collect_files(source_root)
    local_files = collect_files(local_dir)
    changes = 0
    
    # Check updates and creates
    for rel_path in remote_files:
        src = os.path.join(source_root, rel_path.replace("/", os.sep))
        dst = os.path.join(local_dir, rel_path.replace("/", os.sep))
        if os.path.isfile(dst):
            if not files_identical(src, dst):
                changes += 1
        else:
            changes += 1
            
    # Check deletes
    local_only = local_files - remote_files
    changes += len(local_only)
    return changes, list(remote_files)

def sync(repo: str, branch: str, local_dir: str, token: str | None,
         sub_dir: str | None = None,
         download_progress_cb: Callable[[float, bool], None] | None = None,
         sync_progress_cb: Callable[[str, int, int, str], None] | None = None):
    
    if sync_progress_cb:
        sync_progress_cb("downloading", 0, 1, "Wird heruntergeladen...")

    zip_bytes = download_zipball(repo, branch, token, download_progress_cb)

    if sync_progress_cb:
        sync_progress_cb("extracting", 0, 1, "Wird entpackt...")

    source_root = extract_zip_to_temp(zip_bytes, sub_dir)

    try:
        remote_files = collect_files(source_root)
        local_files = collect_files(local_dir)
        
        # Calculate changes first so progress bar maps perfectly to actions
        changes_to_make = []
        for rel_path in remote_files:
            src = os.path.join(source_root, rel_path.replace("/", os.sep))
            dst = os.path.join(local_dir, rel_path.replace("/", os.sep))
            if os.path.isfile(dst):
                if not files_identical(src, dst):
                    changes_to_make.append(('update', src, dst, rel_path))
            else:
                 changes_to_make.append(('create', src, dst, rel_path))

        local_only = local_files - remote_files
        for rel_path in local_only:
             abs_path = os.path.join(local_dir, rel_path.replace("/", os.sep))
             changes_to_make.append(('delete', None, abs_path, rel_path))

        total = len(changes_to_make)
        if total == 0:
            if sync_progress_cb:
                sync_progress_cb("done", 1, 1, "Keine Änderungen (bereits aktuell)")
            return

        for idx, (action, src, dst, rel_path) in enumerate(changes_to_make, 1):
             if action == 'update' or action == 'create':
                 os.makedirs(os.path.dirname(dst), exist_ok=True)
                 shutil.copy2(src, dst)
             elif action == 'delete':
                 try:
                     os.remove(dst)
                 except PermissionError:
                     pass
             if sync_progress_cb:
                 sync_progress_cb("syncing", idx, total, f"{'Aktualisiert' if action=='update' else 'Neu erstellt' if action=='create' else 'Gelöscht'} {rel_path}")

        remove_empty_dirs(local_dir)

        if sync_progress_cb:
            sync_progress_cb("done", total, total, "Synchronisierung abgeschlossen")
            
    finally:
        tmp_root = source_root
        tmp_base = tempfile.gettempdir()
        while os.path.dirname(tmp_root) != tmp_base and tmp_root != tmp_base:
            tmp_root = os.path.dirname(tmp_root)
        shutil.rmtree(tmp_root, ignore_errors=True)


def local_mirror(source_dir: str, target_dir: str,
                 progress_cb: Callable[[str, int, int, str], None] | None = None):
    """One-way mirror from source_dir to target_dir (local-to-local).
    
    Makes target_dir an exact copy of source_dir.
    """
    if not os.path.isdir(source_dir):
        if progress_cb:
            progress_cb("done", 1, 1, "Quellordner existiert nicht")
        return

    os.makedirs(target_dir, exist_ok=True)

    src_files = collect_files(source_dir)
    tgt_files = collect_files(target_dir)

    changes_to_make = []
    for rel_path in src_files:
        src = os.path.join(source_dir, rel_path.replace("/", os.sep))
        dst = os.path.join(target_dir, rel_path.replace("/", os.sep))
        if os.path.isfile(dst):
            if not files_identical(src, dst):
                changes_to_make.append(('update', src, dst, rel_path))
        else:
            changes_to_make.append(('create', src, dst, rel_path))

    tgt_only = tgt_files - src_files
    for rel_path in tgt_only:
        abs_path = os.path.join(target_dir, rel_path.replace("/", os.sep))
        changes_to_make.append(('delete', None, abs_path, rel_path))

    total = len(changes_to_make)
    if total == 0:
        if progress_cb:
            progress_cb("done", 1, 1, "Keine Änderungen (bereits aktuell)")
        return

    for idx, (action, src, dst, rel_path) in enumerate(changes_to_make, 1):
        if action in ('update', 'create'):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
        elif action == 'delete':
            try:
                os.remove(dst)
            except PermissionError:
                pass
        if progress_cb:
            progress_cb("syncing", idx, total,
                        f"{'Aktualisiert' if action=='update' else 'Neu erstellt' if action=='create' else 'Gelöscht'} {rel_path}")

    remove_empty_dirs(target_dir)

    if progress_cb:
        progress_cb("done", total, total, "Sicherung abgeschlossen")
