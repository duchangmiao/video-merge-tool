# -*- coding: utf-8 -*-
"""
VideoMergeQSV —— 多视频合并核心模块（Windows，多显卡自适应：QSV 硬编 / 软件回退）

策略（以第一个视频为参考）：
  1. ffprobe 读取每个视频的视频流/音频流参数；
  2. 参考格式 = 第一个视频的编码、分辨率、帧率、像素格式、位深、Profile + 音频编码/Profile/采样率/声道；
  3. 逐段判定：
       - copy       : 视频与音频全部一致 -> 流拷贝重封装，不重编码（最快）
       - audio_fix  : 视频一致、音频不一致 -> 视频拷贝，音频转 AAC 归一化
       - transcode  : 视频编码/分辨率/帧率/像素格式不一致 -> 优先 QSV 硬编，
                      QSV 不可用或失败时自动回退 CPU 软件编码
  4. 编码参数默认跟随参考格式自动匹配；所有分段归一化后 concat 一次 -c copy 封装输出。

CLI:
  python merge_core.py --out out.mp4 --files a.mp4 b.mp4 c.mp4
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from typing import Callable, List, Optional

def _resource_dirs() -> List[str]:
    """开发态返回脚本目录；PyInstaller 打包态返回 exe 目录与资源解压目录。"""
    here = os.path.dirname(os.path.abspath(__file__))
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        meipass = getattr(sys, "_MEIPASS", exe_dir)
        return [exe_dir, meipass, here]
    return [here]


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FFMPEG_SEARCH_DIRS = [os.path.join(d, "ffmpeg", "bin") for d in _resource_dirs()]
DEFAULT_FFMPEG_DIR = FFMPEG_SEARCH_DIRS[0]

# Windows 下隐藏控制台窗口
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

VALID_PROFILES = ("baseline", "main", "high", "main10")


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def which(name: str, extra_dir: str = "") -> str:
    """在用户指定目录、内置 ffmpeg\\bin 候选目录和 PATH 中查找可执行文件。"""
    exe = name + (".exe" if os.name == "nt" else "")
    candidates = []
    if extra_dir:
        candidates.append(os.path.join(extra_dir, exe))
    candidates += [os.path.join(d, exe) for d in FFMPEG_SEARCH_DIRS]
    found = shutil.which(exe) or ""
    if found:
        candidates.append(found)
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return ""


def quote_arg(a: str) -> str:
    if re.search(r"[\s\"'&|<>]", a):
        return '"' + a.replace('"', '\\"') + '"'
    return a


def run_command(cmd: List[str], log: Callable[[str], None],
                progress_cb: Optional[Callable[[float, str], None]] = None,
                total_duration: float = 0.0,
                cancel_flag: Optional[threading.Event] = None,
                raw_fh=None) -> int:
    """执行命令，实时解析 ffmpeg 的 time= 输出回调进度。返回退出码。

    raw_fh: 可选文件句柄，ffmpeg 完整原始 stderr 全部写入，便于失败排查。
    """
    log("执行: " + " ".join(quote_arg(x) for x in cmd))
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="replace",
        creationflags=_NO_WINDOW,
    )
    time_re = re.compile(r"time=(\d+):(\d+):(\d+\.?\d*)")
    last_frac = 0.0
    assert proc.stderr is not None
    for line in proc.stderr:
        if cancel_flag is not None and cancel_flag.is_set():
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            return -1
        line = line.rstrip()
        if raw_fh is not None:
            try:
                raw_fh.write(line + "\n")
                raw_fh.flush()
            except OSError:
                pass
        if not line.strip():
            continue
        if any(k in line for k in ("error", "Error", "failed", "Failed",
                                    "warning", "Warning", "qsv", "QSV",
                                    "Stream mapping", "Press [q]")):
            log(line.strip())
        m = time_re.search(line)
        if m and progress_cb and total_duration > 0:
            h, mi, s = m.groups()
            cur = int(h) * 3600 + int(mi) * 60 + float(s)
            frac = min(cur / total_duration, 1.0)
            if frac > last_frac:
                last_frac = frac
                progress_cb(frac, line.strip())
    proc.wait()
    return proc.returncode


# ---------------------------------------------------------------------------
# ffprobe 数据结构
# ---------------------------------------------------------------------------

@dataclass
class VideoInfo:
    codec: str = ""
    width: int = 0
    height: int = 0
    fps_num: int = 0
    fps_den: int = 1
    pix_fmt: str = ""
    bit_depth: int = 8
    profile: str = ""

    @property
    def fps(self) -> float:
        return self.fps_num / self.fps_den if self.fps_den else 0.0


@dataclass
class AudioInfo:
    codec: str = ""
    sample_rate: int = 0
    channels: int = 0
    channel_layout: str = ""
    profile: str = ""


@dataclass
class MediaInfo:
    path: str
    duration: float = 0.0
    video: Optional[VideoInfo] = None
    audio: Optional[AudioInfo] = None


def _parse_fps(rate: str) -> tuple:
    if not rate or "/" not in rate:
        try:
            return int(float(rate)), 1
        except (ValueError, TypeError):
            return 0, 1
    n, d = rate.split("/")
    try:
        n, d = int(n), int(d)
    except ValueError:
        return 0, 1
    if d == 0:
        return 0, 1
    return n, d


def ffprobe_media(ffprobe: str, path: str) -> MediaInfo:
    cmd = [ffprobe, "-v", "error", "-show_streams", "-show_format",
           "-print_format", "json", path]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", creationflags=_NO_WINDOW)
    if r.returncode != 0:
        raise RuntimeError(f"ffprobe 解析失败: {path}\n{r.stderr.strip()}")
    data = json.loads(r.stdout)
    info = MediaInfo(path=path)
    try:
        info.duration = float(data.get("format", {}).get("duration", 0) or 0)
    except ValueError:
        info.duration = 0.0

    for s in data.get("streams", []):
        if s.get("codec_type") == "video" and info.video is None:
            v = VideoInfo()
            v.codec = s.get("codec_name", "")
            v.width = int(s.get("width", 0) or 0)
            v.height = int(s.get("height", 0) or 0)
            n, d = _parse_fps(s.get("avg_frame_rate") or s.get("r_frame_rate") or "")
            v.fps_num, v.fps_den = n, d
            v.pix_fmt = s.get("pix_fmt", "") or ""
            depth = s.get("bits_per_raw_sample") or s.get("bits_per_sample") or ""
            try:
                v.bit_depth = int(depth) if depth else 8
            except ValueError:
                v.bit_depth = 8
            v.profile = s.get("profile", "") or ""
            info.video = v
        elif s.get("codec_type") == "audio" and info.audio is None:
            a = AudioInfo()
            a.codec = s.get("codec_name", "")
            a.sample_rate = int(s.get("sample_rate", 0) or 0)
            a.channels = int(s.get("channels", 0) or 0)
            a.channel_layout = s.get("channel_layout", "") or ""
            a.profile = (s.get("profile", "") or "").strip()
            info.audio = a
    return info


# ---------------------------------------------------------------------------
# 编码器 / 默认参数
# ---------------------------------------------------------------------------

QSV_ENCODER_MAP = {
    "h264": "h264_qsv",
    "hevc": "hevc_qsv",
    "h265": "hevc_qsv",
    "av1": "av1_qsv",
    "vp9": "",
}

SW_ENCODER_MAP = {
    "h264": "libx264",
    "hevc": "libx265",
    "h265": "libx265",
    "av1": "libx265",
    "vp9": "libx264",
}

# 不同编码的默认质量值（ICQ/CRF），越小越清晰
DEFAULT_CQ = {"h264": 23, "hevc": 24, "h265": 24, "av1": 28}


def default_cq(codec: str) -> int:
    return DEFAULT_CQ.get(codec, 23)


def default_encoder(codec: str) -> str:
    """按参考编码给出默认 QSV 编码器；无对应硬编时回退 auto（运行时再选软编）。"""
    return QSV_ENCODER_MAP.get(codec, "") or "auto"


def detect_qsv_encoders(ffmpeg: str) -> List[str]:
    """列出 ffmpeg 内置的 QSV 编码器名称。"""
    r = subprocess.run([ffmpeg, "-hide_banner", "-encoders"], capture_output=True,
                       text=True, encoding="utf-8", errors="replace",
                       creationflags=_NO_WINDOW)
    names = []
    for line in r.stdout.splitlines():
        m = re.search(r"(\w+_qsv)\b", line)
        if m:
            names.append(m.group(1))
    return names


def suggested_bitrate(width: int, height: int) -> tuple:
    """按分辨率建议 (目标码率 kbps, 最大码率 kbps)。"""
    pixels = max(width, 1) * max(height, 1)
    if pixels >= 3840 * 2160:
        target = 8000
    elif pixels >= 2560 * 1440:
        target = 6000
    elif pixels >= 1920 * 1080:
        target = 4000
    elif pixels >= 1280 * 720:
        target = 2500
    else:
        target = 1200
    return target, int(round(target * 1.5))


def _parse_kbps(br: str) -> int:
    """把 '4000k' / '4M' / '4000' 解析为 kbps 整数。"""
    if not br:
        return 0
    m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*([kKmM]?)\s*$", str(br))
    if not m:
        return 0
    val = float(m.group(1))
    unit = m.group(2).lower()
    if unit == "m":
        val *= 1000
    return int(round(val))


def _bufsize(target_br: str) -> str:
    k = _parse_kbps(target_br)
    if k <= 0:
        return "0"
    return f"{k * 2}k"


# ---------------------------------------------------------------------------
# 分段计划
# ---------------------------------------------------------------------------

@dataclass
class SegmentPlan:
    index: int
    src: str
    info: MediaInfo
    action: str                 # copy / audio_fix / transcode
    norm_path: str = ""


def fps_close(a: float, b: float, tol: float = 0.15) -> bool:
    if a <= 0 or b <= 0:
        return False
    return abs(a - b) <= tol


def probe_files(files: List[str], ffprobe: str,
                log: Callable[[str], None]) -> tuple:
    """逐个探针解析；不中途中止，返回 (有效 MediaInfo 列表, 损坏文件列表)。

    损坏列表元素为 (路径, 错误信息)。大批量合并时先把全部文件探测一遍，
    便于一次性列出所有坏源文件，而不是处理到中途才失败。
    """
    infos: List[MediaInfo] = []
    bad: List[tuple] = []
    for i, f in enumerate(files):
        try:
            infos.append(ffprobe_media(ffprobe, f))
        except Exception as e:
            reason = str(e).splitlines()[0] if str(e) else "解析失败"
            bad.append((f, reason))
            log(f"  [异常] 第 {i + 1} 个 {os.path.basename(f)}: {reason}")
    return infos, bad


def plan_segments(infos: List[MediaInfo], log: Callable[[str], None],
                  ref_index: int = 0) -> tuple:
    if ref_index < 0 or ref_index >= len(infos):
        ref_index = 0
    ref = infos[ref_index]
    if ref.video is None:
        raise RuntimeError("参考视频未检测到视频流，无法作为参考。")

    log("=" * 60)
    log(f"参考格式（第 {ref_index + 1} 个视频）: {os.path.basename(ref.path)}")
    v = ref.video
    log(f"  视频: {v.codec}  {v.width}x{v.height}  {v.fps:.3f}fps  "
        f"{v.pix_fmt}  {v.bit_depth}bit  profile={v.profile or '自动'}")
    if ref.audio:
        a = ref.audio
        log(f"  音频: {a.codec}  {a.profile or '标准'}  {a.sample_rate}Hz  {a.channels}ch")
    else:
        log("  音频: 无音轨")
    log("=" * 60)

    plans: List[SegmentPlan] = []
    for i, info in enumerate(infos):
        action = "copy"
        rv, ra = ref.video, ref.audio
        sv, sa = info.video, info.audio

        if sv is None:
            raise RuntimeError(f"文件未检测到视频流，无法合并: {info.path}")

        video_ok = (
            sv.codec == rv.codec
            and sv.width == rv.width
            and sv.height == rv.height
            and fps_close(sv.fps, rv.fps)
            and sv.pix_fmt == rv.pix_fmt
            and sv.bit_depth == rv.bit_depth
        )
        if sa is None and ra is None:
            audio_ok = True
        elif sa is None or ra is None:
            audio_ok = False
        else:
            # 编码名/采样率/声道一致；Profile（如 AAC-LC 与 HE-AACv2）不同也需归一化
            profile_ok = (not sa.profile or not ra.profile
                          or sa.profile == ra.profile)
            audio_ok = (sa.codec == ra.codec
                        and profile_ok
                        and sa.sample_rate == ra.sample_rate
                        and sa.channels == ra.channels)

        if not video_ok:
            action = "transcode"
        elif not audio_ok:
            action = "audio_fix"
        else:
            action = "copy"

        tag = {"copy": "直接拷贝", "audio_fix": "视频拷贝/音频归一化",
               "transcode": "转码归一化"}[action]
        log(f"[{i + 1}] {os.path.basename(info.path)} -> {tag}")
        plans.append(SegmentPlan(index=i, src=info.path, info=info, action=action))
    return ref, plans


# ---------------------------------------------------------------------------
# 分段处理
# ---------------------------------------------------------------------------

def _audio_args(ref: MediaInfo) -> List[str]:
    ra = ref.audio
    if ra is None:
        return []
    return ["-c:a", "aac", "-ar", str(ra.sample_rate), "-ac", str(ra.channels)]


def _audio_bitrate(ref: MediaInfo, audio_br: str) -> str:
    """音频码率：auto 时按参考声道数推导，否则用显式值。"""
    if audio_br and str(audio_br).lower() != "auto":
        return audio_br
    ch = ref.audio.channels if ref.audio else 2
    if ch <= 1:
        return "128k"
    if ch == 2:
        return "192k"
    return "320k"


def _silent_audio_input(ref: MediaInfo, duration: float = 0.0) -> List[str]:
    ra = ref.audio
    layout = "stereo" if ra.channels >= 2 else "mono"
    # 时长未知时给一个有限上限（10 小时），避免无限输入挂起；-shortest 仍按视频截断
    dur_sec = duration if duration and duration > 0 else 36000.0
    dur = f":d={dur_sec:.3f}"
    return ["-f", "lavfi", "-i", f"anullsrc=r={ra.sample_rate}:cl={layout}{dur}"]


def _resolve_profile(rv: VideoInfo, profile_pref: str) -> Optional[str]:
    """Profile：显式指定优先；否则跟随参考视频的 Profile。"""
    if profile_pref and profile_pref != "auto":
        return profile_pref if profile_pref in VALID_PROFILES else None
    if rv.profile:
        prof = rv.profile.lower().replace(" ", "")
        if prof in VALID_PROFILES:
            return prof
        if prof == "mainstillpicture":
            return "main"
    return None


def _quality_opts(encoder: str, rc_mode: str, cq: int,
                  target_br: str, max_br: str) -> List[str]:
    """编码码率/质量参数：ICQ 用质量值，VBR 用目标/最大码率。"""
    opts: List[str] = []
    if rc_mode == "vbr" and _parse_kbps(target_br) > 0:
        opts += ["-b:v", target_br]
        if _parse_kbps(max_br) > 0:
            opts += ["-maxrate", max_br]
        opts += ["-bufsize", _bufsize(target_br)]
    else:
        if encoder.endswith("_qsv"):
            opts += ["-global_quality", str(cq)]
        else:
            opts += ["-crf", str(cq)]
    return opts


def transcode_segment(ffmpeg: str, seg: SegmentPlan, ref: MediaInfo,
                      out_path: str, encoder_pref: str, cq: int,
                      preset: str, audio_br: str, rc_mode: str,
                      target_br: str, max_br: str, profile_pref: str,
                      log: Callable[[str], None],
                      progress_cb: Callable[[float, str], None],
                      cancel_flag: threading.Event,
                      raw_fh=None) -> bool:
    """QSV 硬编归一化；QSV 不可用/失败自动回退软件编码。"""
    rv, sv = ref.video, seg.info.video
    assert rv and sv
    src_codec = rv.codec

    if encoder_pref == "auto":
        qsv_candidate = QSV_ENCODER_MAP.get(src_codec, "")
        sw_candidate = SW_ENCODER_MAP.get(src_codec, "libx264")
    else:
        qsv_candidate = encoder_pref if encoder_pref.endswith("_qsv") else ""
        sw_candidate = encoder_pref if not encoder_pref.endswith("_qsv") else "libx264"

    # 软件编码保留参考像素格式；QSV 只接受 nv12/p010
    pix_fmt = rv.pix_fmt if rv.pix_fmt in ("nv12", "p010", "yuv420p", "yuv420p10le") else "nv12"
    if rv.bit_depth == 10 and pix_fmt == "nv12":
        pix_fmt = "p010"
    qsv_pix = "p010" if rv.bit_depth == 10 else "nv12"

    common_video_out = [
        "-r", f"{rv.fps_num}/{rv.fps_den}" if rv.fps_den else f"{rv.fps:.3f}",
        "-s", f"{rv.width}x{rv.height}",
    ]
    prof = _resolve_profile(rv, profile_pref)
    if prof:
        common_video_out += ["-profile:v", prof]

    audio_out = _audio_args(ref) + ["-b:a", _audio_bitrate(ref, audio_br)]
    has_audio = seg.info.audio is not None

    attempts = []
    if qsv_candidate:
        # CPU 解码 -> QSV 编码（显卡硬编）；该路径在核显/虚拟机环境下最稳
        attempts.append(("qsv", [
            "-c:v", qsv_candidate, "-preset", preset, "-pix_fmt", qsv_pix,
        ] + _quality_opts(qsv_candidate, rc_mode, cq, target_br, max_br)
          + common_video_out))
    attempts.append(("software", [
        "-c:v", sw_candidate, "-preset", "medium", "-pix_fmt", pix_fmt,
    ] + _quality_opts(sw_candidate, rc_mode, cq, target_br, max_br)
      + common_video_out))

    for name, v_enc in attempts:
        if cancel_flag.is_set():
            return False
        cmd = [ffmpeg, "-y", "-i", seg.src]
        if has_audio:
            cmd += ["-map", "0:v:0", "-map", "0:a:0"]
            cmd += v_enc + audio_out
        else:
            cmd += _silent_audio_input(ref, seg.info.duration)
            cmd += ["-map", "0:v:0", "-map", "1:a:0", "-shortest"]
            cmd += v_enc + audio_out
        cmd += ["-avoid_negative_ts", "make_zero", out_path]

        log(f"-- 分段 {seg.index + 1} 尝试 [{name}] ...")
        code = run_command(cmd, log, progress_cb=progress_cb,
                           total_duration=seg.info.duration, cancel_flag=cancel_flag,
                           raw_fh=raw_fh)
        if code == 0 and os.path.isfile(out_path) and os.path.getsize(out_path) > 0:
            log(f"-- 分段 {seg.index + 1} [{name}] 完成")
            return True
        log(f"-- 分段 {seg.index + 1} [{name}] 失败(退出码 {code})，尝试下一种方式")
        if os.path.exists(out_path):
            os.remove(out_path)
    return False


def audio_fix_segment(ffmpeg: str, seg: SegmentPlan, ref: MediaInfo,
                      out_path: str, audio_br: str,
                      log: Callable[[str], None],
                      progress_cb: Callable[[float, str], None],
                      cancel_flag: threading.Event,
                      raw_fh=None) -> bool:
    """视频流直接拷贝，仅把音频转成参考规格的 AAC。"""
    audio_out = _audio_args(ref) + ["-b:a", _audio_bitrate(ref, audio_br)]
    # 输入先全部列完，再写输出选项（否则中间的 -c:v copy 会被当成输入解码器）
    cmd = [ffmpeg, "-y", "-i", seg.src]
    if seg.info.audio is None:
        cmd += _silent_audio_input(ref, seg.info.duration)
        cmd += ["-map", "0:v:0", "-map", "1:a:0", "-shortest"]
    else:
        cmd += ["-map", "0:v:0", "-map", "0:a:0"]
    cmd += ["-c:v", "copy"] + audio_out
    cmd += ["-avoid_negative_ts", "make_zero", out_path]
    code = run_command(cmd, log, progress_cb=progress_cb,
                       total_duration=seg.info.duration, cancel_flag=cancel_flag,
                       raw_fh=raw_fh)
    ok = code == 0 and os.path.isfile(out_path) and os.path.getsize(out_path) > 0
    if ok:
        log(f"-- 分段 {seg.index + 1} 音频归一化完成")
    return ok


def rewrap_segment(ffmpeg: str, seg: SegmentPlan, ref: MediaInfo, out_path: str,
                   log: Callable[[str], None],
                   progress_cb: Callable[[float, str], None],
                   cancel_flag: threading.Event,
                   raw_fh=None) -> bool:
    """拷贝段重封装为 mkv：不重编码，仅修正时间戳/edit list，保证 concat 时间轴连续。

    只保留第 1 路视频与第 1 路音频，丢弃字幕/附件等可能导致 MP4 封装失败的流；
    参考有音轨而本段无音轨时，补一条等长静音轨。
    """
    cmd = [ffmpeg, "-y", "-i", seg.src]
    if seg.info.audio is None and ref.audio is not None:
        cmd += _silent_audio_input(ref, seg.info.duration)
        cmd += ["-map", "0:v:0", "-map", "1:a:0", "-shortest"]
    else:
        cmd += ["-map", "0:v:0", "-map", "0:a:0?"]
    cmd += ["-c", "copy", "-avoid_negative_ts", "make_zero", out_path]
    code = run_command(cmd, log, progress_cb=progress_cb,
                       total_duration=seg.info.duration, cancel_flag=cancel_flag,
                       raw_fh=raw_fh)
    ok = code == 0 and os.path.isfile(out_path) and os.path.getsize(out_path) > 0
    if ok:
        log(f"-- 分段 {seg.index + 1} 重封装完成（流拷贝，无重编码）")
    return ok


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

class MergeJob:
    def __init__(self, files: List[str], out_path: str,
                 ffmpeg_dir: str = "",
                 encoder: str = "auto", cq: int = 23, preset: str = "faster",
                 audio_br: str = "auto", rc_mode: str = "icq",
                 target_br: str = "", max_br: str = "", profile_pref: str = "auto",
                 temp_dir: str = "", keep_temp: bool = False,
                 skip_bad: bool = False, ref_index: int = 0,
                 log: Optional[Callable[[str], None]] = None,
                 progress: Optional[Callable[[float, str], None]] = None):
        self.files = files
        self.out_path = out_path
        self.encoder = encoder
        self.cq = cq
        self.preset = preset
        self.audio_br = audio_br
        self.rc_mode = rc_mode
        self.target_br = target_br
        self.max_br = max_br
        self.profile_pref = profile_pref
        self.temp_dir = temp_dir
        self.keep_temp = keep_temp
        self.skip_bad = skip_bad
        self.ref_index = max(0, int(ref_index))
        self.log = log or (lambda s: print(s, flush=True))
        self.progress = progress or (lambda f, s: None)
        self.cancel_flag = threading.Event()

        self.ffmpeg = which("ffmpeg", ffmpeg_dir or DEFAULT_FFMPEG_DIR)
        self.ffprobe = which("ffprobe", ffmpeg_dir or DEFAULT_FFMPEG_DIR)
        if not self.ffmpeg or not self.ffprobe:
            raise RuntimeError(
                "未找到 ffmpeg.exe / ffprobe.exe，请放到 ffmpeg\\bin 或加入系统 PATH。")

    def cancel(self):
        self.cancel_flag.set()
        self.log("收到取消请求，正在停止 ...")

    def _validate_segment(self, seg: SegmentPlan, ref: MediaInfo) -> tuple:
        """分段产物落地后复核：必须有视频流；参考有音频时产物也必须有音频流。"""
        try:
            chk = ffprobe_media(self.ffprobe, seg.norm_path)
        except Exception as e:
            return False, f"中间文件无法解析: {str(e).splitlines()[0]}"
        if chk.video is None:
            return False, "中间文件缺少视频流"
        if ref.audio is not None and chk.audio is None:
            return False, "中间文件缺少音频流"
        return True, ""

    def run(self) -> bool:
        log = self.log

        # 先把全部源文件探测一遍，集中列出损坏/无法读取的文件
        infos, bad = probe_files(self.files, self.ffprobe, log)
        if bad:
            names = "\n".join(f"  - {os.path.basename(p)}: {e}" for p, e in bad)
            if self.skip_bad:
                log(f"有 {len(bad)} 个视频无法读取，已按设置跳过：")
                log(names)
            else:
                raise RuntimeError(
                    f"有 {len(bad)} 个视频无法读取或损坏，请检查后重试（或勾选“跳过无法读取的视频”）：\n{names}")
        if not infos:
            raise RuntimeError("没有可用于合并的有效视频文件。")
        ref, plans = plan_segments(infos, log, self.ref_index)

        if self.temp_dir:
            os.makedirs(self.temp_dir, exist_ok=True)
            work_dir = os.path.abspath(self.temp_dir)
        else:
            work_dir = tempfile.mkdtemp(prefix="vmerge_")

        # 大批量合并前估算临时盘空间：中间分段约占源文件体积，留 15% 余量
        try:
            total_src = sum(os.path.getsize(f) for f in self.files if os.path.isfile(f))
            free_bytes = shutil.disk_usage(work_dir).free
            if free_bytes < total_src * 1.15:
                log(f"警告: 临时盘可用约 {free_bytes / 1e9:.1f}GB，源文件共 "
                    f"{total_src / 1e9:.1f}GB，空间可能不足，请清理后再合并。")
        except OSError:
            pass

        raw_log_path = os.path.join(work_dir, "ffmpeg_full.log")
        raw_fh = open(raw_log_path, "w", encoding="utf-8", errors="replace")
        try:
            total_dur = sum(p.info.duration for p in plans if p.info.duration > 0) or 1.0
            done_dur = 0.0

            for seg in plans:
                if self.cancel_flag.is_set():
                    return False
                seg_norm = os.path.join(work_dir, f"seg_{seg.index:03d}.mkv")
                seg.norm_path = seg_norm

                def seg_progress(f: float, s: str) -> None:
                    self.progress((done_dur + seg.info.duration * f) / total_dur, s)

                if seg.action == "copy":
                    log(f"[{seg.index + 1}] 编码一致，流拷贝重封装（不重编码）")
                    ok = rewrap_segment(self.ffmpeg, seg, ref, seg_norm, log,
                                        seg_progress, self.cancel_flag, raw_fh)
                    if not ok:
                        log(f"分段 {seg.index + 1} 重封装失败，任务中止。完整日志: {raw_log_path}")
                        return False
                elif seg.action == "audio_fix":
                    ok = audio_fix_segment(self.ffmpeg, seg, ref, seg_norm,
                                           self.audio_br, log, seg_progress,
                                           self.cancel_flag, raw_fh)
                    if not ok:
                        log(f"分段 {seg.index + 1} 音频归一化失败，任务中止。完整日志: {raw_log_path}")
                        return False
                else:
                    ok = transcode_segment(
                        self.ffmpeg, seg, ref, seg_norm,
                        self.encoder, self.cq, self.preset, self.audio_br,
                        self.rc_mode, self.target_br, self.max_br, self.profile_pref,
                        log, seg_progress, self.cancel_flag, raw_fh)
                    if not ok:
                        log(f"分段 {seg.index + 1} 转码失败，任务中止。完整日志: {raw_log_path}")
                        return False

                valid, vmsg = self._validate_segment(seg, ref)
                if not valid:
                    log(f"分段 {seg.index + 1} 产物校验失败：{vmsg}。完整日志: {raw_log_path}")
                    return False
                done_dur += seg.info.duration

            list_path = os.path.join(work_dir, "concat_list.txt")
            with open(list_path, "w", encoding="utf-8") as fh:
                for seg in plans:
                    p = os.path.abspath(seg.norm_path).replace("\\", "/").replace("'", "'\\''")
                    fh.write(f"file '{p}'\n")

            os.makedirs(os.path.dirname(os.path.abspath(self.out_path)), exist_ok=True)
            final_cmd = [
                self.ffmpeg, "-y", "-f", "concat", "-safe", "0",
                "-i", list_path, "-c", "copy", "-movflags", "+faststart",
                self.out_path,
            ]
            log("=" * 60)
            log("所有分段已就绪，执行最终合并（流拷贝，无二次重编码）...")
            code = run_command(final_cmd, log,
                               progress_cb=lambda f, s: self.progress(0.97 + 0.03 * f, s),
                               cancel_flag=self.cancel_flag, raw_fh=raw_fh)
            if code != 0:
                log(f"最终合并失败，退出码 {code}。完整日志: {raw_log_path}")
                return False
            if not os.path.isfile(self.out_path) or os.path.getsize(self.out_path) == 0:
                log(f"输出文件为空，合并失败。完整日志: {raw_log_path}")
                return False
            log("=" * 60)
            log(f"合并完成: {self.out_path}")
            return True
        finally:
            try:
                raw_fh.close()
            except OSError:
                pass
            if not self.keep_temp and not self.temp_dir:
                shutil.rmtree(work_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="按第一个视频格式归一化并合并多视频（多显卡自适应：QSV 硬编 / 软件回退）")
    ap.add_argument("--out", required=True, help="输出文件路径")
    ap.add_argument("--files", nargs="+", required=True,
                    help="待合并视频，顺序即拼接顺序，第 1 个为参考")
    ap.add_argument("--ffmpeg-dir", default="", help="ffmpeg.exe/ffprobe.exe 所在目录")
    ap.add_argument("--encoder", default="auto",
                    help="auto / h264_qsv / hevc_qsv / av1_qsv / libx264 / libx265")
    ap.add_argument("--rc-mode", default="icq", choices=["icq", "vbr"],
                    help="码率模式：icq 质量值(默认) / vbr 目标码率")
    ap.add_argument("--cq", type=int, default=23, help="质量值(ICQ/CRF)，默认 23，越小越清晰")
    ap.add_argument("--target-br", default="", help="VBR 目标码率，如 4000k")
    ap.add_argument("--max-br", default="", help="VBR 最大码率，如 6000k")
    ap.add_argument("--profile", default="auto",
                    help="auto 跟随参考 / baseline / main / high / main10")
    ap.add_argument("--preset", default="faster", help="QSV preset: veryfast/faster/fast/medium/slow")
    ap.add_argument("--audio-bitrate", default="auto", help="auto 按声道推导 / 128k 192k 256k 320k")
    ap.add_argument("--temp-dir", default="", help="中间文件目录（默认系统临时目录）")
    ap.add_argument("--keep-temp", action="store_true", help="保留中间文件用于排查")
    ap.add_argument("--skip-bad", action="store_true",
                    help="跳过无法读取/损坏的视频（默认遇到坏文件即中止）")
    ap.add_argument("--ref-index", type=int, default=0,
                    help="作为参考格式的视频序号（从 0 开始，默认 0 即第 1 个）")
    args = ap.parse_args()

    job = MergeJob(
        files=args.files, out_path=args.out, ffmpeg_dir=args.ffmpeg_dir,
        encoder=args.encoder, cq=args.cq, preset=args.preset,
        audio_br=args.audio_bitrate, rc_mode=args.rc_mode,
        target_br=args.target_br, max_br=args.max_br, profile_pref=args.profile,
        temp_dir=args.temp_dir, keep_temp=args.keep_temp,
        skip_bad=args.skip_bad, ref_index=args.ref_index,
    )
    sys.exit(0 if job.run() else 1)


if __name__ == "__main__":
    main()
