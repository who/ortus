"""Check clip color grade (color temperature and saturation) using ffmpeg and OpenCV."""

import os
import subprocess
import tempfile

import cv2
import numpy as np


# sRGB -> XYZ (D65) conversion matrix (IEC 61966-2-1)
_SRGB_TO_XYZ = np.array([
    [0.4124564, 0.3575761, 0.1804375],
    [0.2126729, 0.7151522, 0.0721750],
    [0.0193339, 0.1191920, 0.9503041],
], dtype=np.float64)


def _extract_frames(clip_path: str, tmp_dir: str, n_frames: int = 5) -> list[str]:
    """Extract n evenly spaced frames from clip_path into tmp_dir using ffmpeg.

    Returns a list of absolute paths to the extracted PNG files.
    """
    result = subprocess.run(
        [
            "ffmpeg",
            "-i", clip_path,
            "-vf", f"select='not(mod(n,1))',thumbnail={n_frames}",
            "-vsync", "vfr",
            "-frames:v", str(n_frames),
            "-q:v", "2",
            os.path.join(tmp_dir, "frame_%03d.png"),
            "-y",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg frame extraction failed: {result.stderr.strip()}")
    frames = sorted(
        os.path.join(tmp_dir, f)
        for f in os.listdir(tmp_dir)
        if f.startswith("frame_") and f.endswith(".png")
    )
    if not frames:
        raise RuntimeError("ffmpeg produced no frames")
    return frames


def _rgb_to_cct(r: float, g: float, b: float) -> float:
    """Estimate correlated color temperature from linear-light average RGB values.

    Uses the sRGB->XYZ matrix to derive CIE xy chromaticity, then applies
    McCamy's approximation formula.

    Args:
        r, g, b: Average channel values in [0, 1].

    Returns:
        Estimated CCT in Kelvin.
    """
    rgb = np.array([r, g, b], dtype=np.float64)
    xyz = _SRGB_TO_XYZ @ rgb
    x_sum = xyz[0] + xyz[1] + xyz[2]
    if x_sum < 1e-9:
        return 6500.0  # default to daylight when image is black
    cx = xyz[0] / x_sum
    cy = xyz[1] / x_sum
    n = (cx - 0.3320) / (0.1858 - cy)
    cct = 449.0 * n ** 3 + 3525.0 * n ** 2 + 6823.3 * n + 5520.33
    return float(cct)


def _measure_frame(frame_path: str) -> tuple[float, float]:
    """Measure CCT and mean HSV saturation for a single frame.

    Args:
        frame_path: Path to a PNG frame file.

    Returns:
        Tuple of (cct_kelvin, mean_saturation) where saturation is in [0, 255].

    Raises:
        RuntimeError: If OpenCV cannot read the frame.
    """
    bgr = cv2.imread(frame_path)
    if bgr is None:
        raise RuntimeError(f"OpenCV could not read frame: {frame_path}")

    # --- color temperature ---
    rgb_f = bgr[:, :, ::-1].astype(np.float64) / 255.0
    r_mean = float(rgb_f[:, :, 0].mean())
    g_mean = float(rgb_f[:, :, 1].mean())
    b_mean = float(rgb_f[:, :, 2].mean())
    cct = _rgb_to_cct(r_mean, g_mean, b_mean)

    # --- saturation ---
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    saturation = float(hsv[:, :, 1].mean())

    return cct, saturation


def _measure_clip(clip_path: str) -> tuple[float, float]:
    """Extract frames from clip and return average CCT and average saturation.

    Args:
        clip_path: Path to the video clip.

    Returns:
        Tuple of (avg_cct_kelvin, avg_saturation).

    Raises:
        RuntimeError: If ffmpeg fails or OpenCV cannot process frames.
        FileNotFoundError: If clip_path does not exist.
    """
    if not os.path.exists(clip_path):
        raise FileNotFoundError(f"Clip not found: {clip_path}")

    with tempfile.TemporaryDirectory() as tmp_dir:
        frames = _extract_frames(clip_path, tmp_dir)
        ccts = []
        sats = []
        for frame_path in frames:
            cct, sat = _measure_frame(frame_path)
            ccts.append(cct)
            sats.append(sat)

    return float(np.mean(ccts)), float(np.mean(sats))


def check_color_grade(clip_path: str, criteria: dict) -> dict:
    """Check clip color temperature and saturation against criteria thresholds.

    Args:
        clip_path: Path to the video clip.
        criteria: Dict with optional keys:
            - ``color_temp_range``: [lo, hi] in Kelvin (inclusive).
            - ``max_saturation``: upper bound on mean HSV S channel [0, 255].

    Returns:
        Dict with keys: check, status, measurements, expected, message.
    """
    try:
        avg_cct, avg_sat = _measure_clip(clip_path)
    except (RuntimeError, FileNotFoundError, OSError) as exc:
        return {
            "check": "color_grade",
            "status": "fail",
            "measurements": {"color_temp": None, "saturation": None},
            "expected": criteria,
            "message": f"Could not measure clip: {exc}",
        }

    try:
        color_temp_range = criteria.get("color_temp_range")
        max_saturation = criteria.get("max_saturation")

        expected: dict = {}
        failures: list[str] = []
        passed = True

        if color_temp_range is not None:
            lo, hi = float(color_temp_range[0]), float(color_temp_range[1])
            expected["color_temp_range"] = [lo, hi]
            if not (lo <= avg_cct <= hi):
                passed = False
                failures.append(
                    f"color temp {avg_cct:.0f}K outside [{lo:.0f}, {hi:.0f}]K"
                )

        if max_saturation is not None:
            max_sat = float(max_saturation)
            expected["max_saturation"] = max_sat
            if avg_sat > max_sat:
                passed = False
                failures.append(
                    f"saturation {avg_sat:.1f} exceeds max {max_sat:.1f}"
                )

    except (TypeError, ValueError, IndexError, KeyError) as exc:
        return {
            "check": "color_grade",
            "status": "fail",
            "measurements": {"color_temp": avg_cct, "saturation": avg_sat},
            "expected": criteria,
            "message": f"Invalid criteria format: {exc}",
        }

    if passed:
        message = (
            f"Color grade OK: {avg_cct:.0f}K, saturation {avg_sat:.1f}"
        )
    else:
        message = "Color grade failed: " + "; ".join(failures)

    return {
        "check": "color_grade",
        "status": "pass" if passed else "fail",
        "measurements": {"color_temp": avg_cct, "saturation": avg_sat},
        "expected": expected,
        "message": message,
    }
