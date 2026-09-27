# -*- coding: utf-8 -*-
"""PiRadioBox: a 480 x 320 internet radio, with a windowed desktop mode."""
import argparse
import datetime
import logging
from logging.handlers import RotatingFileHandler
import math
from pathlib import Path
import time

from radio_core import load_config, load_settings, save_settings

BASE = Path(__file__).resolve().parent
LOG = logging.getLogger(__name__)
WIDTH, HEIGHT = 480, 320
BG = (14, 18, 26)
CARD = (25, 33, 46)
ACTIVE = (40, 52, 68)
WHITE = (235, 241, 247)
MUTED = (139, 156, 175)
ACCENT = (255, 143, 96)
CYAN = (90, 213, 220)
GREEN = (93, 213, 143)
RED = (244, 102, 102)


class SystemStats:
    def __init__(self):
        import psutil
        self.psutil = psutil
        self.updated = -math.inf
        self.previous_bytes = None
        self.cpu = self.ram = self.speed = 0
        self.temp = None

    def update(self, now):
        if now - self.updated < 1:
            return
        try:
            self.cpu = self.psutil.cpu_percent()
            self.ram = self.psutil.virtual_memory().percent
            counters = self.psutil.net_io_counters()
            if counters is not None:
                if self.previous_bytes is not None:
                    self.speed = max(0, counters.bytes_recv - self.previous_bytes) / (now - self.updated) / 1024
                self.previous_bytes = counters.bytes_recv
        except Exception:
            LOG.debug("读取系统状态失败", exc_info=True)
        self.updated = now
        try:
            self.temp = float(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000
        except (OSError, ValueError):
            self.temp = None


def find_font(pg, configured):
    candidates = [configured, "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
                  "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
                  "C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf",
                  "/System/Library/Fonts/PingFang.ttc"]
    for path in candidates:
        if path and Path(path).is_file():
            return path
    for name in ("notosanscjk", "wenquanyimicrohei", "microsoftyahei", "simhei"):
        path = pg.font.match_font(name)
        if path:
            return path
    raise RuntimeError("缺少中文字体：安装 fonts-wqy-microhei，或在 config.json 设置 font_path")


class RadioApp:
    def __init__(self, pg, config, settings, state_path, radio, meter):
        self.pg, self.config, self.settings = pg, config, settings
        self.state_path, self.radio, self.meter = state_path, radio, meter
        flags = pg.FULLSCREEN | pg.NOFRAME if config["fullscreen"] else 0
        self.screen = pg.display.set_mode((WIDTH, HEIGHT), flags)
        pg.display.set_caption("PiRadioBox")
        font_path = find_font(pg, config["font_path"])
        self.fonts = {size: pg.font.Font(font_path, size) for size in (11, 12, 14, 16, 20, 26, 62)}
        self.stations = config["stations"]
        self.index = next(i for i, s in enumerate(self.stations) if s["url"] == settings["station_url"])
        self.favorites = set(settings["favorites"])
        self.only_favorites = False
        self.offset = 0.0
        self.list_rect = pg.Rect(8, 45, 154, 228)
        self.vol_rect = pg.Rect(181, 215, 285, 42)
        self.buttons = {
            "filter": pg.Rect(8, 8, 154, 30),
            "sleep": pg.Rect(250, 8, 68, 30),
            "night": pg.Rect(324, 8, 42, 30),
            "info": pg.Rect(372, 8, 42, 30),
            "exit": pg.Rect(420, 8, 52, 30),
            "favorite": pg.Rect(436, 45, 36, 36),
            "up": pg.Rect(8, 280, 73, 32),
            "down": pg.Rect(89, 280, 73, 32),
            "prev": pg.Rect(174, 264, 82, 48),
            "play": pg.Rect(263, 264, 121, 48),
            "next": pg.Rect(391, 264, 81, 48),
        }
        self.running = True
        self.night = self.info = False
        self.drag = None
        self.press = None
        self.scroll_start = 0
        self.moved = False
        self.dirty_at = None
        self.save_failed = False
        self.toast, self.toast_until = "", 0
        self.sleep_choice = 0
        self.sleep_requested_at = 0
        self.stats = SystemStats()
        self.snapshot = radio.snapshot()
        self.ensure_visible()

    def visible_indices(self):
        return [i for i, s in enumerate(self.stations)
                if not self.only_favorites or s["url"] in self.favorites]

    def clamp_scroll(self):
        limit = max(0, len(self.visible_indices()) * 38 - self.list_rect.height)
        self.offset = max(0, min(limit, self.offset))

    def ensure_visible(self):
        indices = self.visible_indices()
        if self.index in indices:
            y = indices.index(self.index) * 38
            if y < self.offset:
                self.offset = y
            elif y + 38 > self.offset + self.list_rect.height:
                self.offset = y + 38 - self.list_rect.height
        self.clamp_scroll()

    def dirty(self):
        self.dirty_at = time.monotonic()

    def notify(self, message):
        self.toast, self.toast_until = message, time.monotonic() + 3

    def persist(self, force=False):
        if self.dirty_at is None or (not force and time.monotonic() - self.dirty_at < 0.8):
            return
        self.settings["favorites"] = [s["url"] for s in self.stations if s["url"] in self.favorites]
        try:
            save_settings(self.state_path, self.settings)
            self.dirty_at = None
            self.save_failed = False
        except OSError:
            if not self.save_failed:
                LOG.exception("保存设置失败")
                self.notify("设置保存失败 · 检查目录权限")
            self.save_failed = True
            self.dirty_at = time.monotonic() + 9

    def select(self, index):
        self.index = index
        self.settings["station_url"] = self.stations[index]["url"]
        self.radio.send("select", self.settings["station_url"])
        self.ensure_visible()
        self.dirty()

    def step(self, amount):
        indices = self.visible_indices()
        if indices:
            position = indices.index(self.index) if self.index in indices else (-1 if amount > 0 else 0)
            self.select(indices[(position + amount) % len(indices)])

    def volume(self, value):
        value = max(0, min(100, int(value)))
        if value != self.settings["volume"]:
            self.settings["volume"] = value
            self.radio.send("volume", value)
            self.dirty()

    def drag_volume(self, x):
        self.volume((x - 188) / 270 * 100)

    def action(self, name):
        if name == "exit":
            self.running = False
        elif name == "filter":
            self.only_favorites = not self.only_favorites
            self.offset = 0
            self.ensure_visible()
        elif name == "favorite":
            url = self.stations[self.index]["url"]
            if url in self.favorites:
                self.favorites.remove(url)
                self.notify("已取消收藏")
            else:
                self.favorites.add(url)
                self.notify("已收藏当前电台")
            self.clamp_scroll()
            self.dirty()
        elif name == "sleep":
            choices = (0, 15, 30, 60, 90)
            now = time.monotonic()
            if self.sleep_choice and now >= self.sleep_requested_at + self.sleep_choice * 60:
                self.sleep_choice = 0
            self.sleep_choice = choices[(choices.index(self.sleep_choice) + 1) % len(choices)]
            self.sleep_requested_at = now
            self.radio.send("sleep", self.sleep_choice)
            self.notify(f"{self.sleep_choice} 分钟后停止播放" if self.sleep_choice else "已取消睡眠定时")
        elif name == "night":
            self.night = not self.night
        elif name == "info":
            self.info = not self.info
        elif name == "up":
            self.offset -= 6 * 38
            self.clamp_scroll()
        elif name == "down":
            self.offset += 6 * 38
            self.clamp_scroll()
        elif name == "prev":
            self.step(-1)
        elif name == "next":
            self.step(1)
        elif name == "play":
            self.radio.send("toggle")

    def pointer(self, kind, pos):
        if self.night:
            if kind == "down":
                self.night = False
                self.drag = self.press = None
            return
        if kind == "down":
            self.press, self.moved = pos, False
            if self.vol_rect.collidepoint(pos):
                self.drag = "volume"
                self.drag_volume(pos[0])
            elif self.list_rect.collidepoint(pos):
                self.drag, self.scroll_start = "list", self.offset
            else:
                self.drag = None
        elif kind == "move" and self.press:
            if self.drag == "volume":
                self.drag_volume(pos[0])
            elif self.drag == "list":
                delta = pos[1] - self.press[1]
                if abs(delta) > 8:
                    self.moved = True
                if self.moved:
                    self.offset = self.scroll_start - delta
                    self.clamp_scroll()
        elif kind == "up" and self.press:
            if self.drag == "list" and not self.moved and self.list_rect.collidepoint(pos):
                row = int((pos[1] - self.list_rect.y + self.offset) // 38)
                indices = self.visible_indices()
                if 0 <= row < len(indices):
                    self.select(indices[row])
            elif self.drag is None:
                for name, rect in self.buttons.items():
                    if rect.collidepoint(self.press) and rect.collidepoint(pos):
                        self.action(name)
                        break
            self.drag = self.press = None

    def handle_event(self, event):
        pg = self.pg
        if event.type == pg.QUIT:
            self.running = False
        elif event.type == pg.KEYDOWN:
            actions = {pg.K_SPACE: "play", pg.K_LEFT: "prev", pg.K_RIGHT: "next",
                       pg.K_n: "night", pg.K_i: "info", pg.K_s: "sleep", pg.K_f: "favorite"}
            if event.key == pg.K_ESCAPE:
                if self.night:
                    self.night = False
                else:
                    self.running = False
            elif event.key in (pg.K_UP, pg.K_EQUALS, pg.K_KP_PLUS):
                self.volume(self.settings["volume"] + 5)
            elif event.key in (pg.K_DOWN, pg.K_MINUS, pg.K_KP_MINUS):
                self.volume(self.settings["volume"] - 5)
            elif event.key in actions:
                self.action(actions[event.key])
        elif event.type == pg.MOUSEWHEEL and not self.night:
            self.offset -= event.y * 38
            self.clamp_scroll()
        elif event.type in (pg.MOUSEBUTTONDOWN, pg.MOUSEBUTTONUP, pg.MOUSEMOTION):
            if getattr(event, "touch", False):
                return  # Finger events below handle touch, without duplicate clicks.
            if event.type != pg.MOUSEMOTION and event.button != 1:
                return
            kind = {pg.MOUSEBUTTONDOWN: "down", pg.MOUSEBUTTONUP: "up", pg.MOUSEMOTION: "move"}[event.type]
            self.pointer(kind, event.pos)
        elif event.type in (pg.FINGERDOWN, pg.FINGERUP, pg.FINGERMOTION):
            kind = {pg.FINGERDOWN: "down", pg.FINGERUP: "up", pg.FINGERMOTION: "move"}[event.type]
            self.pointer(kind, (int(event.x * WIDTH), int(event.y * HEIGHT)))
        elif event.type == pg.WINDOWFOCUSLOST:
            self.drag = self.press = None

    def text(self, text, x, y, size=14, color=WHITE, width=None, scroll=False):
        font = self.fonts[size]
        text = str(text)
        if width and not scroll and font.size(text)[0] > width:
            while text and font.size(text + "…")[0] > width:
                text = text[:-1]
            text += "…"
        surface = font.render(text, True, color)
        old_clip = self.screen.get_clip()
        if width:
            self.screen.set_clip(old_clip.clip(self.pg.Rect(x, y, width, font.get_linesize())))
        offset = 0
        if scroll and width and surface.get_width() > width:
            distance = surface.get_width() - width
            elapsed = time.monotonic() % (distance / 24 + 4)
            offset = min(distance, max(0, elapsed - 2) * 24)
        self.screen.blit(surface, (x - int(offset), y))
        self.screen.set_clip(old_clip)

    def button(self, name, label, color=WHITE, fill=CARD):
        rect = self.buttons[name]
        self.pg.draw.rect(self.screen, fill, rect, border_radius=7)
        font = self.fonts[14]
        width, height = font.size(label)
        self.text(label, rect.centerx - width // 2, rect.centery - height // 2, color=color)

    def draw_meter(self, capture):
        pg = self.pg
        active = self.snapshot.status == "playing"
        for channel, (value, peak) in enumerate(((capture.left, capture.peak_left), (capture.right, capture.peak_right))):
            y = 126 + channel * 19
            self.text("LR"[channel], 184, y - 2, 11, MUTED)
            for i in range(10):
                color = GREEN if i < 6 else ACCENT if i < 8 else RED
                rect = pg.Rect(201 + i * 25, y, 21, 10)
                pg.draw.rect(self.screen, color if active and value >= i + 1 else (35, 45, 57), rect, border_radius=2)
                if active and peak > 0 and i == min(9, int(math.ceil(peak)) - 1):
                    pg.draw.line(self.screen, WHITE, rect.topleft, rect.topright)
        for label, x in (("-60", 201), ("-36", 295), ("-18", 370), ("0", 438)):
            self.text(label, x, 161, 11, MUTED)
        self.text(capture.status, 184, 185, 12, CYAN if capture.updated else MUTED, 278)

    def draw(self):
        pg = self.pg
        self.screen.fill(BG)
        now = datetime.datetime.now()
        snapshot, capture = self.snapshot, self.meter.snapshot()
        if self.night:
            self.text(now.strftime("%H:%M"), 144, 57, 62, (130, 145, 159))
            self.text(now.strftime("%m-%d") + "  " + "周" + "一二三四五六日"[now.weekday()], 195, 136, 16, MUTED)
            self.text(self.stations[self.index]["name"], 40, 178, 20, MUTED, 400)
            self.text(snapshot.detail, 40, 213, 14, MUTED, 400)
            remaining = f"剩余 {math.ceil(snapshot.sleep_remaining / 60)} 分钟 · " if snapshot.sleep_remaining else ""
            self.text(remaining + "轻触屏幕返回", 40, 275, 14, MUTED, 400)
            pg.display.flip()
            return
        indices = self.visible_indices()
        self.button("filter", f"{'收藏' if self.only_favorites else '全部电台'} · {len(indices)}", CYAN)
        self.text(now.strftime("%H:%M"), 177, 13, 20)
        sleep_label = f"{math.ceil(snapshot.sleep_remaining / 60)} 分" if snapshot.sleep_remaining else "定时"
        self.button("sleep", sleep_label, ACCENT if snapshot.sleep_remaining else MUTED)
        self.button("night", "夜间", MUTED)
        self.button("info", "返回" if self.info else "状态", MUTED)
        self.button("exit", "退出", MUTED)
        old_clip = self.screen.get_clip()
        self.screen.set_clip(self.list_rect)
        for row, index in enumerate(indices):
            rect = pg.Rect(8, 45 + row * 38 - int(self.offset), 150, 34)
            if not rect.colliderect(self.list_rect):
                continue
            selected = index == self.index
            pg.draw.rect(self.screen, ACTIVE if selected else CARD, rect, border_radius=6)
            if selected:
                pg.draw.rect(self.screen, ACCENT, (8, rect.y + 7, 3, 20), border_radius=1)
            label = ("* " if self.stations[index]["url"] in self.favorites else "") + self.stations[index]["name"]
            self.text(label, 17, rect.y + 8, 14, ACCENT if selected else WHITE, 134)
        if not indices:
            self.text("还没有收藏", 23, 104, 16, MUTED)
            self.text("点电台右侧 * 添加", 17, 136, 12, MUTED)
        self.screen.set_clip(old_clip)
        if len(indices) > 6:
            total = len(indices) * 38
            thumb = max(12, int(228 * 228 / total))
            y = 45 + int((228 - thumb) * self.offset / (total - 228))
            pg.draw.rect(self.screen, MUTED, (160, y, 2, thumb), border_radius=1)
        self.button("up", "上页", MUTED)
        self.button("down", "下页", MUTED)
        pg.draw.rect(self.screen, CARD, (174, 45, 298, 212), border_radius=9)
        self.text(self.stations[self.index]["name"], 184, 53, 20, ACCENT, 246, scroll=True)
        self.button("favorite", "*", ACCENT if self.stations[self.index]["url"] in self.favorites else MUTED)
        status_color = GREEN if snapshot.status == "playing" else RED if snapshot.status == "error" else MUTED
        self.text(snapshot.detail, 184, 84, 12, status_color, 278)
        if self.info:
            temp = f"{self.stats.temp:.1f}°C" if self.stats.temp is not None else "不可用"
            self.text(f"CPU {self.stats.cpu:.0f}%   内存 {self.stats.ram:.0f}%", 184, 111, 14)
            self.text(f"温度 {temp}   下载 {self.stats.speed:.1f} KB/s", 184, 138, 12, MUTED, 278)
            self.text("网速为整机接收流量", 184, 161, 11, MUTED)
            self.text(capture.device or capture.status, 184, 185, 12, CYAN, 278, scroll=True)
        else:
            self.text(snapshot.metadata or "等待电台曲目信息…", 184, 106, 12, WHITE, 278, scroll=True)
            self.draw_meter(capture)
        self.text(f"音量 {self.settings['volume']}%", 184, 211, 12, CYAN)
        pg.draw.rect(self.screen, BG, (188, 237, 270, 6), border_radius=3)
        fill = round(self.settings["volume"] / 100 * 270)
        if fill:
            pg.draw.rect(self.screen, CYAN, (188, 237, fill, 6), border_radius=3)
        pg.draw.circle(self.screen, WHITE, (188 + fill, 240), 7)
        self.button("prev", "上一台")
        wanted = snapshot.status in ("playing", "connecting", "buffering", "retrying")
        self.button("play", "暂停" if wanted else "播放", BG, ACCENT if wanted else GREEN)
        self.button("next", "下一台")
        if time.monotonic() < self.toast_until:
            pg.draw.rect(self.screen, ACTIVE, (174, 176, 298, 32), border_radius=6)
            self.text(self.toast, 184, 183, 12, WHITE, 278)
        pg.display.flip()

    def run(self):
        clock = self.pg.time.Clock()
        while self.running:
            self.snapshot = self.radio.snapshot()
            if self.snapshot.status == "playing":
                self.meter.active.set()
            else:
                self.meter.active.clear()
            for event in self.pg.event.get():
                self.handle_event(event)
            self.stats.update(time.monotonic())
            self.persist()
            self.draw()
            clock.tick(10 if self.night else 30)


def configure_logging():
    handlers = [logging.StreamHandler()]
    try:
        handlers.append(RotatingFileHandler(BASE / "piradiobox.log", maxBytes=512_000,
                                           backupCount=2, encoding="utf-8"))
    except OSError:
        pass
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=handlers)


def main():
    parser = argparse.ArgumentParser(description="PiRadioBox 网络收音机")
    parser.add_argument("--config", type=Path, default=BASE / "config.json")
    parser.add_argument("--state", type=Path, default=BASE / "state.json")
    parser.add_argument("--windowed", action="store_true", help="窗口模式")
    parser.add_argument("--no-vu", action="store_true", help="禁用输出电平采集")
    parser.add_argument("--check-config", action="store_true", help="仅验证配置")
    parser.add_argument("--list-audio-devices", action="store_true", help="列出音频设备 ID")
    args = parser.parse_args()
    configure_logging()
    try:
        config = load_config(args.config)
        if args.check_config:
            print(f"配置有效，共 {len(config['stations'])} 个电台")
            return 0
        if args.list_audio_devices:
            import soundcard as sc
            print("默认输出:", sc.default_speaker())
            for mic in sc.all_microphones(include_loopback=True):
                print(f"{'MONITOR' if getattr(mic, 'isloopback', False) else 'INPUT'} | {mic.id} | {mic.name}")
            return 0
        if args.windowed:
            config["fullscreen"] = False
        if args.no_vu:
            config["vu_enabled"] = False
        settings = load_settings(args.state, config)
        import pygame
        from radio_audio import AudioMeter, RadioService
        radio = meter = app = None
        try:
            pygame.display.init()
            pygame.font.init()
            # Check font availability before starting any audio.
            find_font(pygame, config["font_path"])
            radio = RadioService(config, settings)
            meter = AudioMeter(config["vu_enabled"], config["monitor_device"])
            app = RadioApp(pygame, config, settings, args.state, radio, meter)
            app.run()
        finally:
            if app is not None:
                app.persist(force=True)
            if radio is not None:
                radio.close()
            if meter is not None:
                meter.close()
            pygame.quit()
    except KeyboardInterrupt:
        return 0
    except Exception:
        LOG.exception("启动或运行失败")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
