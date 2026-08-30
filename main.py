# -*- coding: utf-8 -*-
import pygame
import sys
import vlc
import time
import datetime
import psutil
import threading
import numpy as np
import soundcard as sc

# --- 1. 电台预设列表 ---
STATIONS = [
    {"name": "Lofi Chill 音乐", "url": "https://stream.zeno.fm/f3wvbbqmdg8uv"},
    {"name": "爵士放松电台",     "url": "https://stream.zeno.fm/0r0xa792kwzuv"},
    {"name": "Classic FM 古典", "url": "http://media-ice.musicradio.com/ClassicFMMP3"},
    {"name": "80-90年代经典老歌", "url": "http://stream.zeno.fm/u2e6z6d52zzuv"},
    {"name": "CNR 中国之声",   "url": "http://ngcdn001.cnr.cn/live/zgzs/index.m3u8"},
    {"name": "CNR 音乐之声",   "url": "http://ngcdn003.cnr.cn/live/yyzs/index.m3u8"},
    {"name": "CRI 轻松调频 EZFM","url": "http://live.hitfm.cn/ezfm.m3u8"}
]

current_index = 0
is_playing = False
current_volume = 70       # 初始音量 70%
is_dragging_vol = False   # 音量拖拽标记
current_metadata = "正在读取信息..."

# --- 2. 真实音频响度采集全局变量 ---
real_vu_left = 0.0   # 0.0 ~ 10.0
real_vu_right = 0.0  # 0.0 ~ 10.0
audio_thread_running = True

def audio_capture_worker():
    """后台采集真实音频输出 PCM 并计算 L/R 声道 RMS 响度"""
    global real_vu_left, real_vu_right
    
    try:
        # 获取默认录音/监控设备
        mic = sc.default_microphone()
    except Exception as e:
        print(f"[Warning] 无法获取音频采集设备，尝试搜索 monitor 设备: {e}")
        try:
            mics = sc.all_microphones(include_loopback=True)
            if len(mics) > 0:
                mic = mics[0]
            else:
                return
        except Exception:
            return

    # 采样率 44100, 每次抓取 1024 帧（约 23ms 数据包）
    sample_rate = 44100
    block_size = 512

    with mic.recorder(samplerate=sample_rate, channels=2) as recorder:
        while audio_thread_running:
            try:
                data = recorder.record(numframes=block_size)
                if not is_playing or data is None or len(data) == 0:
                    real_vu_left = max(0.0, real_vu_left - 1.0)
                    real_vu_right = max(0.0, real_vu_right - 1.0)
                    time.sleep(0.03)
                    continue

                # 分离左 (L) 和右 (R) 声道 PCM 数据
                l_channel = data[:, 0]
                r_channel = data[:, 1] if data.shape[1] > 1 else l_channel

                # 计算 RMS (Root Mean Square 均方根振幅)
                rms_l = np.sqrt(np.mean(l_channel**2))
                rms_r = np.sqrt(np.mean(r_channel**2))

                # 映射到 0~10 的电平高度 (结合对数增益)
                val_l = min(10.0, (rms_l * 18.0) ** 0.6 * 10.0)
                val_r = min(10.0, (rms_r * 18.0) ** 0.6 * 10.0)

                # 平滑滤波 (保留小动感)
                real_vu_left = real_vu_left * 0.4 + val_l * 0.6
                real_vu_right = real_vu_right * 0.4 + val_r * 0.6

            except Exception:
                time.sleep(0.05)

# 启动音频监听后台线程
cap_thread = threading.Thread(target=audio_capture_worker, daemon=True)
cap_thread.start()

# --- 3. 硬件数据获取 ---
last_net_bytes = psutil.net_io_counters().bytes_recv
last_net_time = time.time()
current_down_speed = 0.0

def get_cpu_temp():
    try:
        with open("/sys/class/thermal/thermal_zone0/temp", "r") as f:
            return float(f.read()) / 1000.0
    except Exception:
        return 0.0

def update_system_stats():
    global last_net_bytes, last_net_time, current_down_speed
    cpu_use = psutil.cpu_percent()
    ram_use = psutil.virtual_memory().percent
    
    now_time = time.time()
    dt = now_time - last_net_time
    if dt >= 0.5:
        now_bytes = psutil.net_io_counters().bytes_recv
        current_down_speed = (now_bytes - last_net_bytes) / dt / 1024.0
        last_net_bytes = now_bytes
        last_net_time = now_time

    return cpu_use, ram_use, get_cpu_temp()

# --- 4. VLC 初始化 ---
vlc_instance = vlc.Instance('--no-video', '--quiet')
player = vlc_instance.media_player_new()
player.audio_set_volume(current_volume)

def fetch_metadata():
    global current_metadata
    if not is_playing:
        current_metadata = "播放已暂停"
        return
        
    media = player.get_media()
    if media:
        meta = media.get_meta(vlc.Meta.NowPlaying) or media.get_meta(vlc.Meta.Title)
        if meta and meta.strip():
            current_metadata = meta
        else:
            current_metadata = "实时广播直播中"

def play_station(index):
    global is_playing, current_metadata
    station = STATIONS[index]
    current_metadata = "正在连接..."
    media = vlc_instance.media_new(station["url"])
    player.set_media(media)
    player.play()
    is_playing = True

def toggle_play():
    global is_playing
    if is_playing:
        player.pause()
        is_playing = False
    else:
        player.play()
        is_playing = True

def set_volume(val):
    global current_volume
    current_volume = max(0, min(100, int(val)))
    player.audio_set_volume(current_volume)

def exit_app():
    global audio_thread_running
    audio_thread_running = False
    player.stop()
    pygame.quit()
    sys.exit()

# --- 5. Pygame 初始化 ---
pygame.init()
WIDTH, HEIGHT = 480, 320
screen = pygame.display.set_mode((WIDTH, HEIGHT), pygame.FULLSCREEN | pygame.NOFRAME)
pygame.display.set_caption("树莓派电台 真实 VU 响度版")

BG_COLOR     = (15, 18, 25)
CARD_BG      = (26, 32, 44)
CARD_ACTIVE  = (38, 48, 68)
TEXT_COLOR   = (240, 245, 250)
MUTED_TEXT   = (110, 120, 135)
ACCENT_COLOR = (255, 110, 70)
GREEN_COLOR  = (60, 200, 120)
CYAN_COLOR   = (70, 210, 230)
YELLOW_COLOR = (250, 210, 90)
BTN_COLOR    = (40, 50, 68)
RED_COLOR    = (220, 60, 60)

# --- 6. 字体 ---
font_path = "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"
try:
    font_clock  = pygame.font.Font(font_path, 30)
    font_large  = pygame.font.Font(font_path, 22)
    font_medium = pygame.font.Font(font_path, 16)
    font_small  = pygame.font.Font(font_path, 13)
    font_tiny   = pygame.font.Font(font_path, 11)
except IOError:
    print("未检测到中文字体，请运行: sudo apt install fonts-wqy-microhei")
    sys.exit()

# --- 7. 热区定义 ---
station_rects = []
for i in range(len(STATIONS)):
    station_rects.append(pygame.Rect(8, 8 + i * 43, 175, 38))

btn_prev = pygame.Rect(192, 255, 85, 57)
btn_play = pygame.Rect(285, 255, 98, 57)
btn_next = pygame.Rect(390, 255, 82, 57)

# 退出按钮热区（位于网速/日期显示右侧）
btn_exit = pygame.Rect(440, 12, 26, 28)

# 音量滑块热区及槽位定义
vol_x, vol_y, vol_w, vol_h = 202, 108, 260, 20
vol_rect = pygame.Rect(vol_x, vol_y, vol_w, vol_h)

# --- 8. UI 渲染逻辑 ---
def draw_ui(cpu_use, ram_use, cpu_temp):
    screen.fill(BG_COLOR)

    # A. 左侧：电台列表
    for i, station in enumerate(STATIONS):
        rect = station_rects[i]
        is_selected = (i == current_index)
        bg = CARD_ACTIVE if is_selected else CARD_BG
        pygame.draw.rect(screen, bg, rect, border_radius=6)
        
        if is_selected:
            pygame.draw.rect(screen, ACCENT_COLOR, (rect.x, rect.y + 6, 4, rect.height - 12), border_radius=2)

        color = ACCENT_COLOR if is_selected else TEXT_COLOR
        name_str = station["name"]
        if len(name_str) > 8:
            name_str = name_str[:7] + ".."
        txt = font_medium.render(name_str, True, color)
        screen.blit(txt, (rect.x + 10, rect.y + 9))

    # B. 右侧面板背景
    pygame.draw.rect(screen, CARD_BG, (192, 8, 280, 240), border_radius=10)

    # 时间与网速
    now = datetime.datetime.now()
    time_str = now.strftime("%H:%M:%S")
    time_txt = font_clock.render(time_str, True, TEXT_COLOR)
    screen.blit(time_txt, (202, 12))

    net_str = f"| {current_down_speed:.1f} KB/s"
    net_txt = font_small.render(net_str, True, CYAN_COLOR)
    screen.blit(net_txt, (355, 15))

    weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    date_str = f"{now.strftime('%m-%d')} {weekdays[now.weekday()]}"
    date_txt = font_tiny.render(date_str, True, MUTED_TEXT)
    screen.blit(date_txt, (355, 32))

    # 退出按钮 [X]
    pygame.draw.rect(screen, RED_COLOR, btn_exit, border_radius=4)
    exit_txt = font_small.render("X", True, TEXT_COLOR)
    screen.blit(exit_txt, (btn_exit.x + 8, btn_exit.y + 6))

    pygame.draw.line(screen, (45, 55, 75), (202, 48), (462, 48), 1)

    # C. 真实音频 10 段双声道 VU 电平指示灯
    vu_start_x = 202
    vu_start_y = 54
    led_width = 21
    led_height = 8
    gap = 4

    screen.blit(font_tiny.render("L", True, MUTED_TEXT), (vu_start_x, vu_start_y))
    screen.blit(font_tiny.render("R", True, MUTED_TEXT), (vu_start_x, vu_start_y + 12))

    # 10 段指示灯
    for segment in range(10):
        if segment < 6:
            active_color = (60, 210, 100)
            off_color = (20, 45, 25)
        elif segment < 8:
            active_color = (250, 200, 50)
            off_color = (50, 45, 18)
        else:
            active_color = (255, 70, 70)
            off_color = (50, 20, 20)

        l_x = vu_start_x + 16 + segment * (led_width + gap)

        # L 声道灯
        if segment < int(real_vu_left):
            pygame.draw.rect(screen, active_color, (l_x, vu_start_y + 2, led_width, led_height), border_radius=2)
        else:
            pygame.draw.rect(screen, off_color, (l_x, vu_start_y + 2, led_width, led_height), border_radius=2)

        # R 声道灯
        if segment < int(real_vu_right):
            pygame.draw.rect(screen, active_color, (l_x, vu_start_y + 14, led_width, led_height), border_radius=2)
        else:
            pygame.draw.rect(screen, off_color, (l_x, vu_start_y + 14, led_width, led_height), border_radius=2)

    pygame.draw.line(screen, (45, 55, 75), (202, 82), (462, 82), 1)

    # D. 硬件 CPU 监控 + 横向音量滑动条
    sys_info_str = f"CPU: {cpu_use:.0f}% ({cpu_temp:.1f}C)   RAM: {ram_use:.0f}%"
    sys_txt = font_tiny.render(sys_info_str, True, TEXT_COLOR)
    screen.blit(sys_txt, (202, 88))

    vol_text = font_tiny.render(f"VOL {current_volume}%", True, CYAN_COLOR)
    screen.blit(vol_text, (410, 88))

    # 音量条槽位
    pygame.draw.rect(screen, (18, 22, 32), (vol_x, vol_y, vol_w, vol_h), border_radius=10)
    
    # 填充已调高音量的进度部分
    fill_w = int((current_volume / 100.0) * vol_w)
    if fill_w > 0:
        pygame.draw.rect(screen, CYAN_COLOR, (vol_x, vol_y, fill_w, vol_h), border_radius=10)

    # 移动滑块圆点
    knob_x = vol_x + fill_w
    knob_x = max(vol_x + 8, min(vol_x + vol_w - 8, knob_x))
    pygame.draw.circle(screen, TEXT_COLOR, (knob_x, vol_y + vol_h // 2), 7)

    pygame.draw.line(screen, (45, 55, 75), (202, 138), (462, 138), 1)

    # E. 电台名与歌词元数据
    curr_name = STATIONS[current_index]["name"]
    curr_txt = font_medium.render(f"> {curr_name}", True, ACCENT_COLOR)
    screen.blit(curr_txt, (202, 144))

    meta_p1 = current_metadata
    if len(meta_p1) > 22:
        meta_p1 = meta_p1[:21] + ".."
    meta_txt1 = font_small.render(f"TRACK: {meta_p1}", True, YELLOW_COLOR)
    screen.blit(meta_txt1, (202, 168))

    if len(current_metadata) > 22:
        meta_p2 = current_metadata[21:]
        if len(meta_p2) > 24:
            meta_p2 = meta_p2[:22] + ".."
        meta_txt2 = font_tiny.render(f"       {meta_p2}", True, YELLOW_COLOR)
        screen.blit(meta_txt2, (202, 188))
    else:
        status_info = "* 实时采样中 | Audio PCM Capture" if is_playing else "|| 传输暂停"
        info_txt = font_tiny.render(status_info, True, MUTED_TEXT)
        screen.blit(info_txt, (202, 188))

    play_status_str = "LIVE" if is_playing else "PAUSE"
    p_color = GREEN_COLOR if is_playing else MUTED_TEXT
    pygame.draw.rect(screen, (20, 30, 42), (202, 212, 260, 20), border_radius=4)
    screen.blit(font_tiny.render(play_status_str, True, p_color), (210, 215))

    # F. 底部控制按钮
    pygame.draw.rect(screen, BTN_COLOR, btn_prev, border_radius=8)
    screen.blit(font_large.render("|<", True, TEXT_COLOR), (btn_prev.x + 30, btn_prev.y + 14))

    play_bg = ACCENT_COLOR if is_playing else GREEN_COLOR
    pygame.draw.rect(screen, play_bg, btn_play, border_radius=8)
    play_symbol = "PAUSE" if is_playing else "PLAY"
    offset_x = 22 if is_playing else 26
    screen.blit(font_medium.render(play_symbol, True, BG_COLOR), (btn_play.x + offset_x, btn_play.y + 18))

    pygame.draw.rect(screen, BTN_COLOR, btn_next, border_radius=8)
    screen.blit(font_large.render(">|", True, TEXT_COLOR), (btn_next.x + 28, btn_next.y + 14))

    pygame.display.flip()

def handle_vol_drag(mouse_x):
    ratio = (mouse_x - vol_x) / float(vol_w)
    set_volume(ratio * 100)

# --- 9. 主循环 ---
play_station(current_index)
clock = pygame.time.Clock()
last_meta_check = 0

try:
    while True:
        current_time_sec = time.time()

        cpu_use, ram_use, cpu_temp = update_system_stats()

        if current_time_sec - last_meta_check > 2.5:
            last_meta_check = current_time_sec
            fetch_metadata()

        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                exit_app()

            elif event.type == pygame.KEYDOWN:
                if event.key == pygame.K_ESCAPE:
                    exit_app()
                elif event.key == pygame.K_SPACE:
                    toggle_play()

            elif event.type == pygame.MOUSEBUTTONDOWN:
                pos = event.pos

                # 点击退出按钮
                if btn_exit.collidepoint(pos):
                    exit_app()

                # 检测音量条拖拽/点击
                if vol_rect.collidepoint(pos):
                    is_dragging_vol = True
                    handle_vol_drag(pos[0])

                for i, rect in enumerate(station_rects):
                    if rect.collidepoint(pos):
                        if current_index != i:
                            current_index = i
                            play_station(current_index)
                        break

                if btn_prev.collidepoint(pos):
                    current_index = (current_index - 1) % len(STATIONS)
                    play_station(current_index)

                elif btn_play.collidepoint(pos):
                    toggle_play()

                elif btn_next.collidepoint(pos):
                    current_index = (current_index + 1) % len(STATIONS)
                    play_station(current_index)

            elif event.type == pygame.MOUSEBUTTONUP:
                is_dragging_vol = False

            elif event.type == pygame.MOUSEMOTION:
                if is_dragging_vol:
                    handle_vol_drag(event.pos[0])

        draw_ui(cpu_use, ram_use, cpu_temp)
        clock.tick(30)  # 60 FPS 高帧率响应真实音轨

finally:
    exit_app()
