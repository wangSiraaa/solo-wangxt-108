"""合成烘焙数据生成器。

明确不连接真实烘焙机：用物理上说得通的简化模型生成豆温/环境温度曲线，
并主动注入：
  1. 非均匀采样间隔（基础 1s + 抖动，偶发较长间隔）；
  2. 高斯测量噪声（豆温、环境温度独立）；
  3. 探针短暂失联：删去若干采样槽（含一段较长缺口，用于验证插值边界策略）。

生成的"真值事件时间"与采样值独立，仅用于播种 auto 事件，仍允许人工修正。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

# 热事件 kind（不含操作事件 damper/gas）
THERMAL_KINDS = ("charge", "turnaround", "yellow", "first_crack", "drop")


@dataclass
class DamperStep:
    t_s: float
    value: float  # 0-100


@dataclass
class GasStep:
    t_s: float
    value: float  # 0-100


@dataclass
class RoastProfile:
    """一条确定性可复现的合成配方（给定 seed 可完全重现）。"""

    name: str
    variety: str
    charge_g: float
    total_s: float
    # 环境温度分段设定值（炉温目标），随燃气/风门调节缓慢响应
    env_setpoints: list[tuple[float, float]]  # (t_s, setpoint_c)
    gas: list[GasStep] = field(default_factory=list)
    dampers: list[DamperStep] = field(default_factory=list)
    # 真值事件时间（秒）
    true_events: dict[str, float] = field(default_factory=dict)
    # 噪声与缺测参数
    bean_noise_sd: float = 0.8
    env_noise_sd: float = 1.2
    # (起始, 结束) 的探针失联区间；结束后探针恢复
    outage_windows: list[tuple[float, float]] = field(default_factory=list)
    jitter_sd: float = 0.35
    long_gap_every: int | None = None  # 每隔 N 个槽制造一次 4-7s 间隔


def _step_value(steps: list[tuple[float, float]] | list, t: float) -> float:
    val = steps[0][1] if not hasattr(steps[0], "t_s") else steps[0].value
    for s in steps:
        st = s[0] if isinstance(s, tuple) else s.t_s
        sv = s[1] if isinstance(s, tuple) else s.value
        if t + 1e-9 >= st:
            val = sv
    return val


def build_profiles() -> list[RoastProfile]:
    """两条可对比的批次：B 段燃气/风门策略不同，但都为合成数据。"""
    p1 = RoastProfile(
        name="B2026-0914-A",
        variety="Ethiopia Yirgacheffe",
        charge_g=1200.0,
        total_s=720.0,
        # 环境温度分段线性目标 (t_s, ℃)
        env_setpoints=[(0, 188), (300, 206), (575, 224), (700, 232)],
        gas=[GasStep(0, 85), GasStep(300, 70), GasStep(560, 55)],
        dampers=[
            DamperStep(0, 30),
            DamperStep(300, 55),   # 一爆前加大风门
            DamperStep(600, 80),
        ],
        true_events={
            "charge": 0.0,
            "turnaround": 82.0,
            "yellow": 300.0,
            "first_crack": 575.0,
            "drop": 700.0,
        },
        outage_windows=[(140, 154)],   # 14s 缺口：在插值允许范围内
    )
    p2 = RoastProfile(
        name="B2026-0914-B",
        variety="Ethiopia Yirgacheffe",
        charge_g=1180.0,
        total_s=700.0,
        env_setpoints=[(0, 186), (260, 205), (540, 223), (680, 231)],
        gas=[GasStep(0, 80), GasStep(260, 75), GasStep(540, 60)],
        dampers=[
            DamperStep(0, 25),
            DamperStep(260, 40),   # 更早、更温和的风门变化
            DamperStep(560, 70),
        ],
        true_events={
            "charge": 0.0,
            "turnaround": 86.0,
            "yellow": 306.0,
            "first_crack": 560.0,
            "drop": 680.0,
        },
        outage_windows=[(200, 232)],  # 32s 缺口：超过插值上限，必须只标注不插值
        bean_noise_sd=1.1,
    )
    return [p1, p2]


def _simulate_curve(profile: RoastProfile) -> tuple[np.ndarray, np.ndarray]:
    """返回等间隔 1s 网格上的（豆温真值, 环境温度真值）。

    简化集总升温模型，仅用于生成视觉合理的合成曲线，不代表真实烘焙物理：
      环境温度设定值 env_setpoints 为分段线性折线 (t_s, 目标炉温℃)；
      燃气/风门在折线基础上施加小幅乘性扰动（让操作事件在曲线上可见，但
      本系统不据此宣称因果）；环境温度一阶跟踪设定值；
      豆温 dT_bean/dt = (T_env-T_bean)/tau_bean - latent(t)，
      一爆前脱水/反应吸热只压低温升率，不使豆温掉头。
    """
    n = int(profile.total_s) + 1
    t = np.arange(n, dtype=float)
    env = np.empty(n)
    bean = np.empty(n)

    env[0] = float(profile.env_setpoints[0][1])
    bean[0] = 22.0
    tau_env = 25.0
    tau_bean = 24.0
    fc = profile.true_events["first_crack"]

    pts_t = np.array([p[0] for p in profile.env_setpoints], dtype=float)
    pts_v = np.array([p[1] for p in profile.env_setpoints], dtype=float)

    for i in range(1, n):
        ts = float(t[i])
        target = float(np.interp(ts, pts_t, pts_v))
        gas_frac = _step_value(profile.gas, ts) / 100.0
        damper_frac = _step_value(profile.dampers, ts) / 100.0
        # 小幅操作扰动：风门加大瞬间拉低炉温（约 3℃），燃气影响很弱；
        # 主体走势仍由折线目标决定，避免操作与温度的伪因果被读成结论
        eff_target = target + 3.0 * gas_frac - 6.0 * damper_frac
        env[i] = env[i - 1] + (eff_target - env[i - 1]) / tau_env
        latent = 3.0 * math.exp(-0.5 * ((ts - (fc - 35)) / 30.0) ** 2)
        bean[i] = bean[i - 1] + (env[i - 1] - bean[i - 1]) / tau_bean - latent / 60.0

    return bean, env


def generate_samples(profile: RoastProfile, seed: int = 7) -> list[dict]:
    """生成非均匀采样、含噪声与缺测的采样记录列表。

    返回 dict 列表：{t_s, bean_temp_c(None=缺测), env_temp_c(None=缺测), is_missing}
    """
    rng = np.random.default_rng(seed)
    bean_true, env_true = _simulate_curve(profile)

    # 1) 非均匀时间戳：从 0 开始累加抖动间隔，偶发长间隔
    times: list[float] = [0.0]
    idx = 0
    while True:
        dt = max(0.4, rng.normal(1.0, profile.jitter_sd))
        if profile.long_gap_every and idx > 0 and idx % profile.long_gap_every == 0:
            dt += rng.uniform(3.0, 6.0)
        nxt = times[-1] + dt
        if nxt > profile.total_s:
            break
        times.append(round(nxt, 2))
        idx += 1
    t_arr = np.array(times)

    # 2) 在非均匀时刻取真值（网格线性插值，这是生成器内部取数，不展示给用户）
    grid = np.arange(len(bean_true), dtype=float)
    bean_at = np.interp(t_arr, grid, bean_true)
    env_at = np.interp(t_arr, grid, env_true)

    # 3) 注入独立高斯噪声
    bean_obs = bean_at + rng.normal(0.0, profile.bean_noise_sd, size=t_arr.size)
    env_obs = env_at + rng.normal(0.0, profile.env_noise_sd, size=t_arr.size)

    # 4) 探针失联窗口：整个窗口内的采样槽标记为缺测（温度置 None）
    missing = np.zeros(t_arr.size, dtype=bool)
    for lo, hi in profile.outage_windows:
        missing |= (t_arr >= lo) & (t_arr <= hi)

    rows: list[dict] = []
    for i, ts in enumerate(t_arr):
        if missing[i]:
            rows.append(
                {"t_s": float(ts), "bean_temp_c": None, "env_temp_c": None, "is_missing": True}
            )
        else:
            rows.append(
                {
                    "t_s": float(ts),
                    "bean_temp_c": round(float(bean_obs[i]), 2),
                    "env_temp_c": round(float(env_obs[i]), 2),
                    "is_missing": False,
                }
            )
    return rows
