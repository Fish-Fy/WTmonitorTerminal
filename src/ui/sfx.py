"""自定义告警音效(用户 2026-09-29 明确批准): 播放 config.yaml → alerts.sound_file 指向的音频。

边界(红线⑤的修订口径, 仍是红线):
  * 只有**威胁告警**(附近队友连续阵亡 / 同一人连续击杀绕后)走这里;
    低油 / 临界迎角 / 我被击落依旧只有状态栏文字, 永远不发声。
  * 全项目发声 API(QSoundEffect 等)**只许出现在本文件**(tests/test_sfx.py 钉住)。
  * 路径为空 / 文件不存在 / 无声卡 = 静音, 绝不报错、绝不崩。
  * QSoundEffect 原生只支持 WAV; 配了 mp3 之类放不出来时静默, 文字与悬浮窗告警不受影响。
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, QUrl


def _sound_effect_cls():
    """**延迟**导入 QtMultimedia（用户 2026-10-12 报"双击 exe 到窗口出现变慢"）。

    顶层 import 会让每次启动都加载 Qt6Multimedia.dll / QtMultimedia.pyd / Qt6Network.dll
    以及多媒体插件目录 —— 即使 `alerts.sound_file` 是空的、压根不发声。
    实测取证: 修前启动后的进程模块表里确实能看到上面那几个 DLL。
    现在只有真的要播一个存在的音频文件时才把它拉进来。
    """
    from PySide6.QtMultimedia import QSoundEffect
    return QSoundEffect


class Sfx:
    """一个可热换路径的告警音播放器。不发声的情况一律静默降级。"""

    def __init__(self, path: str = "", volume: float = 1.0, parent: QObject | None = None):
        self._parent = parent
        self._volume = max(0.0, min(1.0, float(volume)))
        self._effect: QSoundEffect | None = None
        self._path = ""
        self.set_path(path)

    # ------------------------------------------------------------------ 状态
    @property
    def ready(self) -> bool:
        """当前是否已装载一个可播的文件(测试用, 也供面板显示"音效未配置")。"""
        return self._effect is not None

    @property
    def path(self) -> str:
        return self._path

    # ------------------------------------------------------------------ 配置
    def set_path(self, path: str) -> None:
        """换音频文件; 空串/不存在 = 立刻回到静音。"""
        path = (path or "").strip()
        self._path = path
        old = self._effect
        self._effect = None
        if old is not None:
            # 先松手再换: QSoundEffect 会一直握着源文件的句柄, Windows 上不先清 source
            # 就删不掉/覆盖不了旧文件(测试里表现为 PermissionError winerror 32,
            # 用户侧表现为"换了音效以后旧文件被锁住、内存里还留着上一个播放器")
            try:
                old.stop()
                old.setSource(QUrl())
            except Exception:
                pass                                # 释放失败也不影响换新文件
        if not path:
            return
        try:
            p = Path(path)
            if not p.is_file():
                return
            eff = _sound_effect_cls()(self._parent)     # 用到才导入 QtMultimedia
            eff.setSource(QUrl.fromLocalFile(str(p.resolve())))
            eff.setVolume(self._volume)
            self._effect = eff
        except Exception:
            self._effect = None                 # 音频后端缺失等任何意外: 静音降级

    # ------------------------------------------------------------------ 播放
    def play(self) -> None:
        """触发一次告警音。正在播放则重头再播; 未就绪时什么都不做。"""
        eff = self._effect
        if eff is None:
            return
        try:
            if eff.isPlaying():
                eff.stop()
            eff.play()
        except Exception:
            pass                                # 播放失败不值得打断对局
