"""Download a verified program-only release and hand off replacement on Windows."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
import base64
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from threading import Event, Lock
from zipfile import ZipFile

import httpx

from .i18n import tr


REPOSITORY = "Fish-Fy/WTmonitorTerminal"
RELEASE_URL = f"https://github.com/{REPOSITORY}/releases/latest"
API_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
GITEE_REPO = "FishFy/WTMonitorTerminal"
GH_PROXY = "https://gh-proxy.org"
QUARK_URL = "https://pan.quark.cn/s/d0b76f323948"
EXE = "WTMonitorTerminal.exe"
UNITS = (EXE, "_internal", "licenses", "使用说明.txt")
VERSION_FILE = "update-version.txt"
NOTES_FILE = "update-notes.md"
HISTORY_DIR = "update_history"
NO_NOTES = "本版暂无更新说明。"
MAX_NOTES = 32000
MAX_ZIP = 512 * 1024 * 1024
MAX_UNPACKED = 1024 * 1024 * 1024
VERSION = re.compile(r"v(0|[1-9][0-9]{0,4})\.(0|[1-9][0-9]{0,4})\.(0|[1-9][0-9]{0,4})")


class UpdateError(Exception):
    pass


class UpdateCancelled(UpdateError):
    pass


@dataclass(frozen=True)
class Release:
    tag: str
    url: str
    size: int
    sha256: str
    notes: str = ""

    @property
    def page(self):
        return f"https://github.com/{REPOSITORY}/releases/tag/{self.tag}"


def download_sources(release):
    suffix = f"/releases/download/{release.tag}/{asset_name(release.tag)}"
    return {
        "GitHub": release.url,
        "GitHub 加速站": f"{GH_PROXY}/{release.url}",
        "Gitee": f"https://gitee.com/{GITEE_REPO}{suffix}",
    }


def version_key(tag):
    match = VERSION.fullmatch(tag) if isinstance(tag, str) else None
    if match:
        parts = tuple(map(int, match.groups()))
        if all(part <= 65535 for part in parts):
            return parts
    return None


def read_version(resources):
    try:
        version = (resources / VERSION_FILE).read_text(encoding="ascii").strip()
        return version if version_key(version) else None
    except (OSError, ValueError):
        return None


def asset_name(tag):
    return f"WTMonitorTerminal-{tag}-Update-Windows-x64.zip"


def identify_release(metadata, current):
    if metadata.get("draft") or metadata.get("prerelease"):
        return None
    tag = metadata.get("tag_name")
    latest = version_key(tag)
    installed = version_key(current)
    if latest is None or installed is None or latest <= installed:
        return None
    name = asset_name(tag)
    asset = next((a for a in metadata.get("assets", []) if a.get("name") == name), {})
    url = f"https://github.com/{REPOSITORY}/releases/download/{tag}/{name}"
    size = asset.get("size")
    digest = asset.get("digest")
    if (asset.get("state") != "uploaded" or asset.get("browser_download_url") != url
            or type(size) is not int or not 0 < size <= MAX_ZIP
            or not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest)):
        raise UpdateError(tr("{0} 已发布，暂时没有可验证的在线更新包。", tag))
    notes = metadata.get("body")
    return Release(tag, url, size, digest[7:], notes[:MAX_NOTES].strip() if isinstance(notes, str) else "")


def read_notes(path):
    try:
        with path.open(encoding="utf-8-sig") as stream:
            return stream.read(MAX_NOTES).strip()
    except (OSError, UnicodeError):
        return ""


def record_update(data_root, version, notes):
    from .agent.mapimg import atomic_write

    if version_key(version) is None:
        raise ValueError("Invalid version for update history")
    installed_at = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %z")
    entry = f"已安装 {version} · {installed_at}\n\n{notes}\n"
    atomic_write(data_root / HISTORY_DIR / f"{version}.md", entry.encode("utf-8"))


def history_notes(data_root):
    entries = {}
    for path in (data_root / HISTORY_DIR).glob("v*.md"):
        if version_key(path.stem) and (entry := read_notes(path)):
            entries[path.stem] = entry
    return entries


def check_latest(client, current):
    gitee_feed = f"https://gitee.com/api/v5/repos/{GITEE_REPO}/contents/update-manifest.json"
    for feed in (API_URL, gitee_feed):
        try:
            reply = client.get(feed, headers={"Accept": "application/json"}, follow_redirects=False)
            reply.raise_for_status()
            metadata = reply.json()
            if feed == gitee_feed:
                metadata = json.loads(base64.b64decode(metadata["content"]))
            if not isinstance(metadata, dict) or not version_key(metadata.get("tag_name")):
                raise ValueError("invalid tag")
            return identify_release(metadata, current)
        except (httpx.HTTPError, UpdateError, ValueError, AttributeError, KeyError, TypeError) as error:
            if feed == gitee_feed:
                raise UpdateError(tr("无法从官方发布源取得可验证的版本信息。")) from error


def check_cancel(cancel):
    if cancel.is_set():
        raise UpdateCancelled()


def allowed_redirect(source, url):
    if url.scheme != "https" or url.port not in (None, 443) or url.userinfo:
        return False
    github = {"github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com",
              "github-releases.githubusercontent.com"}
    if source == "GitHub 加速站":
        return url.host == httpx.URL(GH_PROXY).host or url.host in github
    if source == "Gitee":
        return url.host == "gitee.com" or url.host.endswith(".gitee.com")
    return url.host in github


def download_one(client, release, source, url, target, check, progress):
    checksum = hashlib.sha256()
    seen = 0
    for _ in range(5):
        check()
        with client.stream("GET", url, follow_redirects=False) as reply:
            if reply.is_redirect:
                url = reply.url.join(reply.headers.get("location", ""))
                if not allowed_redirect(source, url):
                    raise UpdateError(tr("更新下载跳转地址无效。"))
                continue
            reply.raise_for_status()
            with target.open("xb") as out:
                for chunk in reply.iter_bytes():
                    check()
                    seen += len(chunk)
                    if seen > release.size:
                        raise UpdateError(tr("下载大小与发布记录不一致。"))
                    out.write(chunk)
                    checksum.update(chunk)
                    progress(seen)
            break
    else:
        raise UpdateError(tr("更新下载跳转次数过多。"))
    if seen != release.size or checksum.hexdigest() != release.sha256:
        raise UpdateError(tr("更新包 SHA-256 校验失败。"))


def download(client, release, target, cancel, progress):
    sources = download_sources(release)
    stop = Event()
    lock = Lock()
    percent = -1

    def check():
        check_cancel(cancel)
        check_cancel(stop)

    def report(seen):
        nonlocal percent
        with lock:
            verified_pending = min(seen, release.size - 1)
            current = verified_pending * 100 // release.size
            if current > percent and not stop.is_set():
                percent = current
                progress(verified_pending, release.size)

    check()
    errors = []
    with tempfile.TemporaryDirectory(prefix="mirrors-", dir=target.parent) as directory:
        with ThreadPoolExecutor(max_workers=len(sources)) as pool:
            tasks = {}
            for index, (source, url) in enumerate(sources.items()):
                part = Path(directory) / f"{index}.part"
                task = pool.submit(download_one, client, release, source, url, part, check, report)
                tasks[task] = source, part
            try:
                for task in as_completed(tasks):
                    check_cancel(cancel)
                    source, part = tasks[task]
                    try:
                        task.result()
                    except (httpx.HTTPError, UpdateError, OSError) as error:
                        errors.append(f"{tr(source)}: {error}")
                        continue
                    stop.set()
                    winner = part
                    break
                else:
                    raise UpdateError(tr("所有更新线路均失败。") + "\n" + "\n".join(errors))
            finally:
                stop.set()
        check_cancel(cancel)
        winner.rename(target)
        progress(release.size, release.size)


def extract_update(package, stage, tag, cancel):
    stage.mkdir()
    total = 0
    canonical = {}
    files = set()
    with ZipFile(package) as archive:
        for entry in archive.infolist():
            check_cancel(cancel)
            parts = entry.filename.split("/")
            attrs = entry.external_attr
            if (len(parts) < 2 or parts[0] != "WTMonitorTerminal" or parts[1] not in UNITS
                    or (len(parts) > 2 and parts[1] not in {"_internal", "licenses"})
                    or entry.is_dir() or attrs & 0x410
                    or stat.S_IFMT(attrs >> 16) not in (0, stat.S_IFREG)
                    or any(not part or part in {".", ".."} or part.endswith((".", " "))
                           or re.search(r'[<>:"\\|?*\x00-\x1f]', part)
                           or PureWindowsPath(part).is_reserved() for part in parts)):
                raise UpdateError(tr("更新包中有不允许的文件路径。"))
            for depth in range(2, len(parts) + 1):
                prefix = "/".join(parts[1:depth])
                key = prefix.casefold()
                if canonical.setdefault(key, prefix) != prefix or key in files:
                    raise UpdateError(tr("更新包有重名或冲突的文件。"))
            files.add(key)
            total += entry.file_size
            if total > MAX_UNPACKED:
                raise UpdateError(tr("更新包解压大小超过限制。"))
            destination = stage.joinpath(*parts[1:])
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(entry) as source, destination.open("xb") as out:
                shutil.copyfileobj(source, out)
    if (not (stage / EXE).is_file() or not (stage / "licenses").is_dir()
            or not (stage / UNITS[-1]).is_file() or read_version(stage / "_internal") != tag):
        raise UpdateError(tr("更新包程序文件不完整，或内部版本号不符。"))


def ensure_install_root(root, data_root):
    if not (root / EXE).is_file() or not (root / "_internal").is_dir():
        raise UpdateError(tr("找不到完整的便携版程序目录。"))
    data_root = data_root.resolve()
    for name in UNITS:
        part = root / name
        target = part.resolve()
        if (part.is_symlink() or part.is_junction() or target == data_root
                or target.is_relative_to(data_root) or data_root.is_relative_to(target)):
            raise UpdateError(tr("程序文件与个人数据的位置有重叠，无法安全地自动更新。"))


def prepare(client, release, root, data_root, cancel, progress):
    ensure_install_root(root, data_root)
    if shutil.disk_usage(root).free < 4 * release.size:
        raise UpdateError(tr("程序所在磁盘的可用空间不足。"))
    work = Path(tempfile.mkdtemp(prefix=".wt-update-", dir=root))
    try:
        package = work / "download.zip"
        download(client, release, package, cancel, progress)
        progress(0, 0)
        extract_update(package, work / "stage", release.tag, cancel)
        if release.notes:
            (work / NOTES_FILE).write_text(release.notes, encoding="utf-8")
        package.unlink()
        check_cancel(cancel)
        return work
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise


def launch_installer(work, resources, *, parent_pid=None):
    # Only the installed helper is run. The new package cannot choose the install paths.
    script = (resources / "install_update.ps1").read_text(encoding="utf-8")
    command = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    shell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    env = {key: value for key, value in os.environ.items() if not key.startswith("_PYI_")}
    env.update(WT_UPDATE_PARENT=str(parent_pid if parent_pid is not None else os.getpid()),
               WT_UPDATE_WORK=str(work),
               PYINSTALLER_RESET_ENVIRONMENT="1")
    if getattr(sys, "frozen", False):
        import ctypes
        env["PATH"] = os.pathsep.join(
            path for path in env.get("PATH", "").split(os.pathsep)
            if not Path(path.strip('"')).resolve().is_relative_to(resources.resolve()))
        ctypes.windll.kernel32.SetDllDirectoryW(None)
    try:
        return subprocess.Popen(
            [str(shell), "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", command],
            cwd=work.parent, env=env, creationflags=subprocess.CREATE_NO_WINDOW,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    finally:
        if getattr(sys, "frozen", False):
            ctypes.windll.kernel32.SetDllDirectoryW(sys._MEIPASS)


def mark_started():
    """Signal the installer and return the notes for this restart, if it was an update."""
    ready = os.environ.pop("WT_UPDATE_READY", "")
    if not ready:
        return None
    signal = Path(ready)
    notes = read_notes(signal.with_name(NOTES_FILE))
    try:
        signal.touch(exist_ok=False)
    except OSError:
        return None
    return notes
