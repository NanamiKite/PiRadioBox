# PiRadioBox

一个运行在树莓派上的简单网络电台播放器，针对 480×320 的 3.5 英寸 LCD 屏幕设计。

使用 Python 编写，界面基于 Pygame，音频播放使用 VLC。除了基本的电台播放和音量控制外，还加入了实时双声道 VU 电平、播放信息以及树莓派系统状态显示。

## 功能

* 网络电台播放
* 电台切换
* 播放 / 暂停
* 音量调节
* L/R 双声道实时 VU 电平
* 获取电台 Now Playing / Title 信息
* CPU 使用率和温度显示
* RAM 使用率显示
* 网络下载速度显示
* 480×320 全屏界面
* 支持触摸、鼠标和键盘操作

## 安装

首先安装 VLC 和中文字体：

```bash
sudo apt update
sudo apt install vlc fonts-wqy-microhei
```

然后安装 Python 依赖：

```bash
pip3 install pygame python-vlc psutil numpy soundcard
```

如果使用虚拟环境：

```bash
python3 -m venv venv
source venv/bin/activate
pip install pygame python-vlc psutil numpy soundcard
```

## 运行

```bash
python3 main.py
```

启动后程序会直接进入全屏模式，并自动播放第一台电台。

## 操作

触摸或鼠标：

* 点击左侧电台切换电台
* 点击 `|<` 播放上一台
* 点击 `PLAY / PAUSE` 播放或暂停
* 点击 `>|` 播放下一台
* 拖动音量条调节音量
* 点击右上角 `X` 退出

键盘：

* `Space`：播放 / 暂停
* `Esc`：退出

## 修改电台

电台列表直接写在 `radio.py` 的 `STATIONS` 中：

```python
STATIONS = [
    {"name": "我的电台", "url": "https://example.com/stream"},
    {"name": "另一个电台", "url": "https://example.com/live"},
]
```

只需要修改名称和流媒体地址即可。

默认音量也可以修改：

```python
current_volume = 70
```

屏幕分辨率默认设置为：

```python
WIDTH, HEIGHT = 480, 320
```

如果修改分辨率，还需要相应调整界面中的坐标。

## VU 电平

VU 电平通过 SoundCard 获取 PCM 音频数据，然后分别计算左右声道 RMS，用于显示当前实际音频电平。

音频采集运行在独立线程中，不会阻塞 Pygame 主循环。

播放器输出和音频采集是两个独立部分。系统需要提供可访问的录音或 Loopback/Monitor 设备，VU 电平才能正常获取正在播放的音频。

## 依赖

* Python 3
* Pygame
* python-vlc
* VLC
* NumPy
* SoundCard
* psutil

## License

MIT License
