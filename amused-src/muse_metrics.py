"""
Shared Muse S Athena signal processing — used by local server and cloud report/decoder.
"""

from __future__ import annotations

import numpy as np

PPG_SFREQ = 64.0
IMU_SFREQ = 52.0
HR_MIN_BPM = 40
HR_MAX_BPM = 180
OPTICS_BASELINE_SEC = 10
OPTICS_BASELINE_SAMPLES = int(PPG_SFREQ * OPTICS_BASELINE_SEC)

OPTICS_CHANNELS_8 = [
    "LO_NIR", "RO_NIR", "LO_IR", "RO_IR",
    "LI_NIR", "RI_NIR", "LI_IR", "RI_IR",
]
OPTICS_CHANNELS_4 = OPTICS_CHANNELS_8[:4]
PPG_HR_CHANNEL = "LO_NIR"


def analyze_ppg(ppg_sig: np.ndarray, sfreq: float = PPG_SFREQ):
    """Heart rate + HRV from LO_NIR PPG window."""
    if len(ppg_sig) < int(sfreq * 3):
        return None
    try:
        from scipy.signal import butter, filtfilt, find_peaks

        sig = np.asarray(ppg_sig, dtype=float)
        sig = sig - np.mean(sig)
        nyq = sfreq / 2.0
        b, a = butter(2, [0.7 / nyq, 4.0 / nyq], btype="band")
        filtered = filtfilt(b, a, sig)
        std = np.std(filtered)
        if std < 1e-9:
            return None
        min_dist = int(sfreq * 0.4)
        peaks, _ = find_peaks(filtered, distance=min_dist, prominence=0.15 * std)
        if len(peaks) < 3:
            return None
        ibi_ms = np.diff(peaks) / sfreq * 1000.0
        valid = ibi_ms[(ibi_ms >= 400) & (ibi_ms <= 2000)]
        if len(valid) < 2:
            return None
        hr = 60000.0 / np.median(valid)
        if not (HR_MIN_BPM <= hr <= HR_MAX_BPM):
            return None
        sdnn = float(np.std(valid))
        rmssd = float(np.sqrt(np.mean(np.diff(valid) ** 2))) if len(valid) >= 2 else 0.0
        pnn50 = float(np.sum(np.abs(np.diff(valid)) > 50) / max(len(valid) - 1, 1) * 100)
        return {"hr": hr, "sdnn": sdnn, "rmssd": rmssd, "pnn50": pnn50}
    except Exception:
        return None


def compute_hr_chunk(ppg, sfreq: float = PPG_SFREQ):
    """Single HR value from PPG chunk (report generator compatibility)."""
    result = analyze_ppg(np.asarray(ppg, dtype=float), sfreq)
    if result:
        return result["hr"]
    return None


def _delta_od(current: float, baseline: float) -> float:
    if baseline > 0 and current > 0:
        return float(-np.log10(current / baseline))
    return 0.0


def analyze_fnirs(lo_nir: np.ndarray, lo_ir: np.ndarray, baseline: dict,
                  sfreq: float = PPG_SFREQ):
    """fNIRS prototype: ΔHbO₂/ΔHbR + TSI from outer LO_NIR + LO_IR."""
    win = int(sfreq * 5)
    if len(lo_nir) < win or len(lo_ir) < win:
        return None
    b_nir = baseline.get("LO_NIR")
    b_ir = baseline.get("LO_IR")
    if not b_nir or not b_ir:
        return None
    cur_nir = float(np.median(lo_nir[-win:]))
    cur_ir = float(np.median(lo_ir[-win:]))
    od_nir = _delta_od(cur_nir, b_nir)
    od_ir = _delta_od(cur_ir, b_ir)
    E = np.array([[1.05, 0.78], [1.10, 0.95]])
    dpf, sds = 6.0, 3.0
    try:
        delta = np.linalg.lstsq(E, np.array([od_nir, od_ir]) / (dpf * sds), rcond=None)[0]
        d_hbo2, d_hbr = float(delta[0]), float(delta[1])
        hbo2 = 50.0 + d_hbo2
        hbr = 25.0 + d_hbr
        hbt = hbo2 + hbr
        tsi = float(100.0 * hbo2 / hbt) if hbt > 0 else None
        return {"d_hbo2": d_hbo2, "d_hbr": d_hbr, "tsi": tsi, "hbo2": hbo2, "hbr": hbr}
    except Exception:
        return None


def estimate_spo2(li_nir: np.ndarray, li_ir: np.ndarray, sfreq: float = PPG_SFREQ):
    """SpO₂ prototype from inner LI_NIR + LI_IR (uncalibrated)."""
    if len(li_nir) < int(sfreq * 3) or len(li_ir) < int(sfreq * 3):
        return None
    try:
        from scipy.signal import butter, filtfilt

        def ac_dc(sig):
            sig = np.asarray(sig, dtype=float)
            dc = float(np.mean(sig))
            if dc <= 0:
                return None
            nyq = sfreq / 2.0
            b, a = butter(2, [0.5 / nyq, 4.0 / nyq], btype="band")
            ac = float(np.std(filtfilt(b, a, sig - dc)))
            return ac, dc

        nir = ac_dc(li_nir)
        ir = ac_dc(li_ir)
        if not nir or not ir:
            return None
        R = (nir[0] / nir[1]) / (ir[0] / ir[1])
        spo2 = float(np.clip(104.0 - 17.0 * R, 70.0, 100.0))
        return {"spo2": spo2, "r_value": float(R)}
    except Exception:
        return None


def analyze_motion(acc_xyz: np.ndarray, gyro_xyz: np.ndarray | None = None):
    """Head orientation + movement level from ACC/GYRO."""
    if acc_xyz is None or len(acc_xyz) < 5:
        return None
    am = np.mean(acc_xyz, axis=0)
    pitch = float(np.degrees(np.arctan2(-am[0], np.sqrt(am[1] ** 2 + am[2] ** 2))))
    roll = float(np.degrees(np.arctan2(am[1], am[2])))
    mag = np.linalg.norm(acc_xyz, axis=1)
    acc_std = float(np.std(mag))
    gyro_rms = 0.0
    if gyro_xyz is not None and len(gyro_xyz) >= 3:
        gyro_rms = float(np.mean(np.linalg.norm(gyro_xyz, axis=1)))
    if acc_std > 0.06 or gyro_rms > 25:
        status = "Moving"
    elif acc_std > 0.025 or gyro_rms > 10:
        status = "Slight motion"
    else:
        status = "Still"
    return {
        "status": status, "pitch": pitch, "roll": roll,
        "acc_std": acc_std, "gyro_rms": gyro_rms,
    }


def ppg_channel_series(ppg_dict: dict, channel: str = PPG_HR_CHANNEL):
    """Last sample from named optics channel, or first available."""
    if channel in ppg_dict and ppg_dict[channel]:
        return ppg_dict[channel][-1]
    if ppg_dict:
        return list(ppg_dict.values())[0][-1]
    return None
