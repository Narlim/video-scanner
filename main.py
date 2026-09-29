#!/usr/bin/env python3
"""递归扫描一个目录及其子目录下的视频文件，找出其中刷新率为 60 Hz 的视频。

视频容器没有独立的“刷新率”字段，实际对应的就是视频流的帧率 (fps)，
所以 60 Hz == 60 fps，并把 NTSC 的 59.94 fps 也算作 60 Hz。

用法:
    uv run main.py            # 交互式：在终端提示输入要筛选的目标目录
    uv run main.py [目录]      # 跳过提示，直接筛选指定目录
"""

from __future__ import annotations

import json
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

VIDEO_EXTENSIONS = frozenset({
    ".mp4", ".m4v", ".mkv", ".mov", ".avi", ".webm", ".flv",
    ".wmv", ".mpg", ".mpeg", ".ts", ".m2ts", ".mts", ".3gp", ".ogv",
})

TARGET_HZ = 60.0
TOLERANCE_HZ = 0.5          # 59.94 (NTSC) 与 60 都算 60 Hz
FFPROBE_TIMEOUT_S = 30


def _parse_rate(value: str | None) -> Fraction | None:
    """把 '60000/1001'、'60/1'、'0/0' 解析成分数；无效值返回 None。"""
    if not value or value in {"0/0", "N/A"}:
        return None
    try:
        rate = Fraction(value)
    except (ValueError, ZeroDivisionError):
        return None
    return rate if rate > 0 else None


def probe_frame_rate(path: Path) -> Fraction | None:
    """用 ffprobe 读取第一个视频流的帧率。

    优先用 avg_frame_rate（变帧率视频下更接近真实值），
    当它为 0/0 时退回到 r_frame_rate。
    """
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=avg_frame_rate,r_frame_rate",
        "-of", "json",
        str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=FFPROBE_TIMEOUT_S)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"ffprobe 退出码 {proc.returncode}")

    streams = json.loads(proc.stdout or "{}").get("streams") or []
    if not streams:
        return None
    stream = streams[0]
    return _parse_rate(stream.get("avg_frame_rate")) or _parse_rate(stream.get("r_frame_rate"))


def is_60hz(hz: Fraction | None) -> bool:
    return hz is not None and abs(float(hz) - TARGET_HZ) <= TOLERANCE_HZ


def iter_videos(directory: Path) -> list[Path]:
    """递归收集目录及其所有子目录里的视频文件，按路径排序。"""
    return sorted(
        p for p in directory.rglob("*")
        if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS
    )


def ask_directory() -> Path | None:
    """在终端提示用户输入要筛选的目标目录。

    回车 → 当前目录；输入不存在的路径 → 提示重试，不退出；
    Ctrl+C / Ctrl+D → 返回 None，表示用户放弃。
    """
    while True:
        try:
            raw = input(f"请输入要筛选的目标目录（回车使用当前目录 {Path.cwd()}）：").strip()
        except (EOFError, KeyboardInterrupt):
            print(file=sys.stderr)
            return None

        if not raw:
            return Path.cwd()

        candidate = Path(raw).expanduser()
        if candidate.is_dir():
            return candidate
        print(f"目录不存在：{candidate}，请重新输入。", file=sys.stderr)


def main(argv: list[str]) -> int:
    if len(argv) > 1:
        directory = Path(argv[1]).expanduser()
    else:
        directory = ask_directory()
        if directory is None:
            print("已取消，未进行筛选。", file=sys.stderr)
            return 130

    if not directory.is_dir():
        print(f"错误：{directory} 不是目录", file=sys.stderr)
        return 2

    videos = iter_videos(directory)
    if not videos:
        print(f"{directory} 下没有找到视频文件")
        return 1

    hits: list[Path] = []
    for path in videos:
        rel = path.relative_to(directory)     # 子目录里的文件要能区分开
        try:
            hz = probe_frame_rate(path)
        except FileNotFoundError:
            print("错误：找不到 ffprobe，请先安装 ffmpeg", file=sys.stderr)
            return 2
        except (RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
            print(f"[跳过] {rel}: {exc}", file=sys.stderr)
            continue

        label = f"{float(hz):.3f} Hz" if hz is not None else "未知"
        if is_60hz(hz):
            hits.append(path)
            print(f"[60Hz] {rel}  ({label})")
        else:
            print(f"[    ] {rel}  ({label})")

    print(f"\n共扫描 {len(videos)} 个视频，其中 60Hz 的有 {len(hits)} 个：")
    for path in hits:
        print(f"  {path.relative_to(directory)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
