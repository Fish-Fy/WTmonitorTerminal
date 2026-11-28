"""随公开版携带的离线自检，可验证打包后的资源与手机显示。"""
import http.client
import json
import os
from pathlib import Path
import sys
import tempfile


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def run():
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    report = {"ok": False}
    try:
        index = sys.argv.index("--self-test-report")
        report_path = Path(sys.argv[index + 1])
    except (ValueError, IndexError):
        return 2
    try:
        from PySide6.QtWidgets import QApplication, QInputDialog
        from PySide6.QtGui import QImage
        from src.config import load_config
        from src.ui.main_window import MainWindow
        from src.agent.poller import Poller
        from src.store.models import Marker
        from src.agent.map_identity import image_identity, same_map
        from src.updater import read_version
        from main import app_root, ensure_config, resource_root

        cfg = load_config(ensure_config())
        root = app_root()
        require(not cfg.identity.my_nick, "默认昵称必须为空")
        require(not cfg.config_blk_path and not cfg.alerts.sound_file, "默认配置不能包含本机路径")
        require(cfg.data_dir == "data" and not cfg.web.enabled, "默认数据/共享配置异常")
        report["clean_config"] = True
        resources = resource_root()
        require((resources / "assets/app.ico").is_file(), "图标资源缺失")
        app = QApplication([])
        app.setApplicationName("WTMonitorTerminal Self Test")
        with tempfile.TemporaryDirectory(prefix="wt-self-test-", dir=report_path.resolve().parent) as temporary:
            cfg.data_dir = temporary
            cfg.combat_overlay.enabled = False
            cfg.web.host = "127.0.0.1"
            start = Poller.start
            Poller.start = lambda self: None
            try:
                version = read_version(resources)
                window = MainWindow(cfg, root,
                                    update_resources=resources, update_version=version)
            finally:
                Poller.start = start
            try:
                window.show()
                app.processEvents()
                if version:
                    require(window.update_button.text() == "检查更新", "更新入口未显示")
                    require((resources / "install_update.ps1").is_file(), "更新辅助程序未打包")
                    report["update_controls"] = True
                dialog = QInputDialog.getText
                QInputDialog.getText = lambda *args, **kwargs: ("示例玩家", True)
                try:
                    window.nickname_button.click()
                finally:
                    QInputDialog.getText = dialog
                require(window.roster.my_nick == "示例玩家", "界面昵称未立即生效")
                require(window.nickname_label.toolTip() == "我：示例玩家", "昵称按钮旁的本人名字未更新")
                require(load_config(root / "config.yaml").identity.my_nick == "示例玩家",
                        "界面昵称未保存到配置")
                report["nickname_setting"] = True
                window.roster.absorb_line("对手 (T-34) 击毁了 [ABC] 示例玩家 (M4A1).", 100)
                window._roster_dirty = True
                window._tick_roster()
                require(window.nickname_label.toolTip() == "我：[ABC] 示例玩家", "本人战队前缀未同步")
                tree = window.roster_panel.tree
                titles = [tree.topLevelItem(i).text(0) for i in range(tree.topLevelItemCount())]
                require(sum(title.startswith("我方") for title in titles) == 1,
                        "未找到唯一的我方名单分组")
                require(window.roster.display_names().count("示例玩家") == 0, "本人显示出现重复")
                report["merged_friend_group"] = True
                image = QImage(768, 768, QImage.Format.Format_RGB32)
                image.fill(0xff223344)
                window._on_scene("示例地图")
                window._on_map(image, "示例地图")
                from PySide6.QtCore import QByteArray, QBuffer, QIODevice
                map_bytes = QByteArray()
                map_buffer = QBuffer(map_bytes)
                map_buffer.open(QIODevice.OpenModeFlag.WriteOnly)
                require(image.save(map_buffer, "PNG"), "地图测试图片编码失败")
                identity = image_identity(bytes(map_bytes))
                require(identity is not None and same_map(identity, identity), "地图去重依赖异常")
                report["map_identity"] = True
                from src.agent.mapobj import parse_map_objects
                from src.ui.air_target_icons import ground_symbol_kind, make_target_item
                symbol_objects = parse_map_objects([
                    {"type": "ground_model", "icon": icon, "x": .4, "y": .5, "color": "#043fff"}
                    for icon in ("SPAA", "LightTank", "MediumTank", "HeavyTank", "Artillery",
                                 "TankDestroyer", "IFV", "Wheeled", "NewGroundType")
                ])
                for obj in symbol_objects:
                    kind = ground_symbol_kind(obj)
                    require(kind is not None, f"地面类别未识别: {obj.icon}")
                    require(not make_target_item(kind, obj.color).boundingRect().isEmpty(),
                            f"地面图标未生成: {obj.icon}")
                for kind in ("pillbox", "bombing_point", "defending_point"):
                    require(not make_target_item(kind, "#174dff").boundingRect().isEmpty(),
                            f"空战目标图标未生成: {kind}")
                report["air_and_ground_symbols"] = True
                window.new_scheme("示例方案")
                window._current.markers.append(Marker.new("walk", .5, .5))
                window.map_view.set_scheme(window._current)
                window.persist_schemes()
                require(window.phone_mirror.start(0), "手机服务无法启动")
                from src.ui.phone_panel import PhonePanel
                from src.web.firewall import repair_command
                phone_panel = PhonePanel(window)
                require(phone_panel.repair.isEnabled(), "热点修复按钮不可用")
                program, arguments = repair_command(window.phone_mirror.server.bound_port)
                require(Path(program).is_file() and "-EncodedCommand" in arguments, "防火墙修复命令不可用")
                phone_panel.deleteLater()
                report["hotspot_repair_available"] = True
                window.phone_mirror.capture()
                routes = {}
                for route in ("/", "/static/app.js", "/static/style.css", "/frame.jpg", "/api/snapshot"):
                    connection = http.client.HTTPConnection("127.0.0.1", window.phone_mirror.server.bound_port, timeout=5)
                    connection.request("GET", route)
                    response = connection.getresponse()
                    body = response.read()
                    routes[route] = response.status
                    require(response.status == 200 and body, f"资源加载失败: {route}")
                    if route == "/frame.jpg":
                        require(body.startswith(b"\xff\xd8"), "地图画面格式异常")
                    elif route == "/":
                        page = body.decode("utf-8")
                        require('id="fullscreen"' in page and 'id="map-tools"' in page,
                                "手机全屏/顶部工具菜单资源缺失")
                        require('id="notice"' not in page and 'class="map-title"' not in page,
                                "旧手机底栏/地图标题资源残留")
                    elif route == "/static/app.js":
                        script = body.decode("utf-8")
                        require("fittedImageSize" in script and "lockLandscape" in script,
                                "手机地图填充/横屏逻辑资源缺失")
                    elif route == "/api/snapshot":
                        snapshot = json.loads(body)
                        require("roster_options" in snapshot, "手机PC筛选同步字段缺失")
                        rows = [row for group in snapshot["roster"].values() for row in group]
                        require(rows and all("display_name" in row and "vehicles" in row for row in rows),
                                "手机紧凑名单/载具明细字段缺失")
                    connection.close()
                report["compact_mobile_and_fullscreen"] = True
                window._show_sidebar("panels")
                picker = window.sidebar_pages["panels"].language_combo
                picker.setCurrentIndex(picker.findData("en"))
                report.update(ok=True, window=True, markers=True, routes=routes, isolated_data=True)
            finally:
                window.close()
                app.processEvents()
            Poller.start = lambda self: None
            try:
                english = MainWindow(cfg, root)
            finally:
                Poller.start = start
            try:
                require(english.windowTitle() == "War Thunder Tactical Terminal", "语言切换保存失败")
                english.roster.absorb_line("Falcon (F-16) shot down by Viper (MiG-29)", 30)
                require(english.roster.kill_count("Viper") == 1, "英文被动句击杀解析失败")
                require(english.phone_mirror.start(0), "英文手机页面无法启动")
                connection = http.client.HTTPConnection("127.0.0.1", english.phone_mirror.server.bound_port,
                                                        timeout=5)
                try:
                    connection.request("GET", "/")
                    page = connection.getresponse().read().decode("utf-8")
                finally:
                    connection.close()
                require('<html lang="en">' in page and "<h2>Battle Roster</h2>" in page, "英文手机页面未本地化")
                report["english"] = True
            finally:
                english.close()
                app.processEvents()
        report["data_cleanup"] = True
    except Exception as error:
        report.update(ok=False, error=str(error))
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if report["ok"] else 1
