"""保卫羊村挂机助手（测试版）。

首次运行会从同目录 sources 中的两份原始 EXE 提取内置识图模板到 templates；
不会修改原始 EXE。打包前可直接将 templates 一并打包。
"""
from __future__ import annotations

import json
import random
import struct
import threading
import time
import zlib
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

try:
    import cv2
    import numpy as np
    import pyautogui
    from pynput import keyboard
except ImportError as error:
    raise SystemExit("缺少依赖，请在本文件所在目录运行：pip install -r requirements.txt") from error


ROOT = Path(__file__).resolve().parent
TEMPLATE_DIR = ROOT / "templates"
IMAGE_DIR = ROOT / "图像"
CONFIG_FILE = ROOT / "羊村挂机配置.json"
NEEDED_TEMPLATES = {
    "shengji.png", "liliang.png", "jiahao.png", "queding.png", "tacha.png",
    "gongji.png", "bosscha.png", "cha.png", "boss.png", "4boss.png",
    "yiban.png", "putong.png", "kunnan.png", "qugan.png",
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
}


def extract_embedded_templates() -> list[str]:
    """读取 PyInstaller CArchive；仅把 PNG 模板复制到本工具自己的 templates 文件夹。"""
    TEMPLATE_DIR.mkdir(exist_ok=True)
    IMAGE_DIR.mkdir(exist_ok=True)
    missing = {name for name in NEEDED_TEMPLATES if not (TEMPLATE_DIR / name).exists()}
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
        self.root.title("保卫羊村挂机助手（测试版）")
        self.root.geometry("380x760")
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.click_lock = threading.Lock()
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
        self.log("F8 为全局急停。塔坐标不会保存，需每次地图变化后重新录入。")

    def make_vars(self) -> None:
        self.vars = {key: tk.StringVar(value=str(value)) for key, value in self.data.items()
                     if key not in {"wolf_friend", "wolf_center", "wolf_confirm", "wolf_arrow"}}
        self.tower_enabled = tk.BooleanVar(value=False)
        self.auto_enabled = tk.BooleanVar(value=False)
        self.boss_enabled = tk.BooleanVar(value=False)
        self.topmost_enabled = tk.BooleanVar(value=False)
        self.coord_text = {name: tk.StringVar() for name in ("wolf_friend", "wolf_arrow")}
        self.refresh_coord_text()

    def build_ui(self) -> None:
        book = ttk.Notebook(self.root)
        book.pack(fill="both", expand=True, padx=8, pady=8)
        self.tab_main = ttk.Frame(book)
        tab_config = ttk.Frame(book)
        tab_tower = ttk.Frame(book)
        tab_combat = ttk.Frame(book)
        tab_wolf = ttk.Frame(book)
        book.add(self.tab_main, text="启动")
        book.add(tab_config, text="配置")
        book.add(tab_tower, text="自动升塔")
        book.add(tab_combat, text="战斗设置")
        book.add(tab_wolf, text="敲狼")
        self.build_main(self.tab_main)
        self.build_config(tab_config)
        self.build_tower(tab_tower)
        self.build_combat(tab_combat)
        self.build_wolf(tab_wolf)

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
        ttk.Label(box, text="说明：两种战斗模式只能选择一个；升塔可与任一战斗模式一起运行。",
                  foreground="#666").pack(anchor="w", padx=12, pady=5)
        ttk.Checkbutton(box, text="窗口置顶（游戏点击时工具窗口仍保持显示）",
                        variable=self.topmost_enabled, command=self.toggle_topmost).pack(anchor="w", padx=12, pady=5)
        row = ttk.Frame(frame)
        row.pack(fill="x", padx=10, pady=6)
        ttk.Button(row, text="开始已选功能", command=self.start_selected).pack(side="left", padx=4)
        ttk.Button(row, text="停止全部 / F8", command=self.stop_all).pack(side="left", padx=4)
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
        coords = ttk.LabelFrame(frame, text="本次地图的塔坐标（不会保存）")
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
                ("点好友后等待（秒）", "wolf_after_friend"), ("点确定后等待（秒）", "wolf_after_confirm"),
                ("点右箭头后等待（秒）", "wolf_after_arrow")]
        for row, (label, key) in enumerate(rows): self.add_setting(setting, label, key, row)
        self.add_setting(setting, "间谍狼识图阈值", "wolf_spy_threshold", len(rows), "需上传 jiandielang.png，默认 0.90")
        ttk.Button(frame, text="开始 / 停止敲狼", command=self.toggle_wolf).pack(pady=8)
        self.wolf_event = threading.Event()

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

    def wait_for_image_and_click(self, name: str, threshold: float, event: threading.Event) -> bool:
        """网络较慢时一直等待目标图片出现；F8 或停止按钮可随时中断。"""
        self.log(f"等待 {name} 出现...")
        while event.is_set() and not self.stop_event.is_set():
            if self.click_image(name, threshold):
                return True
            self.wait(0.3, event)
        return False

    def click_xy(self, x: int, y: int) -> None:
        with self.click_lock: pyautogui.click(x, y)

    def start_selected(self) -> None:
        if not (self.tower_enabled.get() or self.auto_enabled.get() or self.boss_enabled.get()):
            messagebox.showwarning("未选择功能", "请至少勾选一个功能。"); return
        self.stop_all(silent=True); self.stop_event.clear()
        if self.tower_enabled.get():
            if not self.tower_coords: messagebox.showwarning("缺少塔坐标", "请先在“自动升塔”页面录入塔坐标。"); return
            self.tower_event.set(); threading.Thread(target=self.tower_loop, daemon=True).start()
        if self.auto_enabled.get() or self.boss_enabled.get():
            self.combat_event.set()
            target = self.auto_loop if self.auto_enabled.get() else self.boss_loop
            threading.Thread(target=target, daemon=True).start()
        self.log("已启动所选功能。")

    def stop_all(self, silent: bool = False) -> None:
        self.stop_event.set(); self.tower_event.clear(); self.combat_event.clear()
        if hasattr(self, "wolf_event"): self.wolf_event.clear()
        if not silent: self.log("已停止全部自动操作。")

    def tower_loop(self) -> None:
        try:
            threshold = float(self.number("tower_threshold")); gap = float(self.number("tower_click_interval"))
            cycle = float(self.number("tower_cycle_interval")); plus = int(self.number("tower_plus_clicks", True))
            while self.tower_event.is_set() and not self.stop_event.is_set():
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
                if self.tower_event.is_set():
                    self.log(f"升塔本轮完成，等待 {cycle:g} 秒。")
                    self.wait(cycle, self.tower_event)
        except ValueError as error: self.log(f"升塔参数错误：{error}")

    def close_popups(self, threshold: float, boss: bool = False) -> None:
        names = (("bosscha.png", "cha.png", "queding.png") if boss else ("queding.png", "tacha.png"))
        for name in names:
            self.click_image(name, threshold); time.sleep(0.3)

    def auto_loop(self) -> None:
        try:
            threshold = float(self.number("auto_threshold")); retry = float(self.number("auto_recheck_seconds")); empty = float(self.number("auto_no_monster_seconds"))
            while self.combat_event.is_set() and not self.stop_event.is_set():
                self.close_popups(threshold)
                if self.click_image("gongji.png", threshold):
                    self.log(f"已攻击，{retry:g} 秒后复查。"); self.wait(retry, self.combat_event)
                else:
                    self.log(f"未发现可攻击目标，等待 {empty:g} 秒。"); self.wait(empty, self.combat_event)
        except ValueError as error: self.log(f"自动攻击参数错误：{error}")

    def boss_loop(self) -> None:
        try:
            threshold = float(self.number("boss_threshold")); minutes = float(self.number("boss_cycle_minutes"))
            while self.combat_event.is_set() and not self.stop_event.is_set():
                self.close_popups(threshold, boss=True)
                # boss / 4boss 与普通狼图片只是“状态标记”，只识别、绝不点击它们。
                # 真正需要点击的是 gongji（攻击）或 qugan（驱赶）按钮。
                if self.find("boss.png", threshold) or self.find("4boss.png", threshold):
                    self.log("识别到 Boss 状态，等待攻击按钮出现。")
                    # Boss 状态切换到攻击按钮可能受网络延迟影响，不能只等固定 0.5 秒。
                    if not self.wait_for_image_and_click("gongji.png", threshold, self.combat_event):
                        break
                elif any(self.find(name, threshold) for name in ("yiban.png", "putong.png", "kunnan.png")):
                    self.log("识别到普通狼状态，准备点击驱赶按钮。")
                    self.wait(0.5, self.combat_event); self.click_image("qugan.png", threshold)
                    self.wait(0.5, self.combat_event); self.click_image("queding.png", threshold)
                else: self.log("未找到 Boss 或普通狼状态。")
                self.log(f"Boss 检查完成，等待 {minutes:g} 分钟。")
                self.wait(minutes * 60, self.combat_event)
        except ValueError as error: self.log(f"Boss 模式参数错误：{error}")

    def toggle_wolf(self) -> None:
        if self.wolf_event.is_set(): self.wolf_event.clear(); self.log("敲狼已停止。"); return
        try:
            coords = [self.data[key] for key in ("wolf_friend", "wolf_arrow")]
            if any(not point for point in coords): raise ValueError("请先录入好友第一位和右箭头坐标")
            if not (IMAGE_DIR / "jiandielang.png").is_file():
                raise ValueError("请先把间谍狼图片命名为 jiandielang.png，并放入“图像”文件夹")
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
            friend, arrow = (self.data[k] for k in ("wolf_friend", "wolf_arrow"))
            while self.wolf_event.is_set() and not self.stop_event.is_set():
                self.click_xy(*friend)
                if not self.wait(float(self.number("wolf_after_friend")), self.wolf_event): break
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
        if key == keyboard.Key.esc and self.capture_target:
            self.root.after(0, self.end_capture); return
        if getattr(key, "char", "").lower() == "k" and self.capture_target:
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

    def log(self, text: str) -> None:
        if not hasattr(self, "log_box"): return
        def write() -> None:
            self.log_box.configure(state="normal")
            self.log_box.insert("end", f"[{time.strftime('%H:%M:%S')}] {text}\n")
            self.log_box.see("end"); self.log_box.configure(state="disabled")
        try: self.root.after(0, write)
        except tk.TclError: pass

    def close(self) -> None:
        self.stop_all(silent=True); self.listener.stop(); self.root.destroy()


def main() -> None:
    missing = extract_embedded_templates()
    root = tk.Tk()
    if missing:
        messagebox.showerror("缺少识图模板", "无法取得模板：\n" + "\n".join(missing) + "\n\n请保留 sources 文件夹，或先准备 templates 文件夹。")
        root.destroy(); return
    AssistantApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
