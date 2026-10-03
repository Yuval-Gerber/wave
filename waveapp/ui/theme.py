"""Wave visual theme (SPEC.md §5): Apple/macOS design language (HIG).

Light appearance, hand-crafted QSS on Apple's HIG palette — community-measured
hex approximations of the adaptive system colors (Apple publishes semantics,
not hex). Real vibrancy materials (NSVisualEffectView blur behind the sidebar
and popovers) arrive with the Phase 8 UI build; reference technique:
github.com/zhiyiYo/PyQt-Frameless-Window (pyobjc + NSVisualEffectView).
"""

from __future__ import annotations

# -- Apple HIG palette (light appearance approximations) ---------------------
BLUE = "#007AFF"  # systemBlue — primary accent
BLUE_DARK = "#0071E3"  # pressed/hover accent
BLUE_SOFT = "rgba(0, 122, 255, 0.12)"  # selection wash
ORANGE_LIVE = "#FF9500"  # systemOrange — live mode
RED = "#FF3B30"  # systemRed — errors
GREEN = "#28CD41"  # systemGreen — ok/profit
GREY = "#8E8E93"  # systemGray — disconnected/idle
TEXT = "#1D1D1F"  # primary label
TEXT_MUTED = "#6E6E73"  # secondary label
BG = "#FFFFFF"  # content/control background
BG_SOFT = "#F5F5F7"  # window background (solid fallback)
SIDEBAR_BG = "rgba(236, 236, 242, 0.55)"  # translucent over the glass material
TOPBAR_BG = "rgba(245, 245, 247, 0.45)"
BORDER = "rgba(0, 0, 0, 0.10)"  # hairline separator
BORDER_STRONG = "rgba(0, 0, 0, 0.16)"
OCEAN = "#0077BE"  # ocean blue — the brand mark (app icon wave & triangle)

# Segmented control (paper/live toggle)
SEGMENT_TRACK = "#E9E9EB"
SEGMENT_ACTIVE = "#FFFFFF"

# -- Typography (8.1): Apple SF Pro, installed from Apple's official package --
FONT_TEXT = "SF Pro Text"  # body & controls (optimized < 20pt)
FONT_DISPLAY = "SF Pro Display"  # titles (optimized ≥ 20pt)
SIZE_TITLE = 22
SIZE_HEADING = 17
SIZE_BODY = 13
SIZE_CAPTION = 11

# -- Spacing & radius scale (8.1) --
SPACE_XS, SPACE_S, SPACE_M, SPACE_L, SPACE_XL = 4, 8, 12, 16, 24
RADIUS_CONTROL = 7
RADIUS_CARD = 12
RADIUS_MODAL = 14

# iOS-notification-style tooltip: gray, translucent, rounded
TOOLTIP_BG = "rgba(235, 235, 240, 0.97)"

APP_QSS = f"""
QWidget {{
    background: transparent;
    color: {TEXT};
    font-family: "SF Pro Text", ".AppleSystemUIFont", "Helvetica Neue", sans-serif;
    font-size: {SIZE_BODY}px;
}}
/* Solid fallback: overridden with transparent on the main window when the
   native liquid-glass material is active (see app._prepare_glass). */
QMainWindow, QDialog {{ background: {BG_SOFT}; }}

QPushButton {{
    background: {BG};
    border: 1px solid {BORDER_STRONG};
    border-radius: 7px;
    padding: 6px 16px;
    color: {TEXT};
}}
QPushButton:hover {{ background: #FAFAFC; }}
QPushButton:pressed {{ background: #F0F0F2; }}
QPushButton:disabled {{ color: {GREY}; border-color: {BORDER}; background: {BG_SOFT}; }}

QPushButton[accent="true"] {{
    background: {BLUE};
    border: none;
    color: white;
    font-weight: 600;
    padding: 7px 18px;
}}
QPushButton[accent="true"]:hover {{ background: {BLUE_DARK}; }}
QPushButton[accent="true"]:pressed {{ background: #0068D0; }}
QPushButton[accent="true"]:disabled {{ background: rgba(0, 122, 255, 0.35); color: white; }}

QLineEdit {{
    background: {BG};
    border: 1px solid {BORDER_STRONG};
    border-radius: 7px;
    padding: 7px 11px;
    selection-background-color: rgba(0, 122, 255, 0.25);
    selection-color: {TEXT};
}}
QLineEdit:focus {{ border: 2px solid {BLUE}; padding: 6px 10px; }}

QComboBox {{
    background: {BG};
    border: 1px solid {BORDER_STRONG};
    border-radius: 7px;
    padding: 5px 26px 5px 11px;
}}
QComboBox:hover {{ background: #FAFAFC; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{
    background: {BG};
    border: 1px solid {BORDER};
    border-radius: 8px;
    selection-background-color: {BLUE};
    selection-color: white;
    outline: none;
}}

QLabel {{ background: transparent; }}
QLabel[muted="true"] {{ color: {TEXT_MUTED}; }}
QLabel[caption="true"] {{ color: {TEXT_MUTED}; font-size: {SIZE_CAPTION}px; }}
QLabel[cardTitle="true"] {{
    font-family: "{FONT_DISPLAY}";
    font-size: {SIZE_HEADING}px;
    font-weight: 600;
}}
QLabel[pageTitle="true"] {{
    font-family: "{FONT_DISPLAY}";
    font-size: {SIZE_TITLE}px;
    font-weight: 700;
}}

QFrame[card="true"] {{
    background: rgba(255, 255, 255, 0.86);
    border: 1px solid {BORDER};
    border-radius: 12px;
}}

QStatusBar {{
    background: {BG_SOFT};
    color: {TEXT_MUTED};
    border-top: 1px solid {BORDER};
}}

QToolTip {{
    background: {TOOLTIP_BG};
    color: {TEXT};
    border: 1px solid rgba(0, 0, 0, 0.06);
    padding: 8px 12px;
    border-radius: 10px;
    font-size: 12.5px;
}}

QScrollBar:vertical {{
    background: transparent;
    width: 8px;
    margin: 2px;
}}
QScrollBar::handle:vertical {{
    background: rgba(0, 0, 0, 0.22);
    border-radius: 4px;
    min-height: 30px;
}}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}

/* Phase 8.6 r3: dropdown lists — white card, no native black chrome */
QComboBox {{
    background: #FFFFFF;
    border: 1px solid {BORDER_STRONG};
    border-radius: {RADIUS_CONTROL}px;
    padding: 5px 26px 5px 10px;
    color: {TEXT};
}}
QComboBox::drop-down {{ border: none; width: 22px; }}
/* the popup CONTAINER window too — it showed as a black card (8.6 r4) */
QComboBoxPrivateContainer {{
    background: #FFFFFF;
    border: 1px solid rgba(0, 0, 0, 0.14);
    border-radius: 10px;
}}
QComboBox QAbstractItemView {{
    background: #FFFFFF;
    border: none;
    padding: 4px;
    outline: none;
    color: {TEXT};
    selection-background-color: rgba(0, 122, 255, 0.14);
    selection-color: {TEXT};
}}

/* Phase 8.6 r3: calendar — light, themed (was native black) */
QCalendarWidget {{ background: #FFFFFF; }}
QCalendarWidget QWidget {{ background: #FFFFFF; alternate-background-color: #FFFFFF; }}
QCalendarWidget QToolButton {{
    background: transparent;
    color: {TEXT};
    border: none;
    border-radius: 6px;
    padding: 5px 8px;
    font-weight: 600;
}}
QCalendarWidget QToolButton:hover {{ background: rgba(0, 0, 0, 0.06); }}
/* 8.6 r4: the month button is perfect WITHOUT the dropdown arrow */
QCalendarWidget QToolButton::menu-indicator {{ image: none; width: 0; }}
QCalendarWidget QMenu {{ background: #FFFFFF; color: {TEXT}; }}
QCalendarWidget QSpinBox {{ background: #FFFFFF; color: {TEXT}; }}
QCalendarWidget QAbstractItemView {{
    background: #FFFFFF;
    color: {TEXT};
    selection-background-color: {BLUE};
    selection-color: #FFFFFF;
    outline: none;
}}
QCalendarWidget QAbstractItemView:disabled {{ color: #C7C7CC; }}
"""
