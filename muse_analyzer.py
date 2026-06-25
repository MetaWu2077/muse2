#!/usr/bin/env python3
"""
Muse Desktop Analyzer — Local .bin file browser & report generator

Browse folders, select Muse session .bin files, and generate EEG reports
with a single click. No server needed — everything runs locally.

Usage:
    python muse_analyzer.py
    python muse_analyzer.py /path/to/muse_sessions
"""

import os
import sys
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from datetime import datetime
from pathlib import Path
import webbrowser

# Ensure amused-src and muse-cloud-server are importable
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
AMUSED_DIR = os.path.join(SCRIPT_DIR, "amused-src")
CLOUD_DIR = os.path.join(SCRIPT_DIR, "muse-cloud-server")
if AMUSED_DIR not in sys.path:
    sys.path.insert(0, AMUSED_DIR)
if CLOUD_DIR not in sys.path:
    sys.path.insert(0, CLOUD_DIR)

# Lazy imports — only when actually generating a report
_report_imports_done = False


def _ensure_imports():
    global _report_imports_done
    if _report_imports_done:
        return True
    try:
        import numpy as np
        import matplotlib
        matplotlib.use("Agg")
        global report_generator
        import report_generator  # from muse-cloud-server/
        _report_imports_done = True
        return True
    except ImportError as e:
        messagebox.showerror("Import Error",
                              f"Dependency missing: {e}\n\n"
                              "Make sure numpy, scipy, matplotlib are installed:\n"
                              "  pip install numpy scipy matplotlib")
        return False


# ── Helpers ────────────────────────────────────────────────────────────────

def fmt_size(size_bytes):
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    else:
        return f"{size_bytes / (1024 * 1024):.1f} MB"


def fmt_date(ts):
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def scan_folder(folder_path):
    """Return list of .bin files with metadata."""
    files = []
    for f in sorted(Path(folder_path).glob("*.bin"), key=lambda x: x.stat().st_mtime, reverse=True):
        st = f.stat()
        report_exists = (f.parent / (f.stem + ".report.html")).exists()
        files.append({
            "name": f.name,
            "path": str(f),
            "size": st.st_size,
            "mtime": st.st_mtime,
            "report_exists": report_exists,
        })
    return files


# ── Main App ────────────────────────────────────────────────────────────────

class MuseAnalyzer(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Muse Desktop Analyzer")
        self.geometry("1000x650")
        self.minsize(700, 400)
        self.configure(bg="#0f1923")

        self.files = []
        self.folder_path = tk.StringVar()
        self.status_text = tk.StringVar(value="Ready. Select a folder to begin.")

        self._build_ui()

        # Accept folder as command-line argument
        if len(sys.argv) > 1 and os.path.isdir(sys.argv[1]):
            self.folder_path.set(sys.argv[1])
            self._load_folder(sys.argv[1])

    # ── UI ──────────────────────────────────────────────────────────────

    def _build_ui(self):
        # ── Top bar ──
        top = tk.Frame(self, bg="#1a2d3d", padx=12, pady=10)
        top.pack(fill=tk.X)

        tk.Label(top, text="📁 文件夹:", fg="#78909c", bg="#1a2d3d",
                 font=("", 11)).pack(side=tk.LEFT)

        self.folder_entry = tk.Entry(top, textvariable=self.folder_path,
                                      bg="#0f1923", fg="#e0e0e0", insertbackground="#ccc",
                                      font=("Consolas", 10), relief=tk.FLAT, width=60)
        self.folder_entry.pack(side=tk.LEFT, padx=(8, 8), fill=tk.X, expand=True)
        self.folder_entry.bind("<Return>", lambda e: self._load_folder(self.folder_path.get()))

        browse_btn = tk.Button(top, text="浏览...", command=self._browse_folder,
                               bg="#2d5f8a", fg="#fff", font=("", 10),
                               relief=tk.FLAT, padx=14, pady=3, cursor="hand2")
        browse_btn.pack(side=tk.LEFT)

        # ── Main area ──
        main = tk.PanedWindow(self, orient=tk.HORIZONTAL, bg="#0f1923", sashwidth=2)
        main.pack(fill=tk.BOTH, expand=True, padx=0, pady=0)

        # Left panel — file list
        left = tk.Frame(main, bg="#15232e", width=380)
        main.add(left)

        left_header = tk.Frame(left, bg="#1e3343", padx=12, pady=8)
        left_header.pack(fill=tk.X)
        self.file_count_label = tk.Label(left_header, text="— 个会话文件", fg="#78909c",
                                         bg="#1e3343", font=("", 10))
        self.file_count_label.pack(side=tk.LEFT)

        # Treeview
        tree_frame = tk.Frame(left, bg="#15232e")
        tree_frame.pack(fill=tk.BOTH, expand=True)

        columns = ("name", "size", "date", "report")
        self.tree = ttk.Treeview(tree_frame, columns=columns, show="headings",
                                  selectmode="browse", height=20)

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Treeview",
                        background="#15232e", foreground="#ccc",
                        fieldbackground="#15232e", rowheight=28,
                        font=("", 10))
        style.configure("Treeview.Heading",
                        background="#1e3343", foreground="#78909c",
                        font=("", 9, "bold"), padding=6)
        style.map("Treeview",
                  background=[("selected", "#1a3a52")],
                  foreground=[("selected", "#4fc3f7")])

        self.tree.heading("name", text="文件名")
        self.tree.heading("size", text="大小")
        self.tree.heading("date", text="日期")
        self.tree.heading("report", text="报告")
        self.tree.column("name", width=180, minwidth=100)
        self.tree.column("size", width=70, minwidth=60, anchor=tk.E)
        self.tree.column("date", width=120, minwidth=80)
        self.tree.column("report", width=50, minwidth=40, anchor=tk.CENTER)

        scrollbar = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self.tree.bind("<<TreeviewSelect>>", self._on_file_select)
        self.tree.bind("<Double-1>", lambda e: self._generate_report())

        # Right panel — detail + actions
        right = tk.Frame(main, bg="#0f1923")
        main.add(right)

        # Info card
        self.info_frame = tk.Frame(right, bg="#15232e", padx=20, pady=16)
        self.info_frame.pack(fill=tk.X, padx=16, pady=(16, 8))

        self.info_label = tk.Label(self.info_frame,
                                    text="选择一个 .bin 文件查看详情\n\n双击或点击下方按钮生成报告",
                                    fg="#78909c", bg="#15232e",
                                    font=("", 10), justify=tk.LEFT)
        self.info_label.pack(anchor=tk.W)

        # Buttons
        btn_frame = tk.Frame(right, bg="#0f1923", padx=16)
        btn_frame.pack(fill=tk.X, pady=8)

        self.gen_btn = tk.Button(btn_frame, text="📄 生成报告",
                                  command=self._generate_report,
                                  bg="#2d5f8a", fg="#fff", font=("", 11),
                                  relief=tk.FLAT, padx=20, pady=6,
                                  cursor="hand2", state=tk.DISABLED)
        self.gen_btn.pack(side=tk.LEFT, padx=(0, 8))

        self.open_btn = tk.Button(btn_frame, text="📂 打开报告",
                                   command=self._open_report,
                                   bg="#37474f", fg="#ccc", font=("", 11),
                                   relief=tk.FLAT, padx=20, pady=6,
                                   cursor="hand2", state=tk.DISABLED)
        self.open_btn.pack(side=tk.LEFT, padx=(0, 8))

        self.batch_btn = tk.Button(btn_frame, text="⚡ 批量生成全部",
                                    command=self._batch_generate,
                                    bg="#5d4037", fg="#ffcc80", font=("", 11),
                                    relief=tk.FLAT, padx=20, pady=6,
                                    cursor="hand2")
        self.batch_btn.pack(side=tk.RIGHT)

        # Progress
        self.progress = ttk.Progressbar(right, mode="indeterminate", length=400)
        self.progress.pack(fill=tk.X, padx=16, pady=(8, 0))
        self.progress.pack_forget()

        # Status bar
        status_bar = tk.Frame(self, bg="#1a2d3d", padx=12, pady=6)
        status_bar.pack(fill=tk.X, side=tk.BOTTOM)
        tk.Label(status_bar, textvariable=self.status_text, fg="#78909c",
                 bg="#1a2d3d", font=("", 9)).pack(side=tk.LEFT)

    # ── Actions ──────────────────────────────────────────────────────────

    def _browse_folder(self):
        path = filedialog.askdirectory(title="选择包含 .bin 文件的文件夹")
        if path:
            self.folder_path.set(path)
            self._load_folder(path)

    def _load_folder(self, path):
        if not os.path.isdir(path):
            messagebox.showwarning("Invalid Path", f"Folder not found:\n{path}")
            return

        self.files = scan_folder(path)
        self.tree.delete(*self.tree.get_children())

        if not self.files:
            self.file_count_label.config(text="没有找到 .bin 文件")
            self.status_text.set(f"No .bin files in: {path}")
            return

        self.file_count_label.config(text=f"{len(self.files)} 个会话文件")
        self.status_text.set(f"Loaded {len(self.files)} sessions from: {path}")

        for i, f in enumerate(self.files):
            report_mark = "✅" if f["report_exists"] else ""
            self.tree.insert("", tk.END, iid=str(i),
                             values=(f["name"], fmt_size(f["size"]),
                                     fmt_date(f["mtime"]), report_mark))

        # Auto-select first file
        if self.files:
            self.tree.selection_set("0")
            self._on_file_select()

    def _on_file_select(self, event=None):
        sel = self.tree.selection()
        if not sel:
            self.gen_btn.config(state=tk.DISABLED)
            self.open_btn.config(state=tk.DISABLED)
            return

        idx = int(sel[0])
        f = self.files[idx]

        info_text = (
            f"📄 文件: {f['name']}\n"
            f"📏 大小: {fmt_size(f['size'])}\n"
            f"🕐 修改时间: {fmt_date(f['mtime'])}\n"
            f"📊 已有报告: {'是' if f['report_exists'] else '否'}"
        )
        self.info_label.config(text=info_text, fg="#e0e0e0")

        self.gen_btn.config(state=tk.NORMAL)
        self.open_btn.config(state=tk.NORMAL if f["report_exists"] else tk.DISABLED)

    def _generate_report(self):
        sel = self.tree.selection()
        if not sel:
            return
        idx = int(sel[0])
        f = self.files[idx]
        self._generate_one(f, idx)

    def _generate_one(self, file_info, idx=None):
        if not _ensure_imports():
            return

        bin_path = file_info["path"]
        report_path = os.path.splitext(bin_path)[0] + ".report.html"

        self.gen_btn.config(state=tk.DISABLED)
        self.batch_btn.config(state=tk.DISABLED)
        self.progress.pack(fill=tk.X, padx=16, pady=(8, 0))
        self.progress.start()
        self.status_text.set(f"Generating report for: {file_info['name']}...")

        def _run():
            try:
                report_generator.generate_report(bin_path, report_path)
                file_info["report_exists"] = True
                self.after(0, lambda: self._on_generate_done(file_info, idx, report_path, None))
            except Exception as e:
                self.after(0, lambda: self._on_generate_done(file_info, idx, report_path, str(e)))

        threading.Thread(target=_run, daemon=True).start()

    def _on_generate_done(self, file_info, idx, report_path, error):
        self.progress.stop()
        self.progress.pack_forget()
        self.gen_btn.config(state=tk.NORMAL)
        self.batch_btn.config(state=tk.NORMAL)

        if error:
            self.status_text.set(f"Failed: {error}")
            messagebox.showerror("Report Error", f"Failed to generate report:\n{error}")
            return

        self.status_text.set(f"Report generated: {file_info['name']}")

        # Update tree
        if idx is not None:
            self.tree.set(str(idx), "report", "✅")
        self.open_btn.config(state=tk.NORMAL)

        # Open in browser
        webbrowser.open(f"file:///{report_path}")

    def _open_report(self):
        sel = self.tree.selection()
        if not sel:
            return
        idx = int(sel[0])
        f = self.files[idx]
        report_path = os.path.splitext(f["path"])[0] + ".report.html"
        if os.path.exists(report_path):
            webbrowser.open(f"file:///{report_path}")
        else:
            messagebox.showinfo("No Report", "Report not generated yet.\nClick 'Generate Report' first.")

    def _batch_generate(self):
        if not self.files:
            return
        pending = [f for f in self.files if not f["report_exists"]]
        if not pending:
            messagebox.showinfo("All Done", "All files already have reports! ✅")
            return

        ok = messagebox.askyesno("Batch Generate",
                                  f"Generate reports for {len(pending)} files?\n"
                                  f"This may take a while.")
        if not ok:
            return

        self._batch_index = 0
        self._batch_pending = pending
        self._batch_total = len(pending)
        self.gen_btn.config(state=tk.DISABLED)
        self.batch_btn.config(state=tk.DISABLED)
        self._batch_next()

    def _batch_next(self):
        if self._batch_index >= self._batch_total:
            self.progress.stop()
            self.progress.pack_forget()
            self.gen_btn.config(state=tk.NORMAL)
            self.batch_btn.config(state=tk.NORMAL)
            self.status_text.set(f"All done! {self._batch_total} reports generated.")

            # Refresh tree
            for i, f in enumerate(self.files):
                if f["report_exists"]:
                    self.tree.set(str(i), "report", "✅")
            return

        f = self._batch_pending[self._batch_index]
        bin_path = f["path"]
        report_path = os.path.splitext(bin_path)[0] + ".report.html"

        if not _ensure_imports():
            return

        self.progress.pack(fill=tk.X, padx=16, pady=(8, 0))
        self.progress.start()
        self.status_text.set(
            f"[{self._batch_index + 1}/{self._batch_total}] {f['name']}")

        def _run():
            try:
                report_generator.generate_report(bin_path, report_path)
                f["report_exists"] = True
            except Exception:
                pass  # Skip failed files in batch mode
            self.after(0, self._batch_next_done)

        threading.Thread(target=_run, daemon=True).start()

    def _batch_next_done(self):
        self._batch_index += 1
        self.progress.stop()
        self.progress.pack_forget()
        self._batch_next()


# ── Entry point ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = MuseAnalyzer()
    app.mainloop()
