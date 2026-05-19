"""
Generate docs/diagrams/d2-dual-deployment.excalidraw and .png.

Run: python3 scripts/generate_diagram.py
"""

import json
import uuid
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    raise SystemExit("pillow not installed – run: pip install pillow")

# ---------------------------------------------------------------------------
# Layout constants (canvas coords, ~1600 px wide)
# ---------------------------------------------------------------------------
W = 1600
H = 1200

LEFT_X1, LEFT_X2 = 30, 730
RIGHT_X1, RIGHT_X2 = 870, 1570
MID_X = (LEFT_X2 + RIGHT_X1) // 2  # 800

LC = (LEFT_X1 + LEFT_X2) // 2  # left column centre  = 380
RC = (RIGHT_X1 + RIGHT_X2) // 2  # right column centre = 1220

BOX_W = 280
BOX_H = 44

# Shared colours
BLUE_FILL = "#dbeafe"  # left column bg
ORANGE_FILL = "#fef3c7"  # right column bg
BOX_STROKE = "#1e40af"
BOX_FILL_DARK = "#bfdbfe"
ORANGE_BOX = "#fde68a"
ORANGE_STROKE = "#92400e"
SERVICE_FILL = "#e0f2fe"
SERVICE_STROKE = "#0369a1"
ARROW_COLOR = "#374151"
MID_ARROW = "#6d28d9"
TEXT_DARK = "#111827"
TEXT_LIGHT = "#1e40af"
FOOTER_FILL = "#f0fdf4"
FOOTER_STROKE = "#16a34a"


def uid() -> str:
    return uuid.uuid4().hex[:16]


# ---------------------------------------------------------------------------
# Excalidraw element builders
# ---------------------------------------------------------------------------


def rect(
    x,
    y,
    w,
    h,
    label="",
    bg="#ffffff",
    stroke="#000000",
    font_size=16,
    bold=False,
    text_color="#000000",
    roughness=1,
    fill_style="solid",
    stroke_width=2,
    text_align="center",
    stroke_style="solid",
):
    eid = uid()
    els = []
    el = {
        "id": eid,
        "type": "rectangle",
        "x": x - w // 2,
        "y": y - h // 2,
        "width": w,
        "height": h,
        "angle": 0,
        "strokeColor": stroke,
        "backgroundColor": bg,
        "fillStyle": fill_style,
        "strokeWidth": stroke_width,
        "strokeStyle": stroke_style,
        "roughness": roughness,
        "opacity": 100,
        "groupIds": [],
        "frameId": None,
        "roundness": {"type": 3},
        "seed": int(eid[:8], 16) % 2**31,
        "version": 1,
        "versionNonce": 1,
        "isDeleted": False,
        "boundElements": [],
        "updated": 1,
        "link": None,
        "locked": False,
    }
    els.append(el)
    if label:
        els.append(
            text_el(
                x,
                y,
                label,
                font_size=font_size,
                bold=bold,
                color=text_color,
                width=w - 16,
                align=text_align,
            )
        )
    return els


def text_el(x, y, content, font_size=16, bold=False, color="#000000", width=300, align="center"):
    return {
        "id": uid(),
        "type": "text",
        "x": x - width // 2,
        "y": y - font_size,
        "width": width,
        "height": font_size * 1.5,
        "angle": 0,
        "strokeColor": color,
        "backgroundColor": "transparent",
        "fillStyle": "solid",
        "strokeWidth": 1,
        "strokeStyle": "solid",
        "roughness": 1,
        "opacity": 100,
        "groupIds": [],
        "frameId": None,
        "roundness": None,
        "seed": 1,
        "version": 1,
        "versionNonce": 1,
        "isDeleted": False,
        "boundElements": [],
        "updated": 1,
        "link": None,
        "locked": False,
        "text": content,
        "fontSize": font_size,
        "fontFamily": 1,
        "textAlign": align,
        "verticalAlign": "middle",
        "containerId": None,
        "originalText": content,
        "lineHeight": 1.25,
    }


def arrow(x1, y1, x2, y2, color="#374151", stroke_width=2, dashed=False, label=""):
    els = []
    eid = uid()
    el = {
        "id": eid,
        "type": "arrow",
        "x": x1,
        "y": y1,
        "width": abs(x2 - x1),
        "height": abs(y2 - y1),
        "angle": 0,
        "strokeColor": color,
        "backgroundColor": "transparent",
        "fillStyle": "solid",
        "strokeWidth": stroke_width,
        "strokeStyle": "dashed" if dashed else "solid",
        "roughness": 1,
        "opacity": 100,
        "groupIds": [],
        "frameId": None,
        "roundness": {"type": 2},
        "seed": 1,
        "version": 1,
        "versionNonce": 1,
        "isDeleted": False,
        "boundElements": [],
        "updated": 1,
        "link": None,
        "locked": False,
        "points": [[0, 0], [x2 - x1, y2 - y1]],
        "lastCommittedPoint": None,
        "startBinding": None,
        "endBinding": None,
        "startArrowhead": None,
        "endArrowhead": "arrow",
    }
    els.append(el)
    if label:
        mx = (x1 + x2) // 2
        my = (y1 + y2) // 2
        els.append(text_el(mx, my - 12, label, font_size=13, color=color, width=200))
    return els


def double_arrow(x1, y1, x2, y2, color="#6d28d9", stroke_width=2, label=""):
    els = arrow(x1, y1, x2, y2, color=color, stroke_width=stroke_width)
    el = els[0]
    el["startArrowhead"] = "arrow"
    if label:
        mx = (x1 + x2) // 2
        my = (y1 + y2) // 2
        els.append(text_el(mx, my, label, font_size=13, color=color, width=200, align="center"))
    return els


# ---------------------------------------------------------------------------
# Build elements list
# ---------------------------------------------------------------------------

elements = []


def add(*el_lists):
    for group in el_lists:
        elements.extend(group)


# ── Column backgrounds ───────────────────────────────────────────────────────
add(
    rect(
        LC,
        620,
        LEFT_X2 - LEFT_X1 - 10,
        1100,
        bg=BLUE_FILL,
        stroke="#93c5fd",
        roughness=0,
        stroke_width=1,
        fill_style="solid",
    )
)
add(
    rect(
        RC,
        620,
        RIGHT_X2 - RIGHT_X1 - 10,
        1100,
        bg=ORANGE_FILL,
        stroke="#fcd34d",
        roughness=0,
        stroke_width=1,
        fill_style="solid",
    )
)

# ── Column headers ────────────────────────────────────────────────────────────
add(
    rect(
        LC,
        80,
        560,
        64,
        label="VPS Runtime",
        bg=BOX_FILL_DARK,
        stroke=BOX_STROKE,
        font_size=20,
        bold=True,
        text_color=BOX_STROKE,
        roughness=0,
    )
)
add(
    rect(
        LC,
        120,
        380,
        32,
        label="AWS Lightsail Tokyo  ·  ~$5 / mo",
        bg="transparent",
        stroke="transparent",
        font_size=14,
        text_color=BOX_STROKE,
        roughness=0,
    )
)

add(
    rect(
        RC,
        80,
        560,
        64,
        label="Fargate Design Artifact",
        bg=ORANGE_BOX,
        stroke=ORANGE_STROKE,
        font_size=20,
        bold=True,
        text_color=ORANGE_STROKE,
        roughness=0,
    )
)
add(
    rect(
        RC,
        120,
        380,
        32,
        label="ECS Fargate / RDS / ALB / SQS  ·  ~$117 / mo idle",
        bg="transparent",
        stroke="transparent",
        font_size=14,
        text_color=ORANGE_STROKE,
        roughness=0,
    )
)

# ── Title ────────────────────────────────────────────────────────────────────
add(
    [
        text_el(
            W // 2,
            28,
            "Task Scheduler MCP — Dual-Target Deployment Topology",
            font_size=22,
            bold=True,
            color=TEXT_DARK,
            width=1400,
            align="center",
        )
    ]
)

# ── LEFT COLUMN: Internet → Cloudflare DNS → Caddy → compose ─────────────────
# Internet
add(
    rect(
        LC,
        190,
        BOX_W,
        BOX_H,
        "Internet",
        bg="#ffffff",
        stroke=BOX_STROKE,
        font_size=16,
        roughness=1,
    )
)
# ↓ arrow
add(arrow(LC, 212, LC, 248, color=ARROW_COLOR))
# Cloudflare DNS
add(
    rect(
        LC,
        270,
        BOX_W,
        BOX_H,
        "Cloudflare DNS",
        bg="#fff7ed",
        stroke="#ea580c",
        font_size=15,
        text_color="#7c2d12",
        roughness=1,
    )
)
# ↓ arrow
add(arrow(LC, 292, LC, 328, color=ARROW_COLOR))
# Caddy 2
add(
    rect(
        LC,
        350,
        BOX_W,
        BOX_H,
        "Caddy 2  (auto-TLS)",
        bg=BOX_FILL_DARK,
        stroke=BOX_STROKE,
        font_size=15,
        text_color=BOX_STROKE,
        roughness=1,
    )
)
# ↓ arrow
add(arrow(LC, 372, LC, 415, color=ARROW_COLOR))

# docker-compose container (large box)
COMPOSE_Y = 570
COMPOSE_H = 460
add(
    rect(
        LC,
        COMPOSE_Y,
        620,
        COMPOSE_H,
        bg="#eff6ff",
        stroke=BOX_STROKE,
        roughness=1,
        stroke_width=2,
        fill_style="solid",
    )
)
add(
    [
        text_el(
            LC,
            415 + 10,
            "docker-compose  ·  AWS Lightsail Tokyo",
            font_size=14,
            bold=True,
            color=BOX_STROKE,
            width=580,
            align="center",
        )
    ]
)

# ↓ services inside compose
SVC_X = LC
service_y_start = 450
service_gap = 52

services = [
    ("mcp-server", "(HTTP + stdio MCP)"),
    ("watcher", "(SKIP LOCKED poll)"),
    ("worker", "(SQS consumer)"),
    ("recurring_watcher", ""),
    ("chain_watcher", ""),
]
for i, (name, hint) in enumerate(services):
    sy = service_y_start + i * service_gap
    label = name if not hint else f"{name}  {hint}"
    add(
        rect(
            SVC_X,
            sy,
            540,
            38,
            label=label,
            bg=SERVICE_FILL,
            stroke=SERVICE_STROKE,
            font_size=13,
            text_color="#0c4a6e",
            roughness=1,
        )
    )

# DB + queue inside compose
DB_Y = service_y_start + len(services) * service_gap + 10
add(
    rect(
        LC - 80,
        DB_Y,
        240,
        38,
        "Postgres 16",
        bg="#e0e7ff",
        stroke="#4338ca",
        font_size=13,
        text_color="#312e81",
    )
)
add(
    rect(
        LC + 90,
        DB_Y,
        200,
        38,
        "ElasticMQ",
        bg="#fce7f3",
        stroke="#9d174d",
        font_size=13,
        text_color="#831843",
    )
)

# ── RIGHT COLUMN: Internet → ALB → ECS → RDS + SQS ──────────────────────────
# Internet
add(
    rect(
        RC,
        190,
        BOX_W,
        BOX_H,
        "Internet",
        bg="#ffffff",
        stroke=ORANGE_STROKE,
        font_size=16,
        roughness=1,
    )
)
add(arrow(RC, 212, RC, 248, color=ARROW_COLOR))
# ALB
add(
    rect(
        RC,
        270,
        BOX_W,
        BOX_H,
        "ALB  (HTTPS / ACM cert)",
        bg=ORANGE_BOX,
        stroke=ORANGE_STROKE,
        font_size=15,
        text_color=ORANGE_STROKE,
        roughness=1,
    )
)
add(arrow(RC, 292, RC, 328, color=ARROW_COLOR))
# ECS Fargate header
add(
    rect(
        RC,
        350,
        BOX_W,
        BOX_H,
        "ECS Fargate tasks",
        bg=ORANGE_BOX,
        stroke=ORANGE_STROKE,
        font_size=15,
        text_color=ORANGE_STROKE,
        roughness=1,
    )
)
add(arrow(RC, 372, RC, 415, color=ARROW_COLOR))

# ECS container (large box)
ECS_Y = 570
ECS_H = 360
add(
    rect(
        RC,
        ECS_Y,
        620,
        ECS_H,
        bg="#fffbeb",
        stroke=ORANGE_STROKE,
        roughness=1,
        stroke_width=2,
        fill_style="solid",
    )
)
add(
    [
        text_el(
            RC,
            415 + 10,
            "ECS Fargate  (same 5 services)",
            font_size=14,
            bold=True,
            color=ORANGE_STROKE,
            width=580,
            align="center",
        )
    ]
)

for i, (name, hint) in enumerate(services):
    sy = service_y_start + i * service_gap
    label = name if not hint else f"{name}  {hint}"
    add(
        rect(
            RC,
            sy,
            540,
            38,
            label=label,
            bg="#fef9c3",
            stroke=ORANGE_STROKE,
            font_size=13,
            text_color="#78350f",
            roughness=1,
        )
    )

# Arrows from ECS → RDS and SQS
ECS_BOTTOM = ECS_Y + ECS_H // 2
RDS_Y = ECS_BOTTOM + 70
add(arrow(RC - 100, ECS_BOTTOM + 10, RC - 100, RDS_Y - 22, color=ARROW_COLOR))
add(arrow(RC + 100, ECS_BOTTOM + 10, RC + 100, RDS_Y - 22, color=ARROW_COLOR))
add(
    rect(
        RC - 100,
        RDS_Y,
        280,
        44,
        "RDS PostgreSQL\n(private subnet)",
        bg="#e0e7ff",
        stroke="#4338ca",
        font_size=13,
        text_color="#312e81",
    )
)
add(
    rect(
        RC + 110,
        RDS_Y,
        220,
        44,
        "SQS (managed)",
        bg="#fce7f3",
        stroke="#9d174d",
        font_size=13,
        text_color="#831843",
    )
)

# ── MIDDLE annotations ────────────────────────────────────────────────────────
MID_Y = 380

# horizontal double-headed arrow at the service level
add(double_arrow(LEFT_X2 + 10, MID_Y, RIGHT_X1 - 10, MID_Y, color=MID_ARROW, stroke_width=2))

add(
    [
        text_el(
            MID_X,
            MID_Y - 22,
            "same Docker image",
            font_size=13,
            bold=True,
            color=MID_ARROW,
            width=140,
            align="center",
        )
    ]
)
add(
    [
        text_el(
            MID_X,
            MID_Y + 8,
            "same code path",
            font_size=13,
            color=MID_ARROW,
            width=140,
            align="center",
        )
    ]
)
add(
    [
        text_el(
            MID_X,
            MID_Y + 30,
            "validated end-to-end\nby validate-fargate.yml",
            font_size=12,
            color="#7c3aed",
            width=150,
            align="center",
        )
    ]
)

# ── Footer ────────────────────────────────────────────────────────────────────
FOOTER_Y = 1130
add(
    rect(
        W // 2,
        FOOTER_Y,
        900,
        44,
        "Both targets reproducible from one Git tag",
        bg=FOOTER_FILL,
        stroke=FOOTER_STROKE,
        font_size=16,
        bold=True,
        text_color="#15803d",
        roughness=0,
    )
)

# ── Assemble Excalidraw JSON ──────────────────────────────────────────────────
excalidraw = {
    "type": "excalidraw",
    "version": 2,
    "source": "https://excalidraw.com",
    "elements": elements,
    "appState": {
        "gridSize": None,
        "viewBackgroundColor": "#ffffff",
    },
    "files": {},
}

OUT_DIR = Path(__file__).parent.parent / "docs" / "diagrams"
OUT_DIR.mkdir(parents=True, exist_ok=True)

EXCALIDRAW_PATH = OUT_DIR / "d2-dual-deployment.excalidraw"
EXCALIDRAW_PATH.write_text(json.dumps(excalidraw, indent=2))
print(f"Written {EXCALIDRAW_PATH}  ({EXCALIDRAW_PATH.stat().st_size / 1024:.1f} KB)")

# ---------------------------------------------------------------------------
# PNG generation via Pillow
# ---------------------------------------------------------------------------

SCALE = 1  # 1:1 → 1600 wide
IMG_W = W * SCALE
IMG_H = H * SCALE

img = Image.new("RGB", (IMG_W, IMG_H), "#ffffff")
draw = ImageDraw.Draw(img)


def px(v):
    return int(v * SCALE)


def load_font(size):
    # Pillow default font; no TTF needed
    return ImageFont.load_default(size=size)


def draw_rrect(x, y, w, h, fill, outline, lw=2, radius=8):
    """Rounded-rect approximation."""
    x, y, w, h = px(x), px(y), px(w), px(h)
    draw.rounded_rectangle(
        [x, y, x + w, y + h], radius=radius, fill=fill, outline=outline, width=lw
    )


def draw_text_centered(cx, cy, text, size=15, bold=False, color="#000000"):
    font = load_font(size)
    lines = text.split("\n")
    line_h = size + 4
    total_h = len(lines) * line_h
    start_y = px(cy) - total_h // 2
    for i, line in enumerate(lines):
        bbox = draw.textbbox((0, 0), line, font=font)
        tw = bbox[2] - bbox[0]
        draw.text((px(cx) - tw // 2, start_y + i * line_h), line, font=font, fill=color)


def draw_box(
    cx, cy, bw, bh, label, fill, outline, lw=2, font_size=15, text_color="#000000", bold=False
):
    draw_rrect(cx - bw // 2, cy - bh // 2, bw, bh, fill, outline, lw)
    if label:
        draw_text_centered(cx, cy, label, size=font_size, color=text_color, bold=bold)


def draw_arrow_down(cx, y1, y2, color="#374151", lw=2):
    x = px(cx)
    draw.line([(x, px(y1)), (x, px(y2))], fill=color, width=lw)
    ah = 8
    draw.polygon([(x, px(y2)), (x - ah, px(y2) - ah), (x + ah, px(y2) - ah)], fill=color)


def draw_arrow_h(x1, x2, cy, color="#6d28d9", lw=2, both_ends=True):
    y = px(cy)
    draw.line([(px(x1), y), (px(x2), y)], fill=color, width=lw)
    ah = 8
    # right arrowhead
    draw.polygon([(px(x2), y), (px(x2) - ah, y - ah), (px(x2) - ah, y + ah)], fill=color)
    if both_ends:
        # left arrowhead
        draw.polygon([(px(x1), y), (px(x1) + ah, y - ah), (px(x1) + ah, y + ah)], fill=color)


# ── Draw column backgrounds ───────────────────────────────────────────────────
draw_rrect(LEFT_X1, 145, LEFT_X2 - LEFT_X1, H - 165, BLUE_FILL, "#93c5fd", lw=1)
draw_rrect(RIGHT_X1, 145, RIGHT_X2 - RIGHT_X1, H - 165, ORANGE_FILL, "#fcd34d", lw=1)

# ── Title ─────────────────────────────────────────────────────────────────────
draw_text_centered(
    W // 2,
    30,
    "Task Scheduler MCP - Dual-Target Deployment Topology",
    size=20,
    bold=True,
    color=TEXT_DARK,
)

# ── Column headers ─────────────────────────────────────────────────────────────
draw_box(
    LC,
    78,
    560,
    50,
    "VPS Runtime",
    BOX_FILL_DARK,
    BOX_STROKE,
    lw=2,
    font_size=18,
    text_color=BOX_STROKE,
    bold=True,
)
draw_text_centered(LC, 112, "AWS Lightsail Tokyo  ·  ~$5 / mo", size=13, color=BOX_STROKE)

draw_box(
    RC,
    78,
    560,
    50,
    "Fargate Design Artifact",
    ORANGE_BOX,
    ORANGE_STROKE,
    lw=2,
    font_size=18,
    text_color=ORANGE_STROKE,
    bold=True,
)
draw_text_centered(
    RC, 112, "ECS Fargate / RDS / ALB / SQS  ·  ~$117 / mo idle", size=13, color=ORANGE_STROKE
)

# ── Left column boxes ─────────────────────────────────────────────────────────
draw_box(
    LC, 190, BOX_W, BOX_H, "Internet", "#ffffff", BOX_STROKE, font_size=15, text_color=TEXT_DARK
)
draw_arrow_down(LC, 212, 249, color=ARROW_COLOR)
draw_box(
    LC,
    270,
    BOX_W,
    BOX_H,
    "Cloudflare DNS",
    "#fff7ed",
    "#ea580c",
    font_size=14,
    text_color="#7c2d12",
)
draw_arrow_down(LC, 292, 329, color=ARROW_COLOR)
draw_box(
    LC,
    350,
    BOX_W,
    BOX_H,
    "Caddy 2  (auto-TLS)",
    BOX_FILL_DARK,
    BOX_STROKE,
    font_size=14,
    text_color=BOX_STROKE,
)
draw_arrow_down(LC, 372, 410, color=ARROW_COLOR)

# compose container
COMPOSE_TOP = 415
COMPOSE_BOT = COMPOSE_TOP + 520
draw_rrect(
    LEFT_X1 + 20,
    COMPOSE_TOP,
    LEFT_X2 - LEFT_X1 - 40,
    COMPOSE_BOT - COMPOSE_TOP,
    "#eff6ff",
    BOX_STROKE,
    lw=2,
)
draw_text_centered(
    LC,
    COMPOSE_TOP + 18,
    "docker-compose  ·  AWS Lightsail Tokyo",
    size=13,
    bold=True,
    color=BOX_STROKE,
)

for i, (name, hint) in enumerate(services):
    sy = COMPOSE_TOP + 40 + i * 52
    label = name if not hint else f"{name}  {hint}"
    draw_box(
        LC, sy, 540, 38, label, SERVICE_FILL, SERVICE_STROKE, font_size=13, text_color="#0c4a6e"
    )

# DB row inside compose
DB_Y_img = COMPOSE_TOP + 40 + len(services) * 52 + 14
draw_box(
    LC - 80,
    DB_Y_img,
    240,
    38,
    "Postgres 16",
    "#e0e7ff",
    "#4338ca",
    font_size=13,
    text_color="#312e81",
)
draw_box(
    LC + 100,
    DB_Y_img,
    200,
    38,
    "ElasticMQ",
    "#fce7f3",
    "#9d174d",
    font_size=13,
    text_color="#831843",
)

# ── Right column boxes ────────────────────────────────────────────────────────
draw_box(
    RC, 190, BOX_W, BOX_H, "Internet", "#ffffff", ORANGE_STROKE, font_size=15, text_color=TEXT_DARK
)
draw_arrow_down(RC, 212, 249, color=ARROW_COLOR)
draw_box(
    RC,
    270,
    BOX_W,
    BOX_H,
    "ALB  (HTTPS / ACM cert)",
    ORANGE_BOX,
    ORANGE_STROKE,
    font_size=14,
    text_color=ORANGE_STROKE,
)
draw_arrow_down(RC, 292, 329, color=ARROW_COLOR)
draw_box(
    RC,
    350,
    BOX_W,
    BOX_H,
    "ECS Fargate tasks",
    ORANGE_BOX,
    ORANGE_STROKE,
    font_size=14,
    text_color=ORANGE_STROKE,
)
draw_arrow_down(RC, 372, 410, color=ARROW_COLOR)

# ECS container
ECS_TOP = COMPOSE_TOP
ECS_BOT = ECS_TOP + 420
draw_rrect(
    RIGHT_X1 + 20,
    ECS_TOP,
    RIGHT_X2 - RIGHT_X1 - 40,
    ECS_BOT - ECS_TOP,
    "#fffbeb",
    ORANGE_STROKE,
    lw=2,
)
draw_text_centered(
    RC, ECS_TOP + 18, "ECS Fargate  (same 5 services)", size=13, bold=True, color=ORANGE_STROKE
)

for i, (name, hint) in enumerate(services):
    sy = COMPOSE_TOP + 40 + i * 52
    label = name if not hint else f"{name}  {hint}"
    draw_box(RC, sy, 540, 38, label, "#fef9c3", ORANGE_STROKE, font_size=13, text_color="#78350f")

# RDS + SQS below ECS
RDS_Y_img = ECS_BOT + 55
draw_arrow_down(RC - 110, ECS_BOT + 5, RDS_Y_img - 22, color=ARROW_COLOR)
draw_arrow_down(RC + 110, ECS_BOT + 5, RDS_Y_img - 22, color=ARROW_COLOR)
draw_box(
    RC - 100,
    RDS_Y_img,
    280,
    44,
    "RDS PostgreSQL\n(private subnet)",
    "#e0e7ff",
    "#4338ca",
    font_size=13,
    text_color="#312e81",
)
draw_box(
    RC + 110,
    RDS_Y_img,
    220,
    44,
    "SQS (managed)",
    "#fce7f3",
    "#9d174d",
    font_size=13,
    text_color="#831843",
)

# ── Middle annotations ────────────────────────────────────────────────────────
draw_arrow_h(LEFT_X2 + 5, RIGHT_X1 - 5, MID_Y, color=MID_ARROW, lw=2)
draw_text_centered(MID_X, MID_Y - 20, "same Docker image", size=13, bold=True, color=MID_ARROW)
draw_text_centered(MID_X, MID_Y + 3, "same code path", size=12, color=MID_ARROW)
draw_text_centered(MID_X, MID_Y + 24, "validated end-to-end", size=11, color="#7c3aed")
draw_text_centered(MID_X, MID_Y + 40, "by validate-fargate.yml", size=11, color="#7c3aed")

# ── Footer ────────────────────────────────────────────────────────────────────
FOOTER_Y_img = H - 58
draw_rrect((W - 900) // 2, FOOTER_Y_img, 900, 44, FOOTER_FILL, FOOTER_STROKE, lw=2)
draw_text_centered(
    W // 2,
    FOOTER_Y_img + 22,
    "Both targets reproducible from one Git tag",
    size=15,
    bold=True,
    color="#15803d",
)

# ── Save PNG ──────────────────────────────────────────────────────────────────
PNG_PATH = OUT_DIR / "d2-dual-deployment.png"
img.save(PNG_PATH, "PNG", optimize=True)
size_kb = PNG_PATH.stat().st_size / 1024
print(f"Written {PNG_PATH}  ({size_kb:.1f} KB)")
if size_kb > 500:
    print(f"WARNING: PNG exceeds 500 KB ({size_kb:.0f} KB) — consider reducing resolution")
else:
    print("PNG size OK (< 500 KB)")
