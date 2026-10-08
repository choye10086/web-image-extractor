# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['E:/ai/图片整合/网页图片提取器.py'],
    pathex=[],
    binaries=[('C:/ProgramData/miniconda3/Library/bin/tcl90.dll', '.'), ('C:/ProgramData/miniconda3/Library/bin/tcl9tk90.dll', '.'), ('C:/ProgramData/miniconda3/Library/bin/zlib.dll', '.'), ('C:/ProgramData/miniconda3/Library/bin/zlib1.dll', '.')],
    datas=[('C:/ProgramData/miniconda3/Library/lib/tcl9.0', 'tcl9.0'), ('C:/ProgramData/miniconda3/Library/lib/tk9.0', 'tk9.0'), ('C:/ProgramData/miniconda3/Library/lib/tcl9', 'tcl9'), ('C:/Users/Admin/Desktop/111/002.jpg', 'default_bg.jpg'), ('E:/ai/图片整合/haar_face.xml', '.'), ('E:/ai/图片整合/app.ico', '.')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='网页图片提取器',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['E:/ai/图片整合/app.ico'],
)
