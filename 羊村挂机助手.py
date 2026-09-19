"""羊村挂机助手1.2。

作者：鹏小白就是我
整合：A_c
操刀师傅：Ai-codex
"""
from __future__ import annotations

import json
import random
import struct
import sys
import threading
import time
import zlib
from pathlib import Path
import tkinter as tk
from tkinter import font as tkfont, messagebox, scrolledtext, ttk

DEPENDENCY_ERROR = None
try:
    import cv2
    import numpy as np
    import pyautogui
    from pynput import keyboard
except ImportError as error:
    DEPENDENCY_ERROR = error
    cv2 = np = pyautogui = keyboard = None


# 打包成 exe 运行时，templates / 图像 / logs 取 exe 所在文件夹，和直接运行脚本时保持一致。
ROOT = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
TEMPLATE_DIR = ROOT / "templates"
IMAGE_DIR = ROOT / "图像"
LOG_DIR = ROOT / "logs"
CONFIG_FILE = ROOT / "羊村挂机配置.json"
NEEDED_TEMPLATES = {
    "shengji.png", "liliang.png", "jiahao.png", "queding.png", "tacha.png",
    "gongji.png", "bosscha.png", "cha.png", "boss.png", "4boss.png",
    "yiban.png", "putong.png", "kunnan.png", "qugan.png", "jiandielang.png",
    "wulang.png", "jiasu.png",
}

DEFAULTS = {
    "tower_threshold": 0.90, "tower_click_interval": 1.0,
    "tower_cycle_interval": 60.0, "tower_plus_clicks": 20,
    "auto_threshold": 0.90, "auto_recheck_seconds": 60.0,
    "auto_no_monster_seconds": 600.0,
    "boss_threshold": 0.90, "boss_cycle_minutes": 10.0,
    "wolf_friend": None, "wolf_center": None, "wolf_confirm": None, "wolf_arrow": None,
    "wolf_range_x": 20, "wolf_range_y": 15, "wolf_click_min": 1,
    "wolf_click_max": 3, "wolf_click_gap_min": 1.0, "wolf_click_gap_max": 2.0,
    "wolf_after_friend": 1.0, "wolf_after_confirm": 1.0, "wolf_after_arrow": 1.0,
    "wolf_spy_threshold": 0.90,
    "wolf_after_jiasu": 1.0, "wolf_jiasu_threshold": 0.90,
    "restart_time": "00:01", "restart_threshold": 0.90,
    "restart_fanpai_clicks": 3, "restart_fanpai_gap": 1.0,
    "restart_after_bwyc_seconds": 10.0, "restart_before_fanpai_seconds": 5.0,
    "restart_jinru_gap": 1.0, "restart_qianxian_gap": 2.0,
    "restart_step_timeout": 10.0, "restart_total_seconds": 60.0,
    "restart_resume_seconds": 10.0,
}

# 升塔与战斗同时开启时，升塔最多等战斗首次攻击的秒数；战斗线程异常也不会一直卡住升塔。
FIRST_STRIKE_TIMEOUT = 30.0

# 定时重开游戏需要的图片，缺图时提示用户补齐。
RESTART_IMAGES = (
    "bwyc.png", "choujiang.png", "jiangpin.png", "fanpai.png", "jinru.png", "qianxian.png",
)

def extract_embedded_templates() -> list[str]:
    """读取 PyInstaller CArchive；仅把 PNG 模板复制到本工具自己的 templates 文件夹。"""
    TEMPLATE_DIR.mkdir(exist_ok=True)
    IMAGE_DIR.mkdir(exist_ok=True)
    # “图像”文件夹里的同名图片仍然可用，因此两处任意一处存在就不算缺失。
    missing = {name for name in NEEDED_TEMPLATES
               if not (TEMPLATE_DIR / name).exists() and not (IMAGE_DIR / name).exists()}
    if not missing:
        return []
    source_dir = ROOT / "sources"
    if not source_dir.exists():
        return sorted(missing)
    for exe in source_dir.glob("*.exe"):
        try:
            data = exe.read_bytes()
            if len(data) < 88 or data[-88:-80] != b"MEI\x0c\x0b\x0a\x0b\x0e":
                continue
            package_len, toc_offset, toc_length = struct.unpack("!III", data[-80:-68])
            package_start = len(data) - package_len
            pos, end = package_start + toc_offset, package_start + toc_offset + toc_length
            while pos < end:
                entry_size, offset, length, _unpacked, compressed, kind = struct.unpack(
                    "!IIIIBc", data[pos:pos + 18]
                )
                name = data[pos + 18:pos + entry_size].split(b"\0", 1)[0].decode("utf-8", "replace")
                if kind == b"b" and name in missing:
                    blob = data[package_start + offset:package_start + offset + length]
                    (TEMPLATE_DIR / name).write_bytes(zlib.decompress(blob) if compressed else blob)
                    missing.remove(name)
                pos += entry_size
        except (OSError, ValueError, struct.error, zlib.error):
            continue
    return sorted(missing)


class AssistantApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("羊村挂机助手1.2")
        self.root.geometry("380x760")
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.click_lock = threading.Lock()
        # 升塔与战斗同时开启时按次序操作：先攻击一次，再完整执行一轮升塔，交替进行。
        self.sequence_lock = threading.Lock()
        self.first_strike = threading.Event()
        self.restart_thread_id: int | None = None
        # 运行日志：按启动时间生成 logs 里的 txt 文件名，默认关闭记录，F12 或按钮随时开关。
        self.log_file = None
        self.log_path: Path | None = None
        self.log_enabled = False
        self.log_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.tower_event = threading.Event()
        self.combat_event = threading.Event()
        self.capture_target: str | None = None
        self.tower_coords: list[tuple[int, int]] = []
        self.template_cache: dict[Path, tuple[int, np.ndarray]] = {}
        self.data = dict(DEFAULTS)
        self.load_config(silent=True)
        self.make_vars()
        self.build_ui()
        self.listener = keyboard.Listener(on_press=self.on_key)
        self.listener.daemon = True
        self.listener.start()
        self.prepare_log_file()
        self.log(f"记录日志：默认关闭；按 F12 或点“记录日志 F12”开始记录到 logs/{self.log_path.name}。")
        self.log("F8 为全局急停。塔坐标不会保存，需每次启动助手后重新录入。")

    def make_vars(self) -> None:
        self.vars = {key: tk.StringVar(value=str(value)) for key, value in self.data.items()
                     if key not in {"wolf_friend", "wolf_center", "wolf_confirm", "wolf_arrow"}}
        self.tower_enabled = tk.BooleanVar(value=False)
        self.auto_enabled = tk.BooleanVar(value=False)
        self.boss_enabled = tk.BooleanVar(value=False)
        self.restart_enabled = tk.BooleanVar(value=False)
        self.topmost_enabled = tk.BooleanVar(value=False)
        self.log_enabled_var = tk.BooleanVar(value=self.log_enabled)
        self.coord_text = {name: tk.StringVar() for name in ("wolf_friend", "wolf_arrow")}
        self.refresh_coord_text()

    def build_ui(self) -> None:
        # 版本信息放在窗口最上方，独占一行，不和任何功能按钮重叠。
        header = ttk.Frame(self.root)
        header.pack(fill="x", padx=10, pady=(8, 0))
        ttk.Label(header, text="羊村挂机助手1.2", font=tkfont.Font(size=13, weight="bold")).pack(anchor="w")
        ttk.Label(header, text="作者：鹏小白就是我　整合：A_c　操刀师傅：Ai-codex",
                  foreground="#555").pack(anchor="w")
        book = ttk.Notebook(self.root)
        book.pack(fill="both", expand=True, padx=8, pady=8)
        self.tab_main = ttk.Frame(book)
        tab_config = ttk.Frame(book)
        tab_tower = ttk.Frame(book)
        tab_combat = ttk.Frame(book)
        tab_wolf = ttk.Frame(book)
        tab_restart = ttk.Frame(book)
        book.add(self.tab_main, text="启动")
        book.add(tab_config, text="配置")
        book.add(tab_tower, text="自动升塔")
        book.add(tab_combat, text="战斗设置")
        book.add(tab_wolf, text="敲狼")
        book.add(tab_restart, text="定时重开")
        self.build_main(self.tab_main)
        self.build_config(tab_config)
        self.build_tower(tab_tower)
        self.build_combat(tab_combat)
        self.build_wolf(tab_wolf)
        self.build_restart(tab_restart)

    def build_main(self, frame: ttk.Frame) -> None:
        box = ttk.LabelFrame(frame, text="启用的功能")
        box.pack(fill="x", padx=10, pady=10)
        ttk.Checkbutton(box, text="自动升塔", variable=self.tower_enabled).pack(anchor="w", padx=12, pady=5)
        self.auto_check = ttk.Checkbutton(box, text="自动攻击（小狼、Boss 都攻击）",
                                          variable=self.auto_enabled, command=self.switch_auto)
        self.auto_check.pack(anchor="w", padx=12, pady=5)
        self.boss_check = ttk.Checkbutton(box, text="漏小狼打 Boss（小狼驱赶，Boss 攻击）",
                                          variable=self.boss_enabled, command=self.switch_boss)
        self.boss_check.pack(anchor="w", padx=12, pady=5)
        ttk.Checkbutton(box, text="定时重开游戏（每天到设定时间自动重开一次）",
                        variable=self.restart_enabled).pack(anchor="w", padx=12, pady=5)
        ttk.Label(box, text="说明：两种战斗模式只能选择一个；升塔可与任一战斗模式一起运行。",
                  foreground="#666").pack(anchor="w", padx=12, pady=5)
        ttk.Label(box, text="原作脚本主体来自羊友：鹏小白就是我；整合想法来自A_c；操刀师傅：Ai-codex",
                  foreground="#666").pack(anchor="w", padx=12, pady=5)
        ttk.Checkbutton(box, text="窗口置顶（游戏点击时工具窗口仍保持显示）",
                        variable=self.topmost_enabled, command=self.toggle_topmost).pack(anchor="w", padx=12, pady=5)
        row = ttk.Frame(frame)
        row.pack(fill="x", padx=10, pady=6)
        ttk.Button(row, text="开始已选功能", command=self.start_selected).pack(side="left", padx=4)
        ttk.Button(row, text="停止全部 / F8", command=self.stop_all).pack(side="left", padx=4)
        ttk.Checkbutton(row, text="记录日志 F12", variable=self.log_enabled_var,
                        command=self.toggle_log).pack(side="left", padx=4)
        ttk.Label(frame, text="运行日志").pack(anchor="w", padx=12, pady=(12, 2))
        self.log_box = scrolledtext.ScrolledText(frame, height=20, state="disabled")
        self.log_box.pack(fill="both", expand=True, padx=10, pady=(0, 10))

    def build_config(self, frame: ttk.Frame) -> None:
        box = ttk.LabelFrame(frame, text="配置文件")
        box.pack(fill="x", padx=10, pady=12)
        ttk.Label(box, text="坐标、阈值和等待时间仅在点击保存后写入文件。\n塔坐标不会保存。",
                  justify="left").pack(anchor="w", padx=10, pady=10)
        row = ttk.Frame(box); row.pack(fill="x", padx=8, pady=(0, 10))
        ttk.Button(row, text="读取配置", command=self.load_config).pack(side="left", padx=3)
        ttk.Button(row, text="保存配置", command=self.save_config).pack(side="left", padx=3)
        ttk.Label(frame, text="安全提示：只通过界面或配置文件修改数值；\n请勿手动改 Python 脚本、模板文件名或配置字段名称。",
                  foreground="#666", justify="left").pack(anchor="w", padx=16, pady=12)

    def add_setting(self, frame: ttk.Frame, label: str, key: str, row: int, hint: str = "") -> None:
        ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", padx=10, pady=5)
        ttk.Entry(frame, textvariable=self.vars[key], width=14).grid(row=row, column=1, sticky="w", pady=5)
        if hint:
            ttk.Label(frame, text=hint, foreground="#666").grid(row=row, column=2, sticky="w", padx=6)

    def build_tower(self, frame: ttk.Frame) -> None:
        coords = ttk.LabelFrame(frame, text="本次启动的塔坐标")
        coords.pack(fill="x", padx=10, pady=10)
        self.tower_label = ttk.Label(coords, text="尚未录入塔坐标")
        self.tower_label.pack(anchor="w", padx=10, pady=5)
        row = ttk.Frame(coords); row.pack(fill="x", padx=6, pady=5)
        ttk.Button(row, text="开始录入（按 K 添加）", command=lambda: self.begin_capture("tower")).pack(side="left", padx=4)
        ttk.Button(row, text="结束录入（ESC）", command=self.end_capture).pack(side="left", padx=4)
        ttk.Button(row, text="清空塔坐标", command=self.clear_towers).pack(side="left", padx=4)
        settings = ttk.LabelFrame(frame, text="升塔参数")
        settings.pack(fill="x", padx=10, pady=8)
        self.add_setting(settings, "识图阈值", "tower_threshold", 0, "默认 0.90")
        self.add_setting(settings, "点击间隔（秒）", "tower_click_interval", 1)
        self.add_setting(settings, "每轮间隔（秒）", "tower_cycle_interval", 2, "默认 60")
        self.add_setting(settings, "加号连点次数", "tower_plus_clicks", 3, "默认 20")

    def build_combat(self, frame: ttk.Frame) -> None:
        auto = ttk.LabelFrame(frame, text="自动攻击：小狼、Boss 都攻击")
        auto.pack(fill="x", padx=10, pady=10)
        self.add_setting(auto, "识图阈值", "auto_threshold", 0, "默认 0.90")
        self.add_setting(auto, "攻击后复查（秒）", "auto_recheck_seconds", 1, "默认 60")
        self.add_setting(auto, "无怪等待（秒）", "auto_no_monster_seconds", 2, "默认 600；可自由修改")
        boss = ttk.LabelFrame(frame, text="漏小狼打 Boss：普通狼驱赶，Boss 攻击")
        boss.pack(fill="x", padx=10, pady=8)
        self.add_setting(boss, "识图阈值", "boss_threshold", 0, "默认 0.90")
        self.add_setting(boss, "检查间隔（分钟）", "boss_cycle_minutes", 1, "默认 10")

    def build_wolf(self, frame: ttk.Frame) -> None:
        point_box = ttk.LabelFrame(frame, text="点击“未录入”后，鼠标移到位置并按 K")
        point_box.pack(fill="x", padx=10, pady=10)
        labels = [("wolf_friend", "好友列表第一位"), ("wolf_arrow", "好友列表右箭头")]
        for row, (key, title) in enumerate(labels):
            ttk.Label(point_box, text=title).grid(row=row, column=0, sticky="w", padx=8, pady=5)
            ttk.Button(point_box, textvariable=self.coord_text[key], width=15,
                       command=lambda k=key: self.begin_capture(k)).grid(row=row, column=1, sticky="w", padx=6)
        setting = ttk.LabelFrame(frame, text="随机点击与等待")
        setting.pack(fill="x", padx=10, pady=8)
        rows = [("横向随机范围（像素）", "wolf_range_x"), ("纵向随机范围（像素）", "wolf_range_y"),
                ("每位好友最少点击次数", "wolf_click_min"), ("每位好友最多点击次数", "wolf_click_max"),
                ("单次点击最短间隔（秒）", "wolf_click_gap_min"), ("单次点击最长间隔（秒）", "wolf_click_gap_max"),
                ("点好友后等待（秒）", "wolf_after_friend"), ("点加速后等待（秒）", "wolf_after_jiasu"),
                ("点确定后等待（秒）", "wolf_after_confirm"),
                ("点右箭头后等待（秒）", "wolf_after_arrow")]
        for row, (label, key) in enumerate(rows): self.add_setting(setting, label, key, row)
        self.add_setting(setting, "加速按钮识图阈值", "wolf_jiasu_threshold", len(rows), "识别 jiasu.png，默认 0.90")
        self.add_setting(setting, "间谍狼识图阈值", "wolf_spy_threshold", len(rows) + 1, "识别 jiandielang.png，默认 0.90")
        ttk.Button(frame, text="开始 / 停止敲狼", command=self.toggle_wolf).pack(pady=8)
        self.wolf_event = threading.Event()

    def build_restart(self, frame: ttk.Frame) -> None:
        info = ttk.LabelFrame(frame, text="每天定时重开游戏")
        info.pack(fill="x", padx=10, pady=10)
        ttk.Label(info, text="到设定时间后依次执行：点击 bwyc.png 重开 → 等待 → 识别到 choujiang.png 就点 jiangpin.png\n"
                             "→ 等待后连点 fanpai.png → 等待后点 jinru.png → 等待后点 qianxian.png。\n"
                             "整段流程限定在设定总时长内；某一步卡住就重新从 bwyc.png 开始。\n"
                             "点 qianxian.png 之前会先停掉正在运行的功能，之后按下面的等待时间自动恢复。",
                  justify="left").pack(anchor="w", padx=10, pady=8)
        setting = ttk.LabelFrame(frame, text="时间与间隔")
        setting.pack(fill="x", padx=10, pady=8)
        rows = [("每天重开时间", "restart_time", "24 小时制 HH:MM，默认 00:01"),
                ("识图阈值", "restart_threshold", "默认 0.90"),
                ("点 bwyc.png 后等待（秒）", "restart_after_bwyc_seconds", "默认 10"),
                ("点 fanpai.png 前等待（秒）", "restart_before_fanpai_seconds", "默认 5"),
                ("fanpai.png 最多点击次数", "restart_fanpai_clicks", "默认 3"),
                ("fanpai.png 点击间隔（秒）", "restart_fanpai_gap", "默认 1"),
                ("点 jinru.png 前等待（秒）", "restart_jinru_gap", "默认 1"),
                ("点 qianxian.png 前等待（秒）", "restart_qianxian_gap", "默认 2"),
                ("单步识图最长等待（秒）", "restart_step_timeout", "超时判定卡住，默认 10"),
                ("单次重开总时长（秒）", "restart_total_seconds", "默认 60"),
                ("点 qianxian.png 后恢复等待（秒）", "restart_resume_seconds", "默认 10")]
        for row, (label, key, hint) in enumerate(rows): self.add_setting(setting, label, key, row, hint)
        ttk.Label(frame, text="所需图片：bwyc.png、choujiang.png、jiangpin.png、fanpai.png、jinru.png、qianxian.png\n"
                              "放在 图像 文件夹（同名图片优先）或 templates 文件夹内。",
                  foreground="#666", justify="left").pack(anchor="w", padx=16, pady=10)
        self.restart_event = threading.Event()

    def switch_auto(self) -> None:
        if self.auto_enabled.get():
            self.boss_enabled.set(False); self.boss_check.state(["disabled"])
        else: self.boss_check.state(["!disabled"])

    def switch_boss(self) -> None:
        if self.boss_enabled.get():
            self.auto_enabled.set(False); self.auto_check.state(["disabled"])
        else: self.auto_check.state(["!disabled"])

    def number(self, key: str, integer: bool = False) -> float | int:
        value = float(self.vars[key].get().strip())
        if value < 0: raise ValueError("不能小于 0")
        return int(value) if integer else value

    def wait(self, seconds: float, event: threading.Event) -> bool:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self.stop_event.is_set() or not event.is_set(): return False
            time.sleep(min(0.1, end - time.monotonic()))
        return True

    def take_action_turn(self, event: threading.Event) -> bool:
        """升塔与战斗共存时按次序取得操作权，避免双方抢鼠标；返回 False 表示已被停止。"""
        while event.is_set() and not self.stop_event.is_set():
            if self.sequence_lock.acquire(timeout=0.2):
                if event.is_set() and not self.stop_event.is_set(): return True
                self.sequence_lock.release()
                return False
        return False

    def release_action_turn(self) -> None:
        """释放本轮操作权。"""
        try: self.sequence_lock.release()
        except RuntimeError: pass

    def wait_first_strike(self) -> bool:
        """共存时先等战斗完成第一次攻击，再开始升塔；返回 False 表示已被停止。"""
        deadline = time.monotonic() + FIRST_STRIKE_TIMEOUT
        while not self.first_strike.is_set():
            if not self.tower_event.is_set() or self.stop_event.is_set(): return False
            if time.monotonic() > deadline:
                self.log("等待战斗首次攻击超时，直接开始本轮升塔。")
                return True
            time.sleep(0.1)
        return True

    def toggle_topmost(self) -> None:
        self.root.attributes("-topmost", self.topmost_enabled.get())
        self.log("窗口置顶已" + ("开启。" if self.topmost_enabled.get() else "关闭。"))

    def image_path(self, name: str) -> Path:
        """玩家放入“图像”文件夹的同名图片优先于内置模板。"""
        custom = IMAGE_DIR / name
        return custom if custom.is_file() else TEMPLATE_DIR / name

    def find(self, name: str, threshold: float):
        """使用 imdecode + matchTemplate，避免 OpenCV 无法读取中文路径的问题。"""
        try:
            path = self.image_path(name)
            stamp = path.stat().st_mtime_ns
            cached = self.template_cache.get(path)
            if cached is None or cached[0] != stamp:
                raw = np.frombuffer(path.read_bytes(), dtype=np.uint8)
                template = cv2.imdecode(raw, cv2.IMREAD_GRAYSCALE)
                if template is None:
                    self.log(f"图片无法读取：{path.name}")
                    return None
                self.template_cache[path] = (stamp, template)
            else:
                template = cached[1]
            screen = np.array(pyautogui.screenshot())
            gray = cv2.cvtColor(screen, cv2.COLOR_RGBA2GRAY if screen.shape[2] == 4 else cv2.COLOR_RGB2GRAY)
            height, width = template.shape[:2]
            if gray.shape[0] < height or gray.shape[1] < width:
                return None
            _min, score, _min_loc, location = cv2.minMaxLoc(cv2.matchTemplate(gray, template, cv2.TM_CCOEFF_NORMED))
            if score < threshold:
                return None
            return location[0], location[1], width, height
        except (OSError, ValueError, cv2.error) as error:
            self.log(f"识图错误（{name}）：{error}")
            return None

    def click_image(self, name: str, threshold: float) -> bool:
        location = self.find(name, threshold)
        if not location: return False
        x, y, width, height = location
        with self.click_lock: pyautogui.click(x + width // 2, y + height // 2)
        self.log(f"识别并点击：{name}")
        return True

    def wait_for_image_and_click(self, name: str, threshold: float, event: threading.Event,
                                 timeout: float | None = None) -> bool:
        """网络较慢时一直等待目标图片出现；F8 或停止按钮可随时中断。"""
        if not self.wait_for_image(name, threshold, event, timeout): return False
        return self.click_image(name, threshold)

    def wait_for_image(self, name: str, threshold: float, event: threading.Event,
                       timeout: float | None = None) -> bool:
        """等待目标图片出现；timeout 为 None 时一直等，传秒数则超时返回 False。"""
        self.log(f"等待 {name} 出现...")
        end = None if timeout is None else time.monotonic() + timeout
        while event.is_set() and not self.stop_event.is_set():
            if self.find(name, threshold): return True
            if end is not None and time.monotonic() > end: return False
            self.wait(0.3, event)
        return False

    def click_xy(self, x: int, y: int) -> None:
        with self.click_lock: pyautogui.click(x, y)

    def start_selected(self) -> None:
        if not (self.tower_enabled.get() or self.auto_enabled.get() or self.boss_enabled.get()
                or self.restart_enabled.get()):
            messagebox.showwarning("未选择功能", "请至少勾选一个功能。"); return
        if self.tower_enabled.get() and not self.tower_coords:
            messagebox.showwarning("缺少塔坐标", "请先在“自动升塔”页面录入塔坐标，建议升塔数略大于苦工数。"); return
        if self.restart_enabled.get():
            # 定时重开的图片缺失时先提示，避免到点才发现流程走不下去。
            lack = [name for name in RESTART_IMAGES if not self.image_path(name).is_file()]
            if lack:
                messagebox.showwarning("缺少定时重开图片", "请把下列图片放入 图像 或 templates 文件夹：\n" + "\n".join(lack)); return
        # 检查通过后才停止正在运行的功能，避免启动失败时把原有功能一起停掉。
        self.stop_all(silent=True); self.stop_event.clear()
        combat_on = self.auto_enabled.get() or self.boss_enabled.get()
        # 同时开启升塔和战斗时先让战斗出手：先点 gongji，再完整跑一轮升塔。
        if self.tower_enabled.get() and combat_on: self.first_strike.clear()
        else: self.first_strike.set()
        if combat_on:
            self.combat_event.set()
            target = self.auto_loop if self.auto_enabled.get() else self.boss_loop
            threading.Thread(target=target, daemon=True).start()
        if self.tower_enabled.get():
            self.tower_event.set(); threading.Thread(target=self.tower_loop, daemon=True).start()
        if self.restart_enabled.get():
            self.restart_event.set(); threading.Thread(target=self.restart_loop, daemon=True).start()
        self.log("已启动所选功能。")

    def stop_all(self, silent: bool = False) -> None:
        self.stop_event.set(); self.tower_event.clear(); self.combat_event.clear()
        if hasattr(self, "wolf_event"): self.wolf_event.clear()
        if not silent: self.log("已停止全部自动操作。")

    def tower_loop(self) -> None:
        try:
            threshold = float(self.number("tower_threshold")); gap = float(self.number("tower_click_interval"))
            cycle = float(self.number("tower_cycle_interval")); plus = int(self.number("tower_plus_clicks", True))
            # 与战斗同时开启时先让战斗出手：先 gongji，再完整跑一轮升塔。
            if self.combat_event.is_set():
                self.log("已同时开启战斗，先等战斗攻击一次，再开始本轮升塔。")
            if not self.wait_first_strike(): return
            while self.tower_event.is_set() and not self.stop_event.is_set():
                # 一整轮升塔期间独占操作权，战斗线程会等本轮结束后再攻击。
                if not self.take_action_turn(self.tower_event): break
                try:
                    for x, y in list(self.tower_coords):
                        if not self.tower_event.is_set(): break
                        self.click_xy(x, y); self.log(f"点击塔：({x}, {y})")
                        if not self.wait(gap, self.tower_event): break
                        # 每座塔必须完整执行：升級 → 力量 → 加号连点 → 确定 → 叉号，才会切换下一座塔。
                        # 即使中途没有识别到按钮，也尝试点叉号回到塔列表，避免卡在上一座塔的界面。
                        completed = True
                        for image in ("shengji.png", "liliang.png", "jiahao.png"):
                            if not self.click_image(image, threshold):
                                if image == "jiahao.png":
                                    self.log("本座塔未识别到 jiahao.png，点击叉号后切换下一座塔。")
                                else:
                                    self.log(f"本座塔未识别到 {image}，点击叉号后切换下一座塔。")
                                completed = False
                                break
                            if image == "jiahao.png":
                                with self.click_lock: pyautogui.click(clicks=plus, interval=0.01)
                            if not self.wait(gap, self.tower_event):
                                completed = False
                                break
                        if completed and self.tower_event.is_set():
                            if not self.click_image("queding.png", threshold):
                                self.log("本座塔未识别到 queding.png。")
                        if self.tower_event.is_set():
                            self.click_image("tacha.png", threshold)
                            self.wait(gap, self.tower_event)
                finally:
                    self.release_action_turn()
                if self.tower_event.is_set():
                    self.log(f"升塔本轮完成，等待 {cycle:g} 秒。")
                    self.wait(cycle, self.tower_event)
        except ValueError as error: self.log(f"升塔参数错误：{error}")

    def close_popups(self, threshold: float, boss: bool = False) -> None:
        names = (("bosscha.png", "cha.png", "queding.png") if boss else ("queding.png", "tacha.png"))
        for name in names:
            self.click_image(name, threshold); time.sleep(0.3)

    def wulang_state(self, threshold: float) -> bool:
        """识别 wulang.png 判断是否处于无狼状态。"""
        return self.find("wulang.png", threshold) is not None

    def auto_loop(self) -> None:
        try:
            threshold = float(self.number("auto_threshold")); retry = float(self.number("auto_recheck_seconds")); empty = float(self.number("auto_no_monster_seconds"))
            while self.combat_event.is_set() and not self.stop_event.is_set():
                # 升塔正在跑本轮时在这里等待，等本轮升塔结束后再攻击，避免双方抢鼠标。
                if not self.take_action_turn(self.combat_event): break
                try:
                    self.close_popups(threshold)
                    # 识别到 wulang.png 说明当前没有可攻击目标，直接跳过攻击，避免 gongji.png 误识别。
                    no_monster = self.wulang_state(threshold)
                    attacked = False if no_monster else self.click_image("gongji.png", threshold)
                finally:
                    self.release_action_turn()
                # 第一次攻击已经出手，允许升塔开始本轮。
                self.first_strike.set()
                if no_monster:
                    self.log(f"识别到无狼状态，未发现可攻击目标，等待 {empty:g} 秒。"); self.wait(empty, self.combat_event)
                elif attacked:
                    self.log(f"已攻击，{retry:g} 秒后复查。"); self.wait(retry, self.combat_event)
                else:
                    self.log(f"未发现可攻击目标，等待 {empty:g} 秒。"); self.wait(empty, self.combat_event)
        except ValueError as error: self.log(f"自动攻击参数错误：{error}")

    def boss_loop(self) -> None:
        try:
            threshold = float(self.number("boss_threshold")); minutes = float(self.number("boss_cycle_minutes"))
            while self.combat_event.is_set() and not self.stop_event.is_set():
                # 升塔正在跑本轮时在这里等待，等本轮升塔结束后再检查 Boss，避免双方抢鼠标。
                if not self.take_action_turn(self.combat_event): break
                try:
                    self.close_popups(threshold, boss=True)
                    if self.find("boss.png", threshold) or self.find("4boss.png", threshold):
                        self.log("识别到 Boss 状态，点击攻击按钮。")
                        # Boss 状态切换到攻击按钮可能受网络延迟影响，不能只等固定 0.5 秒。
                        if not self.wait_for_image_and_click("gongji.png", threshold, self.combat_event):
                            break
                    elif any(self.find(name, threshold) for name in ("yiban.png", "putong.png", "kunnan.png")):
                        self.log("识别到普通狼状态，点击驱赶按钮。")
                        self.wait(0.5, self.combat_event); self.click_image("qugan.png", threshold)
                        self.wait(0.5, self.combat_event); self.click_image("queding.png", threshold)
                    else: self.log("未找到 Boss 或普通狼状态。")
                finally:
                    self.release_action_turn()
                # 第一次检查已经出手，允许升塔开始本轮。
                self.first_strike.set()
                self.log(f"Boss 检查完成，等待 {minutes:g} 分钟。")
                self.wait(minutes * 60, self.combat_event)
        except ValueError as error: self.log(f"Boss 模式参数错误：{error}")

    def restart_loop(self) -> None:
        """每天到设定时间执行一次定时重开游戏。"""
        my_id = threading.get_ident()
        self.restart_thread_id = my_id   # 新线程接管后，旧的定时重开线程会自己退出
        fired_date = ""
        self.log("定时重开已启动，到点后会自动重开游戏。")
        while self.restart_event.is_set() and not self.stop_event.is_set():
            if self.restart_thread_id != my_id: return
            target = self.vars["restart_time"].get().strip()
            try:
                # 同时把用户输入的 "0:01" 这类写法规范成 00:01，方便和系统时间比较。
                target = time.strftime("%H:%M", time.strptime(target, "%H:%M"))
            except ValueError:
                self.log("定时重开时间格式错误，已停止定时重开；请按 24 小时制填写，例如 00:01。")
                self.restart_event.clear()
                self.root.after(0, lambda: self.restart_enabled.set(False))
                return
            now = time.localtime()
            today = time.strftime("%Y-%m-%d", now)
            if time.strftime("%H:%M", now) == target and today != fired_date:
                fired_date = today
                self.log(f"系统时间到达 {target}，开始定时重开游戏。")
                self.restart_sequence()
            if not self.wait(1.0, self.restart_event): break
        self.log("定时重开已停止。")

    def restart_sequence(self) -> None:
        """执行一次定时重开：先停掉正在运行的功能，重开游戏，再恢复原本开启的功能。"""
        try:
            threshold = float(self.number("restart_threshold"))
            fanpai_times = int(self.number("restart_fanpai_clicks", True))
            fanpai_gap = float(self.number("restart_fanpai_gap"))
            after_bwyc = float(self.number("restart_after_bwyc_seconds"))
            before_fanpai = float(self.number("restart_before_fanpai_seconds"))
            jinru_gap = float(self.number("restart_jinru_gap"))
            qianxian_gap = float(self.number("restart_qianxian_gap"))
            step_timeout = float(self.number("restart_step_timeout"))
            total_seconds = float(self.number("restart_total_seconds"))
            resume_seconds = float(self.number("restart_resume_seconds"))
        except ValueError as error:
            self.log(f"定时重开参数错误：{error}"); return
        # 记录当前正在运行的功能，稍后只恢复这些；原本关闭的功能保持关闭。
        was_tower, was_combat, was_wolf = self.tower_event.is_set(), self.combat_event.is_set(), self.wolf_event.is_set()
        self.log("定时重开：先停止正在运行的功能。")
        self.tower_event.clear(); self.combat_event.clear(); self.wolf_event.clear()
        # 等其它功能收尾并独占操作权，避免重开流程和它们互相抢鼠标。
        if not self.sequence_lock.acquire(timeout=5.0): self.log("等待其它功能停下超时，继续执行定时重开。")
        try:
            self.restart_attempts(threshold, fanpai_times, fanpai_gap, after_bwyc, before_fanpai,
                                  jinru_gap, qianxian_gap, step_timeout, total_seconds)
        finally:
            self.release_action_turn()
        # 点 qianxian.png 之后等待设定时间，再把原本开启的功能恢复回来。
        if resume_seconds > 0 and not self.wait(resume_seconds, self.restart_event):
            self.log("定时重开已中断，不再自动恢复功能。"); return
        self.resume_features(was_tower, was_combat, was_wolf)

    def restart_attempts(self, threshold: float, fanpai_times: int, fanpai_gap: float, after_bwyc: float,
                         before_fanpai: float, jinru_gap: float, qianxian_gap: float,
                         step_timeout: float, total_seconds: float) -> bool:
        """在总时长内反复尝试重开；某一步卡住就重新从 bwyc.png 开始。"""
        deadline = time.monotonic() + total_seconds
        attempt = 0
        while True:
            if self.stop_event.is_set() or not self.restart_event.is_set(): return False
            if time.monotonic() >= deadline:
                self.log(f"定时重开超过 {total_seconds:g} 秒仍未完成，放弃本次重开。")
                return False
            attempt += 1
            if attempt > 1: self.log(f"上一次重开没走完，重新从 bwyc.png 开始（第 {attempt} 次尝试）。")
            if self.restart_once(threshold, fanpai_times, fanpai_gap, after_bwyc, before_fanpai,
                                 jinru_gap, qianxian_gap, step_timeout):
                self.log("定时重开流程完成。")
                return True

    def restart_once(self, threshold: float, fanpai_times: int, fanpai_gap: float, after_bwyc: float,
                     before_fanpai: float, jinru_gap: float, qianxian_gap: float, step_timeout: float) -> bool:
        """走一次重开流程：重开 → 领奖 → 翻牌 → 进入 → 前线；某一步没识别到就返回 False。"""
        self.log("点击 bwyc.png 重开游戏。")
        if not self.wait_for_image_and_click("bwyc.png", threshold, self.restart_event, step_timeout):
            self.log("未识别到 bwyc.png，本次重开没走完。"); return False
        # 点完 bwyc.png 先等游戏重开，再去找抽奖界面。
        if not self.wait(after_bwyc, self.restart_event): return False
        if not self.wait_for_image("choujiang.png", threshold, self.restart_event, step_timeout):
            self.log("未识别到 choujiang.png，本次重开没走完。"); return False
        if not self.wait_for_image_and_click("jiangpin.png", threshold, self.restart_event, step_timeout):
            self.log("未识别到 jiangpin.png，本次重开没走完。"); return False
        # 等一会再点 fanpai.png；最多点 fanpai_times 次，每次间隔 fanpai_gap 秒。
        if not self.wait(before_fanpai, self.restart_event): return False
        for count in range(max(fanpai_times, 0)):
            if count and not self.wait(fanpai_gap, self.restart_event): return False
            if not self.click_image("fanpai.png", threshold): break
        # 不管 fanpai.png 点了几次，都继续往下走。
        if not self.wait(jinru_gap, self.restart_event): return False
        if not self.wait_for_image_and_click("jinru.png", threshold, self.restart_event, step_timeout):
            self.log("未识别到 jinru.png，本次重开没走完。"); return False
        if not self.wait(qianxian_gap, self.restart_event): return False
        if not self.wait_for_image_and_click("qianxian.png", threshold, self.restart_event, step_timeout):
            self.log("未识别到 qianxian.png，本次重开没走完。"); return False
        return True

    def resume_features(self, was_tower: bool, was_combat: bool, was_wolf: bool) -> None:
        """恢复定时重开前正在运行的功能；原本关闭的功能保持关闭。"""
        self.log("定时重开流程结束，恢复原本开启的功能。")
        def resume() -> None:
            if was_wolf and not self.wolf_event.is_set(): self.toggle_wolf()
            if (was_tower or was_combat) and (self.tower_enabled.get() or self.auto_enabled.get() or self.boss_enabled.get()):
                self.start_selected()
            elif was_tower or was_combat:
                self.log("原本开启的功能已被取消勾选，本次不恢复。")
        self.root.after(0, resume)

    def toggle_wolf(self) -> None:
        if self.wolf_event.is_set(): self.wolf_event.clear(); self.log("敲狼已停止。"); return
        try:
            coords = [self.data[key] for key in ("wolf_friend", "wolf_arrow")]
            if any(not point for point in coords): raise ValueError("请先录入好友第一位和右箭头坐标")
            if not self.image_path("jiandielang.png").is_file():
                raise ValueError("缺少命名为 jiandielang.png 的间谍狼图片，请放入“图像”文件夹，或保留 templates 文件夹内的同名模板")
            self.stop_event.clear(); self.wolf_event.set(); threading.Thread(target=self.wolf_loop, daemon=True).start(); self.log("敲狼已启动。")
        except ValueError as error: messagebox.showwarning("无法启动敲狼", str(error))

    def wolf_loop(self) -> None:
        try:
            rx, ry = int(self.number("wolf_range_x", True)), int(self.number("wolf_range_y", True))
            least, most = int(self.number("wolf_click_min", True)), int(self.number("wolf_click_max", True))
            if least < 1 or most < least: raise ValueError("随机点击次数范围不正确")
            g1, g2 = float(self.number("wolf_click_gap_min")), float(self.number("wolf_click_gap_max"))
            if g2 < g1: raise ValueError("点击间隔范围不正确")
            threshold = float(self.number("wolf_spy_threshold"))
            jiasu_threshold = float(self.number("wolf_jiasu_threshold"))
            friend, arrow = (self.data[k] for k in ("wolf_friend", "wolf_arrow"))
            while self.wolf_event.is_set() and not self.stop_event.is_set():
                self.click_xy(*friend)
                if not self.wait(float(self.number("wolf_after_friend")), self.wolf_event): break
                # 进入好友界面后先加速好友矿产：识别到 jiasu.png 才点，等待后再点确定。
                if self.click_image("jiasu.png", jiasu_threshold):
                    if not self.wait(float(self.number("wolf_after_jiasu")), self.wolf_event): break
                    if not self.click_image("queding.png", threshold): self.log("未识别到 queding.png。")
                else:
                    self.log("未识别到 jiasu.png，跳过加速好友矿产。")
                # 间谍狼图片是动态点击区域：找到才敲；没找到就跳过确定，直接翻下一位好友。
                spy = self.find("jiandielang.png", threshold)
                if spy:
                    left, top, width, height = spy
                    center_x, center_y = left + width // 2, top + height // 2
                    low_x, high_x = max(left, center_x - rx), min(left + width - 1, center_x + rx)
                    low_y, high_y = max(top, center_y - ry), min(top + height - 1, center_y + ry)
                    self.log("识别到间谍狼，在图片范围内随机敲狼。")
                    for _ in range(random.randint(least, most)):
                        self.click_xy(random.randint(low_x, high_x), random.randint(low_y, high_y))
                        if not self.wait(random.uniform(g1, g2), self.wolf_event): break
                    if not self.wait_for_image_and_click("queding.png", threshold, self.wolf_event):
                        break
                else:
                    self.log("未识别到间谍狼，跳过敲狼并切换下一位好友。")
                self.click_xy(*arrow)
                self.wait(float(self.number("wolf_after_arrow")), self.wolf_event)
        except ValueError as error: self.log(f"敲狼参数错误：{error}"); self.wolf_event.clear()

    def begin_capture(self, target: str) -> None:
        self.capture_target = target
        self.log("请把鼠标移动到目标位置，然后按 K；按 ESC 取消。")

    def end_capture(self) -> None:
        self.capture_target = None; self.log("已结束坐标录入。")

    def on_key(self, key) -> None:
        if key == keyboard.Key.f8:
            self.root.after(0, self.stop_all); return
        if key == keyboard.Key.f12:
            self.root.after(0, self.toggle_log); return
        if key == keyboard.Key.esc and self.capture_target:
            self.root.after(0, self.end_capture); return
        if (getattr(key, "char", None) or "").lower() == "k" and self.capture_target:
            point = pyautogui.position(); target = self.capture_target
            self.root.after(0, lambda: self.record_point(target, (point.x, point.y)))

    def record_point(self, target: str, point: tuple[int, int]) -> None:
        if target == "tower":
            self.tower_coords.append(point); self.tower_label.config(text=f"已录入 {len(self.tower_coords)} 个塔坐标")
            self.log(f"已录入塔坐标：{point}")
        else:
            self.data[target] = point; self.refresh_coord_text(); self.capture_target = None
            self.log(f"已录入 {target}：{point}")

    def clear_towers(self) -> None:
        self.tower_coords.clear(); self.tower_label.config(text="尚未录入塔坐标"); self.log("已清空塔坐标。")

    def refresh_coord_text(self) -> None:
        for key, variable in self.coord_text.items():
            point = self.data.get(key); variable.set("未录入" if not point else f"({point[0]}, {point[1]})")

    def config_from_ui(self) -> dict:
        result = dict(DEFAULTS)
        for key, variable in self.vars.items(): result[key] = variable.get().strip()
        for key in self.coord_text: result[key] = self.data.get(key)
        return result

    def save_config(self) -> None:
        try:
            CONFIG_FILE.write_text(json.dumps(self.config_from_ui(), ensure_ascii=False, indent=2), encoding="utf-8")
            self.log(f"配置已手动保存：{CONFIG_FILE.name}")
        except OSError as error: messagebox.showerror("保存失败", str(error))

    def load_config(self, silent: bool = False) -> None:
        if not CONFIG_FILE.exists():
            if not silent: self.log("还没有已保存的配置。")
            return
        try:
            saved = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            self.data.update({key: value for key, value in saved.items() if key in DEFAULTS})
            if hasattr(self, "vars"):
                for key, variable in self.vars.items(): variable.set(str(self.data[key]))
                self.refresh_coord_text()
            if not silent: self.log("已读取手动保存的配置。")
        except (OSError, json.JSONDecodeError) as error:
            if not silent: messagebox.showerror("读取失败", str(error))

    def log(self, text: str, force: bool = False) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.write_log_file(f"[{stamp}] {text}\n", force)
        if not hasattr(self, "log_box"): return
        def write() -> None:
            self.log_box.configure(state="normal")
            self.log_box.insert("end", f"[{stamp}] {text}\n")
            self.log_box.see("end"); self.log_box.configure(state="disabled")
        try: self.root.after(0, write)
        except tk.TclError: pass

    def prepare_log_file(self) -> None:
        """按启动时间定好本次运行的日志文件名（24 小时制，精确到秒）。"""
        self.log_path = LOG_DIR / f"{time.strftime('%Y-%m-%d_%H-%M-%S')}.txt"

    def open_log_file(self) -> bool:
        """真正开始记录时才创建日志文件；已经打开就直接复用。"""
        if self.log_file is not None: return True
        try:
            LOG_DIR.mkdir(exist_ok=True)
            self.log_file = self.log_path.open("a", encoding="utf-8")
            return True
        except OSError as error:
            self.log_file = None
            self.log(f"创建运行日志失败：{error}")
            return False

    def write_log_file(self, line: str, force: bool = False) -> None:
        """把一行日志写进日志文件；关闭记录时不写，只有开关本身例外。"""
        if self.log_path is None or not (self.log_enabled or force): return
        if not self.open_log_file(): return
        try:
            with self.log_lock:
                self.log_file.write(line); self.log_file.flush()
        except (OSError, ValueError): pass

    def toggle_log(self) -> None:
        """F12 或「记录日志 F12」按钮：开关运行日志，界面日志照常显示。"""
        state = "关闭" if self.log_enabled else "开启"
        self.log_enabled = not self.log_enabled
        self.log_enabled_var.set(self.log_enabled)
        self.log(f"运行日志已{state}，文件：logs/{self.log_path.name}。", force=True)

    def close(self) -> None:
        self.stop_all(silent=True); self.listener.stop()
        self.log(f"本次运行结束，日志文件：logs/{self.log_path.name}。" if self.log_enabled else "本次运行结束（本次未记录日志）。")
        if self.log_file:
            try: self.log_file.close()
            except OSError: pass
        self.root.destroy()


def main() -> None:
    missing = extract_embedded_templates()
    if DEPENDENCY_ERROR:
        raise SystemExit("缺少依赖，请在本文件所在目录运行：pip install -r requirements.txt") from DEPENDENCY_ERROR
    root = tk.Tk()
    if missing:
        messagebox.showerror("缺少识图模板", "无法取得模板：\n" + "\n".join(missing) + "\n\n请保留 图像 文件夹，或先准备 templates 文件夹。")
        root.destroy(); return
    AssistantApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
