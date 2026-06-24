"""
Band Power 计算模块 — 基于 Welch PSD 的 EEG 频段功率提取.

支持频段:
    Delta  (0.5–4 Hz)   — 深度睡眠、无意识
    Theta  (4–8 Hz)     — 冥想、浅睡、创造力
    Alpha  (8–12 Hz)    — 放松、闭眼静息
    Beta   (12–30 Hz)   — 活跃思考、专注、警觉
    Gamma  (30–45 Hz)   — 高级认知、信息整合

Reference: scipy.signal.welch for PSD estimation.
"""

from __future__ import annotations

import numpy as np
from scipy.signal import welch

# ---------------------------------------------------------------------------
# Band definitions
# ---------------------------------------------------------------------------

BANDS: dict[str, tuple[float, float]] = {
    "delta": (0.5, 4.0),
    "theta": (4.0, 8.0),
    "alpha": (8.0, 12.0),
    "beta": (12.0, 30.0),
    "gamma": (30.0, 45.0),
}

BAND_COLORS: dict[str, str] = {
    "delta": "#1f77b4",
    "theta": "#ff7f0e",
    "alpha": "#2ca02c",
    "beta": "#d62728",
    "gamma": "#9467bd",
}

CHANNEL_NAMES: list[str] = ["TP9", "AF7", "AF8", "TP10"]


def compute_band_power(
    data: np.ndarray,
    sfreq: float,
    bands: dict[str, tuple[float, float]] | None = None,
    nperseg: int | None = None,
) -> dict[str, dict[str, np.ndarray]]:
    """对多通道 EEG 数据计算各频段功率.

    Parameters
    ----------
    data : np.ndarray
        形状 (n_samples, n_channels) 的 EEG 数据.
    sfreq : float
        采样率 (Hz).  Muse S EEG 为 256 Hz.
    bands : dict | None
        频段定义 {name: (low, high)}.  默认使用 BANDS.
    nperseg : int | None
        Welch 方法每段点数.  默认取 sfreq（1 秒窗口）.

    Returns
    -------
    dict[str, dict[str, np.ndarray]]
        {
            "<band>": {
                "abs": np.ndarray shape (n_channels,)  绝对功率 (μV²),
                "rel": np.ndarray shape (n_channels,)  相对功率 (占总功率比例),
            },
            ...
        }
    """
    if bands is None:
        bands = BANDS

    if nperseg is None:
        nperseg = int(sfreq)  # 1-second windows

    # Clamp nperseg to available data length
    n_samples = data.shape[0]
    if nperseg > n_samples:
        nperseg = max(64, n_samples // 2)

    n_channels = data.shape[1]

    # Welch PSD → (freqs, psd) where psd shape = (nperseg//2+1, n_channels)
    freqs, psd = welch(
        data,
        fs=sfreq,
        nperseg=nperseg,
        axis=0,
        scaling="density",
    )

    # Frequency resolution
    freq_res = freqs[1] - freqs[0]

    # Total power (broadband 0.5–45 Hz)
    total_mask = (freqs >= 0.5) & (freqs <= 45.0)
    total_power = np.trapezoid(psd[total_mask, :], dx=freq_res, axis=0)  # (n_channels,)

    results: dict[str, dict[str, np.ndarray]] = {}

    for band_name, (low, high) in bands.items():
        band_mask = (freqs >= low) & (freqs <= high)
        band_psd = psd[band_mask, :]

        # Absolute power — integrate PSD over the band
        abs_power = np.trapezoid(band_psd, dx=freq_res, axis=0)  # (n_channels,)

        # Relative power — proportion of total power
        # Guard against division by zero
        with np.errstate(divide="ignore", invalid="ignore"):
            rel_power = np.where(total_power > 0, abs_power / total_power, 0.0)

        results[band_name] = {"abs": abs_power, "rel": rel_power}

    return results


def band_power_summary(
    bp: dict[str, dict[str, np.ndarray]]
) -> dict[str, dict[str, list[float]]]:
    """将 band power 结果转为 JSON 友好的 Python 原生类型.

    Parameters
    ----------
    bp : dict
        compute_band_power() 的返回值.

    Returns
    -------
    dict
        {band: {"abs": [ch0, ch1, ...], "rel": [ch0, ch1, ...]}}
    """
    return {
        band: {
            "abs": abs_vals.tolist(),
            "rel": rel_vals.tolist(),
        }
        for band, vals in bp.items()
        for abs_vals, rel_vals in [(vals["abs"], vals["rel"])]
    }


# ---------------------------------------------------------------------------
# Quick test (runs when executed directly)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    # Generate synthetic EEG-like data (4 channels, 2 seconds, 256 Hz)
    sfreq = 256.0
    duration = 2.0
    n_samples = int(sfreq * duration)
    t = np.arange(n_samples) / sfreq

    rng = np.random.default_rng(42)
    # Simulate: alpha peak at 10 Hz on all channels + noise
    synth = np.column_stack(
        [
            10.0 * np.sin(2 * np.pi * 10.0 * t)   # strong alpha
            + 5.0 * np.sin(2 * np.pi * 20.0 * t)   # some beta
            + 2.0 * rng.standard_normal(n_samples)  # noise
            for _ in range(4)
        ]
    )

    bp = compute_band_power(synth, sfreq)
    summary = band_power_summary(bp)

    print("Band Power (synthetic, expected: alpha dominant):")
    print(f"{'Band':<8} {'Channel':<8} {'Abs (uV^2)':<12} {'Rel':<8}")
    print("-" * 40)
    for band_name in BANDS:
        for ch_idx, ch_name in enumerate(CHANNEL_NAMES):
            a = summary[band_name]["abs"][ch_idx]
            r = summary[band_name]["rel"][ch_idx]
            print(f"{band_name:<8} {ch_name:<8} {a:<12.4f} {r:<8.4f}")
