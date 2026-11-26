"""地图和战场事件的显示设置。

字段名沿用历史, 但 F 之后语义变了, 按实际作用记录:
  own_deaths       事件面板: 我被打; 并决定"我被击落"要不要在地图上画红环(唯一位置可证实的环)
  player_trail     地图: 我自己的轨迹线
  teammate_trails  地图: 队友轨迹线
  teammate_deaths  地图: HUD 确认且可定位的相邻队友阵亡合成计数红叉，标记定时消失
  teammate_heading 地图: 队友载具图标外的方向环
  guard_circle     地图: 以我为中心的警戒圈(威胁告警"附近"半径的可视化, 可选)
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class DisplaySettings:
    player_trail: bool = True          # 玩家自身轨迹
    teammate_trails: bool = False      # 队友轨迹
    teammate_deaths: bool = False      # 队友死亡标记
    teammate_heading: bool = False     # 队友方向环
    own_deaths: bool = True            # 我被击落: 地图红环 + 面板条目
    follow_player: bool = False        # 跟随玩家
    trail_jump_thresh: float = 0.08    # 归一化坐标跳变阈值, 超过视为重生
    trail_max: int = 1500              # 单条轨迹最大点数
    marker_scale: float = 1.0          # 标点图标缩放
    guard_circle: bool = True          # 警戒圈(以我为中心, 半径=威胁告警的附近判定半径)
