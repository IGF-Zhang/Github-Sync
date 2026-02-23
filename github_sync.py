#!/usr/bin/env python3
"""
GitHub Branch → Local Directory One-Way Sync Tool

Downloads the branch as a ZIP archive, extracts to a temp folder,
then compares locally and syncs changes. Remote is always authoritative.
"""

import argparse
import hashlib
import io
import os
import shutil
import sys
import tempfile
import zipfile

import requests

# ──────────────────────────────────────────────
# GitHub API helpers
# ──────────────────────────────────────────────

API_BASE = "https://api.github.com"


def _headers(token: str | None) -> dict:
    h = {"Accept": "application/vnd.github+json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _check_response(resp: requests.Response, context: str):
    """Raise a clear error on non-2xx responses."""
    if resp.status_code == 401:
        sys.exit("❌ 认证失败 (401)：Token 无效或已过期。请检查 GITHUB_TOKEN。")
    if resp.status_code == 403:
        body = resp.json()
        msg = body.get("message", "")
        sys.exit(f"❌ 权限不足 (403)：{msg}")
    if resp.status_code == 404:
        sys.exit(f"❌ 未找到 (404)：{context}")
    if not resp.ok:
        sys.exit(f"❌ API 请求失败 ({resp.status_code})：{context}\n{resp.text}")


def download_zipball(repo: str, branch: str, token: str | None) -> bytes:
    """Download the branch as a ZIP archive and return raw bytes."""
    url = f"{API_BASE}/repos/{repo}/zipball/{branch}"
    print(f"📦 正在下载 ZIP 压缩包 ({repo}@{branch}) ...")
    resp = requests.get(url, headers=_headers(token), timeout=120, stream=True)
    _check_response(resp, f"分支 '{branch}' 在仓库 '{repo}' 中不存在")

    # Read with progress
    chunks = []
    downloaded = 0
    for chunk in resp.iter_content(chunk_size=1024 * 256):
        chunks.append(chunk)
        downloaded += len(chunk)
        mb = downloaded / (1024 * 1024)
        print(f"\r   已下载 {mb:.1f} MB ...", end="", flush=True)

    print(f"\r   已下载 {downloaded / (1024 * 1024):.1f} MB ✅       ")
    return b"".join(chunks)


def extract_zip_to_temp(zip_bytes: bytes, sub_dir: str | None) -> str:
    """Extract ZIP to a temp directory, return path to the content root.

    GitHub ZIP has a top-level dir like 'repo-sha/'.  We detect it and
    return the effective root (optionally including sub_dir offset).
    """
    tmp_dir = tempfile.mkdtemp(prefix="github_sync_")
    print(f"📂 正在解压到临时目录 ...")

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        zf.extractall(tmp_dir)

    # Detect the top-level directory GitHub creates (e.g. 'Repo-abc1234/')
    entries = os.listdir(tmp_dir)
    if len(entries) == 1 and os.path.isdir(os.path.join(tmp_dir, entries[0])):
        content_root = os.path.join(tmp_dir, entries[0])
    else:
        content_root = tmp_dir

    # If sub_dir specified, narrow down to that subdirectory
    if sub_dir:
        sub_path = os.path.join(content_root, sub_dir.replace("/", os.sep))
        if not os.path.isdir(sub_path):
            shutil.rmtree(tmp_dir, ignore_errors=True)
            sys.exit(f"❌ 子目录 '{sub_dir}' 在远端仓库中不存在。")
        content_root = sub_path

    return content_root


# ──────────────────────────────────────────────
# Filesystem helpers
# ──────────────────────────────────────────────

def collect_files(root: str) -> set[str]:
    """Return a set of relative POSIX paths for every file under *root*."""
    paths: set[str] = set()
    for dirpath, _, filenames in os.walk(root):
        for fname in filenames:
            abs_path = os.path.join(dirpath, fname)
            rel = os.path.relpath(abs_path, root).replace(os.sep, "/")
            paths.add(rel)
    return paths


def _is_binary(data: bytes) -> bool:
    """Heuristic: file is binary if it contains null bytes in the first 8KB."""
    return b"\x00" in data[:8192]


def files_identical(path_a: str, path_b: str) -> bool:
    """Return True if two files have identical content.

    For text files, line endings (CRLF vs LF) are normalized before
    comparison so that Windows/Unix differences are ignored.
    For binary files, exact byte comparison is used.
    """
    try:
        with open(path_a, "rb") as fa:
            data_a = fa.read()
        with open(path_b, "rb") as fb:
            data_b = fb.read()
    except (OSError, PermissionError):
        return False

    # Exact match — fast path
    if data_a == data_b:
        return True

    # For text files, normalize CRLF → LF and compare
    if not _is_binary(data_a) and not _is_binary(data_b):
        return data_a.replace(b"\r\n", b"\n") == data_b.replace(b"\r\n", b"\n")

    return False


def remove_empty_dirs(root: str):
    """Remove empty directories bottom-up (excluding *root* itself)."""
    for dirpath, _, _ in os.walk(root, topdown=False):
        if dirpath == root:
            continue
        if not os.listdir(dirpath):
            try:
                os.rmdir(dirpath)
            except OSError:
                pass


# ──────────────────────────────────────────────
# Main sync logic
# ──────────────────────────────────────────────

def sync(repo: str, branch: str, local_dir: str, token: str | None,
         sub_dir: str | None = None):

    label = f"{repo}@{branch}"
    if sub_dir:
        label += f"/{sub_dir}"
    print(f"🔄 正在同步  {label}  →  {local_dir}\n")

    # ── Step 1: Download ZIP ──
    zip_bytes = download_zipball(repo, branch, token)

    # ── Step 2: Extract to temp ──
    source_root = extract_zip_to_temp(zip_bytes, sub_dir)

    try:
        # ── Step 3: Compare and sync ──
        remote_files = collect_files(source_root)
        local_files = collect_files(local_dir)

        skipped = 0
        updated = 0
        created = 0
        deleted = 0
        errors  = 0

        # -- Process remote files --
        total = len(remote_files)
        print(f"\n🔍 开始对比 {total} 个远端文件 ...\n")

        for idx, rel_path in enumerate(sorted(remote_files), 1):
            src = os.path.join(source_root, rel_path.replace("/", os.sep))
            dst = os.path.join(local_dir, rel_path.replace("/", os.sep))
            progress = f"[{idx}/{total}]"

            if os.path.isfile(dst):
                # Both exist — compare
                if files_identical(src, dst):
                    #print(f"  {progress} [SKIP]   📄 {rel_path}")
                    skipped += 1
                else:
                    try:
                        os.makedirs(os.path.dirname(dst), exist_ok=True)
                        shutil.copy2(src, dst)
                        print(f"  {progress} [UPDATE] 📄 {rel_path}")
                        updated += 1
                    except PermissionError:
                        print(f"  {progress} [ERROR]  ⛔ {rel_path}  (写入被拒绝)")
                        errors += 1
            else:
                # Remote-only — create
                try:
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    shutil.copy2(src, dst)
                    print(f"  {progress} [CREATE] 📄 {rel_path}")
                    created += 1
                except PermissionError:
                    print(f"  {progress} [ERROR]  ⛔ {rel_path}  (写入被拒绝)")
                    errors += 1

        # -- Delete local-only files --
        local_only = local_files - remote_files
        for rel_path in sorted(local_only):
            abs_path = os.path.join(local_dir, rel_path.replace("/", os.sep))
            try:
                os.remove(abs_path)
                print(f"  [DELETE] 🗑️  {rel_path}")
                deleted += 1
            except PermissionError:
                print(f"  [ERROR]  ⛔ {rel_path}  (删除被拒绝)")
                errors += 1

        # -- Clean up empty directories --
        remove_empty_dirs(local_dir)

        # -- Summary --
        print()
        print(
            f"[DONE] ✅ 同步完成。"
            f"跳过 {skipped} 个文件，"
            f"更新 {updated} 个文件，"
            f"新增 {created} 个文件，"
            f"删除 {deleted} 个文件。"
        )
        if errors:
            print(f"       ⚠️  {errors} 个文件因权限问题未能处理。")

    finally:
        # ── Step 4: Clean up temp directory ──
        # Walk up to the actual temp root (source_root may be a subdirectory)
        tmp_root = source_root
        tmp_base = tempfile.gettempdir()
        while os.path.dirname(tmp_root) != tmp_base and tmp_root != tmp_base:
            tmp_root = os.path.dirname(tmp_root)
        shutil.rmtree(tmp_root, ignore_errors=True)
        print("🧹 临时文件已清理。")


# ──────────────────────────────────────────────
# CLI entry point
# ──────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="将 GitHub 仓库指定分支的内容单向同步到本地文件夹（ZIP 快速模式）。"
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("GITHUB_TOKEN"),
        help="GitHub Personal Access Token（也可通过环境变量 GITHUB_TOKEN 传入）",
    )
    parser.add_argument(
        "--repo",
        required=True,
        help="仓库全名，例如 octocat/Hello-World",
    )
    parser.add_argument(
        "--branch",
        required=True,
        help="目标分支名称，例如 main",
    )
    parser.add_argument(
        "--local-dir",
        required=True,
        dest="local_dir",
        help="同步到的本地文件夹路径",
    )
    parser.add_argument(
        "--sub-dir",
        default=None,
        dest="sub_dir",
        help="仅同步仓库中的某个子目录，例如 Skripte",
    )
    args = parser.parse_args()

    # ── Validate local dir ──
    local_dir = os.path.abspath(args.local_dir)
    if not os.path.isdir(local_dir):
        answer = input(
            f"📁 本地目录 '{local_dir}' 不存在，是否自动创建？[y/N] "
        ).strip().lower()
        if answer in ("y", "yes"):
            os.makedirs(local_dir, exist_ok=True)
            print(f"   已创建目录：{local_dir}")
        else:
            sys.exit("已取消。")

    sync(
        repo=args.repo,
        branch=args.branch,
        local_dir=local_dir,
        token=args.token,
        sub_dir=args.sub_dir,
    )


if __name__ == "__main__":
    main()
