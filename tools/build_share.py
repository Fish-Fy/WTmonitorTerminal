"""从白名单源文件构建无个人信息的 Windows 便携版，不改动个人配置或旧 dist。"""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime
import hashlib
from importlib.metadata import distribution
import json
import marshal
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import types
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import yaml
from src.config import Config, load_config
from src import updater


def public_config():
    config = json.loads(json.dumps(asdict(Config())))
    config["identity"]["my_nick"] = ""
    config["config_blk_path"] = ""
    config["alerts"]["sound_file"] = ""
    config["data_dir"] = "data"
    config["web"]["enabled"] = False
    config["web"]["host"] = "0.0.0.0"
    return config


def sensitive_tokens():
    nick = load_config(ROOT / "config.yaml").identity.my_nick
    values = [nick, str(Path.home()), str(ROOT), Path.home().name]
    if os.environ.get("USERNAME"):
        values.append(os.environ["USERNAME"])
    return sorted({value for value in values if value and len(value) >= 4}, key=len, reverse=True)


def check_text(text, tokens, location, findings):
    folded = text.replace("/", "\\").casefold()
    if any(token.replace("/", "\\").casefold() in folded for token in tokens):
        findings.append(location)


def audit_code(code, tokens, location, findings, filenames):
    if isinstance(code, types.CodeType):
        filenames.add(code.co_filename)
        check_text(code.co_filename, tokens, location + ":filename", findings)
        for index, value in enumerate(code.co_consts):
            audit_code(value, tokens, f"{location}:{index}", findings, filenames)
    elif isinstance(code, str):
        check_text(code, tokens, location, findings)
    elif isinstance(code, (tuple, list, frozenset)):
        for value in code:
            audit_code(value, tokens, location, findings, filenames)


def bundle_maps(payload):
    from PIL import Image
    from src.agent.mapimg import DUP_SKIP, safe_scene_name
    from src.ui.map_library import THUMB_PX

    root = ROOT / load_config(ROOT / "config.yaml").data_dir / "maps"
    allowed = set()
    for scene in sorted(root.iterdir()):
        if (scene.name.startswith(".") or scene.name in DUP_SKIP or not scene.is_dir()
                or scene.is_symlink() or scene.is_junction()
                or scene.name != safe_scene_name(scene.name)):
            continue
        base = scene / "base.png"
        if not base.is_file() or base.is_symlink() or base.stat().st_size == 0:
            continue
        relative = Path("data/maps") / scene.name
        target = payload / relative
        target.mkdir(parents=True)
        # 只接受普通地图图像，拒绝可能携带私有文本或 EXIF 的附件。
        image_info = {"dpi", "jfif", "jfif_version", "jfif_unit", "jfif_density"}
        with Image.open(base) as image:
            if image.format not in {"PNG", "JPEG"} or image.info.keys() - image_info:
                raise ValueError(f"地图底图包含非白名单图像元数据：{scene.name}")
            image.load()
            shutil.copyfile(base, target / "base.png")
            thumb = scene / "thumb.png"
            copied = False
            if thumb.is_file() and not thumb.is_symlink() and thumb.stat().st_mtime_ns >= base.stat().st_mtime_ns:
                with Image.open(thumb) as preview:
                    if preview.format == "PNG" and not preview.info.keys() - image_info:
                        preview.verify()
                        shutil.copyfile(thumb, target / "thumb.png")
                        copied = True
            if not copied:
                preview = image.convert("RGB")
                preview.thumbnail((THUMB_PX, THUMB_PX), Image.Resampling.LANCZOS)
                preview.info.clear()
                preview.save(target / "thumb.png")
        allowed.update({relative / "base.png", relative / "thumb.png"})

        meta_file = scene / "meta.json"
        if meta_file.is_file() and not meta_file.is_symlink():
            meta = json.loads(meta_file.read_text(encoding="utf-8-sig"))
            keys = ("kinds", "kind", "manual", "kind_rule", "public_level", "public_image", "public_source")
            public = {key: meta[key] for key in keys if key in meta}
            if meta.get("public_layouts"):
                public["public_layouts"] = [
                    {**{key: layout[key] for key in ("id", "name", "source", "difficulty") if key in layout},
                     "objects": [{key: obj[key] for key in ("x", "y", "type", "color", "icon", "zone_label")
                                  if key in obj} for obj in layout["objects"]]}
                    for layout in meta["public_layouts"]]
            (target / "meta.json").write_text(json.dumps(public, ensure_ascii=False), encoding="utf-8")
            allowed.add(relative / "meta.json")
    if not allowed:
        raise RuntimeError("没有找到可打包的地图底图")
    return allowed


def audit(payload, work, tokens, map_files=()):
    from PyInstaller.archive.readers import CArchiveReader
    from PyInstaller.loader.pyimod01_archive import ZlibArchiveReader
    findings = []
    filenames = set()
    blocked_names = {"data", ".venv", ".git", ".mnemon", "tests", "AGENT.md", "selfsigned.cer"}
    blocked_extensions = {".pfx", ".p12", ".key", ".cer", ".log", ".lnk", ".pdb"}
    file_count = 0
    for file in payload.rglob("*"):
        if not file.is_file():
            continue
        relative = file.relative_to(payload)
        if (any(part in blocked_names for part in relative.parts) and relative not in map_files
                or file.suffix.lower() in blocked_extensions):
            findings.append(str(relative) + ":forbidden-file")
        check_text(str(relative), tokens, str(relative) + ":path", findings)
        data = file.read_bytes()
        for token in tokens:
            if any(encoded in data for encoded in (token.encode("utf-8"), token.encode("utf-16-le"))):
                findings.append(str(relative) + ":private-bytes")
                break
        file_count += 1
    executable = payload / "WTMonitorTerminal.exe"
    archive = CArchiveReader(str(executable))
    modules = 0
    for name, entry in archive.toc.items():
        if entry[-1] in ("s", "m", "M"):
            audit_code(marshal.loads(archive.extract(name)), tokens, name, findings, filenames)
        elif name.endswith(".pyz"):
            pyz_path = work / "audit.pyz"
            pyz_path.write_bytes(archive.extract(name))
            pyz = ZlibArchiveReader(str(pyz_path))
            for module in pyz.toc:
                value = pyz.extract(module)
                audit_code(value, tokens, module, findings, filenames)
                modules += 1
    for library in payload.rglob("base_library.zip"):
        with zipfile.ZipFile(library) as archive:
            for member in archive.namelist():
                if member.endswith(".pyc"):
                    audit_code(marshal.loads(archive.read(member)[16:]), tokens, member, findings, filenames)
    if findings:
        (work / "privacy-findings.json").write_text(json.dumps(findings, ensure_ascii=False, indent=2), encoding="utf-8")
        raise RuntimeError(f"隐私检查未通过，发现 {len(findings)} 项；详情仅保存在本机构建目录")
    configs = list(payload.rglob("config.yaml"))
    assert configs
    for path in configs:
        cfg = load_config(path)
        assert not cfg.identity.my_nick and not cfg.config_blk_path and not cfg.alerts.sound_file
        assert cfg.data_dir == "data" and not cfg.web.enabled
    return {"files": file_count, "python_modules": modules, "code_filenames": len(filenames),
            "private_findings": 0, "personal_data_included": False}


def archive_payload(payload, output, work, *, program_only=False):
    temporary_zip = work / output.name
    with zipfile.ZipFile(temporary_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for file in sorted(payload.rglob("*")):
            relative = file.relative_to(payload)
            if file.is_file() and (not program_only or relative.parts[0] in updater.UNITS):
                archive.write(file, Path("WTMonitorTerminal") / relative)
    if program_only:
        from threading import Event
        updater.extract_update(temporary_zip, work / "validate-update", updater.read_version(payload / "_internal"),
                               Event())
    else:
        with zipfile.ZipFile(temporary_zip) as archive:
            if archive.testzip() is not None:
                raise RuntimeError("公开版 ZIP 校验失败")
    with temporary_zip.open("rb") as packed:
        sha = hashlib.file_digest(packed, "sha256").hexdigest()
    with temporary_zip.open("rb") as packed, output.open("xb") as dest:
        shutil.copyfileobj(packed, dest)
    output.with_suffix(".sha256").write_text(f"{sha}  {output.name}\n", encoding="ascii")
    return {"archive": output.name, "bytes": output.stat().st_size, "sha256": sha}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", default=datetime.now().strftime("%Y%m%d"))
    parser.add_argument("--version", help="发布版本号，例如 v1.0.0；省略时使用日期版本")
    parser.add_argument("--include-maps", action="store_true", help="附带现存底图、缩略图及公开任务资料")
    parser.add_argument("--source-smoke", action="store_true", help="在隔离的公开版源码副本中自检，避免启动临时 EXE")
    args = parser.parse_args()
    if args.version and not re.fullmatch(r"v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", args.version):
        parser.error("版本号格式应为 v1.0.0 或 1.0.0")
    release_version = args.version.removeprefix("v") if args.version else None
    if release_version and any(int(part) > 65535 for part in release_version.split(".")):
        parser.error("版本号每一段不能大于 65535")
    assert re.fullmatch(r"\d{8}", args.tag)
    assert os.name == "nt" and struct.calcsize("P") == 8, "请使用 Windows 64 位 Python 构建"
    tag = args.tag
    release = ROOT / "release"
    release.mkdir(exist_ok=True)
    basename = f"WTMonitorTerminal-{'v' + release_version if release_version else tag}-Windows-x64"
    output = release / f"{basename}.zip"
    update_output = release / updater.asset_name("v" + release_version) if release_version else None
    for file in (output, update_output):
        if file and (file.exists() or file.with_suffix(".sha256").exists()):
            raise FileExistsError(f"不覆盖已有发布包或校验文件: {file.name}")
    work = ROOT / ".tmp" / ("share-build-" + tag + "-" + datetime.now().strftime("%H%M%S-%f"))
    stage = work / "source"
    stage.mkdir(parents=True)
    nick = load_config(ROOT / "config.yaml").identity.my_nick
    tokens = sensitive_tokens()
    # 白名单：只复制程序源码和手机网页，绝不复制项目目录、个人 assets 目录或 data。
    source_files = [ROOT / "main.py", *sorted((ROOT / "src").rglob("*.py")),
                    *sorted((ROOT / "src/web/static").glob("*"))]
    for source in source_files:
        if not source.is_file() or "__pycache__" in source.parts:
            continue
        destination = stage / source.relative_to(ROOT)
        destination.parent.mkdir(parents=True, exist_ok=True)
        text = source.read_text(encoding="utf-8")
        if nick:
            text = re.sub(re.escape(nick), "示例玩家", text, flags=re.IGNORECASE)
        destination.write_text(text, encoding="utf-8")
    shutil.copyfile(ROOT / "tools/public_launcher.py", stage / "launcher.py")
    shutil.copyfile(ROOT / "tools/public_smoke.py", stage / "share_smoke.py")
    config = "# 公开分享版默认配置；填写自己的游戏昵称后启用阵营识别。\n" + yaml.safe_dump(
        public_config(), allow_unicode=True, sort_keys=False)
    (stage / "config.yaml").write_text(config, encoding="utf-8")
    package_data = [("config.yaml", "."), ("src/web/static", "src/web/static")]
    for asset in ("assets/app.ico", "assets/fonts/rajdhani/Rajdhani-Bold.ttf", "assets/fonts/rajdhani/OFL.txt"):
        destination = stage / asset
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / asset, destination)
        package_data.append((asset, Path(asset).parent.as_posix()))
    if release_version:
        (stage / updater.VERSION_FILE).write_text("v" + release_version + "\n", encoding="ascii")
        shutil.copyfile(ROOT / "assets/install_update.ps1", stage / "install_update.ps1")
        package_data.extend([("update-version.txt", "."), ("install_update.ps1", ".")])
        release_note = ROOT / "release/github" / f"RELEASE-v{release_version}.md"
        if release_note.is_file():
            shutil.copyfile(release_note, stage / updater.NOTES_FILE)
            package_data.append(("update-notes.md", "."))
    year, month, day = int(tag[:4]), int(tag[4:6]), int(tag[6:])
    version = release_version or f"{year}.{month}.{day}"
    numeric_version = tuple(int(part) for part in version.split(".")) + (0,)
    version_info = f"""VSVersionInfo(ffi=FixedFileInfo(filevers={numeric_version},prodvers={numeric_version},mask=0x3f,flags=0,OS=0x40004,fileType=1,subtype=0,date=(0,0)),kids=[StringFileInfo([StringTable('080404B0',[StringStruct('CompanyName','WTMonitorTerminal'),StringStruct('FileDescription','War Thunder 战术终端'),StringStruct('FileVersion','{version}'),StringStruct('ProductVersion','{version}'),StringStruct('ProductName','WTMonitorTerminal'),StringStruct('OriginalFilename','WTMonitorTerminal.exe')])]),VarFileInfo([VarStruct('Translation',[2052,1200])])])"""
    (stage / "version_info.txt").write_text(version_info, encoding="utf-8")
    spec = """a=Analysis(['launcher.py'],pathex=[],binaries=[],datas=package_data,hiddenimports=[],hookspath=[],runtime_hooks=[],excludes=['numpy','tkinter','unittest','pydoc_data','test','PySide6.QtWebEngineCore','PySide6.QtWebEngineWidgets','PySide6.QtMultimediaWidgets','PySide6.QtQuick','PySide6.QtQml','PySide6.Qt3DCore'],optimize=2,noarchive=False)
pyz=PYZ(a.pure)
exe=EXE(pyz,a.scripts,[],exclude_binaries=True,name='WTMonitorTerminal',console=False,debug=False,strip=False,upx=False,icon='assets/app.ico',version='version_info.txt')
coll=COLLECT(exe,a.binaries,a.datas,strip=False,upx=False,name='WTMonitorTerminal')
"""
    (stage / "share.spec").write_text(f"package_data = {package_data!r}\n{spec}", encoding="utf-8")
    build_log = work / "build.log"
    # 只让打包器从 Python/Qt 与 Windows 标准目录解析 DLL。
    # 开发工具附加的 Poppler 等 PATH 可能含同名、导出符号不同的 ICU DLL。
    environment = os.environ.copy()
    windows = Path(os.environ.get("SystemRoot", "C:/Windows"))
    environment["PATH"] = os.pathsep.join((str(Path(sys.executable).parent), sys.base_prefix,
                                           str(windows / "System32"), str(windows)))
    print("构建公开版（隔离目录，不使用个人配置）…", flush=True)
    with build_log.open("w", encoding="utf-8") as log:
        subprocess.run([sys.executable, "-m", "PyInstaller", "share.spec", "--noconfirm", "--clean",
                        "--distpath", str(work / "dist"), "--workpath", str(work / "build")],
                       cwd=stage, env=environment, stdout=log, stderr=subprocess.STDOUT, check=True)
    payload = work / "dist/WTMonitorTerminal"
    # 配置另放一份到 exe 同级，接收者可直接编辑；包内默认配置同样经过清理。
    shutil.copyfile(stage / "config.yaml", payload / "config.yaml")
    map_files = bundle_maps(payload) if args.include_maps else set()
    map_count = sum(path.name == "base.png" for path in map_files)
    includes_sun_city_update = release_version in {"1.1.1", "1.1.2", "1.1.3", "1.1.4", "1.1.5"}
    if includes_sun_city_update:
        from src.agent.bundled_maps import SCENE, SUN_CITY_SHA256, SUN_CITY_UPDATE

        map_path = Path("data/maps") / SCENE
        if map_path / "base.png" not in map_files:
            raise RuntimeError(f"v{release_version} requires the updated Sun City map. Build with --include-maps.")
        if hashlib.sha256((payload / map_path / "base.png").read_bytes()).hexdigest() != SUN_CITY_SHA256:
            raise RuntimeError("The public Sun City map does not match the audited v2.61 asset.")
        shutil.copytree(payload / map_path, payload / "_internal" / SUN_CITY_UPDATE)
    maps_note = (f"此包附带 {map_count} 张地图的底图、缩略图及可用的公开任务布局。"
                 if map_count else "首次使用时，程序会在本文件夹下生成自己的 data 目录。")
    sun_city_note = "更新也会替换已知的旧版日耀之城陆战底图。" if includes_sun_city_update else ""
    update_note = f"""软件更新
右侧昵称旁的“检查更新”会查找新版，程序启动后也会在后台检查一次。下载会同时使用 GitHub、GitHub 下载加速站和 Gitee，采用最先通过校验的更新包。下载就绪后点击“安装新版”并重启。
下载前显示新版改动，在线安装完成后再次提示并保存日志。右键点击“检查更新”即可回看。
在线更新保留 config.yaml、个人方案和地图库。{sun_city_note}完整地图库随完整包提供。
若从没有“检查更新”的旧版手动迁移：关闭两个版本，把新版完整包解压到新目录，然后将旧版的 data 和 config.yaml 复制到新目录。覆盖同名项目时选用旧版文件，先保留旧文件夹，确认新版读到方案后再整理。
如果修改过 config.yaml 中的 data_dir，请保留该设置，并一起迁移实际数据目录。

""" if release_version else ""
    instructions = f"""WTMonitorTerminal 战术终端 · 公开分享版 v{version}

使用方法
1. 把整个压缩包解压到一个可写的普通文件夹，保留 _internal 文件夹。
2. 双击 WTMonitorTerminal.exe，无需安装 Python。
3. 点击右上方“设置昵称”，填写自己的游戏昵称；保存后立即生效，以后可点“修改昵称”更改。
4. 游戏本地战术接口地址为 127.0.0.1:8111；程序会自动读取可用的数据。

地图与标点
右侧可切换地图库、面板设置、地图显示和载具状态显示。
先新建标点方案；地图右键添加标点，Shift+左键快速加点，选中节点后 Shift+点击另一节点连线。
在“标点方案”中可绑定多个任务，也可复制分享码。导入时选地图、粘贴分享码、预览标点、路线和区域，再保存为新方案。
“绘制区域”可从圆心拖拽画圆，或依次点击三个圆周点画圆。多边形按 Enter 保存；三点画圆和多边形均可用 Backspace 撤回上一个点，Esc 取消。
右键已有标点，选择“从此点续画路线”可连续添加路线点并连线，按 Esc 结束。连接路线点时显示双尖括号，狙击点指向交战点时显示进攻箭头。
复制分享码可选汉字短码（WT5）或字母数字码。含图外圆心、大圆时字母数字码使用 WT4。接收方需使用支持对应格式的版本，新版也可读取 WT1、WT2、WT3。
地图会自动缓存，同底图自动合并，人工命名优先。没有实时对象时会明确显示缓存提示。
载具状态显示中可切换姿态仪样式；本局载具识别中可开启“高亮击毁过我的玩家”，标记持续到本局结束。

手机显示
电脑中点击“手机显示”并开启；手机和电脑处于同一局域网后，在手机浏览器打开面板给出的地址。
支持地图、标点、轨迹和本局载具列表只读显示，双指缩放、拖动和自动重连。
手机和平板名单按“昵称、载具、击杀、状态”紧凑显示，点击玩家可展开本局载具明细。
右上角“全屏”可请求隐藏浏览器栏并切为横屏；不支持的浏览器会提示，iPhone/iPad可添加到主屏幕后打开。
地图名显示在顶部；“⋯”菜单提供缩放、复位和地图铺满。地图默认等比例填满，边缘内容可拖动查看。
手机连接电脑移动热点时，使用面板中 192.168.137.1 的地址；打不开可点击“修复热点连接（管理员）”，允许 Windows 管理员确认后刷新手机页面。
修复只允许 192.168.137.0/24 热点网段访问当前手机显示端口，规则会保留；换端口后需再次修复。
默认未开启网络共享；无需向公网转发端口。

{update_note}数据与隐私
{maps_note}
此包使用空白昵称和默认配置。个人方案、位置缓存、对局记录、日志和个人证书均排除在分享包之外。
以后转发程序前请检查自己的 data 和改过的配置。
保留 _internal 和 licenses 文件夹。接收者可自行修改配置与个人数据，互不影响。
"""
    (payload / "使用说明.txt").write_text(instructions, encoding="utf-8-sig")
    licenses = payload / "licenses"
    licenses.mkdir()
    shutil.copyfile(Path(sys.base_prefix) / "LICENSE.txt", licenses / "Python-LICENSE.txt")
    components = []
    for name in ("PySide6", "PySide6_Essentials", "PySide6_Addons", "shiboken6", "httpx", "httpcore",
                 "PyYAML", "certifi", "anyio", "idna", "h11", "typing_extensions", "Pillow", "numpy"):
        dist = distribution(name)
        components.append(f"{name} {dist.version}")
        for file in dist.files or []:
            if any(term in file.name.lower() for term in ("license", "copying", "notice")):
                source = Path(dist.locate_file(file))
                if source.is_file():
                    destination = licenses / name / file.name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, destination)
    (licenses / "components.txt").write_text("\n".join(components) + "\n", encoding="utf-8")
    print("检查文件白名单、内嵌 Python 代码和个人信息…", flush=True)
    privacy = audit(payload, work, tokens, map_files)
    # 自检在副本中执行，不让测试创建的 data 或配置污染可分享文件夹。
    validation = work / "validation"
    if args.source_smoke:
        shutil.copytree(stage, validation)
        command = [sys.executable, str(validation / "launcher.py")]
    else:
        shutil.copytree(payload, validation)
        command = [str(validation / "WTMonitorTerminal.exe")]
    smoke_report = work / "smoke-report.json"
    result = subprocess.run([*command, "--self-test", "--self-test-report", str(smoke_report)],
                            cwd=validation, timeout=60)
    if result.returncode != 0 or not smoke_report.is_file():
        raise RuntimeError(f"打包自检失败，退出码 {result.returncode}")
    smoke = json.loads(smoke_report.read_text(encoding="utf-8"))
    assert smoke.get("ok"), smoke
    smoke["execution"] = "staged_source" if args.source_smoke else "packaged_exe"
    # 确认待发副本没有在验证过程中创建任何个人数据。
    privacy = audit(payload, work, tokens, map_files)
    report = {**archive_payload(payload, output, work), "version": version,
              "privacy": privacy, "smoke_test": smoke, "bundled_maps": map_count}
    if update_output:
        report["program_update"] = archive_payload(payload, update_output, work, program_only=True)
    (release / f"{basename}-validation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
