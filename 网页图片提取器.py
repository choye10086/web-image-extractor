# -*- coding: utf-8 -*-
"""
网页图片提取器（背景图版）
输入网址 -> 提取页面中所有原始高清图片 -> 按网页标题命名文件夹保存
微信公众号文章的图片自动用文章标题命名
支持自定义背景图，显示方式自动适配：
  · 图片与日志区比例接近（相似度 ≥70%）→ 完整显示原图，四周用同图模糊铺满
  · 比例差异较大 → 裁取图片完整画面，AI 离线识别人脸（多人取最左边的人），
    构图保证"脸 + 身体"都露出来并保持居中；无人脸时回退到显著性主体识别
背景保存在本地，关闭后下次打开仍然生效；窗口可拉伸、可全屏（F11）
"""

import os
import re
import sys
import io
import json
import time
import struct
import math
import hashlib
import collections
import threading
import queue
import mimetypes
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse, unquote, quote, parse_qs

import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageTk, ImageDraw, ImageFilter, ImageEnhance, ImageSequence

# 人脸识别（numpy + 离线 Haar 级联）。缺失时自动退回显著性主体识别
try:
    import haar_face as _hf
except Exception:
    _hf = None

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# 打包后修复 conda 版 Tcl/Tk 的运行时路径（必须在 import tkinter 之前）
if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
    os.environ.setdefault("TCL_LIBRARY", os.path.join(sys._MEIPASS, "tcl9.0"))
    os.environ.setdefault("TK_LIBRARY", os.path.join(sys._MEIPASS, "tk9.0"))
    os.environ.setdefault("TCL_MODULE_PATH", os.path.join(sys._MEIPASS, "tcl9"))

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

# 小红书等站点：PC 版页面常常只给封面，换移动端 UA 能拿到更完整的图文数据
MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
             "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1")

IMG_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".avif", ".tiff", ".svg", ".jfif"}
MIN_IMAGE_BYTES = 15 * 1024  # 小于 15KB 的视为图标/缩略图，跳过

TIMEOUT = 20
IMG_TIMEOUT = 90          # 单张图片的下载超时：微博原图常有十几 MB，20 秒不够
RETRIES = 2
WORKERS = 6

WIN_W, WIN_H = 680, 560
FONT = "Microsoft YaHei UI"

# ================= 背景图管理（保存到本地，下次打开自动生效） =================

BG_FILE = "背景设置.dat"
# 文件格式：[5字节魔数][1字节模式][主体x/y float][人脸框fx/fy/fw/fh float][JPEG数据]
PAYLOAD_MAGIC = b"WBBG4"
PAYLOAD_MAGIC_V3 = b"WBBG3"
PAYLOAD_MAGIC_OLD = b"WBBG2"

ANCHOR_SUBJECT = 0   # (x, y) = 主体在图片中的归一化位置（裁剪模式时用来居中）
ANCHOR_CROP = 1      # (x, y) = 旧版的裁剪偏移比例（仅用于兼容历史文件）

# 比例相似度阈值：图片与日志区比例 ≥40% 相似 → 完整显示；否则裁剪并让主体居中
ASPECT_SIMILARITY = 0.40

# 渲染缓存：窗口拉伸时避免反复解码大图和重建模糊层
_src_cache = {"key": None, "img": None}
_blur_cache = {"key": None, "img": None}


def parse_bg_payload(payload: bytes):
    """解析背景文件 -> (JPEG 数据, (x, y), 模式, 人脸框或 None)
    人脸框 = (fx, fy, fw, fh) 归一化坐标，用于"含身体"的裁剪构图"""
    if payload[:5] == PAYLOAD_MAGIC and len(payload) >= 30:
        try:
            mode = payload[5]
            x, y, fx, fy, fw, fh = struct.unpack("<6f", payload[6:30])
            x = min(1.0, max(0.0, x))
            y = min(1.0, max(0.0, y))
            face = None
            if 0.0 < fw <= 1.0 and 0.0 < fh <= 1.0 and fw + fh > 0:
                face = (fx, fy, fw, fh)
            if len(payload) > 30:
                return payload[30:], (x, y), mode, face
        except Exception:
            pass
    if payload[:5] == PAYLOAD_MAGIC_V3 and len(payload) >= 14:
        try:
            mode = payload[5]
            x, y = struct.unpack("<ff", payload[6:14])
            x = min(1.0, max(0.0, x))
            y = min(1.0, max(0.0, y))
            if len(payload) > 14:
                return payload[14:], (x, y), mode, None
        except Exception:
            pass
    if payload[:5] == PAYLOAD_MAGIC_OLD and len(payload) >= 13:
        try:
            ax, ay = struct.unpack("<ff", payload[5:13])
            ax = min(1.0, max(0.0, ax))
            ay = min(1.0, max(0.0, ay))
            if len(payload) > 13:
                return payload[13:], (ax, ay), ANCHOR_CROP, None
        except Exception:
            pass
    if payload:
        return payload, (0.5, 0.5), ANCHOR_SUBJECT, None   # 兼容：文件本身就是一张图片
    return None, None, ANCHOR_SUBJECT, None


def build_bg_payload(jpeg: bytes, point, mode=ANCHOR_SUBJECT, face=None) -> bytes:
    x, y = point
    if face and len(face) == 4:
        box = struct.pack("<4f", *face)
    else:
        box = struct.pack("<4f", 0.0, 0.0, 0.0, 0.0)
    return PAYLOAD_MAGIC + bytes([mode & 0xFF]) + struct.pack("<ff", x, y) + box + jpeg


def log_watch_dirs() -> list:
    """需要留意"log 残留"的目录：当前工作目录 + 程序所在目录"""
    dirs = []
    try:
        dirs.append(os.getcwd())
    except OSError:
        pass
    if getattr(sys, "frozen", False):
        dirs.append(os.path.dirname(sys.executable))
    else:
        dirs.append(os.path.dirname(os.path.abspath(__file__)))
    out = []
    for d in dirs:
        if d and d not in out:
            out.append(d)
    return out


def log_folder_states(dirs) -> dict:
    return {d: os.path.isdir(os.path.join(d, "log")) for d in dirs}


def cleanup_new_log_folders(states):
    """个别系统组件（安全软件/输入法等）会在选图过程中往当前目录新建空的 log 文件夹。
    这里把"操作前不存在、操作后新出现且为空"的 log 目录清掉，只删空文件夹，绝对安全。"""
    for d, existed in states.items():
        p = os.path.join(d, "log")
        if existed or not os.path.isdir(p):
            continue
        try:
            if not os.listdir(p):
                os.rmdir(p)
        except OSError:
            pass


def settings_dir() -> str:
    """程序数据目录（背景设置等）：放在用户 AppData 下，不污染程序所在文件夹"""
    local = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(local, "网页图片提取器")


def use_safe_workdir():
    """把当前工作目录切到数据目录。
    这样即使有第三方组件（安全软件、输入法等）往相对路径写 log 之类的文件，
    也只会落在用户数据目录里，不会出现在程序文件夹或桌面上。"""
    try:
        d = settings_dir()
        os.makedirs(d, exist_ok=True)
        os.chdir(d)
    except OSError:
        pass


def default_save_dir() -> str:
    """默认的图片保存位置：桌面，其次程序所在目录"""
    d = os.path.join(os.path.expanduser("~"), "Desktop")
    if os.path.isdir(d):
        return d
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def bg_dirs() -> list:
    """读取背景文件的候选目录：优先用户数据目录；兼容旧版留在程序目录里的文件"""
    dirs = [settings_dir()]
    if getattr(sys, "frozen", False):
        dirs.append(os.path.dirname(sys.executable))
    else:
        dirs.append(os.path.dirname(os.path.abspath(__file__)))
    out = []
    for d in dirs:
        if d and d not in out:
            out.append(d)
    return out


def save_background(data: bytes, point, mode=ANCHOR_SUBJECT, face=None) -> str:
    """把背景保存到用户数据目录（关闭后下次打开仍然生效）。
    返回保存路径，失败返回 None。程序文件夹里不会产生任何文件。"""
    payload = build_bg_payload(data, point, mode, face)
    try:
        d = settings_dir()
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, BG_FILE)
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(payload)
        os.replace(tmp, path)   # 原子替换，避免写到一半被打断
        return path
    except OSError:
        return None


def load_background_bytes():
    """获取当前背景：(JPEG 数据, 主体坐标, 模式, 人脸框)。
    优先本地保存的自定义背景，其次打包内置的默认背景。
    任何读取失败都不抛异常（返回 (None, None, 模式, None)），保证程序总能启动。"""
    for d in bg_dirs():
        try:
            with open(os.path.join(d, BG_FILE), "rb") as f:
                data, point, mode, face = parse_bg_payload(f.read())
            if data:
                return data, point, mode, face
        except FileNotFoundError:
            continue
        except Exception:
            continue

    # 打包时内置的默认背景
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        p = os.path.join(sys._MEIPASS, "default_bg.jpg")
    else:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "default_bg.jpg")
    for _ in range(2):
        try:
            with open(p, "rb") as f:
                return f.read(), (0.5, 0.5), ANCHOR_CROP, None   # 默认背景稍后由 AI 自动识别主体
        except PermissionError:
            time.sleep(1.2)   # 解包瞬间被杀毒软件短暂锁定：等一会儿重试
        except Exception:
            break
    return None, None, ANCHOR_SUBJECT, None


def app_icon_path():
    """定位应用图标 app.ico（打包内置 / 源码同目录），找不到返回 None"""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        p = os.path.join(sys._MEIPASS, "app.ico")
    else:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.ico")
    return p if os.path.exists(p) else None


def normalize_bg_bytes(raw: bytes, max_w: int = 1920) -> bytes:
    """把任意图片统一转成适合嵌入的 JPEG（处理透明、缩小超大图）"""
    img = Image.open(io.BytesIO(raw))
    img.load()
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
        bg.alpha_composite(img)
        img = bg
    img = img.convert("RGB")
    if img.width > max_w:
        img = img.resize((max_w, math.ceil(img.height * max_w / img.width)),
                         Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=88)
    return buf.getvalue()


def _source_image(raw: bytes):
    """解码背景图（带 1 条缓存，避免窗口拉伸时反复解码大图）"""
    key = (len(raw), raw[:32], raw[-32:])
    if _src_cache["key"] != key:
        _src_cache["key"] = key
        _src_cache["img"] = Image.open(io.BytesIO(raw)).convert("RGB")
    return _src_cache["img"]


def aspect_similarity(img_w: int, img_h: int, w: int, h: int) -> float:
    """图片与显示区域的"比例相似度"（0~1）：1 = 长宽比完全一致"""
    a = img_w / img_h
    b = w / h
    return min(a, b) / max(a, b)


def background_mode(src_w: int, src_h: int, w: int, h: int,
                    force_fit: bool = False) -> str:
    """按比例相似度选择显示方式：
    "fit"  = 比例接近（相似度 ≥ 40%）或全屏模式 → 完整显示原图，空隙用同图模糊铺满
    "crop" = 比例差得较多                        → 用完整图片的裁剪部分，主体居中"""
    if force_fit:
        return "fit"
    return "fit" if aspect_similarity(src_w, src_h, w, h) >= ASPECT_SIMILARITY else "crop"


def cover_scale(img_w: int, img_h: int, w: int, h: int, point=None, max_zoom: float = 2.0):
    """裁剪模式下的缩放比例。
    常规 cover：max(w/W, h/H)，总有一根轴刚好铺满、没有裁剪自由度；
    若主体恰好落在那根轴的偏侧位置，就永远无法居中。
    此时按需额外放大（最多 max_zoom 倍），让主体在两根轴上都能居中。"""
    scale = max(w / img_w, h / img_h)
    if not point:
        return scale
    px, py = point[0] * img_w, point[1] * img_h
    need = scale
    for span, view, p in ((img_w, w, px), (img_h, h, py)):
        # 主体到最近边的距离；贴边时限制放大倍数，避免无限放大
        m = max(min(p, span - p), 0.12 * span)
        if m * scale < view / 2:          # 当前比例下这根轴无法让主体居中
            need = max(need, view / (2 * m))
    return min(need, scale * max_zoom)


def _blurred_fill(src: Image.Image, w: int, h: int) -> Image.Image:
    """用同一张图放大模糊后铺满整块区域（作为完整显示时的四周背景）"""
    key = (id(src), w, h)
    if _blur_cache["key"] == key:
        return _blur_cache["img"].copy()
    sw, sh = max(24, w // 8), max(24, h // 8)
    k = max(sw / src.width, sh / src.height)
    tmp = src.resize((max(1, math.ceil(src.width * k)),
                      max(1, math.ceil(src.height * k))), Image.BILINEAR)
    left = max(0, (tmp.width - sw) // 2)
    top = max(0, (tmp.height - sh) // 2)
    tmp = tmp.crop((left, top, left + sw, top + sh))
    tmp = tmp.filter(ImageFilter.GaussianBlur(12)).resize((w, h), Image.BILINEAR)
    base = ImageEnhance.Brightness(tmp).enhance(0.62)   # 压暗，突出中间原图
    _blur_cache["key"], _blur_cache["img"] = key, base
    return base.copy()


def _fit_layer(src: Image.Image, w: int, h: int) -> Image.Image:
    """完整显示：整张图按比例缩到区域内并居中，四周同图模糊铺满，不裁掉任何内容"""
    base = _blurred_fill(src, w, h)
    k = min(w / src.width, h / src.height)
    fw = min(w, max(1, int(round(src.width * k))))
    fh = min(h, max(1, int(round(src.height * k))))
    base.paste(src.resize((fw, fh), Image.LANCZOS), ((w - fw) // 2, (h - fh) // 2))
    return base


def _crop_layer(src: Image.Image, w: int, h: int, point=(0.5, 0.5),
                face=None) -> Image.Image:
    """裁剪显示：从完整图片里裁一块铺满。
    有人脸框时：用最小放大（保证身体也露出来），以取景点为中心，
                并调整位置保证人脸完整可见；
    没有人脸时：沿用主体点居中逻辑（必要时适度放大让点能居中）。"""
    if face:
        scale = max(w / src.width, h / src.height)     # 最小放大 = 露出更多身体
    else:
        scale = cover_scale(src.width, src.height, w, h, point)
    nw = math.ceil(src.width * scale)
    nh = math.ceil(src.height * scale)
    img = src.resize((nw, nh), Image.LANCZOS)
    if point:
        px, py = point[0] * nw, point[1] * nh
        left = int(round(px - w / 2.0))
        top = int(round(py - h / 2.0))
    else:
        left = (nw - w) // 2
        top = (nh - h) // 2
    left = max(0, min(nw - w, left))   # 主体靠近边缘时贴边，图片不出空白
    top = max(0, min(nh - h, top))

    if face:   # 人脸完整可见（留一点余量），避免只显示半张脸
        fx, fy, fw, fh = face
        fx0, fy0 = fx * nw, fy * nh
        fw_p, fh_p = fw * nw, fh * nh
        m = 0.18 * fh_p
        if fy0 - m < top:
            top = max(0, min(nh - h, int(round(fy0 - m))))
        if fy0 + fh_p + m > top + h:
            top = max(0, min(nh - h, int(round(fy0 + fh_p + m - h))))
        if fx0 - m < left:
            left = max(0, min(nw - w, int(round(fx0 - m))))
        if fx0 + fw_p + m > left + w:
            left = max(0, min(nw - w, int(round(fx0 + fw_p + m - w))))
    return img.crop((left, top, left + w, top + h))


def compose_background(raw: bytes, w: int, h: int, point=(0.5, 0.5),
                       face=None, force_fit: bool = False) -> Image.Image:
    """按比例相似度自动选显示方式，并叠加轻微暗色保证白色日志文字可读。
    force_fit=True（全屏）时始终完整显示整张图，剩余区域用同图模糊填充。"""
    src = _source_image(raw)
    if background_mode(src.width, src.height, w, h, force_fit) == "fit":
        img = _fit_layer(src, w, h)
    else:
        img = _crop_layer(src, w, h, point, face)
    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 55))
    return Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")


def render_background(raw: bytes, w: int, h: int, point=(0.5, 0.5), face=None,
                      force_fit: bool = False):
    """渲染成 Tk 可用的图像"""
    return ImageTk.PhotoImage(compose_background(raw, w, h, point, face, force_fit))


# ================= AI 主体识别（离线运行，不需要联网） =================
# 说明：当背景图与日志区比例差异较大时（相似度 < 70%），程序需要裁剪图片，
# 此时用这里的识别结果作为"主体位置"，保证裁剪后主体始终在窗口正中央。
# 全程自动运行（界面上不设按钮），不阻塞操作。
#
# 算法：频谱残差显著性检测（Hou & Zhang, CVPR 2007）
#   1) 把图缩到 64×64 灰度，做 2D FFT，取对数幅度谱减去它的平滑均值
#      （"残差"= 与众不同的部分，通常就是主体），只保留相位重构回空间域；
#   2) 叠加通道饱和度和中心先验做加权；
#   3) 阈值 + 8邻域连通域，取质量最大的区域重心作为主体位置。
# 另外用肤色（YCbCr）区域做一次校正：人物照里人脸/皮肤通常就是主体。
# 纯 Python + Pillow 实现，不引入 numpy/opencv，打包体积不变。

SR_N = 64            # 频谱残差分析分辨率（2 的幂）
MIN_SKIN_AREA = 0.004
MAX_SKIN_AREA = 0.30


def _fft(a):
    """原地基-2 FFT（长度必须是 2 的幂）"""
    n = len(a)
    j = 0
    for i in range(1, n):
        bit = n >> 1
        while j & bit:
            j ^= bit
            bit >>= 1
        j |= bit
        if i < j:
            a[i], a[j] = a[j], a[i]
    length = 2
    while length <= n:
        ang = -2.0 * math.pi / length
        wl = complex(math.cos(ang), math.sin(ang))
        half = length >> 1
        for i in range(0, n, length):
            w = 1 + 0j
            for k in range(i, i + half):
                u = a[k]
                v = a[k + half] * w
                a[k] = u + v
                a[k + half] = u - v
                w *= wl
        length <<= 1


def _fft2(rows, w, h, inverse=False):
    """二维 FFT（先行后列）。inverse 用共轭法实现。"""
    for y in range(h):
        r = rows[y]
        if inverse:
            r = [z.conjugate() for z in r]
        _fft(r)
        if inverse:
            r = [z.conjugate() / w for z in r]
        rows[y] = r
    for x in range(w):
        col = [rows[y][x] for y in range(h)]
        if inverse:
            col = [z.conjugate() for z in col]
        _fft(col)
        if inverse:
            col = [z.conjugate() / h for z in col]
        for y in range(h):
            rows[y][x] = col[y]


def _blur(plane, w, h, r, passes=2):
    """可分离盒式模糊（跑两遍近似高斯），O(n)，比逐点卷积快很多"""
    data = list(plane)
    win = 2 * r + 1
    out = [0.0] * (w * h)
    for _ in range(passes):
        for y in range(h):
            base = y * w
            s = 0.0
            for x in range(-r, r + 1):
                s += data[base + min(w - 1, max(0, x))]
            for x in range(w):
                out[base + x] = s / win
                s -= data[base + min(w - 1, max(0, x - r))]
                s += data[base + min(w - 1, max(0, x + r + 1))]
        data, out = out, data
        for x in range(w):
            s = 0.0
            for y in range(-r, r + 1):
                s += data[min(h - 1, max(0, y)) * w + x]
            for y in range(h):
                out[y * w + x] = s / win
                s -= data[min(h - 1, max(0, y - r)) * w + x]
                s += data[min(h - 1, max(0, y + r + 1)) * w + x]
        data, out = out, data
    return data


def _spectral_residual(gray, w, h):
    """频谱残差显著性图"""
    rows = [[complex(gray[y * w + x]) for x in range(w)] for y in range(h)]
    _fft2(rows, w, h, False)

    logamp = [0.0] * (w * h)
    phase = [1 + 0j] * (w * h)
    for y in range(h):
        for x in range(w):
            z = rows[y][x]
            amp = abs(z)
            logamp[y * w + x] = math.log(1.0 + amp)
            if amp > 1e-9:
                phase[y * w + x] = z / amp

    avg = _blur(logamp, w, h, 3)
    rec_rows = [[0j] * w for _ in range(h)]
    for y in range(h):
        row = rec_rows[y]
        for x in range(w):
            i = y * w + x
            m = math.exp(logamp[i] - avg[i])
            p = phase[i]
            row[x] = complex(m * p.real, m * p.imag)

    _fft2(rec_rows, w, h, True)
    sal = [0.0] * (w * h)
    for y in range(h):
        for x in range(w):
            z = rec_rows[y][x]
            sal[y * w + x] = z.real * z.real + z.imag * z.imag
    return _blur(sal, w, h, 3)


def _components(mask, w, h):
    """8 邻域连通域，返回每个区域包含的像素索引列表"""
    seen = bytearray(w * h)
    comps = []
    for start in range(w * h):
        if not mask[start] or seen[start]:
            continue
        seen[start] = 1
        stack = [start]
        px = []
        while stack:
            k = stack.pop()
            px.append(k)
            x, y = k % w, k // w
            for ny in (y - 1, y, y + 1):
                if ny < 0 or ny >= h:
                    continue
                for nx in (x - 1, x, x + 1):
                    if nx < 0 or nx >= w:
                        continue
                    j = ny * w + nx
                    if mask[j] and not seen[j]:
                        seen[j] = 1
                        stack.append(j)
        comps.append(px)
    return comps


def _centroid(weights, idxs, w):
    """按权重求重心（归一化到 0..1）"""
    tot = 0.0
    sx = sy = 0.0
    for k in idxs:
        v = weights[k]
        tot += v
        sx += v * ((k % w) + 0.5)
        sy += v * (k // w + 0.5)
    if tot <= 1e-9:
        return None
    return sx / tot / w, sy / tot / w


def detect_subject(img):
    """识别图片主体位置。
    返回 (sx, sy, conf)：sx/sy 是主体在图片中的归一化坐标（0~1），conf 是置信度（0~1）。"""
    w = h = SR_N
    try:
        small = img.convert("RGB").resize((w, h), Image.LANCZOS)
        px = list(small.getdata())
    except Exception:
        return 0.5, 0.5, 0.0

    gray = [0.0] * (w * h)
    sat = [0.0] * (w * h)
    skin = bytearray(w * h)
    for i, (r, g, b) in enumerate(px):
        gray[i] = (0.299 * r + 0.587 * g + 0.114 * b) / 255.0
        mx = max(r, g, b)
        mn = min(r, g, b)
        sat[i] = (mx - mn) / mx if mx else 0.0
        # 肤色判定（YCbCr 经典区间）
        yy = 0.299 * r + 0.587 * g + 0.114 * b
        cb = 128 - 0.168736 * r - 0.331264 * g + 0.5 * b
        cr = 128 + 0.5 * r - 0.418688 * g - 0.081312 * b
        if 77 <= cb <= 127 and 133 <= cr <= 173 and yy > 40:
            skin[i] = 1

    sal = _spectral_residual(gray, w, h)

    lo, hi = min(sal), max(sal)
    rng = (hi - lo) or 1.0
    comb = [0.0] * (w * h)
    for y in range(h):
        ny = (y + 0.5) / h - 0.5
        for x in range(w):
            i = y * w + x
            nx = (x + 0.5) / w - 0.5
            prior = math.exp(-((nx / 0.42) ** 2 + (ny / 0.48) ** 2))   # 轻微中心先验
            comb[i] = ((sal[i] - lo) / rng) * (0.62 + 0.38 * prior) * (0.78 + 0.22 * sat[i])

    peak = max(comb) or 1.0

    # --- 显著性最大区域 ---
    comps = _components([v > 0.42 * peak for v in comb], w, h)
    best_c = None
    best_score = -1.0
    for px_list in comps:
        area = len(px_list)
        mass = sum(comb[k] for k in px_list)
        # 面积太大（整图都显著）说明没有明确主体，压低它的分数
        score = mass * (1.0 - 0.75 * (area / (w * h)))
        if score > best_score:
            best_score, best_c = score, px_list
    c1 = _centroid(comb, best_c, w) if best_c else None
    if c1 is None:
        c1 = _centroid(comb, list(range(w * h)), w) or (0.5, 0.5)
    conf = min(1.0, best_score / (peak * 0.25)) if best_score > 0 else 0.0

    # --- 肤色区域校正（人物照的主体往往是脸/人） ---
    skin_area = sum(skin)
    c2 = None
    if MIN_SKIN_AREA * w * h <= skin_area <= MAX_SKIN_AREA * w * h:
        sc = _components(skin, w, h)
        if sc:
            big = max(sc, key=len)
            if len(big) >= MIN_SKIN_AREA * w * h:
                mean_sal = sum(comb[k] for k in big) / len(big)
                if mean_sal >= 0.35 * peak:      # 肤色区域本身也要够"显眼"
                    c2 = _centroid(comb, big, w)
                    if c2:
                        conf = max(conf, 0.6)

    if c2:
        sx = 0.55 * c2[0] + 0.45 * c1[0]
        sy = 0.55 * c2[1] + 0.45 * c1[1]
    else:
        sx, sy = c1
    return min(1.0, max(0.0, sx)), min(1.0, max(0.0, sy)), conf


# ================= 主体识别（人脸优先，显著性兜底） =================

def subject_from_image(img):
    """AI 识别背景主体。返回 (取景点, 人脸框或 None, 描述文字)。
    优先人脸：多人时取最左边的人，取景点落在胸口（保证身体露出来）；
    没有人脸时回退到显著性主体识别。全程离线运行。"""
    if _hf is not None:
        try:
            face, n, _dt = _hf.detect_main_face(img)
            if face is not None:
                t = _hf.face_crop_target(face)
                label = "人脸" if n <= 1 else f"人脸（{n} 人中取最左）"
                return (t["cx"], t["cy"]), \
                       (face["fx"], face["fy"], face["fw"], face["fh"]), label
        except Exception:
            pass
    sx, sy, conf = detect_subject(img)
    return (sx, sy), None, f"主体（置信度 {int(conf * 100)}%）"


# ================= 核心逻辑：图片提取 =================

def sanitize(name: str, maxlen: int = 60) -> str:
    """把网页标题转成合法的文件夹/文件名"""
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', " ", name).strip()
    name = re.sub(r"\s+", " ", name)
    return name[:maxlen] or "未命名网页"


# 网址里允许出现的字符（其余如空白、引号、中文标点、表情一律视为粘贴带进来的杂字）
_URL_CHARS = r"A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%"
# 遇到这些字符即认为网址结束：空白、引号、尖括号、中文标点、表情
_URL_END = (r"\s\"'<>`\u2010-\u205e\u2600-\u27bf\u3000-\u303f"
            r"\ufe0f\uff00-\uffef\U0001F000-\U0001FAFF")
_URL_RE = re.compile(r"https?://[^" + _URL_END + r"]+", re.I)
_BARE_DOMAIN_RE = re.compile(r"(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}(?:/[^" + _URL_END + r"]*)?")


def _clean_url(u: str) -> str:
    """去掉网址尾部误粘进来的标点，并把非 ASCII 字符编码，
    避免 requests 因网址含非法字符而报错。"""
    u = u.rstrip(".,;:!?'\"、，。；：！？）】》”’")
    try:
        return quote(u, safe=":/?#[]@!$&'()*+,;=%~")
    except Exception:
        return u


def extract_url(text: str) -> str:
    """从粘贴的文本里挑出真正的网址。

    用户常直接粘贴整段分享文案（标题 + 表情 + 中文说明 + 链接），
    这里只保留 http(s) 链接本体；没有协议前缀时再尝试识别裸域名。"""
    if not text:
        return ""
    t = text.strip()
    m = _URL_RE.search(t)
    if m:
        return _clean_url(m.group(0))
    # 没有协议前缀：找一个像域名的片段（如 xhslink.com/o/abc123）
    m = _BARE_DOMAIN_RE.search(t)
    if not m:
        return ""
    cand = m.group(0)
    if "." not in cand.split("/")[0]:
        return ""
    return _clean_url("https://" + cand)


def xhs_origin_url(u: str) -> str:
    """把小红书的任意 CDN 地址，换成作者上传的**未压缩原图**地址。

    页面里投放的都是加工过的版本 —— PC 端给 !nd_dft_wlteh_webp_3、移动端给
    !h5_1080jpg，长边一律被压到 1080；而原件一直在 sns-img-qc 图床（腾讯 COS）上，
    凭路径末尾的 fileId 就能直接取到：加 imageView2/0/format/jpg 换成 JPEG 原图
    （实测同一张图 1080x1159/251KB → 1875x2012/682KB）。

    注意：只去 ! 后缀是行不通的 —— 每个展示场景都有各自的签名路径，裸地址一律 403。
    取不到可靠 fileId 时返回空串，交给调用方回退到原地址。
    """
    p = urlparse(u or "")
    if not _XHS_HOST.search(p.netloc or ""):
        return ""
    if "/comment/" in (p.path or ""):        # 评论配图不在成果库里
        return ""
    fid = p.path.rstrip("/").rsplit("/", 1)[-1].split("!", 1)[0]
    fid = _IMG_TAIL_EXT.sub("", fid)
    if not re.fullmatch(r"[a-z0-9]{20,}", fid, re.I):
        return ""
    return f"https://{_XHS_ORIGIN_HOST}/{fid}?{_XHS_ORIGIN_QUERY}"


def upgrade_candidates(u: str) -> list:
    """
    生成"原图候选地址"列表，按最可能是原图的顺序排列（原始地址最后兜底）。
    覆盖常见缩略图规则：云存储缩放参数、WordPress 尺寸后缀、微信宽度号、
    新浪 @参数、!后缀变体、_thumb/-small 等常见后缀。
    """
    cands = []

    def add(x):
        if x and x not in cands and x != u:
            cands.append(x)

    p = urlparse(u)
    query = ("?" + p.query) if p.query else ""

    # 1. 微信公众号图片：路径末尾的数字是宽度，改成 /0 即原图
    m = re.match(r"^(https?://mmbiz\.qpic\.cn/[^?]+?)/\d+(\?\S*)?$", u)
    if m:
        add(m.group(1) + "/0" + (m.group(2) or ""))

    # 1b. 微博图床：/orj360/ /thumb150/ /bmiddle/ /mw690/ 等尺寸目录 → /large/
    #     （实测 /orj360/ 是 360px 缩略图，换成 /large/ 就是 806x1080 的原图）
    m = re.match(r"^(https?://[a-z0-9]+\.sinaimg\.cn)/(?:thumb\d+|small|bmiddle|mw\d+|orj\d+|"
                 r"thumbnail|wap\d+|mini|woriginal)(/.+)$", u, re.I)
    if m:
        add(f"{m.group(1)}/large{m.group(2)}")

    # 1b. 小红书图床：页面投放的都是加工过的版本（PC 端 !nd_dft_wlteh_webp_3、
    #     移动端 !h5_1080jpg，长边被压到 1080），真正的原件保存在 sns-img-qc 图床上。
    #     注意：直接去掉 ! 后缀是无效的 —— 裸地址会被 CDN 拒绝（403，每个场景都有
    #     自己的签名路径）；必须用 fileId 去换，实测 1080x1159 → 1875x2012 原图。
    xo = xhs_origin_url(u)
    if xo:
        add(xo)

    # 1b. 维基百科 thumb 缩略图 → 原图
    m = re.match(r"^(https://upload\.wikimedia\.org/wikipedia/[^/]+)/thumb/(.+?)/[^/]+?(\.\w+)?$", u)
    if m:
        add(f"{m.group(1)}/{m.group(2)}")

    # 2. 云存储图片处理参数（七牛 imageView、阿里 OSS、又拍、缩略等）→ 去掉整个参数串
    if p.query and re.search(
        r"imageView|x-oss-process|imageMogr|thumbnail|watermark|zoom|resize|"
        r"[?&]w=\d|[?&]h=\d|width=|height=|max-?width|style=|s=\d{2,}",
        p.query, re.I):
        add(u.split("?", 1)[0])

    # 3. 新浪等 CDN 的 @压缩参数（...jpg@1e_1c_1280w）
    if "@" in p.path:
        add(f"{p.scheme}://{p.netloc}{p.path.split('@', 1)[0]}{query}")

    # 4. URL 路径中的 ! 变体（...jpg!hd / !list）
    if "!" in p.path:
        add(f"{p.scheme}://{p.netloc}{p.path.split('!', 1)[0]}{query}")

    stem, ext = os.path.splitext(p.path)
    # 5. WordPress / CMS 的 -1024x768 尺寸后缀
    m2 = re.search(r"-(\d{2,5})x(\d{2,5})$", stem)
    if m2:
        add(f"{p.scheme}://{p.netloc}{stem[:m2.start()]}{ext}{query}")

    # 6. 常见缩略图后缀
    low = stem.lower()
    for suf in ("_thumb", "-thumb", ".thumb", "_thumbnail", "-thumbnail",
                "_small", "-small", "_s", "-120x120", "-150x150", "_mid"):
        if low.endswith(suf):
            add(f"{p.scheme}://{p.netloc}{stem[:-len(suf)]}{ext}{query}")
            break

    if u not in cands:
        cands.append(u)  # 原始地址兜底
    return cands


def is_gif_bytes(data: bytes) -> bool:
    """按文件头判断是否为 GIF（不看扩展名，防止后缀名骗人）"""
    return data[:6] in (b"GIF87a", b"GIF89a")


def is_webp_bytes(data: bytes) -> bool:
    """按文件头判断是否为 WebP（小红书等 CDN 会返回 webp，很多软件打不开）"""
    return data[:4] == b"RIFF" and data[8:12] == b"WEBP"


def pixel_dims(data: bytes):
    """只读图片头拿到 (宽, 高)，不解码像素（大图也很快）；失败返回 None。

    用途：把实际下载到的分辨率写进日志，方便确认拿到的是原图而不是缩略图。"""
    try:
        with Image.open(io.BytesIO(data)) as im:
            return im.width, im.height
    except Exception:
        return None


def pixel_area(data: bytes) -> int:
    """图片的像素面积（宽×高），用来在内容重复的图片里挑最清晰的那张。
    解码失败时退回字节数（比较的两张属于同类数据，单位一致，仍然可判大小）。"""
    d = pixel_dims(data)
    return (d[0] * d[1]) if d else (len(data) // 1024)


def images_to_jpgs(data: bytes, seen: set, quality: int = 92,
                   max_frames: int = 1000, want=("GIF",)) -> list:
    """把动图/特殊格式转成 JPEG 字节列表。
    - 动图逐帧转换；静态图得到单张
    - 画面完全相同的帧只保留第一张（seen 可跨文件复用，实现全局去重）
    返回 [(jpeg_bytes, 帧序号)]，没有任何新画面时返回空列表。"""
    out = []
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
    except Exception:
        return out
    if getattr(im, "format", "") not in want:
        return out

    for i, frame in enumerate(ImageSequence.Iterator(im), 1):
        try:
            rgb = frame.convert("RGB")
        except Exception:
            continue
        key = hashlib.md5(rgb.tobytes()).hexdigest()   # 帧画面指纹
        if key in seen:
            continue                                    # 相同画面：只留第一张
        seen.add(key)
        buf = io.BytesIO()
        rgb.save(buf, "JPEG", quality=quality)
        out.append((buf.getvalue(), i))
        if len(out) >= max_frames:
            break
    return out


def filename_from_url(url: str, content_type: str = "", index: int = 0) -> str:
    """从 URL 或响应类型推断文件名"""
    path = unquote(urlparse(url).path)
    base = os.path.basename(path)
    base = re.sub(r'[\\/:*?"<>|]', "_", base)
    ext = os.path.splitext(base)[1].lower()
    if not base or ext not in IMG_EXT:
        guess = mimetypes.guess_extension((content_type or "").split(";")[0].strip())
        ext = guess if guess in IMG_EXT else ".jpg"
        stem = re.sub(r"\W+", "_", base) if base else f"image_{index:03d}"
        base = (stem or f"image_{index:03d}") + ext
    return base


def ext_from(content_type: str, url: str) -> str:
    """按响应类型优先推断扩展名"""
    guess = mimetypes.guess_extension((content_type or "").split(";")[0].strip())
    if guess in IMG_EXT:
        return guess
    e = os.path.splitext(urlparse(url).path)[1].lower()
    return e if e in IMG_EXT else ".jpg"


def make_filename(name_stem, idx: int, content_type: str, url: str,
                  fallback_index: int) -> str:
    """生成保存文件名：微信文章/小红书笔记 -> 标题_序号，其他 -> URL/类型推断"""
    if name_stem:
        return f"{name_stem}_{idx + 1:02d}{ext_from(content_type, url or '')}"
    return filename_from_url(url, content_type, fallback_index)


def pick_largest_from_srcset(srcset: str) -> str:
    """从 srcset 中挑选分辨率最大的候选"""
    best, best_w = None, -1
    for part in srcset.split(","):
        seg = part.strip().split()
        if not seg:
            continue
        url = seg[0]
        w = 0
        if len(seg) > 1 and seg[1].endswith("w"):
            try:
                w = int(seg[1][:-1])
            except ValueError:
                w = 0
        if w >= best_w:
            best, best_w = url, w
    return best


def collect_image_urls(html: str, page_url: str) -> list:
    """收集页面里所有可能的原图地址（含懒加载、srcset 大图、a 链接原图）"""
    soup = BeautifulSoup(html, "lxml")
    found = {}

    def add(u):
        if not u:
            return
        u = urljoin(page_url, u.strip())
        if u.startswith(("http://", "https://")) and u not in found:
            found[u] = True

    for img in soup.find_all("img"):
        # srcset 里最大的那张通常是高清原图
        srcset = img.get("srcset") or img.get("data-srcset") or ""
        if srcset:
            add(pick_largest_from_srcset(srcset))
        # 常见懒加载属性，一般存的是原图
        for attr in ("data-src", "data-original", "data-original-src", "data-lazy-src",
                     "data-real-src", "data-actualsrc"):
            add(img.get(attr))
        add(img.get("src"))
        # 图片被 <a href="大图.jpg"> 包裹时，优先取链接指向的原图
        parent_a = img.find_parent("a")
        if parent_a:
            href = parent_a.get("href") or ""
            if os.path.splitext(urlparse(href).path)[1].lower() in IMG_EXT:
                add(href)

    # <source> 标签（picture 元素）
    for source in soup.find_all("source"):
        srcset = source.get("srcset") or ""
        if srcset:
            add(pick_largest_from_srcset(srcset))

    # 内联样式里的背景大图
    for tag in soup.find_all(style=True):
        for m in re.findall(r'url\((["\']?)(.*?)\1\)', tag["style"]):
            add(m[1])

    # 页面由 JS 动态渲染、标签里只找到一两张图时，再扫一遍内嵌数据里的图片地址
    # （翻页相册、图集站常把整组图藏在脚本变量里，只有翻页后才显示出来）
    if len(found) <= 2:
        flat = unescape_json_url(html)
        extra = 0
        for m in re.finditer(r'https?://[^\s"\'<>\\,]{8,}?\.(?:jpe?g|png|webp|gif|bmp|avif)'
                             r'(?:\?[^\s"\'<>\\]*)?', flat, re.I):
            if extra >= 120:
                break
            u = m.group(0)
            if re.search(r"icon|logo|avatar|sprite|favicon|pixel|blank|placeholder|"
                         r"loading|1x1|advert", u, re.I):
                continue
            n0 = len(found)
            add(u)
            if len(found) > n0:
                extra += 1

    return list(found.keys())


# 小红书等站点：图片藏在页面内嵌的 JSON 数据里，普通 <img> 抓不到
_XHS_HOST = re.compile(r"(xhscdn\.com|xiaohongshu\.com)", re.I)
# 头像 / 表情 / 图标等非正文图，一律不要
_XHS_NOISE = re.compile(r"avatar|emoji|/icon/|sprite|/logo|placeholder|"
                        r"default_cover|/common/|loading", re.I)
# 小红书前端静态资源：笔记图片在 sns-* 图床，而 fe-static / fe-video / fe-platform
# 上是脚本、样式和图标。页面解析失败时（比如笔记已删、跳 404）整页兜底扫描会把
# 它们全当图片收进来，必须按"扩展名 + 域名"挡掉。
_XHS_ASSET_EXT = re.compile(r"\.(?:js|css|ico|json|map|woff2?|ttf|otf|svg|zip|apk|gz|txt|wasm|"
                            r"mp4|m3u8|srt|ts|mov|flv|mkv|m4a|mp3|aac|webm)(?:\?|#|$)", re.I)
# fe-static / fe-video-qc 等是前端资源；sns-video / sns-subtitle 是视频笔记自带的
# 视频流与字幕（.srt），兜底扫描时都会被当成"图片"，必须挡掉
_XHS_STATIC_HOST = re.compile(r"(?:^|\.)fe-|(?:^|\.)sns-(?:video|subtitle|live)", re.I)
_XHS_ASSET_PATH = re.compile(r"/fe-platform-file/|/fe-platform/|/as/v\d+/|/subtitle/|/stream/",
                             re.I)


def json_span_end(s: str, i: int) -> int:
    """s[i] 为 [ 或 {，返回与之配对的收尾字符下标，找不到返回 -1。

    会跳过字符串字面量与反斜杠转义，所以元素内部的嵌套数组（如 imageList
    每张图里的 infoList）不会被误当成数组结尾。这是关键：原来用
    `"imageList":\\[(.*?)\\]` 非贪婪匹配，会被第一张图里的 infoList 提前截断，
    结果一篇多图笔记只能拿到封面。"""
    stack, in_str, esc = [], False, False
    while i < len(s):
        c = s[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c in "[{":
            stack.append(c)
        elif c in "]}":
            if not stack:
                return -1
            stack.pop()
            if not stack:
                return i
        i += 1
    return -1


def unescape_json_url(u: str) -> str:
    """还原 JS/JSON 里的转义（\\u002F 等）"""
    if not isinstance(u, str):
        return ""
    for a, b in (("\\u002F", "/"), ("\\u002f", "/"), ("\\/", "/"),
                 ("\\u0026", "&"), ("\\u003D", "="), ("\\u003F", "?"),
                 ("\\u0025", "%"), ("\\u0023", "#")):
        u = u.replace(a, b)
    return u.strip()


# 预览缩略图特征（!nd_prv_… 等）与常见图片扩展名
_XHS_PREVIEW = re.compile(r"nd_prv|nd_pv|nd_thumb|thumb|_pre$|/pre/|mini", re.I)
_IMG_TAIL_EXT = re.compile(r"\.(?:jpe?g|png|webp|gif|bmp|avif|heic)$", re.I)

# 小红书原图通道：作者上传的原件保存在 sns-img-qc 图床（腾讯 COS），
# 加 imageView2/0/format/jpg 可直接换出未压缩的 JPEG 原图；不加则会拿回 HEIC 原件
_XHS_ORIGIN_HOST = "sns-img-qc.xhscdn.com"
_XHS_ORIGIN_QUERY = "imageView2/0/format/jpg"

# 微博图床的尺寸目录段 → 清晰度（/large/ 与 /original/ 是原图级）
_WB_DIR_SCORE = {
    "large": 3, "original": 3, "bmw": 3, "mw2000": 3, "woriginal": 3,
    "mw690": 2, "bmiddle": 2, "orj480": 2, "orj720": 2, "crop": 1,
}


def image_identity(u: str) -> str:
    """图片身份：只看文件名主干，忽略 CDN 域名、目录层级和尺寸后缀。

    同一张图常给出多个地址（不同 CDN 域名、不同目录、小红书的 !nd_prv / !nd_dft、
    微博的 /orj360/ 与 /large/），只有按主干合并，才能保证一张图只下载一次；
    主干太短（多半是图标）的直接返回空。"""
    seg = urlparse(u).path.rstrip("/").rsplit("/", 1)[-1]
    seg = seg.split("!", 1)[0]                      # 小红书：去掉 !nd_dft_wlteh_webp_3
    seg = _IMG_TAIL_EXT.sub("", seg)
    seg = re.sub(r"[_\-.](?:wm|pre|prv|thumb|small|mini)$", "", seg, flags=re.I)
    return seg if len(seg) >= 8 else ""


def image_quality(u: str) -> int:
    """清晰度打分：3=原图，2=中等，1=缩略图"""
    p = urlparse(u)
    path = p.path
    parts = path.split("/")
    seg = parts[1].lower() if len(parts) > 1 else ""
    if "sinaimg" in p.netloc.lower():              # 微博图床：尺寸在目录段里
        if seg in _WB_DIR_SCORE:
            return _WB_DIR_SCORE[seg]
        if seg.startswith(("orj", "thumb", "small", "wap", "mini")):
            return 1
        return 2
    if p.netloc.lower() == _XHS_ORIGIN_HOST and "imageView2/0" in (p.query or ""):
        return 4                                   # 小红书原图通道：未压缩原件
    if _XHS_PREVIEW.search(path):
        return 1
    return 2 if "!" in path else 3


def xhs_image_lists(html: str) -> list:
    """取出页面内所有 imageList 数组（多图笔记的每一张都在同一个 imageList 里）"""
    lists = []

    # 路子 1：把 window.__INITIAL_STATE__ 当 JSON 解析，递归找全部 imageList（最稳）
    m = re.search(r"window\.__INITIAL_STATE__\s*=\s*", html)
    if m:
        i = html.find("{", m.end())
        j = json_span_end(html, i) if i >= 0 else -1
        if j > i:
            raw = re.sub(r"\bundefined\b", "null", html[i:j + 1])
            try:
                state = json.loads(raw)

                def walk(node):
                    if isinstance(node, dict):
                        for k, v in node.items():
                            if k == "imageList" and isinstance(v, list):
                                lists.append(v)
                            walk(v)
                    elif isinstance(node, list):
                        for v in node:
                            walk(v)

                walk(state)
            except Exception:
                lists = []

    # 路子 2：解析失败时，直接定位 "imageList":[ 并配平取出（不再被嵌套数组截断）
    if not lists:
        for m in re.finditer(r'"imageList"\s*:\s*\[', html):
            i = m.end() - 1
            j = json_span_end(html, i)
            if j > i:
                try:
                    lists.append(json.loads(html[i:j + 1]))
                except Exception:
                    pass
    return lists


def xhs_extra_images(html: str) -> list:
    """从小红书页面内嵌数据中提取笔记的**全部**图片（含轮播/翻页里的每一张）。

    一张图只留最清晰的那个地址：同一张图的预览缩略图（!nd_prv…）会被
    原图/默认展示图替换掉，所以列表里不会出现"高清 + 缩略图"两份。"""
    urls, index, scores = [], {}, []

    def offer(u) -> bool:
        """收录一个地址；返回 True 表示这张图已经有地址了（含替换成更清晰的）"""
        if not isinstance(u, str) or len(u) < 12:
            return False
        u2 = unescape_json_url(u)
        if not u2.startswith(("http://", "https://")):
            return False
        p2 = urlparse(u2)
        if not _XHS_HOST.search(p2.netloc):
            return False
        if _XHS_NOISE.search(u2):
            return False
        if (_XHS_ASSET_EXT.search(p2.path) or _XHS_STATIC_HOST.search(p2.netloc)
                or _XHS_ASSET_PATH.search(p2.path)):
            return False        # 脚本/样式/图标/视频/字幕等，不是笔记图片
        key = image_identity(u2)
        if not key:
            return False
        score = image_quality(u2)
        i = index.get(key)
        if i is None:
            index[key] = len(urls)
            urls.append(u2)
            scores.append(score)
        elif score > scores[i]:          # 同一张图遇到更清晰的版本 → 换掉缩略图
            urls[i] = u2
            scores[i] = score
        return True

    for lst in xhs_image_lists(html):
        for item in lst:
            if isinstance(item, dict):
                # 每张图只贡献一个地址：按清晰度优先顺序试，取到第一个就停
                for k in ("originUrl", "urlDefault", "url", "urlPre"):
                    v = item.get(k)
                    if isinstance(v, dict):          # 少数结构里 url 是个对象
                        v = v.get("url") or v.get("urlDefault")
                    if offer(v):
                        break
                else:
                    # infoList 是同一张图的场景变体（水印/缩略），兜底才用
                    info = item.get("infoList")
                    if isinstance(info, list):
                        for it in info:
                            if isinstance(it, dict) and \
                                    offer(it.get("url") or it.get("urlDefault")):
                                break
            elif isinstance(item, str):
                offer(item)

    # 兜底 1：内嵌数据没读到时，整页按字符串扫一遍小红书 CDN 图片地址
    if len(urls) < 2:
        flat = unescape_json_url(html)
        for mm in re.finditer(r'https?://[^\s"\'<>\\,]{10,}?'
                              r'(?:xhscdn\.com|xiaohongshu\.com)/[^\s"\'<>\\,]+', flat, re.I):
            offer(mm.group(0))
    # 兜底 2：og:image（有些页面只剩封面）
    if not urls:
        for m in re.finditer(r'<meta[^>]+(?:property|name)="(?:og:image|twitter:image)"'
                             r'[^>]+content="([^"]+)"', html):
            offer(m.group(1))
    return urls[:300]


def xhs_fetch_more(sess, page_url: str, timeout: int = TIMEOUT) -> list:
    """PC 页面只读到封面时，换移动端 UA / 移动版地址再取一次。

    小红书 PC 版对未登录访问常常只投放封面（或直接弹登录框），
    而移动版页面（discovery/item）的内嵌数据里通常带着整篇笔记的图片。"""
    tries = [page_url]
    m = re.search(r"/(?:explore|discovery/item)/([0-9a-zA-Z]{16,})", page_url or "")
    if m:
        token = ""
        try:
            token = (parse_qs(urlparse(page_url).query).get("xsec_token") or [""])[0]
        except Exception:
            token = ""
        u = f"https://www.xiaohongshu.com/discovery/item/{m.group(1)}"
        if token:
            u += "?xsec_token=" + token + "&xsec_source=pc_share"
        tries.append(u)

    best = []
    for u in dict.fromkeys(tries):          # 去重后的地址，最多请求 2 次
        try:
            r = sess.get(u, timeout=timeout,
                         headers={"User-Agent": MOBILE_UA,
                                  "Referer": "https://www.xiaohongshu.com/"})
        except Exception:
            continue
        got = xhs_extra_images(r.text or "")
        if len(got) > len(best):
            best = got
        if len(best) >= 2:                  # 已经拿到多图，不用再试
            break
    return best


# ================= 微博 =================

# 微博 PC 页面对真实浏览器只回"访客系统"验证页，对爬虫 UA 才吐正文（实测有效）
CRAWLER_UA = ("Mozilla/5.0 (compatible; Baiduspider/2.0; "
              "+http://www.baidu.com/search/spider.html)")


def is_weibo_url(url: str) -> bool:
    """是否微博域名（weibo.com / weibo.cn / weibo.com.cn；t.cn 短链跳转后也算）"""
    host = (urlparse(url).netloc or "").lower().split(":")[0]
    return bool(re.search(r"(^|\.)(weibo\.(com|cn)|weibo\.com\.cn)$", host))


def weibo_extra_images(html: str) -> list:
    """从微博页面里取出正文图片（按出现顺序）。

    微博正文图都在 wx/ws/ww 图床上（头像在 tva/tvax 上），所以限定这几个域名
    就能天然滤掉头像、图标和视频封面。取图优先级：

    1. `clear_picSrc` —— 微博自己给的"干净图片列表"（URL 编码、逗号分隔），
       多图帖（>9 张）里这是最完整、顺序最准的一份，且不含页面皮肤图；
    2. `<img>` 标签 —— 图片少时页面不给 clear_picSrc，退回到标签里捡；
    3. 全文兜底 —— 先剥掉 `<style>` 块（博主自定义皮肤的 background-image
       藏在这里，实测会被误当正文图），再扫一遍图床链接。

    同一张图的不同 CDN 节点（wx1/wx3）、不同尺寸目录（/orj360/ 与 /mw690/）
    文件名主干相同，按主干合并并保留最清晰的那一档。"""
    text = html.replace("&amp;", "&")
    urls, index, scores = [], {}, []

    def push(u):
        if not u:
            return
        u = u.strip()
        if u.startswith("//"):
            u = "https:" + u
        else:
            u = u.replace("http://", "https://", 1)
        # 只认 wx/ws/ww 图床（正文图都在这几个上）；头像是 tva/tvax，
        # 皮肤背景图是 img.t.sinajs.cn，一律不要
        if not re.match(r"(?:wx|ws|ww)\d*\.sinaimg\.cn$", urlparse(u).netloc, re.I):
            return
        # crop.* 是头像裁剪段，出现在 <img> 里但不是正文图
        if urlparse(u).path.lstrip("/").lower().startswith("crop"):
            return
        key = image_identity(u)
        if not key:
            return
        score = image_quality(u)
        i = index.get(key)
        if i is None:
            index[key] = len(urls)
            urls.append(u)
            scores.append(score)
        elif score > scores[i]:
            urls[i] = u
            scores[i] = score

    # 1. 官方图片列表（多图帖一定带这个）
    for m in re.finditer(r"clear_picSrc=([^\"'&\s]+)", text):
        for part in unquote(m.group(1)).split(","):
            if part.startswith(("//", "http")):
                push(part)

    # 2. <img> 标签（img src / data-src 都算）
    for m in re.finditer(r"<img\b[^>]*?\b(?:data-)?src=[\"']([^\"']+)[\"']", text, re.I):
        if "sinaimg.cn" in m.group(1):
            push(m.group(1))

    # 3. 兜底：剥掉 <style> 后全文扫（皮肤背景图都在 style 里，这样能避开）
    if len(urls) < 2:
        body = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.S | re.I)
        for m in re.finditer(r"(?:https?:)?//(?:wx|ws|ww)\d*\.sinaimg\.cn/[^\"'\\\s<>)]+",
                             body, re.I):
            push(m.group(0))
    return urls[:300]


# ================= 立体触感按钮 =================

class TactileButton(tk.Canvas):
    """渐变 + 软阴影 + 悬停提亮 + 按压下沉的立体按钮（Canvas 绘制）。

    比普通 tk.Button 多了：圆角、渐变面、内高光、投影，
    以及鼠标悬停变亮、按下整体下沉 2px 的"物理"反馈。
    """

    # state -> dict(填充色, 阴影透明度, 下沉像素)
    SCHEMES = {
        "normal":   dict(fill=(0, 0, 0),       sh=120, dy=0),
        "hover":    dict(fill=(28, 28, 32),    sh=150, dy=0),
        "press":    dict(fill=(0, 0, 0),       sh=60,  dy=2),
        "disabled": dict(fill=(152, 152, 158), sh=45,  dy=0),
    }

    def __init__(self, master, text, command, font, w=264, h=64, radius=16, bg="white"):
        self._bw, self._bh, self._br, self._bpad = w, h, radius, 12
        self._font, self._text, self._command = font, text, command
        self._state = "normal"
        self._press_inside = False
        self._enabled = True
        super().__init__(master, width=w + self._bpad * 2, height=h + self._bpad * 2,
                         bg=bg, highlightthickness=0, bd=0, cursor="hand2")
        self._imgs = {s: ImageTk.PhotoImage(self._render(s)) for s in self.SCHEMES}
        cx, cy = self._bpad + w // 2, self._bpad + h // 2
        self._img_id = self.create_image(cx, cy, image=self._imgs["normal"])
        # 文字带一层轻微投影，黑水晶面上更立体
        self._txtsh_id = self.create_text(cx + 1, cy, text=text, font=font,
                                          fill="#0a0a10")
        self._txt_id = self.create_text(cx, cy - 1, text=text, font=font, fill="white")
        self.bind("<Enter>", lambda e: self._set_state("hover"))
        self.bind("<Leave>", lambda e: self._set_state("normal"))
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)

    # ---- 绘制 ----
    def _render(self, state):
        sc = self.SCHEMES[state]
        w, h, r, pad = self._bw, self._bh, self._br, self._bpad

        # 纯黑平面 + 圆角（无高光）
        body = Image.new("RGBA", (w, h), sc["fill"] + (255,))
        mask = Image.new("L", (w, h), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), radius=r, fill=255)
        body.putalpha(mask)

        img = Image.new("RGBA", (w + pad * 2, h + pad * 2), (0, 0, 0, 0))
        sh = Image.new("RGBA", img.size, (0, 0, 0, 0))
        ImageDraw.Draw(sh).rounded_rectangle((pad, pad + 5, pad + w, pad + h + 5),
                                             radius=r, fill=(10, 12, 22, sc["sh"]))
        img.alpha_composite(sh.filter(ImageFilter.GaussianBlur(5)))
        img.alpha_composite(body, (pad, pad + sc["dy"]))
        return img

    # ---- 状态切换 ----
    def _place(self, dy):
        cx, cy = self._bpad + self._bw // 2, self._bpad + self._bh // 2
        self.coords(self._img_id, cx, cy + dy)
        self.coords(self._txt_id, cx, cy - 1 + dy)

    def _set_state(self, st):
        if not self._enabled or self._state == st:
            return
        self._state = st
        self.itemconfig(self._img_id, image=self._imgs[st])
        self._place(self.SCHEMES[st]["dy"])

    def _on_press(self, _e):
        if not self._enabled:
            return
        self._press_inside = True
        self._set_state("press")

    def _on_release(self, e):
        if not self._enabled:
            return
        inside = (0 <= e.x <= self.winfo_width() and 0 <= e.y <= self.winfo_height())
        was_inside = self._press_inside
        self._press_inside = False
        self._set_state("hover" if inside else "normal")
        if was_inside and inside and self._command:
            self._command()

    # ---- 对外接口 ----
    def set_enabled(self, on):
        """启用/禁用按钮（禁用时置灰且不响应）"""
        self._enabled = bool(on)
        self.config(cursor="hand2" if on else "arrow")
        self._set_state("normal" if on else "disabled")
        self._state = "normal" if on else "disabled"
        self.itemconfig(self._img_id, image=self._imgs[self._state])
        self._place(self.SCHEMES[self._state]["dy"])


# ================= 界面 =================

class ImageExtractorApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("网页图片提取器")
        self.root.geometry(f"{WIN_W}x{WIN_H}")
        self.root.minsize(560, 440)
        self.root.resizable(True, True)   # 可拉伸窗口
        self.root.configure(bg="white")

        self.msg_q = queue.Queue()
        self.downloading = False
        self._bg_raw, self._bg_point, self._bg_mode, self._bg_face = load_background_bytes()
        self._bg_photo = None
        self._log_lines = collections.deque(maxlen=500)
        self._log_size = (0, 0)
        self._resize_job = None
        self._fs = False          # 是否处于全屏（全屏时背景始终完整显示整张图）

        self._build_ui()
        self._set_window_icon()
        self._fit_retry = 0
        self.root.after(60, self._fit_window_to_background)   # 打开时按上次背景图比例定窗口
        self.root.bind("<F11>", self._toggle_fullscreen)
        self.root.bind("<Escape>", self._exit_fullscreen)
        self.root.report_callback_exception = self._on_tk_error  # 界面回调异常可见化
        self.root.after(100, self._poll_queue)
        # 内置默认背景 / 旧版背景可能没有主体坐标：后台 AI 识别补上（不阻塞界面）
        if self._bg_raw and self._bg_mode != ANCHOR_SUBJECT:
            threading.Thread(target=self._auto_detect_bg, daemon=True).start()

    def _set_window_icon(self):
        """设置窗口标题栏/任务栏图标（app.ico 不存在则静默跳过）"""
        p = app_icon_path()
        if not p:
            return
        try:
            img = Image.open(p)
            img.load()
            self._icon_photo = ImageTk.PhotoImage(img)   # 保存引用，防止被回收
            self.root.iconphoto(True, self._icon_photo)
        except Exception:
            pass

    def _on_tk_error(self, exc, val, tb):
        """界面回调里的异常：显示在日志区 + 弹窗提示，
        避免"点了没反应"的静默失败（窗口模式下看不出报错）"""
        import traceback
        err = "".join(traceback.format_exception(exc, val, tb))
        tail = err.strip().splitlines()[-1] if err.strip() else "未知错误"
        try:
            self.log_write(f"操作出错：{tail}")
            self.status_var.set("操作出错")
        except Exception:
            pass
        try:
            messagebox.showerror("操作出错", tail)
        except Exception:
            pass

    def _toggle_fullscreen(self, event=None):
        self._fs = not getattr(self, "_fs", False)
        self.root.attributes("-fullscreen", self._fs)
        # 全屏：始终完整显示整张背景图，剩余区域自动用同图模糊填充
        self.root.after(80, self._apply_background)

    def _exit_fullscreen(self, event=None):
        if getattr(self, "_fs", False):
            self._fs = False
            self.root.attributes("-fullscreen", False)
            self.root.after(80, self._apply_background)

    # ---------- 初始窗口：按上次保存的背景图比例调整，使图片刚好完美铺满 ----------
    def _fit_window_to_background(self):
        """按背景图长宽比设置窗口大小：让日志区（背景显示区）的比例与图片一致，
        这样打开时图片刚好完整铺满，无裁剪也无模糊填充边。"""
        if not self._bg_raw:
            return
        try:
            src = _source_image(self._bg_raw)
            iw, ih = src.width, src.height
        except Exception:
            return
        if iw < 2 or ih < 2:
            return
        r = iw / ih                       # 图片长宽比（宽 / 高）

        # 量出"窗口 - 日志区"的固定外框尺寸（标题/按钮/状态栏 + 边距）
        self.root.update_idletasks()
        if self.log_canvas.winfo_width() <= 10 or self.log_canvas.winfo_height() <= 10:
            # 窗口还没完成布局，稍后重试（最多 5 次）
            n = getattr(self, "_fit_retry", 0)
            if n < 5:
                self._fit_retry = n + 1
                self.root.after(60, self._fit_window_to_background)
            return
        cw = self.root.winfo_width() - self.log_canvas.winfo_width()
        ch = self.root.winfo_height() - self.log_canvas.winfo_height()
        if cw <= 0 or ch <= 0:
            return

        # 沿用默认窗口的宽度作为显示区宽度，再按图片比例反解高度（观感与原窗口一致）
        lw = max(WIN_W - cw, 320)
        lh = lw / r

        # 只受屏幕可用范围限制（等比缩放），比例始终不变
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        k = min(1.0, (sw * 0.92 - cw) / lw, (sh * 0.88 - ch) / lh)
        k = max(k, 0.05)
        lw, lh = lw * k, lh * k

        W = int(round(lw + cw))
        H = int(round(lh + ch))
        # 窗口保持最小可用尺寸（极端长条图无法完美填充时在此兜底）
        W, H = max(W, 400), max(H, 400)
        # 关键：暂放宽 minsize，否则窗口管理器会把宽度顶回 560 而破坏比例
        self.root.minsize(min(560, W), min(440, H))
        self.root.geometry(f"{W}x{H}")
        self.root.after(60, self._apply_background)

    # ---------- 界面（pack 布局，窗口可拉伸） ----------
    def _build_ui(self):
        top = tk.Frame(self.root, bg="white")
        top.pack(side="top", fill="x", padx=24, pady=(14, 6))

        # 标题
        tk.Label(top, text="网页图片提取器", font=(FONT, 15, "bold"),
                 bg="white", fg="#111111").pack(pady=(0, 8))

        # 第一行：网址
        row1 = tk.Frame(top, bg="white")
        row1.pack(fill="x")
        tk.Label(row1, text="网址", font=(FONT, 10), bg="white",
                 fg="#444444").pack(side="left")
        self.url_var = tk.StringVar()
        self.url_entry = tk.Entry(row1, textvariable=self.url_var,
                                  font=(FONT, 10), relief="solid")
        self.url_entry.pack(side="left", fill="x", expand=True, padx=(8, 0), ipady=3)

        # 第二行：保存位置 + 更换背景
        row2 = tk.Frame(top, bg="white")
        row2.pack(fill="x", pady=(8, 0))
        tk.Label(row2, text="保存到", font=(FONT, 10), bg="white",
                 fg="#444444").pack(side="left")
        default_dir = default_save_dir()
        self.dir_var = tk.StringVar(value=default_dir)
        tk.Entry(row2, textvariable=self.dir_var, font=(FONT, 9),
                 relief="solid").pack(side="left", fill="x", expand=True,
                                      padx=(8, 6), ipady=2)
        tk.Button(row2, text="选择文件夹", command=self.choose_dir,
                  font=(FONT, 9), relief="flat", bg="#e8e8e8",
                  cursor="hand2").pack(side="left")
        self.bg_btn = tk.Button(row2, text="更换背景", command=self.change_background,
                                font=(FONT, 9), relief="flat", bg="#e8e8e8",
                                cursor="hand2", padx=10)
        self.bg_btn.pack(side="left", padx=(6, 0))

        # 第三行：提取按钮（单独一行，字号放大一倍，立体触感）
        self.go_btn = TactileButton(top, "提取图片", self.start,
                                    font=(FONT, 20, "bold"), w=280, h=68,
                                    radius=18, bg="white")
        self.go_btn.pack(pady=(16, 0))

        # 状态与进度
        self.status_var = tk.StringVar(
            value="粘贴网页/微信/小红书/微博链接后点「提取图片」，F11 可全屏")
        tk.Label(top, textvariable=self.status_var, font=(FONT, 9),
                 bg="white", fg="#555555", anchor="w").pack(fill="x", pady=(10, 2))

        # 黑白主题进度条（覆盖系统默认蓝色）
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("bw.Horizontal.TProgressbar",
                        troughcolor="#e5e5e5", background="#111111",
                        bordercolor="#cccccc", lightcolor="#111111",
                        darkcolor="#111111")
        self.progress = ttk.Progressbar(top, mode="determinate",
                                        style="bw.Horizontal.TProgressbar")
        self.progress.pack(fill="x")

        # 日志区（背景图显示在这个区域内，随窗口拉伸）
        self.log_canvas = tk.Canvas(self.root, bg="#dcdcdc", highlightthickness=1,
                                    highlightbackground="#cccccc")
        self.log_canvas.pack(side="top", fill="both", expand=True,
                             padx=24, pady=(10, 20))
        self.log_canvas.bind("<Configure>", self._on_log_resize)

    # ---------- 背景（只应用在日志区） ----------
    def _on_log_resize(self, event):
        """窗口拉伸时重新渲染背景和日志（防抖）"""
        new_size = (event.width, event.height)
        if new_size == self._log_size or event.width < 10 or event.height < 10:
            return
        self._log_size = new_size
        if self._resize_job:
            self.root.after_cancel(self._resize_job)
        self._resize_job = self.root.after(80, self._apply_background)

    def _apply_background(self):
        self._resize_job = None
        w = self.log_canvas.winfo_width()
        h = self.log_canvas.winfo_height()
        if w < 10 or h < 10:
            return
        self.log_canvas.delete("logbg")
        self.log_canvas.delete("logline")
        self._bg_photo = None
        if self._bg_raw:
            try:
                self._bg_photo = render_background(self._bg_raw, w, h,
                                                   self._bg_point or (0.5, 0.5),
                                                   self._bg_face,
                                                   force_fit=getattr(self, "_fs", False))
                self.log_canvas.create_image(0, 0, anchor="nw",
                                             image=self._bg_photo, tags="logbg")
            except Exception:
                self._bg_photo = None
        self._redraw_log()

    def _auto_detect_bg(self):
        """后台线程：给当前背景做 AI 识别（人脸优先，显著性兜底）"""
        try:
            img = Image.open(io.BytesIO(self._bg_raw))
            img.load()
            point, face, label = subject_from_image(img)
        except Exception:
            return
        self.msg_q.put(("bgsubj", (point, face, label)))

    def _apply_auto_subject(self, res):
        point, face, label = res
        # 识别期间用户可能已手动换过背景（新背景自带主体坐标），不要覆盖
        if not self._bg_raw or self._bg_mode == ANCHOR_SUBJECT:
            return
        self._bg_point, self._bg_face = point, face
        self._bg_mode = ANCHOR_SUBJECT
        self._apply_background()
        # 只有"比例差异大→需要裁剪"时才提示主体居中，比例接近时图片本来就是完整显示
        src = _source_image(self._bg_raw)
        w = max(self.log_canvas.winfo_width(), 10)
        h = max(self.log_canvas.winfo_height(), 10)
        if background_mode(src.width, src.height, w, h,
                           force_fit=getattr(self, "_fs", False)) == "crop":
            how = ("人脸已居中，构图含身体" if face else "背景主体已自动居中")
            self.log_write(f"AI 识别完成：{how}（{label}），拉伸窗口时保持居中")

    def _redraw_log(self):
        self.log_canvas.delete("logline")
        if not self._log_lines:
            return
        w = self.log_canvas.winfo_width()
        h = self.log_canvas.winfo_height()
        line_h = 19
        max_lines = max(1, (h - 16) // line_h)
        max_chars = max(20, (w - 24) // 8)
        lines = list(self._log_lines)[-max_lines:]
        y = 8
        for line in lines:
            if len(line) > max_chars:
                line = line[:max_chars - 1] + "…"
            if self._bg_photo is not None:
                self.log_canvas.create_text(13, y, text=line, anchor="nw",
                                            font=(FONT, 9), fill="#222222",
                                            tags="logline")
                self.log_canvas.create_text(12, y - 1, text=line, anchor="nw",
                                            font=(FONT, 9), fill="white",
                                            tags="logline")
            else:
                self.log_canvas.create_text(13, y, text=line, anchor="nw",
                                            font=(FONT, 9), fill="#333333",
                                            tags="logline")
            y += line_h

    def log_write(self, text: str):
        self._log_lines.append(text)
        self._redraw_log()

    def choose_dir(self):
        states = log_folder_states(log_watch_dirs())
        try:
            d = filedialog.askdirectory(title="选择保存位置")
            if d:
                self.dir_var.set(d)
        finally:
            cleanup_new_log_folders(states)

    # ---------- 更换背景（选图后直接生效，不再弹预览） ----------
    def change_background(self):
        # 记录操作前状态，结束后清掉过程中新出现的空 log 残留文件夹
        states = log_folder_states(log_watch_dirs())
        try:
            self._change_background_inner()
        finally:
            cleanup_new_log_folders(states)

    def _change_background_inner(self):
        path = filedialog.askopenfilename(
            title="选择背景图片",
            filetypes=[("图片文件", "*.jpg *.jpeg *.png *.bmp *.webp"),
                       ("所有文件", "*.*")])
        if not path:
            return
        try:
            with open(path, "rb") as f:
                raw = f.read()
            img = Image.open(io.BytesIO(raw))
            img.load()
        except Exception as e:
            messagebox.showerror("更换背景失败", f"无法读取这张图片：\n{e}")
            return

        # 当前日志区尺寸，用于判断背景是"完整显示"还是"裁剪显示"
        w = self.log_canvas.winfo_width()
        h = self.log_canvas.winfo_height()

        # AI 自动识别主体：优先人脸（多人取最左，构图含身体），无人脸用显著性主体
        self.status_var.set("正在 AI 识别图片主体…")
        try:
            self.root.config(cursor="watch")
            self.root.update_idletasks()
        except Exception:
            pass
        try:
            point, face, label = subject_from_image(img)
        except Exception:
            point, face, label = (0.5, 0.5), None, ""
        finally:
            try:
                self.root.config(cursor="")
            except Exception:
                pass

        # 不弹预览，选图后直接生效（取消机会在文件选择框，点取消即不变更）
        try:
            data = normalize_bg_bytes(raw)
        except Exception as e:
            messagebox.showerror("更换背景失败", f"图片处理失败：\n{e}")
            return
        self._bg_raw, self._bg_point = data, point
        self._bg_face, self._bg_mode = face, ANCHOR_SUBJECT
        self._apply_background()

        saved = save_background(data, point, ANCHOR_SUBJECT, face)
        if saved:
            # 不弹窗，只在日志区提示
            src = _source_image(data)
            if background_mode(src.width, src.height, max(w, 10), max(h, 10),
                               force_fit=getattr(self, "_fs", False)) == "fit":
                how = ("全屏模式：完整显示整张图片，剩余区域同图模糊填充"
                       if getattr(self, "_fs", False)
                       else "图片完整显示（比例接近窗口，四周同图模糊铺满）")
            elif face:
                how = f"图片裁剪显示，{label}已居中，构图含身体"
            else:
                how = (f"图片裁剪显示，主体自动居中"
                       f"（AI 识别位置 {point[0] * 100:.0f}%，{point[1] * 100:.0f}%）")
            self.log_write(f"背景已确定 ✔ {how}，拉伸窗口时保持居中，下次打开仍生效")
            self.status_var.set("背景已更换 ✔")
        else:
            messagebox.showwarning(
                "背景已应用，但保存失败",
                "新背景已在本次使用中生效，但无法写入本地文件，\n"
                "关闭程序后可能丢失。\n\n"
                "可以把程序放到有写入权限的文件夹（比如桌面）再试一次。")

    def _poll_queue(self):
        try:
            while True:
                kind, data = self.msg_q.get_nowait()
                if kind == "log":
                    self.log_write(data)
                elif kind == "status":
                    self.status_var.set(data)
                elif kind == "progress":
                    done, total = data
                    self.progress["maximum"] = max(total, 1)
                    self.progress["value"] = done
                elif kind == "bgsubj":
                    self._apply_auto_subject(data)
                elif kind == "done":
                    self._finish(data)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)

    def _finish(self, summary):
        self.downloading = False
        self.go_btn.set_enabled(True)
        if summary.get("ok"):
            msg = (f"完成！共下载 {summary['ok_n']} 张图片（跳过 {summary['skip_n']} 张、失败 {summary['fail_n']} 张）\n"
                   f"保存位置：{summary['folder']}")
            self.status_var.set("提取完成 ✔")
            messagebox.showinfo("提取完成", msg)
        else:
            self.status_var.set("提取失败")
            messagebox.showerror("提取失败", summary.get("error", "未知错误"))

    # ---------- 主流程 ----------
    def start(self):
        if self.downloading:
            return
        raw = self.url_var.get().strip()
        if not raw:
            messagebox.showwarning("提示", "请先输入网址")
            return
        # 用户常常直接粘贴整段分享文案（标题 + 表情 + 中文 + 链接），先挑出真正的网址
        url = extract_url(raw)
        if not url:
            messagebox.showwarning(
                "提示",
                "没识别出有效网址。\n\n"
                "请只粘贴链接本身，例如：\n"
                "https://mp.weixin.qq.com/s/xxxxxx\n"
                "http://xhslink.com/o/xxxxxx\n"
                "https://weibo.com/1234567890/AbCdEfGhI")
            return
        if url != raw:
            self.url_var.set(url)      # 回填清洗后的链接，方便确认

        self.downloading = True
        self.go_btn.set_enabled(False)
        self._log_lines.clear()
        self._redraw_log()
        self.progress["value"] = 0
        self.status_var.set("正在获取网页…")
        save_root = self.dir_var.get() or default_save_dir()
        threading.Thread(target=self._run, args=(url, save_root), daemon=True).start()

    def _run(self, url: str, save_root: str):
        q = self.msg_q
        try:
            q.put(("log", f"正在访问：{url}"))
            sess = requests.Session()
            sess.headers.update(HEADERS)
            # 微博：正文页只对爬虫 UA 开放（真实浏览器 UA 会撞"Sina Visitor System"），
            # 所以一上来就用爬虫 UA，省一次往返
            first = ({"User-Agent": CRAWLER_UA, "Referer": "https://weibo.com/"}
                     if is_weibo_url(url) else None)
            resp = sess.get(url, timeout=TIMEOUT, headers=first)
            resp.raise_for_status()
            resp.encoding = resp.apparent_encoding or "utf-8"

            # 短链（t.cn 之类）跳转后才暴露是微博，这时再换爬虫 UA 重读一次
            if is_weibo_url(resp.url or url) and \
                    ("Sina Visitor System" in resp.text or "sinaimg.cn" not in resp.text):
                try:
                    r2 = sess.get(resp.url or url, timeout=TIMEOUT,
                                  headers={"User-Agent": CRAWLER_UA,
                                           "Referer": "https://weibo.com/"})
                    if r2.ok and len(r2.text) > len(resp.text):
                        resp = r2
                        resp.encoding = resp.apparent_encoding or "utf-8"
                        q.put(("log", "微博正文页需要爬虫视图，已自动切换重读"))
                except requests.RequestException as e:
                    q.put(("log", f"微博重新读取失败：{e}"))

            soup = BeautifulSoup(resp.text, "lxml")
            title_tag = soup.title
            title_text = title_tag.get_text(strip=True) if title_tag else ""
            og = soup.find("meta", attrs={"property": "og:title"})
            if og and og.get("content", "").strip():
                title_text = og["content"].strip()
            # 微博标题尾巴都是"…来自某某 - 微博"，去掉更干净
            if is_weibo_url(resp.url or url):
                title_text = re.sub(r"\s*[-_]\s*微博$", "", title_text).strip()
            folder_name = sanitize(title_text) if title_text else sanitize(urlparse(url).netloc)
            out_dir = os.path.join(save_root, folder_name)
            os.makedirs(out_dir, exist_ok=True)
            q.put(("log", f"网页标题：{title_text or folder_name}"))
            q.put(("log", f"保存文件夹：{out_dir}"))

            # 微信公众号文章 / 小红书笔记 / 微博：图片文件名用"标题 + 序号"
            is_wechat = ("mp.weixin.qq.com" in url
                         or "mmbiz.qpic.cn" in resp.text
                         or any("mmbiz.qpic.cn" in u for u in [resp.url]))
            page_url = resp.url or url          # 短链跳转后的真实地址，用作 Referer
            is_xhs_page = bool(_XHS_HOST.search(urlparse(page_url).netloc))
            is_weibo_page = is_weibo_url(page_url) or is_weibo_url(url)
            name_stem = (sanitize(title_text, 40)
                         if ((is_wechat or is_xhs_page or is_weibo_page) and title_text)
                         else None)

            # 微信公众号：正文图都在 #js_content 容器里，只在容器内取图，
            # 免得把正文外的作者头像、公众号二维码、相关阅读缩略图一起下下来
            scope_html = resp.text
            if is_wechat:
                scope = (soup.select_one("#js_content")
                         or soup.select_one(".rich_media_content")
                         or soup.select_one("#js_article"))
                if scope is not None and scope.find("img") is not None:
                    scope_html = str(scope)
            img_urls = collect_image_urls(scope_html, url)
            # 微博：正文图都在 wx/ws/ww 图床上（页面只给 /orj360/ 缩略图，
            # 下载时会被自动换成 /large/ 原图），头像/推荐位一律不要
            if is_weibo_page:
                wb = weibo_extra_images(resp.text)
                if len(wb) >= 2:
                    img_urls = wb
                    q.put(("log", f"识别到微博正文图片 {len(wb)} 张（下载时自动换原图）"))
                elif wb:
                    img_urls = wb + [u for u in img_urls if u not in wb]
            # 小红书：整篇笔记（含轮播/翻页）的图片都放在内嵌数据的 imageList 里
            xhs = xhs_extra_images(resp.text)
            if is_xhs_page and len(xhs) < 2:
                # 内嵌数据读不全（未登录/PC 版只给封面）时，换移动端页面再试一次
                more = xhs_fetch_more(sess, page_url)
                if len(more) > len(xhs):
                    q.put(("log", f"改用移动端页面重新解析，读到 {len(more)} 张"))
                    xhs = more
            if xhs:
                if is_xhs_page and len(xhs) >= 2:
                    img_urls = xhs      # 只留正文图，避免头像、推荐位缩略图混进来
                else:
                    img_urls = xhs + [u for u in img_urls if u not in xhs]
                q.put(("log", f"识别到小红书笔记图片 {len(xhs)} 张"
                              f"（多图笔记里每一张都已包含）"))
                if len(xhs) == 1:
                    q.put(("log", "提示：只读到封面。建议用手机 App 里的「分享 → 复制链接」"
                                  "再试一次（浏览器地址栏里复制的网址可能缺少必要参数）"))
            # 为每个地址生成"原图候选"，并去重（不同标签常指向同一张图）
            tasks = []
            seen_tasks = set()
            for u in img_urls:
                key = tuple(upgrade_candidates(u))
                if key not in seen_tasks:
                    seen_tasks.add(key)
                    tasks.append(list(key))
            total = len(tasks)
            q.put(("log", f"共发现 {total} 个图片，开始下载原图…"))
            q.put(("status", f"发现 {total} 张图片，下载原图中…"))
            if not total:
                if is_xhs_page:
                    extra = ("\n小红书笔记的图片需要登录态才能读到，\n"
                             "可以先用浏览器打开该笔记，把图片另存后再处理。")
                elif is_weibo_page:
                    extra = ("\n微博对未登录访问有频率限制（可能返回验证页），\n"
                             "过几分钟再试通常就好了；也可以换用手机 App 的分享链接。")
                else:
                    extra = ""
                q.put(("done", {"ok": False,
                                "error": "页面上没有发现图片。\n"
                                         "可能是动态加载的网页（需要登录或滚动加载），\n"
                                         "请换一个网址试试。" + extra}))
                return

            used_names = set()
            downloaded_urls = set()
            seen_gif_frames = set()   # GIF 帧画面指纹（跨文件全局去重）
            seen_digest = {}          # 文件内容 md5 -> (文件名, 像素面积)：只留清晰的那张
            ok_n = skip_n = fail_n = dup_n = done_n = 0

            def unique_name(nm):
                """同名时自动加序号，保证不覆盖已经写出的文件"""
                st, ex = os.path.splitext(nm)
                cand, k = nm, 1
                while cand.lower() in used_names:
                    cand = f"{st}_{k}{ex}"
                    k += 1
                used_names.add(cand.lower())
                return cand

            def fetch(u):
                """下载单个地址，成功返回 (数据, Content-Type)，失败返回 (None, 错误)"""
                last_err = None
                for _ in range(RETRIES + 1):
                    try:
                        r = sess.get(u, timeout=IMG_TIMEOUT, stream=True,
                                     headers={"Referer": page_url})
                        r.raise_for_status()
                        ctype = r.headers.get("Content-Type", "")
                        if ctype and not ctype.startswith("image/"):
                            return None, "非图片内容"
                        data = r.content
                        if len(data) < MIN_IMAGE_BYTES:
                            return None, "太小(图标)"
                        return (data, ctype), None
                    except Exception as e:
                        last_err = e
                return None, str(last_err)

            def download(idx_cands):
                """按候选顺序尝试，返回 (idx, url, 结果, err, upgraded)"""
                idx, cands = idx_cands
                last_err = None
                for i, c in enumerate(cands):
                    if c in downloaded_urls:
                        return idx, None, None, "dup", False
                    res, err = fetch(c)
                    if res is not None:
                        downloaded_urls.add(c)
                        return idx, c, res, None, (i < len(cands) - 1)
                    if err == "太小(图标)":
                        return idx, None, None, "icon", False
                    last_err = err
                return idx, None, None, last_err, False

            with ThreadPoolExecutor(max_workers=WORKERS) as pool:
                futures = [pool.submit(download, (i, cands))
                           for i, cands in enumerate(tasks)]
                for fut in as_completed(futures):
                    idx, u, res, err, upgraded = fut.result()
                    done_n += 1
                    if err == "dup":
                        dup_n += 1
                    elif err == "icon":
                        skip_n += 1
                        q.put(("log", f"跳过(图标)  {u or ''}"))
                    elif err:
                        fail_n += 1
                        q.put(("log", f"失败        {u}  ({err})"))
                    else:
                        data, ctype = res
                        name = make_filename(name_stem, idx, ctype, u, ok_n + 1)
                        stem, ext = os.path.splitext(name)
                        mark = "  [原图]" if upgraded else ""

                        # 同一张图常同时给出缩略图和高清图两个地址（甚至跨域名），
                        # 这里按文件内容再去一次重：内容一样就只留像素最大的那张
                        dig = hashlib.md5(data).hexdigest()
                        dims = pixel_dims(data)
                        area = (dims[0] * dims[1]) if dims else (len(data) // 1024)
                        size_tag = f"  {dims[0]}x{dims[1]}" if dims else ""
                        prev = seen_digest.get(dig)
                        if prev and area <= prev[1]:
                            skip_n += 1
                            q.put(("log", f"跳过(同一张图已下载)  {name}"))
                        else:
                            written = []

                            # GIF 拆帧、WEBP 转码：动图逐帧，画面相同的帧只留第一张
                            if is_gif_bytes(data):
                                kind, want = "GIF", ("GIF",)
                            elif is_webp_bytes(data):
                                kind, want = "WEBP", ("WEBP",)
                            else:
                                kind, want = "", None
                            frames = (images_to_jpgs(data, seen_gif_frames, want=want)
                                      if want else [])

                            if frames:
                                many = len(frames) > 1
                                for k, (fb, fno) in enumerate(frames, 1):
                                    fn = f"{stem}_{k:02d}.jpg" if many else f"{stem}.jpg"
                                    fn = unique_name(fn)
                                    try:
                                        with open(os.path.join(out_dir, fn), "wb") as f:
                                            f.write(fb)
                                        ok_n += 1
                                        written.append(fn)
                                        tag = (f"  [{kind} 第 {fno} 帧 → {k}/{len(frames)}]"
                                               if many else f"  [{kind} 转 JPG]")
                                        d2 = pixel_dims(fb)
                                        st2 = f"  {d2[0]}x{d2[1]}" if d2 else ""
                                        q.put(("log", f"已下载 ✔  {fn}  "
                                                     f"({len(fb) // 1024} KB{st2}){tag}{mark}"))
                                    except OSError as e:
                                        fail_n += 1
                                        q.put(("log", f"写入失败    {u}  ({e})"))
                            elif want == ("GIF",):
                                skip_n += 1
                                q.put(("log", f"跳过(GIF 里没有新画面)  {name}"))
                            else:
                                # 普通图片，或 WEBP 转换失败时原样写入
                                candidate = unique_name(name)
                                try:
                                    with open(os.path.join(out_dir, candidate), "wb") as f:
                                        f.write(data)
                                    ok_n += 1
                                    written.append(candidate)
                                    q.put(("log", f"已下载 ✔  {candidate}  "
                                                 f"({len(data)//1024} KB{size_tag}){mark}"))
                                except OSError as e:
                                    fail_n += 1
                                    q.put(("log", f"写入失败    {u}  ({e})"))

                            if written:
                                seen_digest[dig] = (written[0], area)
                                if prev:        # 这张更清晰 → 撤掉先前存的缩略图
                                    try:
                                        os.remove(os.path.join(out_dir, prev[0]))
                                        q.put(("log", f"已用更清晰的版本替换 {prev[0]}"))
                                    except OSError:
                                        pass
                    q.put(("progress", (done_n, total)))
                    q.put(("status", f"进度：{done_n}/{total}"))

            q.put(("done", {"ok": True, "ok_n": ok_n, "skip_n": skip_n + dup_n,
                            "fail_n": fail_n, "folder": out_dir}))
        except requests.exceptions.InvalidURL:
            q.put(("done", {"ok": False,
                            "error": "网址格式不正确，无法解析。\n"
                                     "请只粘贴链接本身（以 http:// 或 https:// 开头），\n"
                                     "不要带上标题、表情或中文说明。"}))
        except requests.exceptions.MissingSchema:
            q.put(("done", {"ok": False,
                            "error": "这个链接缺少 http:// 或 https:// 前缀，\n请补全后重试。"}))
        except requests.exceptions.SSLError:
            q.put(("done", {"ok": False, "error": "HTTPS 证书校验失败，请检查网址是否正确。"}))
        except requests.exceptions.ConnectionError:
            q.put(("done", {"ok": False, "error": "无法连接该网址，请检查网络或网址是否有效。"}))
        except requests.exceptions.Timeout:
            q.put(("done", {"ok": False, "error": "访问超时，请稍后重试。"}))
        except requests.exceptions.RequestException as e:
            q.put(("done", {"ok": False, "error": f"访问失败：{e}"}))
        except Exception as e:
            q.put(("done", {"ok": False, "error": str(e)}))


def main():
    root = None
    use_safe_workdir()   # 工作目录切到用户数据目录，程序文件夹保持干净
    try:
        root = tk.Tk()
        try:
            from ctypes import windll
            windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
        ImageExtractorApp(root)
        root.mainloop()
    except Exception:
        import traceback
        import tkinter.messagebox as mb
        err = traceback.format_exc()
        try:
            mb.showerror("启动出错", err[-1500:])
        except Exception:
            pass
        raise


if __name__ == "__main__":
    main()
