"""人脸检测（numpy 向量化复刻 OpenCV Haar 级联语义，完全离线、不需要联网）

依据 opencv 4.x modules/objdetect/src/cascadedetect.{hpp,cpp}：
  * 按尺度把【图像】缩放，窗口恒为 origWinSize(24x24)，特征矩形不做任何缩放
  * normrect = Rect(1,1,22,22)，方差在该 22x22 区域上算
  * varianceNormFactor = 1/sqrt(area*valsqsum - valsum^2) = 1/(A*σ)
  * 特征值 = Σ(xml权重 × 矩形积分和) * varianceNormFactor，与阈值直接比较
  * 门槛 area*vnf < 0.1  （即 σ > 10）的低对比度窗口直接丢弃
  * stump: value < threshold ? left : right

对外接口：
  detect_main_face(img)  -> (最左边的人脸 dict 或 None, 全部人脸列表)
  face_crop_target(face) -> 含身体的取景点 dict
"""
import os
import re
import sys
import time

import numpy as np
from PIL import Image


def _xml_path():
    """级联数据文件：打包后从解包目录取，开发时与脚本同目录"""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return os.path.join(sys._MEIPASS, "haar_face.xml")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "haar_face.xml")


def parse_cascade(path):
    text = open(path, encoding="utf-8", errors="ignore").read()
    fs, fe = text.index("<features>"), text.index("</features>")
    feat_sec = text[fs:fe]
    features = []
    for block in re.findall(r"<rects>(.*?)</rects>", feat_sec, re.S):
        nums = [float(t) for t in re.findall(r"-?\d+\.?\d*", re.sub(r"<[^>]*>", " ", block))]
        features.append([tuple(nums[i:i + 5]) for i in range(0, len(nums), 5)])
    stages = []
    sths = re.findall(r"<stageThreshold>(.*?)</stageThreshold>", text)
    wblocks = re.findall(r"<weakClassifiers>(.*?)</weakClassifiers>", text, re.S)
    for sth, blk in zip(sths, wblocks):
        nodes = re.findall(r"<internalNodes>(.*?)</internalNodes>", blk, re.S)
        leaves = re.findall(r"<leafValues>(.*?)</leafValues>", blk, re.S)
        wcs = []
        for n, l in zip(nodes, leaves):
            nn = [float(t) for t in re.findall(r"-?\d+\.?\d*(?:[eE][-+]?\d+)?", re.sub(r"<[^>]*>", " ", n))]
            ll = [float(t) for t in re.findall(r"-?\d+\.?\d*(?:[eE][-+]?\d+)?", re.sub(r"<[^>]*>", " ", l))]
            wcs.append((int(nn[2]), nn[3], ll[0], ll[1]))    # featureIdx, threshold, left, right
        stages.append((float(sth), wcs))
    return features, stages


_F, _S = parse_cascade(_xml_path())
_FEAT = [np.asarray(r, np.float64) for r in _F]   # (n,5): x y w h weight（原始权重）
WIN = 24          # origWinSize
NORM = (1, 1, 22, 22)   # normrect: x, y, w, h
NORM_A = float(NORM[2] * NORM[3])


def _integrals(gray):
    a = gray.astype(np.int64)
    ii = np.zeros((a.shape[0] + 1, a.shape[1] + 1), np.int64)
    ii[1:, 1:] = a.cumsum(0).cumsum(1)
    b = gray.astype(np.float64)
    iq = np.zeros((a.shape[0] + 1, a.shape[1] + 1), np.float64)
    iq[1:, 1:] = (b * b).cumsum(0).cumsum(1)
    return ii, iq


def _rs(II, x0, y0, x1, y1):
    return II[y1, x1] - II[y0, x1] - II[y1, x0] + II[y0, x0]


def detect_raw(gray, scale_factor=1.1, max_dim=288, step=2, min_sigma=10.0):
    """返回 (检测框列表, 到原图的缩放系数 k)。
    检测框为原图坐标 (x, y, w, h)，与 OpenCV detectMultiScale 同构。
    step: 扫描步长（OpenCV 为 1，此处取 2 以提速；背景图定位主体不需要像素级精度）"""
    H0, W0 = gray.shape
    k = min(1.0, max_dim / float(max(H0, W0)))
    if k < 1.0:
        gray = np.asarray(Image.fromarray(gray).resize(
            (max(1, int(round(W0 * k))), max(1, int(round(H0 * k)))), Image.BILINEAR))
    H, W = gray.shape

    dets = []
    sc = 1.0                      # 窗口 24px 起步（≈ 图像长边的 8%）
    while True:
        wsize = int(round(WIN * sc))
        if wsize > min(H, W):
            break
        sw = int(round(W / sc))
        sh = int(round(H / sc))
        if sw < WIN or sh < WIN:
            break
        small = np.asarray(Image.fromarray(gray).resize((sw, sh), Image.BILINEAR)) \
            if (sw != W or sh != H) else gray
        II, IQ = _integrals(small)
        Hh, Ww = small.shape

        xs = np.arange(0, Ww - WIN + 1, step)
        ys = np.arange(0, Hh - WIN + 1, step)
        if xs.size and ys.size:
            gx, gy = np.meshgrid(xs, ys)
            X = gx.ravel().astype(np.int32)
            Y = gy.ravel().astype(np.int32)

            # ---- σ 门槛（normrect 是固定的 22x22，不随尺度变化）----
            nx0, ny0 = NORM[0], NORM[1]
            nx1, ny1 = nx0 + NORM[2], ny0 + NORM[3]
            s1 = _rs(II, X + nx0, Y + ny0, X + nx1, Y + ny1).astype(np.float64)
            s2 = _rs(IQ, X + nx0, Y + ny0, X + nx1, Y + ny1)
            nf = NORM_A * s2 - s1 * s1
            ok = nf > 0.0
            vnf = np.zeros_like(nf)
            if ok.any():
                vnf[ok] = 1.0 / np.sqrt(nf[ok])      # = 1/(A*σ)
            gate = ok & (NORM_A * vnf < 1e-1)        # σ > 10 的低对比度窗口丢弃
            X, Y, vnf = X[gate], Y[gate], vnf[gate]

            # ---- 级联逐层筛选 ----
            passed = np.ones(X.shape[0], bool)
            for sth, wcs in _S:
                if not passed.any():
                    break
                X, Y, vnf = X[passed], Y[passed], vnf[passed]
                ssum = np.zeros(X.shape[0], np.float64)
                for fi, fthr, l0, l1 in wcs:
                    val = np.zeros(X.shape[0], np.float64)
                    for rx, ry, rw, rh, wgt in _FEAT[fi]:
                        x0 = X + int(rx); x1 = x0 + int(rw)
                        y0 = Y + int(ry); y1 = y0 + int(rh)
                        val += wgt * _rs(II, x0, y0, x1, y1)
                    val *= vnf
                    ssum += np.where(val < fthr, l0, l1)
                passed = ssum > sth
            if passed.any():
                for x, y in zip(X[passed].tolist(), Y[passed].tolist()):
                    dets.append((int(round(x * sc)), int(round(y * sc)), wsize))
        sc *= scale_factor
    return dets, k


class _SR:
    """OpenCV SimilarRects(eps=0.2)"""
    def __init__(self, eps=0.2):
        self.eps = eps

    def __call__(self, a, b):
        d = self.eps * 0.5 * (min(a[2], b[2]) + min(a[3], b[3]))
        return (abs(a[0] - b[0]) <= d and abs(a[1] - b[1]) <= d and
                abs(a[2] - b[2]) <= d and abs(a[3] - b[3]) <= d)


def group_rectangles(dets, min_neighbors=3, eps=0.2):
    """复刻 OpenCV groupRectangles：簇内数量必须 > min_neighbors"""
    if len(dets) < 2:
        return []
    rects = [(x, y, w, w) for (x, y, w) in dets]
    sr = _SR(eps)
    n = len(rects)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(n):
        for j in range(i + 1, n):
            if sr(rects[i], rects[j]):
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[ri] = rj
    clusters = {}
    for i in range(n):
        clusters.setdefault(find(i), []).append(rects[i])
    out = []
    for members in clusters.values():
        if len(members) <= min_neighbors:
            continue
        c = len(members)
        out.append((sum(m[0] for m in members) / c,
                    sum(m[1] for m in members) / c,
                    sum(m[2] for m in members) / c,
                    sum(m[3] for m in members) / c,
                    c))
    return out


def detect_faces(img, scale_factor=1.1, max_dim=288, step=2, min_neighbors=3):
    """img: PIL Image。返回 (人脸列表, 耗时)。
    每个人脸: {"fx","fy","fw","fh","cx","cy","members"}（均为原图归一化坐标）"""
    gray = np.asarray(img.convert("L"))
    H0, W0 = gray.shape
    t0 = time.time()
    dets, k = detect_raw(gray, scale_factor=scale_factor, max_dim=max_dim, step=step)
    groups = group_rectangles(dets, min_neighbors=min_neighbors)
    dt = time.time() - t0
    res = []
    for gx, gy, gw, gh, c in groups:
        px, py, pw = gx / k, gy / k, gw / k
        fw, fh = pw / W0, pw / H0
        res.append({"members": c,
                    "fx": min(1.0, max(0.0, px / W0)),
                    "fy": min(1.0, max(0.0, py / H0)),
                    "fw": min(1.0, max(0.0, fw)),
                    "fh": min(1.0, max(0.0, fh)),
                    "cx": min(1.0, max(0.0, (px + pw / 2) / W0)),
                    "cy": min(1.0, max(0.0, (py + pw / 2) / H0))})
    return res, dt


def pick_leftmost_face(faces):
    """多人时取最左边的人。
    先用"不小于最大人脸 55% 的大小"过滤掉零碎误报，再按 x 从左到右选。"""
    if not faces:
        return None
    big = max(f["fw"] for f in faces)
    solid = [f for f in faces if f["fw"] >= big * 0.55]
    return min(solid or faces, key=lambda f: f["fx"])


def detect_main_face(img):
    """两档精度扫描：先快（288px），找不到再细（384px，能发现更小的脸）。
    返回 (最左边的人脸 dict 或 None, 检测到的人脸总数, 耗时秒)"""
    t0 = time.time()
    try:
        faces, _ = detect_faces(img, max_dim=288)
        if not faces:
            faces, _ = detect_faces(img, max_dim=384)
    except Exception:
        return None, 0, time.time() - t0
    return pick_leftmost_face(faces), len(faces), time.time() - t0


def face_crop_target(face):
    """由人脸推一个"含身体"的取景点，返回 (cx, cy, fx, fy, fw, fh) 归一化坐标。
    取景点落在胸口附近：宽度按肩宽、高度按头到腰估算，
    这样裁剪时不会只剩一张脸，同时保证脸一定在画面内。"""
    fx, fy, fw, fh = face["fx"], face["fy"], face["fw"], face["fh"]
    # 人体框（头 + 躯干）：以人脸为基准外扩
    body_w = min(1.0, fw * 3.2)
    body_h = min(1.0, fh * 5.5)
    top = max(0.0, fy - fh * 0.45)                 # 头顶留点余量
    bcx = fx + fw / 2.0
    # 取景点放在"上胸"位置：既露头也露身体
    cy = min(1.0, max(0.0, top + body_h * 0.34))
    return {"cx": min(1.0, max(0.0, bcx)), "cy": cy,
            "body_w": body_w, "body_h": body_h, "top": top,
            "fx": fx, "fy": fy, "fw": fw, "fh": fh}

