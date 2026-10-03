# PyInstaller spec — Wave.app (onedir BUNDLE, ad-hoc signed per §2).
# Build with: scripts/build_app.sh

from waveapp import __version__  # single version source (Phase 9)

a = Analysis(
    ["waveapp/__main__.py"],
    pathex=[],
    binaries=[],
    # data files that live inside the package must be declared explicitly
    datas=[
        ("waveapp/ui/wave_stroke.json", "waveapp/ui"),
        ("waveapp/ui/word_stroke.json", "waveapp/ui"),
        ("waveapp/ui/check.svg", "waveapp/ui"),
        ("waveapp/persistence/migrations", "waveapp/persistence/migrations"),
    ],
    hiddenimports=["LocalAuthentication", "pyqt_liquidglass", "pyqtgraph"],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Wave",
    debug=False,
    strip=False,
    upx=False,
    console=False,
    target_arch="arm64",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="Wave",
)

app = BUNDLE(
    coll,
    name="Wave.app",
    icon="assets/wave.icns",
    bundle_identifier="com.yuval.wave",
    info_plist={
        "CFBundleName": "Wave",
        "CFBundleDisplayName": "Wave",
        "CFBundleShortVersionString": __version__,
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "13.0",
    },
)
