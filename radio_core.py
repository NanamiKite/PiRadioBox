"""Configuration and playback policy; no GUI or audio dependencies."""
from dataclasses import dataclass
import json
import logging
import math
from pathlib import Path
import time
from urllib.parse import urlsplit

LOG = logging.getLogger(__name__)


def read_json(path):
    with Path(path).open(encoding="utf-8-sig") as stream:
        return json.load(stream)


def integer(value, name, low, high):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{name} 必须是 {low} 到 {high} 之间的整数")
    return value


def load_config(path):
    config = read_json(path)
    if not isinstance(config, dict):
        raise ValueError("配置必须是 JSON 对象")
    stations = config.get("stations")
    if not isinstance(stations, list) or not stations:
        raise ValueError("stations 至少需要一个电台")
    urls = set()
    for station in stations:
        if not isinstance(station, dict):
            raise ValueError("每个电台必须包含 name 和 url")
        name, url = station.get("name"), station.get("url")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("电台名称不能为空")
        if not isinstance(url, str) or any(ch.isspace() for ch in url):
            raise ValueError(f"{name}: 电台地址格式错误")
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError(f"{name}: 电台地址必须是 HTTP 或 HTTPS")
        if url in urls:
            raise ValueError(f"{name}: 电台地址重复")
        urls.add(url)
    for name, default, low, high in (
        ("volume", 40, 0, 100), ("connect_timeout", 20, 5, 120),
        ("max_retries", 3, 0, 10), ("network_caching_ms", 1500, 100, 10000),
    ):
        config[name] = integer(config.get(name, default), name, low, high)
    for name, default in (("autoplay", True), ("fullscreen", True), ("vu_enabled", True)):
        config.setdefault(name, default)
        if not isinstance(config[name], bool):
            raise ValueError(f"{name} 必须是 true 或 false")
    for name in ("font_path", "monitor_device"):
        config.setdefault(name, "")
        if not isinstance(config[name], str):
            raise ValueError(f"{name} 必须是字符串")
    config.setdefault("slideshow_dir", "slides")
    if not isinstance(config["slideshow_dir"], str):
        raise ValueError("slideshow_dir 必须是字符串")
    config["slideshow_interval"] = integer(config.get("slideshow_interval", 20),
                                            "slideshow_interval", 5, 3600)
    return config


def load_settings(path, config):
    urls = {s["url"] for s in config["stations"]}
    settings = {"station_url": config["stations"][0]["url"],
                "volume": config["volume"], "favorites": []}
    try:
        data = read_json(path)
        if not isinstance(data, dict):
            raise ValueError("状态文件必须是 JSON 对象")
        if data.get("station_url") in urls:
            settings["station_url"] = data["station_url"]
        volume = data.get("volume", settings["volume"])
        settings["volume"] = integer(volume, "volume", 0, 100)
        favorites = data.get("favorites", [])
        if isinstance(favorites, list):
            settings["favorites"] = list(dict.fromkeys(
                url for url in favorites if isinstance(url, str) and url in urls))
    except FileNotFoundError:
        pass
    except (OSError, ValueError, TypeError):
        LOG.warning("无法读取用户设置，使用默认值", exc_info=True)
    return settings


def save_settings(path, settings):
    """Replace atomically so interrupted writes do not truncate the saved state."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(settings, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def rms_to_level(rms):
    """Map -60..0 dBFS to ten LEDs; silence and invalid samples stay dark."""
    if not math.isfinite(rms) or rms <= 0:
        return 0.0
    return max(0.0, min(10.0, (20 * math.log10(rms) + 60) / 6))


@dataclass(frozen=True)
class PlaybackSnapshot:
    status: str = "stopped"
    detail: str = "选择电台开始播放"
    metadata: str = ""
    retries: int = 0
    sleep_remaining: float = 0


class PlaybackController:
    """Single-threaded state machine; a worker owns this and the VLC backend."""

    def __init__(self, backend, config, clock=time.monotonic):
        self.backend, self.config, self.clock = backend, config, clock
        self.url = None
        self.volume = config["volume"]
        self.wanted = False
        self.status, self.detail, self.metadata = "stopped", "选择电台开始播放", ""
        self.retries = 0
        self.retry_at = None
        self.started_at = 0
        self.healthy_since = None
        self.last_progress_at = 0
        self.last_position = None
        self.last_meta_at = -math.inf
        self.sleep_deadline = None

    def snapshot(self):
        remaining = max(0, self.sleep_deadline - self.clock()) if self.sleep_deadline else 0
        return PlaybackSnapshot(self.status, self.detail, self.metadata, self.retries, remaining)

    def select(self, url):
        self.url, self.wanted, self.retries = url, True, 0
        self.metadata = ""
        self._start()

    def _start(self):
        self.retry_at = None
        self.healthy_since = None
        self.last_position = None
        self.last_meta_at = -math.inf
        self.started_at = self.last_progress_at = self.clock()
        self.status, self.detail = "connecting", "正在连接电台…"
        try:
            if self.backend.play(self.url, self.volume) == -1:
                self._fail("无法启动播放")
        except Exception:
            LOG.exception("启动播放失败")
            self._fail("无法启动播放")

    def _fail(self, reason):
        self.healthy_since = None
        try:
            self.backend.stop()
        except Exception:
            LOG.exception("停止播放器失败")
        if self.retries < self.config["max_retries"]:
            self.retries += 1
            delay = min(30, 2 ** self.retries)
            self.retry_at = self.clock() + delay
            self.status, self.detail = "retrying", f"{reason} · {delay} 秒后重试 ({self.retries}/{self.config['max_retries']})"
        else:
            self.retry_at = None
            self.wanted = False
            self.status, self.detail = "error", f"{reason} · 点击播放重试"
        LOG.warning("播放状态: %s", self.detail)

    def stop(self, reason="已暂停 · 再次播放返回直播"):
        self.wanted = False
        self.retry_at = None
        self.healthy_since = None
        self.status, self.detail = "paused", reason
        self.backend.stop()

    def toggle(self):
        if self.wanted:
            self.stop()
        elif self.url:
            self.select(self.url)

    def set_volume(self, volume):
        self.volume = max(0, min(100, int(volume)))
        self.backend.set_volume(self.volume)

    def set_sleep(self, minutes):
        self.sleep_deadline = self.clock() + minutes * 60 if minutes else None

    def tick(self):
        now = self.clock()
        if self.sleep_deadline is not None and now >= self.sleep_deadline:
            self.sleep_deadline = None
            self.stop("睡眠定时已到 · 播放已停止")
        if not self.wanted:
            return
        if self.retry_at is not None:
            if now >= self.retry_at:
                self._start()
            return
        state = self.backend.state()
        if state in ("error", "ended", "stopped"):
            self._fail("连接中断" if state != "error" else "电台连接失败")
            return
        if state == "playing":
            if self.status != "playing":
                self.backend.set_volume(self.volume)
                self.last_progress_at = now
                self.healthy_since = now
            self.status, self.detail = "playing", "LIVE · 正在直播"
            position = self.backend.position()
            if position is not None and position >= 0:
                if position != self.last_position:
                    self.last_progress_at = now
                    self.last_position = position
                elif now - self.last_progress_at >= self.config["connect_timeout"]:
                    self._fail("音频流停止更新")
                    return
            if self.healthy_since is not None and now - self.healthy_since >= 30:
                self.retries = 0
            if now - self.last_meta_at >= 2.5:
                self.last_meta_at = now
                self.metadata = self.backend.metadata() or "电台未提供曲目信息"
        else:
            if self.status == "playing":
                self.started_at = now
            self.healthy_since = None
            self.status = "buffering" if state == "buffering" else "connecting"
            self.detail = "正在缓冲…" if state == "buffering" else "正在连接电台…"
            if now - self.started_at >= self.config["connect_timeout"]:
                self._fail("连接或缓冲超时")
