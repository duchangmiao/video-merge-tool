# -*- coding: utf-8 -*-
"""
VideoMergeQSV 图形界面（Tkinter，无第三方依赖）

使用：
  1. 便携版已内置 ffmpeg/ffprobe，自动查找，无需手动指定目录；开发调试时可放到 ffmpeg\\bin\\ 或加入 PATH；
  2. 双击 run.bat，或 python app.py；
  3. 添加视频（顺序即拼接顺序，第 1 个为参考格式），选择输出路径，点“开始合并”。

编码参数默认跟随参考格式自动匹配；码率模式/码率/Profile 可在下方调整。
"""

import os
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import merge_core

APP_VERSION = "1.0.1"
DEFAULT_FFMPEG_DIR = merge_core.DEFAULT_FFMPEG_DIR


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title(f"视频合并工具 · 多显卡自适应版 v{APP_VERSION}")
        root.geometry("960x780")
        root.minsize(860, 620)

        self.job: merge_core.MergeJob | None = None
        self.worker: threading.Thread | None = None
        self.ref_info: merge_core.MediaInfo | None = None
        self.ref_index: int = 0
        self.probe_cache: dict = {}
        self.scanning: bool = False

        self._build_ui()
        self._detect_ffmpeg()

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        pad = {"padx": 6, "pady": 3}

        # 整页可滚动容器：内容超出窗口高度时右侧出现滚动条
        outer = ttk.Frame(self.root)
        outer.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(outer, highlightthickness=0)
        vbar = ttk.Scrollbar(outer, orient="vertical", command=self.canvas.yview)
        self.inner = ttk.Frame(self.canvas)
        self._win_id = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind(
            "<Configure>",
            lambda _e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind(
            "<Configure>",
            lambda e: self.canvas.itemconfigure(self._win_id, width=e.width))
        self.canvas.configure(yscrollcommand=vbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        vbar.pack(side="right", fill="y")
        self.canvas.bind_all("<MouseWheel>", self._on_wheel)

        frm = ttk.LabelFrame(self.inner, text="视频列表（从上到下拼接；点选任一项可将其设为参考格式）")
        frm.pack(fill="x", **pad)

        self.listbox = tk.Listbox(frm, selectmode=tk.EXTENDED, height=8,
                                  activestyle="dotbox")
        self.listbox.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=6)
        sb = ttk.Scrollbar(frm, orient="vertical", command=self.listbox.yview)
        sb.pack(side="left", fill="y", pady=6)
        self.listbox.config(yscrollcommand=sb.set)
        self.listbox.bind("<<ListboxSelect>>", self._on_list_select)

        btns = ttk.Frame(frm)
        btns.pack(side="right", fill="y", padx=6, pady=6)
        ttk.Button(btns, text="添加视频", command=self.add_files).pack(fill="x", pady=2)
        ttk.Button(btns, text="删除选中", command=self.remove_selected).pack(fill="x", pady=2)
        ttk.Button(btns, text="上移", command=lambda: self.move(-1)).pack(fill="x", pady=2)
        ttk.Button(btns, text="下移", command=lambda: self.move(1)).pack(fill="x", pady=2)
        ttk.Button(btns, text="清空", command=self.clear_list).pack(fill="x", pady=2)

        # 格式分类概览
        ovf = ttk.LabelFrame(self.inner, text="格式分类概览（自动扫描；绿色行为当前参考格式）")
        ovf.pack(fill="x", **pad)
        self.ov_summary = ttk.Label(ovf, text="暂无视频", foreground="#666")
        self.ov_summary.pack(anchor="w", padx=8, pady=(4, 0))
        ov_row = ttk.Frame(ovf)
        ov_row.pack(fill="x", padx=6, pady=4)
        self.ov_tree = ttk.Treeview(
            ov_row, columns=("n", "video", "audio", "members"),
            show="headings", height=4, selectmode="browse")
        self.ov_tree.heading("n", text="数量")
        self.ov_tree.heading("video", text="视频格式")
        self.ov_tree.heading("audio", text="音频格式")
        self.ov_tree.heading("members", text="成员示例")
        self.ov_tree.column("n", width=50, anchor="center")
        self.ov_tree.column("video", width=210)
        self.ov_tree.column("audio", width=190)
        self.ov_tree.column("members", width=380)
        ov_sb = ttk.Scrollbar(ov_row, orient="vertical", command=self.ov_tree.yview)
        self.ov_tree.config(yscrollcommand=ov_sb.set)
        self.ov_tree.pack(side="left", fill="both", expand=True)
        ov_sb.pack(side="left", fill="y")
        self.ov_tree.tag_configure("ref", background="#d9ead3")
        self.ov_tree.bind("<<TreeviewSelect>>", self._on_overview_select)
        self.ov_groups: dict = {}

        ref = ttk.LabelFrame(self.inner, text="参考格式（默认第 1 个；点选列表视频可切换）")
        ref.pack(fill="x", **pad)
        self.ref_label = ttk.Label(ref, text="未检测到视频", foreground="#666")
        self.ref_label.pack(anchor="w", padx=8, pady=4)

        opt = ttk.LabelFrame(self.inner, text="输出与选项（默认跟随参考格式自动匹配）")
        opt.pack(fill="x", **pad)

        # 行 A：编码器 / 码率模式 / Profile
        row = ttk.Frame(opt)
        row.pack(fill="x", padx=6, pady=2)
        ttk.Label(row, text="编码器:").pack(side="left")
        self.enc_var = tk.StringVar(value="auto")
        ttk.Combobox(row, textvariable=self.enc_var, width=14, state="readonly",
                     values=["auto", "h264_qsv", "hevc_qsv", "av1_qsv",
                             "libx264", "libx265"]).pack(side="left", padx=6)

        ttk.Label(row, text="码率模式:").pack(side="left", padx=(16, 0))
        self.rc_mode_var = tk.StringVar(value="icq")
        rc_cb = ttk.Combobox(row, textvariable=self.rc_mode_var, width=10, state="readonly",
                             values=["icq", "vbr"])
        rc_cb.pack(side="left", padx=6)
        rc_cb.bind("<<ComboboxSelected>>", lambda _e: self._sync_mode())
        ttk.Label(row, text="(icq=质量 / vbr=目标码率)", foreground="#666").pack(side="left")

        ttk.Label(row, text="Profile:").pack(side="left", padx=(16, 0))
        self.profile_var = tk.StringVar(value="auto")
        ttk.Combobox(row, textvariable=self.profile_var, width=10, state="readonly",
                     values=["auto", "baseline", "main", "high", "main10"]).pack(side="left", padx=6)

        # 行 B：质量CQ / 目标码率 / 最大码率 / Preset / 音频码率
        row = ttk.Frame(opt)
        row.pack(fill="x", padx=6, pady=2)
        ttk.Label(row, text="质量CQ:").pack(side="left")
        self.cq_var = tk.IntVar(value=23)
        self.cq_spin = ttk.Spinbox(row, from_=18, to=32, width=5, textvariable=self.cq_var)
        self.cq_spin.pack(side="left", padx=6)

        ttk.Label(row, text="目标码率(kbps):").pack(side="left", padx=(16, 0))
        self.target_var = tk.StringVar(value="")
        self.target_entry = ttk.Entry(row, textvariable=self.target_var, width=8)
        self.target_entry.pack(side="left", padx=6)

        ttk.Label(row, text="最大码率(kbps):").pack(side="left", padx=(12, 0))
        self.max_var = tk.StringVar(value="")
        self.max_entry = ttk.Entry(row, textvariable=self.max_var, width=8)
        self.max_entry.pack(side="left", padx=6)

        ttk.Label(row, text="Preset:").pack(side="left", padx=(16, 0))
        self.preset_var = tk.StringVar(value="faster")
        ttk.Combobox(row, textvariable=self.preset_var, width=10, state="readonly",
                     values=["veryfast", "faster", "fast", "medium", "slow"]).pack(side="left", padx=6)

        ttk.Label(row, text="音频码率:").pack(side="left", padx=(16, 0))
        self.abr_var = tk.StringVar(value="auto")
        ttk.Combobox(row, textvariable=self.abr_var, width=8, state="readonly",
                     values=["auto", "128k", "192k", "256k", "320k"]).pack(side="left", padx=6)

        # 行 C：输出文件 / 临时目录
        row = ttk.Frame(opt)
        row.pack(fill="x", padx=6, pady=2)
        ttk.Label(row, text="输出文件:").pack(side="left")
        self.out_var = tk.StringVar()
        ttk.Entry(row, textvariable=self.out_var).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(row, text="浏览", command=self.pick_out).pack(side="left")

        row = ttk.Frame(opt)
        row.pack(fill="x", padx=6, pady=2)
        self.keep_temp_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(row, text="保留中间文件（排查用）",
                        variable=self.keep_temp_var).pack(side="left")
        self.skip_bad_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(row, text="跳过无法读取/损坏的视频",
                        variable=self.skip_bad_var).pack(side="left", padx=(16, 0))
        ttk.Label(row, text="临时目录:").pack(side="left", padx=(20, 0))
        self.temp_var = tk.StringVar()
        ttk.Entry(row, textvariable=self.temp_var).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(row, text="浏览", command=self.pick_temp).pack(side="left")

        act = ttk.Frame(self.inner)
        act.pack(fill="x", **pad)
        self.start_btn = ttk.Button(act, text="开始合并", command=self.start)
        self.start_btn.pack(side="left", padx=6)
        self.cancel_btn = ttk.Button(act, text="取消", command=self.cancel, state="disabled")
        self.cancel_btn.pack(side="left", padx=6)
        self.progress = ttk.Progressbar(act, mode="determinate", maximum=1000)
        self.progress.pack(side="left", fill="x", expand=True, padx=10)

        logf = ttk.LabelFrame(self.inner, text="日志")
        logf.pack(fill="both", expand=True, **pad)
        self.log_box = tk.Text(logf, height=10, wrap="word", state="disabled",
                               background="#1e1e1e", foreground="#d4d4d4",
                               insertbackground="#d4d4d4")
        self.log_box.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=6)
        lsb = ttk.Scrollbar(logf, orient="vertical", command=self.log_box.yview)
        lsb.pack(side="left", fill="y", pady=6)
        self.log_box.config(yscrollcommand=lsb.set)

        self._sync_mode()

    def _sync_mode(self):
        """按码率模式联动启用/禁用对应控件。"""
        vbr = self.rc_mode_var.get() == "vbr"
        state_target = "normal" if vbr else "disabled"
        self.target_entry.config(state=state_target)
        self.max_entry.config(state=state_target)
        self.cq_spin.config(state="normal" if not vbr else "disabled")

    def _on_wheel(self, event):
        """鼠标滚轮滚动整页（内容可滚动时才滚动）。"""
        if self.canvas.yview() != (0.0, 1.0):
            self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    # ------------------------------------------------------------ 文件操作
    def add_files(self):
        paths = filedialog.askopenfilenames(
            title="选择视频文件",
            filetypes=[("视频文件", "*.mp4 *.mov *.mkv *.avi *.flv *.ts *.m4v *.wmv *.webm *.mpg *.mpeg"),
                       ("所有文件", "*.*")])
        for p in paths:
            self.listbox.insert(tk.END, p)
        self.refresh_ref()

    def remove_selected(self):
        removed = [self.listbox.get(i) for i in self.listbox.curselection()]
        for i in reversed(self.listbox.curselection()):
            self.listbox.delete(i)
        for p in removed:
            self.probe_cache.pop(p, None)
        self._clamp_ref()
        self.refresh_ref()

    def move(self, delta: int):
        sel = list(self.listbox.curselection())
        if not sel:
            return
        for i in sel:
            j = i + delta
            if j < 0 or j >= self.listbox.size():
                continue
            val = self.listbox.get(i)
            self.listbox.delete(i)
            self.listbox.insert(j, val)
            self.listbox.selection_set(j)
        self.refresh_ref()

    def clear_list(self):
        self.listbox.delete(0, tk.END)
        self.probe_cache.clear()
        self.ref_index = 0
        self.refresh_ref()

    def all_files(self) -> list:
        return [self.listbox.get(i) for i in range(self.listbox.size())]

    def _clamp_ref(self):
        n = self.listbox.size()
        if self.ref_index >= n:
            self.ref_index = max(0, n - 1)

    # ------------------------------------------------------------ 参考格式
    def _on_list_select(self, _event=None):
        sel = self.listbox.curselection()
        if not sel:
            return
        self.ref_index = sel[0]
        self.refresh_ref()

    def _probe(self, ffprobe: str, path: str) -> merge_core.MediaInfo:
        info = self.probe_cache.get(path)
        if info is None:
            info = merge_core.ffprobe_media(ffprobe, path)
            self.probe_cache[path] = info
        return info

    def refresh_ref(self):
        files = self.all_files()
        self._clamp_ref()
        if not files:
            self.ref_label.config(text="未检测到视频", foreground="#666")
            self.ref_info = None
            self._populate_overview([], None)
            return
        ffprobe = merge_core.which("ffprobe", DEFAULT_FFMPEG_DIR)
        if not ffprobe:
            self.ref_label.config(text="未找到 ffprobe，无法读取参考格式", foreground="#c00")
            return
        try:
            info = self._probe(ffprobe, files[self.ref_index])
        except Exception as e:
            self.ref_label.config(text=f"解析失败: {e}", foreground="#c00")
            self.ref_info = None
            self._refresh_overview()
            return
        self.ref_info = info
        v, a = info.video, info.audio
        txt = f"参考（第 {self.ref_index + 1} 个）: {os.path.basename(files[self.ref_index])}\n"
        if v:
            txt += f"视频 {v.codec} {v.width}x{v.height} {v.fps:.2f}fps {v.pix_fmt} {v.bit_depth}bit"
        if a:
            prof = f" {a.profile}" if a.profile else ""
            txt += f"  | 音频 {a.codec}{prof} {a.sample_rate}Hz {a.channels}ch"
        self.ref_label.config(text=txt, foreground="#222")
        self._auto_fill_defaults(v)
        self._refresh_overview()

    def _auto_fill_defaults(self, v: merge_core.VideoInfo):
        """按参考编码/分辨率自动回填默认编码参数；回填后仍可手动修改。"""
        if v is None:
            return
        # 编码器跟随参考编码选对应 QSV 硬编；CQ 按编码给默认值；音频码率 auto 按声道推导
        self.enc_var.set(merge_core.default_encoder(v.codec))
        self.cq_var.set(merge_core.default_cq(v.codec))
        self.abr_var.set("auto")
        # VBR 建议码率按分辨率
        t, m = merge_core.suggested_bitrate(v.width, v.height)
        self.target_var.set(str(t))
        self.max_var.set(str(m))

    # ------------------------------------------------------------ 格式概览
    def _refresh_overview(self):
        files = self.all_files()
        if self.scanning:
            return
        if not files:
            self._populate_overview([], None)
            return
        ffprobe = merge_core.which("ffprobe", DEFAULT_FFMPEG_DIR)
        self.scanning = True
        self.ov_summary.config(text="正在扫描全部视频格式 ...", foreground="#666")

        def worker():
            infos = []
            for p in files:
                try:
                    infos.append(self._probe(ffprobe, p))
                except Exception as e:
                    infos.append(("__bad__", str(e).splitlines()[0]))
            self.root.after(0, self._overview_done, infos)

        threading.Thread(target=worker, daemon=True).start()

    def _overview_done(self, infos):
        self.scanning = False
        self._populate_overview(infos, self.ref_info)

    @staticmethod
    def _sig_key(info) -> tuple:
        if isinstance(info, tuple) and info and info[0] == "__bad__":
            return ("__bad__",)
        v, a = info.video, info.audio
        return (
            v.codec if v else "?",
            v.width if v else 0, v.height if v else 0,
            round(v.fps, 2) if v else 0,
            v.pix_fmt if v else "?", v.bit_depth if v else 0,
            a.codec if a else "none", a.profile if a else "-",
            a.sample_rate if a else 0, a.channels if a else 0,
        )

    def _sig_text(self, key: tuple) -> tuple:
        if key[0] == "__bad__":
            return "无法解析", "（损坏/非标准文件）"
        vc, w, h, fps, pix, bd, ac, prof, sr, ch = key
        vtxt = f"{vc} {w}x{h} {fps}fps {pix} {bd}bit"
        if ac == "none":
            atxt = "无音轨"
        elif prof and prof != "-":
            atxt = f"{ac} {prof} {sr}Hz {ch}ch"
        else:
            atxt = f"{ac} {sr}Hz {ch}ch"
        return vtxt, atxt

    @staticmethod
    def _member_name(info) -> str:
        if isinstance(info, tuple):
            return "损坏文件"
        return os.path.basename(info.path)

    def _populate_overview(self, infos: list, ref_info):
        for iid in self.ov_tree.get_children():
            self.ov_tree.delete(iid)
        self.ov_groups = {}
        if not infos:
            self.ov_summary.config(text="暂无视频", foreground="#666")
            return
        groups: dict = {}
        order: list = []
        for info in infos:
            key = self._sig_key(info)
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(info)
        ref_key = self._sig_key(ref_info) if ref_info is not None else None
        total = len(infos)
        mismatch = 0
        for key in order:
            members = groups[key]
            if ref_key is not None and key != ref_key:
                mismatch += len(members)
            names = [self._member_name(x) for x in members[:3]]
            more = "" if len(members) <= 3 else f" 等 {len(members)} 个"
            vtxt, atxt = self._sig_text(key)
            tag = "ref" if ref_key is not None and key == ref_key else ""
            iid = self.ov_tree.insert("", tk.END, tags=tag,
                                      values=(len(members), vtxt, atxt, "、".join(names) + more))
            self.ov_groups[iid] = members
        self.ov_summary.config(
            text=f"共 {total} 个视频；{mismatch} 个与当前参考格式不一致（合并时自动归一化到参考格式）",
            foreground="#222" if mismatch == 0 else "#9c6500")

    def _on_overview_select(self, _event=None):
        """点击概览分组行：把该分组中第一个正常视频设为参考格式并跟随参数。"""
        sel = self.ov_tree.selection()
        if not sel:
            return
        members = self.ov_groups.get(sel[0], [])
        target = next((m for m in members
                       if not (isinstance(m, tuple) and m and m[0] == "__bad__")), None)
        if target is None:
            self._log("该分组为损坏/无法解析的视频，不能作为参考格式。")
            return
        files = self.all_files()
        try:
            idx = files.index(target.path)
        except ValueError:
            return
        self.ref_index = idx
        self.refresh_ref()

    # ------------------------------------------------------------ 路径选择
    def pick_out(self):
        p = filedialog.asksaveasfilename(
            title="输出文件", defaultextension=".mp4",
            filetypes=[("MP4", "*.mp4"), ("MKV", "*.mkv"), ("所有文件", "*.*")])
        if p:
            self.out_var.set(p)

    def pick_temp(self):
        p = filedialog.askdirectory(title="选择临时目录")
        if p:
            self.temp_var.set(p)

    def _detect_ffmpeg(self):
        ff = merge_core.which("ffmpeg", DEFAULT_FFMPEG_DIR)
        fp = merge_core.which("ffprobe", DEFAULT_FFMPEG_DIR)
        if ff and fp:
            encoders = merge_core.detect_qsv_encoders(ff)
            qsv_ok = any(e in encoders for e in ("h264_qsv", "hevc_qsv"))
            tag = "已找到内置 ffmpeg，QSV 编码器: " + (", ".join(encoders) if encoders else "无")
            self._log(tag + ("  [可用]" if qsv_ok else "（将使用软件编码）"))
        else:
            self._log("未找到内置 ffmpeg.exe/ffprobe.exe。")

    # ------------------------------------------------------------ 日志/进度
    def _log(self, msg: str):
        self.log_box.config(state="normal")
        self.log_box.insert(tk.END, msg + "\n")
        self.log_box.see(tk.END)
        self.log_box.config(state="disabled")

    def _set_progress(self, frac: float):
        self.progress["value"] = frac * 1000

    # ------------------------------------------------------------ 任务
    def start(self):
        files = self.all_files()
        out = self.out_var.get().strip()
        if len(files) < 2:
            messagebox.showwarning("提示", "请至少添加 2 个视频。")
            return
        if not out:
            messagebox.showwarning("提示", "请选择输出文件路径。")
            return
        if not os.path.isdir(os.path.dirname(os.path.abspath(out))):
            messagebox.showwarning("提示", "输出目录不存在。")
            return

        rc_mode = self.rc_mode_var.get()
        target_br = ""
        max_br = ""
        if rc_mode == "vbr":
            t = self.target_var.get().strip()
            m = self.max_var.get().strip()
            if not t:
                messagebox.showwarning("提示", "VBR 模式请填写目标码率。")
                return
            target_br = f"{t}k"
            max_br = f"{m}k" if m else ""

        self.start_btn.config(state="disabled")
        self.cancel_btn.config(state="normal")
        self._set_progress(0)

        self.job = merge_core.MergeJob(
            files=files, out_path=out, ffmpeg_dir=DEFAULT_FFMPEG_DIR,
            encoder=self.enc_var.get(), cq=int(self.cq_var.get()),
            preset=self.preset_var.get(), audio_br=self.abr_var.get(),
            rc_mode=rc_mode, target_br=target_br, max_br=max_br,
            profile_pref=self.profile_var.get(),
            temp_dir=self.temp_var.get().strip(), keep_temp=self.keep_temp_var.get(),
            skip_bad=self.skip_bad_var.get(), ref_index=self.ref_index,
            log=lambda s: self.root.after(0, self._log, s),
            progress=lambda f, s: self.root.after(0, self._set_progress, f),
        )

        def worker():
            try:
                ok = self.job.run()
            except Exception as e:
                self.root.after(0, self._log, f"异常: {e}")
                ok = False
            self.root.after(0, self._done, ok)

        self.worker = threading.Thread(target=worker, daemon=True)
        self.worker.start()

    def _done(self, ok: bool):
        self.start_btn.config(state="normal")
        self.cancel_btn.config(state="disabled")
        if ok:
            self._set_progress(1)
            messagebox.showinfo("完成", "合并完成！")
        else:
            self._log("任务未完成（已取消或失败，详见上方日志）。")

    def cancel(self):
        if self.job:
            self.job.cancel()


def main():
    root = tk.Tk()
    try:
        ttk.Style().theme_use("clam")
    except Exception:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
