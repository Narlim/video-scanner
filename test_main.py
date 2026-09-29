#!/usr/bin/env python3
"""main.py 的单元测试：逐个测试其中的功能。

- 纯函数：_parse_rate / is_60hz / iter_videos
- 外部调用：probe_frame_rate（用假 subprocess 测解析与分支，另有一条
  真实 ffprobe 的集成测试守住命令行契约）
- 端到端：main（退出码、分类标记、汇总、坏文件跳过）
"""

from __future__ import annotations

import builtins
import json
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path

import pytest

import main

FFPROBE = shutil.which("ffprobe")
FFMPEG = shutil.which("ffmpeg")


# --------------------------------------------------------------------------
# _parse_rate
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("60/1", Fraction(60)),
        ("60000/1001", Fraction(60000, 1001)),
        ("30/1", Fraction(30)),
        ("60", Fraction(60)),          # 无斜杠也接受
        ("0/0", None),                 # ffprobe 表示“未知”
        ("0/1", None),                 # 非正数
        ("-60/1", None),
        ("1/0", None),                 # 除零
        ("N/A", None),
        ("", None),
        (None, None),
        ("abc", None),
    ],
)
def test_parse_rate(value, expected):
    assert main._parse_rate(value) == expected


# --------------------------------------------------------------------------
# is_60hz
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("hz", "expected"),
    [
        (Fraction(60), True),
        (Fraction(60000, 1001), True),   # NTSC 59.94
        (Fraction(119, 2), True),        # 59.5 下边界（闭区间）
        (Fraction(121, 2), True),        # 60.5 上边界（闭区间）
        (Fraction(5949, 100), False),    # 59.49 刚好越界
        (Fraction(6051, 100), False),    # 60.51 刚好越界
        (Fraction(30), False),
        (Fraction(50), False),
        (Fraction(120), False),
        (None, False),
    ],
)
def test_is_60hz(hz, expected):
    assert main.is_60hz(hz) is expected


# --------------------------------------------------------------------------
# iter_videos
# --------------------------------------------------------------------------

def test_iter_videos_filters_extensions_and_sorts(tmp_path):
    for name in ("b.mkv", "a.mp4", "c.MP4"):
        (tmp_path / name).write_bytes(b"")
    (tmp_path / "notes.txt").write_text("忽略我")
    (tmp_path / "noext").write_bytes(b"")

    assert [p.name for p in main.iter_videos(tmp_path)] == ["a.mp4", "b.mkv", "c.MP4"]


def test_iter_videos_recurses_into_subdirectories(tmp_path):
    (tmp_path / "root.mp4").write_bytes(b"")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "one.mp4").write_bytes(b"")
    (tmp_path / "sub" / "deep" / "deeper").mkdir(parents=True)
    (tmp_path / "sub" / "deep" / "deeper" / "two.MKV").write_bytes(b"")
    (tmp_path / "sub" / "notes.txt").write_text("忽略我")
    (tmp_path / "sub" / "empty_dir").mkdir()

    assert [str(p.relative_to(tmp_path)) for p in main.iter_videos(tmp_path)] == [
        "root.mp4",
        "sub/deep/deeper/two.MKV",
        "sub/one.mp4",
    ]


def test_iter_videos_returns_empty_when_no_videos(tmp_path):
    (tmp_path / "a.txt").write_text("x")
    assert main.iter_videos(tmp_path) == []


# --------------------------------------------------------------------------
# probe_frame_rate（假 subprocess）
# --------------------------------------------------------------------------

def _patch_ffprobe(monkeypatch, payload=None, returncode=0, stderr=""):
    """替换 subprocess.run，返回假 CompletedProcess，并把调用参数挂在 fake.cmd 上。"""
    def fake(cmd, **kwargs):
        fake.cmd = cmd
        stdout = json.dumps(payload) if payload is not None else ""
        return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)

    monkeypatch.setattr(main.subprocess, "run", fake)
    return fake


def test_probe_frame_rate_command_contract(monkeypatch, tmp_path):
    video = tmp_path / "v.mp4"
    fake = _patch_ffprobe(monkeypatch, {"streams": [{"avg_frame_rate": "60/1"}]})

    main.probe_frame_rate(video)

    cmd = fake.cmd
    assert cmd[0] == "ffprobe"
    assert cmd[-1] == str(video)                              # 路径必须是最后一个参数
    assert "stream=avg_frame_rate,r_frame_rate" in cmd        # 否则解析不出帧率
    assert cmd[cmd.index("-select_streams") + 1] == "v:0"     # 只看第一个视频流


def test_probe_frame_rate_prefers_avg(monkeypatch):
    _patch_ffprobe(monkeypatch, {"streams": [
        {"avg_frame_rate": "60/1", "r_frame_rate": "30/1"},
    ]})
    assert main.probe_frame_rate(Path("x.mp4")) == Fraction(60)


def test_probe_frame_rate_falls_back_to_r_frame_rate(monkeypatch):
    _patch_ffprobe(monkeypatch, {"streams": [
        {"avg_frame_rate": "0/0", "r_frame_rate": "60000/1001"},
    ]})
    assert main.probe_frame_rate(Path("x.mp4")) == Fraction(60000, 1001)


@pytest.mark.parametrize(
    "payload",
    [
        {"streams": []},          # 没有视频流
        {},                       # 没有 streams 字段
        {"streams": [{}]},        # 字段缺失
        {"streams": [{"avg_frame_rate": "0/0", "r_frame_rate": "0/0"}]},
    ],
)
def test_probe_frame_rate_returns_none(monkeypatch, payload):
    _patch_ffprobe(monkeypatch, payload)
    assert main.probe_frame_rate(Path("x.mp4")) is None


def test_probe_frame_rate_raises_on_ffprobe_failure(monkeypatch):
    _patch_ffprobe(monkeypatch, returncode=1, stderr="moov atom not found")
    with pytest.raises(RuntimeError, match="moov atom not found"):
        main.probe_frame_rate(Path("x.mp4"))


def test_probe_frame_rate_raises_with_exit_code_when_stderr_empty(monkeypatch):
    _patch_ffprobe(monkeypatch, returncode=3, stderr="")
    with pytest.raises(RuntimeError, match="3"):
        main.probe_frame_rate(Path("x.mp4"))


# --------------------------------------------------------------------------
# ask_directory
# --------------------------------------------------------------------------

def _fake_input(monkeypatch, values):
    """按顺序返回给定值的 input() 替身；最后一次提示语存在 fake.prompt 上。"""
    it = iter(values)

    def fake(prompt=""):
        fake.prompt = prompt
        return next(it)

    monkeypatch.setattr(builtins, "input", fake)
    return fake


def test_ask_directory_returns_entered_directory(monkeypatch, tmp_path):
    target = tmp_path / "vids"
    target.mkdir()
    fake = _fake_input(monkeypatch, [str(target)])

    assert main.ask_directory() == target
    assert "请输入要筛选的目标目录" in fake.prompt


@pytest.mark.parametrize("raw", ["", "   ", "\t"])
def test_ask_directory_blank_input_uses_cwd(monkeypatch, tmp_path, raw):
    monkeypatch.chdir(tmp_path)
    _fake_input(monkeypatch, [raw])

    assert main.ask_directory() == tmp_path


def test_ask_directory_strips_surrounding_whitespace(monkeypatch, tmp_path):
    target = tmp_path / "vids"
    target.mkdir()
    _fake_input(monkeypatch, [f"  {target}  "])

    assert main.ask_directory() == target


def test_ask_directory_reprompts_until_valid(monkeypatch, tmp_path, capsys):
    """不存在的路径、以及存在但只是文件的路径，都应重新询问而不是退出。"""
    target = tmp_path / "vids"
    target.mkdir()
    missing = tmp_path / "nope"
    a_file = tmp_path / "a.mp4"
    a_file.write_bytes(b"")
    _fake_input(monkeypatch, [str(missing), str(a_file), str(target)])

    assert main.ask_directory() == target
    assert capsys.readouterr().err.count("目录不存在") == 2


def test_ask_directory_expands_user(monkeypatch, tmp_path):
    home = tmp_path / "home"
    (home / "vids").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    _fake_input(monkeypatch, ["~/vids"])

    assert main.ask_directory() == home / "vids"


@pytest.mark.parametrize("exc", [EOFError, KeyboardInterrupt])
def test_ask_directory_returns_none_on_cancel(monkeypatch, exc):
    def boom(prompt=""):
        raise exc

    monkeypatch.setattr(builtins, "input", boom)

    assert main.ask_directory() is None


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def _make_videos(directory, *names):
    for name in names:
        (directory / name).write_bytes(b"")


def test_main_classifies_and_summarizes(monkeypatch, tmp_path, capsys):
    _make_videos(tmp_path, "a60.mp4", "b5994.mp4", "c30.mp4", "d25.mkv")
    rates = {
        "a60.mp4": Fraction(60),
        "b5994.mp4": Fraction(60000, 1001),
        "c30.mp4": Fraction(30),
        "d25.mkv": Fraction(25),
    }
    monkeypatch.setattr(main, "probe_frame_rate", lambda p: rates[p.name])

    rc = main.main(["main.py", str(tmp_path)])
    out = capsys.readouterr().out

    assert rc == 0
    assert "[60Hz] a60.mp4" in out
    assert "[60Hz] b5994.mp4" in out
    assert "[    ] c30.mp4" in out
    assert "[    ] d25.mkv" in out
    assert "共扫描 4 个视频" in out
    assert "60Hz 的有 2 个" in out
    assert out.rstrip().endswith("b5994.mp4")     # 汇总里只列 60Hz 的


def test_main_skips_unprobeable_file(monkeypatch, tmp_path, capsys):
    _make_videos(tmp_path, "good.mp4", "bad.mp4")

    def probe(path):
        if path.name == "bad.mp4":
            raise RuntimeError("Invalid data found when processing input")
        return Fraction(60)

    monkeypatch.setattr(main, "probe_frame_rate", probe)
    rc = main.main(["main.py", str(tmp_path)])
    captured = capsys.readouterr()

    assert rc == 0                                   # 单个坏文件不中断整体
    assert "[60Hz] good.mp4" in captured.out
    assert "60Hz 的有 1 个" in captured.out
    assert "bad.mp4" in captured.err
    assert "跳过" in captured.err


def test_main_reports_nested_files_with_relative_path(monkeypatch, tmp_path, capsys):
    """不同子目录里的同名文件必须能区分，汇总里也只列相对路径。"""
    (tmp_path / "top.mp4").write_bytes(b"")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "top.mp4").write_bytes(b"")
    rates = {
        str(tmp_path / "top.mp4"): Fraction(60),
        str(tmp_path / "sub" / "top.mp4"): Fraction(30),
    }
    monkeypatch.setattr(main, "probe_frame_rate", lambda p: rates[str(p)])

    assert main.main(["main.py", str(tmp_path)]) == 0
    out = capsys.readouterr().out

    assert "[60Hz] top.mp4" in out
    assert "[    ] sub/top.mp4" in out
    summary = out.split("共扫描", 1)[1]
    assert "  top.mp4" in summary
    assert "sub/top.mp4" not in summary


def test_main_reports_nested_skipped_file_with_relative_path(monkeypatch, tmp_path, capsys):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "bad.mp4").write_bytes(b"")

    def probe(_path):
        raise RuntimeError("moov atom not found")

    monkeypatch.setattr(main, "probe_frame_rate", probe)

    assert main.main(["main.py", str(tmp_path)]) == 0
    assert "sub/bad.mp4" in capsys.readouterr().err


def test_main_missing_ffprobe_exits_2(monkeypatch, tmp_path, capsys):
    _make_videos(tmp_path, "a.mp4")

    def probe(_path):
        raise FileNotFoundError("ffprobe")

    monkeypatch.setattr(main, "probe_frame_rate", probe)

    assert main.main(["main.py", str(tmp_path)]) == 2
    assert "ffprobe" in capsys.readouterr().err


def test_main_exits_2_on_non_directory(tmp_path, capsys):
    target = tmp_path / "nope.mp4"
    target.write_bytes(b"")

    assert main.main(["main.py", str(target)]) == 2
    assert "不是目录" in capsys.readouterr().err


def test_main_exits_1_when_directory_has_no_videos(tmp_path, capsys):
    (tmp_path / "a.txt").write_text("x")

    assert main.main(["main.py", str(tmp_path)]) == 1
    assert "没有找到视频文件" in capsys.readouterr().out


def test_main_prompts_when_no_argument(monkeypatch, tmp_path, capsys):
    target = tmp_path / "vids"
    target.mkdir()
    _make_videos(target, "a.mp4")
    fake = _fake_input(monkeypatch, [str(target)])
    monkeypatch.setattr(main, "probe_frame_rate", lambda _p: Fraction(60))

    assert main.main(["main.py"]) == 0
    assert "请输入要筛选的目标目录" in fake.prompt
    assert "[60Hz] a.mp4" in capsys.readouterr().out


def test_main_blank_prompt_scans_cwd(monkeypatch, tmp_path, capsys):
    _make_videos(tmp_path, "a.mp4")
    monkeypatch.chdir(tmp_path)
    _fake_input(monkeypatch, [""])
    monkeypatch.setattr(main, "probe_frame_rate", lambda _p: Fraction(60))

    assert main.main(["main.py"]) == 0
    assert "[60Hz] a.mp4" in capsys.readouterr().out


def test_main_argument_skips_prompt(monkeypatch, tmp_path, capsys):
    _make_videos(tmp_path, "a.mp4")
    monkeypatch.setattr(main, "probe_frame_rate", lambda _p: Fraction(60))

    def boom(prompt=""):
        raise AssertionError("给了命令行参数就不该再提示输入")

    monkeypatch.setattr(builtins, "input", boom)

    assert main.main(["main.py", str(tmp_path)]) == 0
    assert "[60Hz] a.mp4" in capsys.readouterr().out


def test_main_expands_user_in_argument(monkeypatch, tmp_path, capsys):
    home = tmp_path / "home"
    target = home / "vids"
    target.mkdir(parents=True)
    _make_videos(target, "a.mp4")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(main, "probe_frame_rate", lambda _p: Fraction(60))

    assert main.main(["main.py", "~/vids"]) == 0
    assert "[60Hz] a.mp4" in capsys.readouterr().out


def test_main_cancelled_prompt_exits_130(monkeypatch, capsys):
    def boom(prompt=""):
        raise KeyboardInterrupt

    monkeypatch.setattr(builtins, "input", boom)

    assert main.main(["main.py"]) == 130
    assert "已取消" in capsys.readouterr().err


# --------------------------------------------------------------------------
# 集成：真实 ffprobe
# --------------------------------------------------------------------------

@pytest.mark.skipif(not (FFPROBE and FFMPEG), reason="需要 ffprobe/ffmpeg")
@pytest.mark.parametrize(
    ("rate", "expected_60hz"),
    [("60", True), ("60000/1001", True), ("30", False)],
)
def test_real_ffprobe_roundtrip(tmp_path, rate, expected_60hz):
    """守住命令行契约：假 subprocess 测不到 ffprobe 参数本身写错的情况。"""
    video = tmp_path / "clip.mp4"
    for encoder in ("libx264", "mpeg4"):
        proc = subprocess.run(
            ["ffmpeg", "-v", "error", "-f", "lavfi",
             "-i", f"testsrc=size=64x64:rate={rate}",
             "-t", "0.5", "-pix_fmt", "yuv420p", "-c:v", encoder,
             str(video), "-y"],
            capture_output=True, text=True,
        )
        if proc.returncode == 0:
            break
    else:
        pytest.skip("ffmpeg 无法生成测试视频")

    hz = main.probe_frame_rate(video)
    assert hz is not None, "ffprobe 参数写错会让帧率解析不出来"
    assert main.is_60hz(hz) is expected_60hz
