# -*- coding: utf-8 -*-
"""
网页图片提取器 —— 自测脚本

覆盖：小红书原图通道（本次重点）、小红书多图/去重、微博 /large/ 升级、
微信 /0 原图、通用网站兜底、工具函数与链接清洗。

用法：  .venv/Scripts/python.exe _test_all.py
"""
import importlib.util
import os

ROOT = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("xhs_app", os.path.join(ROOT, "网页图片提取器.py"))
A = importlib.util.module_from_spec(spec)
spec.loader.exec_module(A)

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}   {detail}")


print("=" * 70)
print("【小红书原图通道】sns-img-qc + imageView2/0/format/jpg")

PC_URL = ("http://sns-webpic-qc.xhscdn.com/20261001/abcdef0123456789abcdef0123456789/"
          "1040g00831fathet4ma0g4bulihbd7t4bauooe00!nd_dft_wlteh_webp_3")
H5_URL = ("http://sns-webpic-qc.xhscdn.com/20261001/abcdef0123456789abcdef0123456789/"
          "1040g00831fathet4ma0g4bulihbd7t4bauooe00!h5_1080jpg")
WANT = ("https://sns-img-qc.xhscdn.com/1040g00831fathet4ma0g4bulihbd7t4bauooe00"
        "?imageView2/0/format/jpg")

check("PC 端地址能推出原图", A.xhs_origin_url(PC_URL) == WANT, A.xhs_origin_url(PC_URL))
check("移动端地址也能推出原图", A.xhs_origin_url(H5_URL) == WANT, A.xhs_origin_url(H5_URL))
check("原图候选排在首位", A.upgrade_candidates(PC_URL)[0] == WANT, A.upgrade_candidates(PC_URL)[:1])
check("原图候选排在首位(移动端)", A.upgrade_candidates(H5_URL)[0] == WANT)
check("原始地址仍在候选里兜底", PC_URL in A.upgrade_candidates(PC_URL))
check("原图地址评为最高清晰度", A.image_quality(WANT) == 4, A.image_quality(WANT))
check("加工版评分低于原图", A.image_quality(PC_URL) < 4, A.image_quality(PC_URL))
check("原图与各加工版同身份(不会重复下)",
      A.image_identity(WANT) == A.image_identity(PC_URL) == A.image_identity(H5_URL))
check("评论配图不参与", A.xhs_origin_url(
    "http://sns-img-qc.xhscdn.com/comment/1040g2h031li4gchs5e004bulihbd7t4bdpiebu0") == "")
check("非小红书域名不参与", A.xhs_origin_url("https://example.com/a/abcdefghij0123456789.jpg") == "")
check("fileId 过短不参与", A.xhs_origin_url("http://sns-webpic-qc.xhscdn.com/2026/ab/short.jpg") == "")

print()
print("【回归】其它平台的候选顺序不受影响")
WX = "https://mmbiz.qpic.cn/mmbiz_jpg/abcdefg/640?wx_fmt=jpeg"
check("微信首候选仍为 /0 原图", A.upgrade_candidates(WX)[0].endswith("/0?wx_fmt=jpeg"),
      A.upgrade_candidates(WX)[:1])
WB = "https://wx1.sinaimg.cn/orj360/a1b2c3d4ly1abcdef1234567.jpg"
check("微博首候选仍为 /large/", "/large/" in A.upgrade_candidates(WB)[0],
      A.upgrade_candidates(WB)[:1])
EX = "https://example.com/photos/pic-800x600.jpg"
check("通用地址不会被误加小红书候选",
      all("sns-img-qc" not in c for c in A.upgrade_candidates(EX)))
check("通用地址的 WordPress 尺寸规则仍生效",
      any(c.endswith("pic.jpg") for c in A.upgrade_candidates(EX)),
      str(A.upgrade_candidates(EX)))

print()
print("【回归】清晰度打分")
check("微博 large=3", A.image_quality("https://wx1.sinaimg.cn/large/a.jpg") == 3)
check("微博缩略图=1", A.image_quality("https://wx1.sinaimg.cn/orj360/a.jpg") == 1)
check("小红书预览图=1", A.image_quality(
    "http://sns-webpic-qc.xhscdn.com/x/1040gabcd!nd_prv_wlteh_jpg_3") == 1)

print()
print("【回归】图片身份合并（同一张图只下一次）")
k1 = A.image_identity(PC_URL)
k2 = A.image_identity(H5_URL)
check("不同处理后缀视为同一张图", bool(k1) and k1 == k2, f"{k1!r} vs {k2!r}")

print()
print("【回归】链接清洗")
txt = ("40 【某标题 | 小红书】 😆 J0h5WkyUP4Yn4aT 😆 "
       "https://www.xiaohongshu.com/discovery/item/67de6f11000000001e0043c2"
       "?xsec_token=ABoO6J2EFL9A_qAtF7czXVwVbQpZ6azrbA66TkX1FvvrA=&xsec_source=pc_share")
u = A.extract_url(txt)
check("能从整段分享文案里挑出链接", isinstance(u, str) and "xiaohongshu.com" in (u or ""), repr(u))
check("挑出的链接含 xsec_token", "xsec_token=" in (u or ""), repr(u))
check("纯中文无链接时返回空", not A.extract_url("这里没有任何链接哦"))

print()
print("=" * 70)
print(f"通过 {PASS} 项，失败 {FAIL} 项")
raise SystemExit(1 if FAIL else 0)
