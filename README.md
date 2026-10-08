# 网页图片提取器

粘贴一个网址（或整段小红书/微信分享文案），自动抓取页面里的**高清原图**，按网页标题建文件夹保存到本地。Windows 桌面 GUI 工具，单文件 exe，无需安装 Python。

## 主要功能

- **自动升级原图**：各平台页面里通常只投放压缩版，程序会还原出作者上传的原件
  - 小红书：凭图片 fileId 直连原图图床取未压缩原图（实测 1080×1159 → 1875×2012）
  - 微信公众号：路径宽度号 `/640` → `/0`
  - 微博：`/orj360/`、`/mw690/` 等缩略目录 → `/large/`（即原图）
  - 通用网站：七牛/OSS 缩放参数、WordPress `-1024x768` 后缀、新浪 `@` 参数等
- **小红书多图笔记**：从页面内嵌数据里解析完整的 `imageList`，轮播/翻页的每一张都能取到，不会只下封面
- **微博动态页**：以爬虫 UA 读取正文，官方 `clear_picSrc` 取图，自动过滤博主皮肤背景图与头像
- **智能去重**：同一张图的多个地址（缩略图 + 高清图、不同 CDN 域名）只下载一次，只留像素最大的那份；相同内容按 md5 兜底去重
- **格式处理**：WEBP 自动转 JPG（Windows 很多软件打不开 webp）；GIF 自动拆帧成 JPG（重复画面全局去重）
- **命名与归档**：按网页标题建文件夹，图片命名为「标题_序号.jpg」

## 用法

1. 打开 `网页图片提取器.exe`
2. 粘贴网址（也可以直接粘贴整段分享文案，程序会自动挑出里面的链接）
3. 点「提取图片」，选择保存位置即可

## 源码结构

| 文件 | 说明 |
| --- | --- |
| `网页图片提取器.py` | 主程序（GUI + 抓取 + 下载全逻辑） |
| `haar_face.py` | 纯 numpy 实现的 Haar 级联人脸检测（离线，用于背景图主体识别） |
| `haar_face.xml` | 人脸检测级联数据 |
| `app.ico` | 程序图标 |
| `网页图片提取器.spec` | PyInstaller 打包配置 |
| `_test_all.py` | 自测脚本（22 项，含各平台解析规则回归） |

## 本地运行与打包

环境：Python 3.14（`Miniconda`），依赖 `requests` `beautifulsoup4` `lxml` `pillow` `numpy` `pyinstaller`。

```bash
# 自测
python _test_all.py

# 打包（conda 环境需手动带上 tcl/tk）
pyinstaller --onefile --windowed --name 网页图片提取器 --icon app.ico \
  --add-binary "C:/ProgramData/miniconda3/Library/bin/tcl90.dll;." \
  --add-binary "C:/ProgramData/miniconda3/Library/bin/tcl9tk90.dll;." \
  --add-binary "C:/ProgramData/miniconda3/Library/bin/zlib.dll;." \
  --add-binary "C:/ProgramData/miniconda3/Library/bin/zlib1.dll;." \
  --add-data "C:/ProgramData/miniconda3/Library/lib/tcl9.0;tcl9.0" \
  --add-data "C:/ProgramData/miniconda3/Library/lib/tk9.0;tk9.0" \
  --add-data "C:/ProgramData/miniconda3/Library/lib/tcl9;tcl9" \
  --add-data "haar_face.xml;." --add-data "app.ico;." \
  网页图片提取器.py
```

打包好的 exe 见本仓库的 Releases 页面。

## 说明

仅用于下载公开可见的网页图片，请遵守各平台的服务条款与版权规定。
