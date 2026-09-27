"""Audio workers. VLC and capture failures do not take down the interface."""
from dataclasses import dataclass
import logging
import math
import queue
import threading
import time

from radio_core import PlaybackController, PlaybackSnapshot, rms_to_level

LOG = logging.getLogger(__name__)


class VlcBackend:
    def __init__(self, config):
        import vlc
        self.vlc = vlc
        self.instance = vlc.Instance("--no-video", "--no-video-title-show",
                                     f"--network-caching={config['network_caching_ms']}")
        if self.instance is None:
            raise RuntimeError("无法初始化 VLC，请确认已安装同位数的 VLC")
        self.player = self.instance.media_player_new()

    def play(self, url, volume):
        self.player.stop()
        media = self.instance.media_new(url)
        try:
            self.player.set_media(media)
        finally:
            media.release()
        self.set_volume(volume)
        return self.player.play()

    def stop(self):
        self.player.stop()

    def set_volume(self, volume):
        self.player.audio_set_volume(volume)

    def state(self):
        state = self.player.get_state()
        return {self.vlc.State.Playing: "playing", self.vlc.State.Opening: "opening",
                self.vlc.State.Buffering: "buffering", self.vlc.State.Error: "error",
                self.vlc.State.Ended: "ended", self.vlc.State.Stopped: "stopped",
                self.vlc.State.Paused: "paused"}.get(state, "opening")

    def position(self):
        return self.player.get_time()

    def metadata(self):
        media = self.player.get_media()
        if media is None:
            return ""
        try:
            text = media.get_meta(self.vlc.Meta.NowPlaying) or media.get_meta(self.vlc.Meta.Title)
            return " ".join(text.split())[:512] if text else ""
        finally:
            media.release()

    def close(self):
        try:
            self.player.stop()
        finally:
            self.player.release()
            self.instance.release()


class RadioService:
    def __init__(self, config, settings):
        self.config, self.settings = config, dict(settings)
        self.commands = queue.Queue()
        self.quit = threading.Event()
        self.lock = threading.Lock()
        self.current = PlaybackSnapshot(detail="播放器初始化中…")
        self.thread = threading.Thread(target=self._run, name="vlc-player", daemon=True)
        self.thread.start()

    def send(self, command, value=None):
        self.commands.put((command, value))

    def snapshot(self):
        with self.lock:
            return self.current

    def _publish(self, snapshot):
        with self.lock:
            self.current = snapshot

    def _run(self):
        backend = None
        try:
            backend = VlcBackend(self.config)
            controller = PlaybackController(backend, self.config)
            controller.url = self.settings["station_url"]
            controller.set_volume(self.settings["volume"])
            if self.config["autoplay"]:
                controller.select(controller.url)
            while not self.quit.is_set():
                # Drain a bounded batch; collapse slider events into one VLC call.
                volume = None
                for _ in range(100):
                    try:
                        command, value = self.commands.get_nowait()
                    except queue.Empty:
                        break
                    if command == "select":
                        controller.select(value)
                    elif command == "toggle":
                        controller.toggle()
                    elif command == "volume":
                        volume = value
                    elif command == "sleep":
                        controller.set_sleep(value)
                if volume is not None:
                    controller.set_volume(volume)
                controller.tick()
                self._publish(controller.snapshot())
                self.quit.wait(0.1)
        except Exception:
            LOG.exception("播放器后台任务失败")
            self._publish(PlaybackSnapshot("error", "播放器不可用 · 请检查日志并重启"))
        finally:
            if backend is not None:
                try:
                    backend.close()
                except Exception:
                    LOG.exception("释放 VLC 失败")

    def close(self):
        self.quit.set()
        self.thread.join(timeout=3)
        if self.thread.is_alive():
            LOG.warning("VLC 仍在结束底层调用，后台线程未在 3 秒内退出")


def choose_monitor(sc, device=""):
    """Never silently substitute a physical microphone or another output."""
    monitors = [mic for mic in sc.all_microphones(include_loopback=True)
                if getattr(mic, "isloopback", False)]
    if device:
        matches = [mic for mic in monitors if mic.id == device or mic.name == device]
    else:
        speaker = sc.default_speaker()
        if speaker is None:
            raise RuntimeError("没有默认输出设备")
        matches = [mic for mic in monitors
                   if mic.id in (speaker.id, speaker.id + ".monitor")]
        if not matches:
            matches = [mic for mic in monitors
                       if mic.name in (speaker.name, "Monitor of " + speaker.name)]
    if len(matches) != 1:
        raise RuntimeError("未找到唯一的输出回环，请设置 monitor_device")
    return matches[0]


@dataclass(frozen=True)
class CaptureSnapshot:
    left: float = 0
    right: float = 0
    peak_left: float = 0
    peak_right: float = 0
    status: str = "正在查找输出回环…"
    device: str = ""
    updated: float = 0


class AudioMeter:
    def __init__(self, enabled=True, device=""):
        self.enabled, self.device = enabled, device
        self.quit = threading.Event()
        self.active = threading.Event()
        self.lock = threading.Lock()
        self.current = CaptureSnapshot(status="电平已关闭" if not enabled else "正在查找输出回环…")
        self.thread = None
        if enabled:
            self.thread = threading.Thread(target=self._run, name="output-meter", daemon=True)
            self.thread.start()

    def snapshot(self):
        with self.lock:
            result = self.current
        if result.updated and time.monotonic() - result.updated > 2:
            return CaptureSnapshot(status="电平不可用 · 采样无响应", device=result.device)
        return result

    def _publish(self, result):
        with self.lock:
            self.current = result

    def _run(self):
        try:
            import numpy as np
            import soundcard as sc
        except Exception:
            LOG.exception("电平依赖不可用")
            self._publish(CaptureSnapshot(status="电平不可用 · 检查音频依赖"))
            return
        while not self.quit.is_set():
            try:
                mic = choose_monitor(sc, self.device)
                LOG.info("电平采集设备: %s", mic.name)
                levels, peaks, holds = [0.0, 0.0], [0.0, 0.0], [0.0, 0.0]
                last = time.monotonic()
                next_device_check = last + 5
                # Use native channel count; avoids requesting stereo from mono devices.
                # Read half of a small two-block buffer to reduce capture latency.
                with mic.recorder(samplerate=48000, blocksize=1024) as recorder:
                    while not self.quit.is_set():
                        if not self.device and time.monotonic() >= next_device_check:
                            if choose_monitor(sc).id != mic.id:
                                break
                            next_device_check = time.monotonic() + 5
                        data = recorder.record(numframes=512)
                        now = time.monotonic()
                        dt, last = min(0.25, now - last), now
                        targets = [0.0, 0.0]
                        if data is None or not len(data):
                            raise RuntimeError("输出回环没有返回采样数据")
                        if self.active.is_set():
                            if data.ndim == 1:
                                data = data[:, None]
                            for channel in range(2):
                                pcm = data[:, min(channel, data.shape[1] - 1)].astype(float)
                                rms = float(np.sqrt(np.mean(pcm * pcm)))
                                targets[channel] = rms_to_level(rms)
                        for channel in range(2):
                            tau = 0.035 if targets[channel] > levels[channel] else 0.3
                            alpha = 1 - math.exp(-dt / tau)
                            levels[channel] += alpha * (targets[channel] - levels[channel])
                            if levels[channel] >= peaks[channel]:
                                peaks[channel], holds[channel] = levels[channel], now + 0.8
                            elif now > holds[channel]:
                                peaks[channel] = max(levels[channel], peaks[channel] - dt * 5)
                        self._publish(CaptureSnapshot(*levels, *peaks,
                                      "输出电平 · dBFS", mic.name, now))
            except Exception:
                LOG.warning("输出回环采集失败，10 秒后重试", exc_info=True)
                self._publish(CaptureSnapshot(status="电平不可用 · 等待输出回环"))
                self.quit.wait(10)

    def close(self):
        self.quit.set()
        if self.thread is not None:
            self.thread.join(timeout=2)
            if self.thread.is_alive():
                LOG.warning("音频驱动仍在等待采样，采集线程未在 2 秒内退出")
