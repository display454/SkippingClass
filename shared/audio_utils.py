"""音频处理：探测、转码、切片。

为什么必须预处理：
1. 各家接口要求「格式/采样率/声道」声明与实际一致；
2. base64 直传体积膨胀 1.33 倍，原始高码率音频很容易把请求体顶到几百 MB；
3. 不同厂商的分段上限不同（OpenAI 25 分钟、百炼 10MB、火山 5 小时），
   统一转成 16kHz/单声道/32kbps 后，体积可预测，分段长度就能自动算。

转码后体积约为原始的 1/5 ~ 1/8，语音识别质量几乎无损。
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
from pathlib import Path

_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

TARGET_RATE = 16000
TARGET_BITS = 16
TARGET_CHANNEL = 1
TARGET_BITRATE = "32k"
BYTES_PER_SECOND = 4000      # 32kbps ≈ 4KB/s，用于按体积反推分段长度


class AudioError(RuntimeError):
    pass


def _run(cmd: list[str], timeout: int = 600) -> subprocess.CompletedProcess:
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = _CREATE_NO_WINDOW
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, **kwargs
        )
    except FileNotFoundError as exc:
        raise AudioError(f"找不到可执行文件：{cmd[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise AudioError(f"命令执行超时：{cmd[0]}") from exc


def find_ffmpeg() -> str | None:
    return shutil.which("ffmpeg")


def find_ffprobe() -> str | None:
    probe = shutil.which("ffprobe")
    if probe:
        return probe
    ff = find_ffmpeg()
    if ff:
        candidate = Path(ff).with_name("ffprobe.exe" if os.name == "nt" else "ffprobe")
        if candidate.exists():
            return str(candidate)
    return None


def probe(path: str | Path) -> dict:
    path = Path(path)
    info = {
        "duration": 0.0,
        "size": path.stat().st_size if path.exists() else 0,
        "format_name": path.suffix.lstrip(".").lower(),
    }

    probe_bin = find_ffprobe()
    if probe_bin:
        res = _run([
            probe_bin, "-v", "error",
            "-show_entries", "format=duration,format_name",
            "-show_entries", "stream=sample_rate,channels",
            "-of", "json", str(path),
        ], timeout=60)
        if res.returncode == 0 and res.stdout.strip():
            try:
                data = json.loads(res.stdout)
                fmt = data.get("format", {})
                info["duration"] = float(fmt.get("duration") or 0)
                if fmt.get("format_name"):
                    info["format_name"] = fmt["format_name"]
                streams = data.get("streams") or []
                if streams:
                    info["sample_rate"] = streams[0].get("sample_rate")
                    info["channels"] = streams[0].get("channels")
            except (json.JSONDecodeError, ValueError, TypeError):
                pass

    if info["duration"] <= 0:
        ff = find_ffmpeg()
        if ff:
            res = _run([ff, "-hide_banner", "-i", str(path)], timeout=60)
            m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", res.stderr or "")
            if m:
                h, mi, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
                info["duration"] = h * 3600 + mi * 60 + s

    return info


def plan_segments(duration: float, segment_seconds: int) -> list[tuple[float, float]]:
    if duration <= 0:
        return [(0.0, 0.0)]
    if segment_seconds <= 0:
        return [(0.0, duration)]

    count = max(1, math.ceil(duration / segment_seconds))
    segments: list[tuple[float, float]] = []
    for i in range(count):
        start = i * segment_seconds
        length = min(segment_seconds, duration - start)
        if length <= 0:
            continue
        segments.append((round(start, 3), round(length, 3)))

    if len(segments) >= 2 and segments[-1][1] < min(5, segment_seconds):
        # 编码补齐可能多出不足一秒的尾段，单独转码会产生空文件。
        # 从前段挪几秒给尾段，既不漏内容，也不突破引擎单段时长上限。
        start, previous = segments[-2]
        total = previous + segments[-1][1]
        tail = min(5, segment_seconds)
        segments[-2] = (start, round(total - tail, 3))
        segments[-1] = (round(start + total - tail, 3), tail)

    return segments


def effective_segment_seconds(spec, user_segment_seconds: int) -> int:
    """综合「用户设定 / 厂商时长上限 / 厂商体积上限 / 码率」取最小值。

    体积上限换算成秒：max_bytes ÷ 4KB/s。这样换引擎时不用手动改分段，
    选 OpenAI 会自动压到 25 分钟以内，选百炼会压到 10 分钟左右。
    """
    by_bytes = int(getattr(spec, "max_segment_bytes", 20 * 1024 * 1024) / BYTES_PER_SECOND)
    by_seconds = int(getattr(spec, "max_segment_seconds", 1800))
    chosen = min(int(user_segment_seconds), by_seconds, by_bytes)
    return max(60, chosen)


def extract_segments(
    src: str | Path,
    out_dir: str | Path,
    duration: float,
    segment_seconds: int,
) -> list[dict]:
    """按段转码，返回 [{index, path, offset, length, size}]。"""
    ff = find_ffmpeg()
    if not ff:
        raise AudioError(
            "未检测到 ffmpeg，无法处理音频。"
            "Windows 可用 winget install Gyan.FFmpeg 安装后重启本应用"
        )

    src = Path(src)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    results: list[dict] = []
    for idx, (start, length) in enumerate(plan_segments(duration, segment_seconds)):
        out_path = out_dir / f"seg_{idx:03d}.mp3"
        cmd = [
            ff, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-ss", f"{start:.3f}",
            "-i", str(src),
            "-t", f"{length:.3f}",
            "-vn", "-map", "0:a:0?",
            "-ac", str(TARGET_CHANNEL),
            "-ar", str(TARGET_RATE),
            "-c:a", "libmp3lame",
            "-b:a", TARGET_BITRATE,
            "-f", "mp3",
            str(out_path),
        ]
        res = _run(cmd, timeout=1800)
        if res.returncode != 0 or not out_path.exists() or out_path.stat().st_size < 1024:
            detail = (res.stderr or res.stdout or "").strip().splitlines()
            raise AudioError(
                f"第 {idx + 1} 段转码失败：{detail[-1] if detail else '未知错误'}"
            )

        results.append({
            "index": idx,
            "path": str(out_path),
            "offset": start,
            "length": length,
            "size": out_path.stat().st_size,
        })

    return results


def format_duration(seconds: float) -> str:
    if not seconds or seconds < 0:
        return "--:--"
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def cleanup(paths: list[str | Path]) -> None:
    for p in paths:
        try:
            Path(p).unlink(missing_ok=True)
        except OSError:
            pass
