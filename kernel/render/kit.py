"""卡片设计系统（token + 组件），所有卡片共用一套视觉语言。

基线来自 V2 的 WDSJ 卡片（`data/templates/daily_rank_card.html` 与
`daily_arena_card.html`）：纸感底 + 毛玻璃白卡 + 像素图标 + 等宽数字。

三条硬规则（都是踩过的坑）：

1. **字体与图标一律内联成 data URI**。`page.set_content()` 之后 Chromium
   会拒绝 `file://` 子资源，模板里写相对路径会渲染成空白。
2. **数字统一 Monocraft + `tabular-nums`**，否则竖列数字对不齐，
   一眼就能看出是拼的。
3. **色值只从 Theme 取**，组件里不写死颜色。加主题只加一个 Theme 实例。

用法：

    body = head(icon="head_bed", title="起床战争", sub="_yyyay_") \
         + hero([("击杀", "5399"), ("胜场", "821")]) \
         + kv([("死亡", "4069"), ("KD", "1.33")]) \
         + foot("洛花星雨 · 幻梦", tag="33 项")
    html = page(body, theme=WARM, width=560)
"""
from __future__ import annotations

import base64
import functools
import html as _html
import re
from dataclasses import dataclass
from pathlib import Path

# ══════════════════════════════════════════════════════════
#  资产
# ══════════════════════════════════════════════════════════


def assets_root() -> Path:
    """仓库根的 `assets/`。内核、插件、脚本三种调用深度都能找到。"""
    here = Path(__file__).resolve()
    for up in (here.parent, here.parent.parent, here.parent.parent.parent,
               here.parent.parent.parent.parent):
        cand = up / "assets"
        if cand.is_dir():
            return cand
    return here.parent / "assets"


_MIME = {
    "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "gif": "image/gif", "webp": "image/webp", "svg": "image/svg+xml",
    "ttf": "font/ttf", "otf": "font/otf", "woff2": "font/woff2",
}


@functools.lru_cache(maxsize=256)
def data_uri(path: str) -> str:
    """文件转 data URI；不存在返回空串（调用方自然会渲染成占位）。"""
    p = Path(path)
    if not p.is_file():
        return ""
    mime = _MIME.get(p.suffix.lower().lstrip("."), "application/octet-stream")
    try:
        raw = p.read_bytes()
    except OSError:
        return ""
    return f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")


@functools.lru_cache(maxsize=1)
def font_face() -> str:
    """Monocraft 的 @font-face。编一次缓存住，别每张卡都 base64。"""
    uri = data_uri(str(assets_root() / "fonts" / "monocraft.ttf"))
    if not uri:
        return ""
    return ('@font-face{font-family:"Monocraft";src:url(' + uri
            + ') format("truetype");font-weight:400 700;font-display:block}')


@functools.lru_cache(maxsize=256)
def icon_uri(name: str) -> str:
    """图标名：`head_bed`（默认 wdsj 组）或 `daily:medal_gold`。"""
    group, leaf = name.split(":", 1) if ":" in name else ("wdsj", name)
    return data_uri(str(assets_root() / "icons" / group / f"{leaf}.png"))


# ══════════════════════════════════════════════════════════
#  主题
# ══════════════════════════════════════════════════════════


@dataclass(frozen=True)
class Theme:
    key: str
    glow_a: str            # 左上光晕
    glow_b: str            # 右上光晕
    bg: tuple[str, str, str]   # 页面底色三段
    wrap: str              # 毛玻璃卡底
    title: str             # 主标题
    muted: str             # 次要文字
    rule: str              # 表头下划线
    row_line: str          # 行分隔
    text: str              # 正文
    num: str               # 数字强调
    accent: tuple[str, str]    # 图标底色渐变两端
    shadow: str = "0 14px 34px rgba(120,90,70,.10), inset 0 1px 0 rgba(255,255,255,.95)"


# 起床战争：暖橙纸感（照抄 daily_rank_card.html）
WARM = Theme(
    key="warm",
    glow_a="rgba(255,172,132,.60)", glow_b="rgba(255,206,186,.45)",
    bg=("#fdf4ec", "#f9ece2", "#f3eff2"), wrap="rgba(255,250,245,.78)",
    title="#4a2f26", muted="#9a8a80",
    rule="rgba(226,178,150,.55)", row_line="rgba(215,190,175,.30)",
    text="#4a3d35", num="#c2432c",
    accent=("rgba(255,164,130,.46)", "rgba(238,90,58,.22)"),
)

# 竞技场：冷蓝纸感（照抄 daily_arena_card.html）
COOL = Theme(
    key="cool",
    glow_a="rgba(148,184,255,.58)", glow_b="rgba(190,214,255,.48)",
    bg=("#f3f8ff", "#eaf1fc", "#efeff8"), wrap="rgba(246,250,255,.78)",
    title="#28334f", muted="#8792a8",
    rule="rgba(168,190,228,.58)", row_line="rgba(180,198,230,.32)",
    text="#3a4459", num="#3d63e0",
    accent=("rgba(128,168,255,.46)", "rgba(79,110,224,.22)"),
    shadow="0 14px 34px rgba(70,90,140,.10), inset 0 1px 0 rgba(255,255,255,.95)",
)

THEMES = {"warm": WARM, "cool": COOL}


# ══════════════════════════════════════════════════════════
#  页面骨架
# ══════════════════════════════════════════════════════════


def _base_css(t: Theme, width: int) -> str:
    g0, g1, g2 = t.bg
    a0, a1 = t.accent
    return f"""
*{{margin:0;padding:0;box-sizing:border-box}}
body{{width:{width}px;padding:22px;-webkit-font-smoothing:antialiased;
  font-family:"PingFang SC","HarmonyOS Sans SC","Microsoft YaHei UI","Noto Sans CJK SC",system-ui,sans-serif;
  color:{t.text};
  background:
    radial-gradient(640px 340px at 8% -10%, {t.glow_a}, transparent 60%),
    radial-gradient(320px 220px at 98% -4%, {t.glow_b}, transparent 62%),
    linear-gradient(160deg,{g0} 0%,{g1} 45%,{g2} 100%)}}
.wrap{{border-radius:24px;padding:20px 22px 16px;background:{t.wrap};
  border:1px solid rgba(255,255,255,.9);backdrop-filter:blur(16px);
  box-shadow:{t.shadow}}}
.mono{{font-family:"Monocraft","JetBrains Mono",Consolas,monospace;font-variant-numeric:tabular-nums}}

/* 头部 */
.head{{display:flex;align-items:center;gap:13px;margin-bottom:16px}}
.hic{{width:46px;height:46px;border-radius:13px;flex:none;display:flex;
  align-items:center;justify-content:center;box-shadow:inset 0 0 0 1px rgba(0,0,0,.05);
  background:linear-gradient(135deg,{a0},{a1})}}
.hic img{{width:32px;height:32px;image-rendering:pixelated}}
.ht{{min-width:0}}
.title{{font-size:21px;font-weight:800;letter-spacing:.5px;color:{t.title};line-height:1.25}}
.sub{{font-size:12.5px;margin-top:3px;color:{t.muted};font-variant-numeric:tabular-nums;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.when{{margin-left:auto;text-align:right;font-size:12px;line-height:1.5;color:{t.muted};
  flex:none}}

/* 主指标：大号数字 */
.hero{{display:grid;gap:9px;margin-bottom:13px}}
.hbox{{background:rgba(255,255,255,.72);border:1px solid rgba(255,255,255,.92);
  border-radius:14px;padding:11px 13px 12px;position:relative;overflow:hidden}}
.hbox .hl{{display:flex;align-items:center;gap:5px;font-size:11.5px;color:{t.muted};
  margin-bottom:5px;letter-spacing:.3px}}
.hbox .hl img{{width:15px;height:15px;image-rendering:pixelated;opacity:.9}}
.hbox .hv{{font-family:"Monocraft","JetBrains Mono",monospace;font-size:27px;
  font-weight:700;color:{t.num};line-height:1.05;font-variant-numeric:tabular-nums;
  letter-spacing:-.5px}}
.hbox .hv.txt{{font-family:"PingFang SC","HarmonyOS Sans SC","Microsoft YaHei UI","Noto Sans CJK SC",sans-serif;
  font-size:23px;font-weight:800;letter-spacing:0;line-height:1.2}}
.hbox .hs{{font-size:10.5px;color:{t.muted};margin-top:4px;font-variant-numeric:tabular-nums}}

/* 次级指标：小格子 */
.kv{{display:grid;gap:7px 9px}}
.kv .item{{background:rgba(255,255,255,.55);border:1px solid rgba(255,255,255,.85);
  border-radius:10px;padding:7px 10px 8px}}
.kv .k{{display:flex;align-items:center;gap:4px;font-size:10.5px;color:{t.muted};
  letter-spacing:.3px;line-height:1.4}}
.kv .k img{{width:13px;height:13px;image-rendering:pixelated;opacity:.85}}
.kv .v{{font-family:"Monocraft","JetBrains Mono",monospace;font-size:15px;font-weight:700;
  color:{t.text};line-height:1.2;font-variant-numeric:tabular-nums}}

/* 细分隔 */
.thin{{height:1px;background:{t.row_line};margin:13px 0 11px}}

/* 底部 */
.foot{{display:flex;align-items:center;gap:8px;font-size:11.5px;color:{t.muted};
  font-variant-numeric:tabular-nums}}
.foot .sep{{flex:1;height:1px;
  background:linear-gradient(90deg,transparent,{t.row_line},transparent)}}
.pill{{font-family:"Monocraft","JetBrains Mono",monospace;font-size:11px;
  padding:2px 8px;border-radius:7px;background:rgba(150,130,110,.09);color:{t.muted}}}

/* 表格（榜单类卡片用） */
table{{width:100%;border-collapse:collapse}}
th{{font-size:12px;font-weight:600;padding:8px 5px;text-align:left;white-space:nowrap;
  color:{t.muted};border-bottom:1.5px solid {t.rule}}}
th img{{width:16px;height:16px;vertical-align:-3px;image-rendering:pixelated;margin-right:3px}}
th.num,td.num{{text-align:center}}
tbody tr{{border-top:1px solid {t.row_line}}}
tbody tr:first-child{{border-top:none}}
td{{padding:8px 5px;font-size:13.5px;color:{t.text}}}
td.num{{font-family:"Monocraft","JetBrains Mono",monospace;font-weight:700;
  color:{t.num};font-variant-numeric:tabular-nums;font-size:13px}}
td.name{{font-weight:600;max-width:150px;overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap}}
"""


def page(body: str, *, theme: Theme = WARM, width: int = 560) -> str:
    """包一层完整 HTML。`body` 应为 `.wrap` 内部的片段。"""
    return (
        '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">'
        f"<style>{font_face()}{_base_css(theme, width)}</style></head><body>"
        f'<div class="wrap">{body}</div></body></html>'
    )


# ══════════════════════════════════════════════════════════
#  组件
# ══════════════════════════════════════════════════════════


class Html(str):
    """已经是安全 HTML 的字符串。组件遇到它不再二次转义。

    存在的理由：字段值可能带 Minecraft 颜色码（段位 `§bScSL§f大师+`
    这类），解析后是 HTML 片段，但绝不能跳过转义——`mc_text()` 内部
    已经对每个片段做过 `esc()`。
    """


def esc(x) -> str:
    if isinstance(x, Html):
        return str(x)
    return _html.escape("" if x is None else str(x), quote=True)


def head(*, icon: str = "", title: str = "", sub: str = "",
         right: str = "", bare_icon: str = "") -> str:
    """头部：方块图标 + 标题/副标题 + 右侧补充。

    `bare_icon` 直接吃一个 data URI（比如头像），优先级高于 `icon`。
    """
    src = bare_icon or (icon_uri(icon) if icon else "")
    pic = f'<img src="{src}" alt="">' if src else ""
    sub_html = f'<div class="sub">{sub}</div>' if sub else ""
    when = f'<div class="when">{right}</div>' if right else ""
    return (f'<div class="head"><div class="hic">{pic}</div>'
            f'<div class="ht"><div class="title">{title}</div>{sub_html}</div>'
            f"{when}</div>")


def _hero_font(items: list[tuple[str, str]]) -> str:
    """按最长数值定字号，整行统一 —— 各自缩放会高低不齐。"""
    longest = max((len(str(v)) for _, v in items), default=1)
    if longest <= 3:
        return "27px"
    if longest == 4:
        return "24px"
    if longest == 5:
        return "21px"
    if longest == 6:
        return "19px"
    return "17px"


def best_cols(n: int, *, max_rows: int = 4) -> int:
    """选列数：缺口最小优先，其次列数最多；行数超过 max_rows 的候选直接弃。

    8 个 → 4 列 2 行（不留缺口）；9 个 → 3 列 3 行；10 个 → 4 列（余 2）
    比 2 列 5 行更好看。
    """
    best: tuple[tuple[int, int], int] | None = None
    for c in (4, 3, 2):
        rows = -(-n // c)
        if rows > max_rows:
            continue
        key = ((-n) % c, -c)
        if best is None or key < best[0]:
            best = (key, c)
    return best[1] if best else 3


def _is_text(value: str) -> bool:
    """值里非 ASCII 字符超过一半就当文本（段位「大师+」这类），
    用无衬线字体，别拿等宽像素字体硬套中文。"""
    s = str(value)
    if not s:
        return False
    non_ascii = sum(1 for ch in s if ord(ch) > 127)
    return non_ascii * 2 > len(s)


def hero(items: list[tuple[str, str]], *, cols: int = 4,
         icons: list[str] | None = None,
         subs: list[str] | None = None) -> str:
    """主指标：大号数字格子。items = [(标签, 值), ...]

    纯数字走等宽像素字体，含中文的值（段位之类）走无衬线粗体，
    否则 Monocraft 没有中文字形，会回退成另一种字体、字重也对不上。
    """
    size = _hero_font(items)
    cells = []
    for i, (label, value) in enumerate(items):
        ic = icons[i] if icons and i < len(icons) and icons[i] else ""
        pic = f'<img src="{icon_uri(ic)}" alt="">' if ic else ""
        sb = subs[i] if subs and i < len(subs) and subs[i] else ""
        sub_html = f'<div class="hs">{sb}</div>' if sb else ""
        cls = "hv txt" if _is_text(value) else "hv"
        size_attr = f'font-size:{size}' if cls == "hv" else ""
        cells.append(f'<div class="hbox"><div class="hl">{pic}{esc(label)}</div>'
                     f'<div class="{cls}" style="{size_attr}">{esc(value)}</div>'
                     f"{sub_html}</div>")
    return f'<div class="hero" style="grid-template-columns:repeat({cols},1fr)">' \
           + "".join(cells) + "</div>"


def kv(items: list[tuple[str, str]], *, cols: int = 3,
       icons: list[str] | None = None) -> str:
    """次级指标：小格子。items = [(标签, 值), ...]"""
    cells = []
    for i, (label, value) in enumerate(items):
        ic = icons[i] if icons and i < len(icons) and icons[i] else ""
        pic = f'<img src="{icon_uri(ic)}" alt="">' if ic else ""
        cells.append(f'<div class="item"><div class="k">{pic}{esc(label)}</div>'
                     f'<div class="v">{esc(value)}</div></div>')
    return f'<div class="kv" style="grid-template-columns:repeat({cols},1fr)">' \
           + "".join(cells) + "</div>"


def table(cols: list[dict], rows: list[dict]) -> str:
    """榜单表格。

    cols: [{key, label, num?, icon?, width?}]
    rows: [{key: value, ...}]，值为已转义的 HTML 片段或纯文本
    """
    ths = []
    for c in cols:
        pic = f'<img src="{icon_uri(c["icon"])}" alt="">' if c.get("icon") else ""
        cls = ' class="num"' if c.get("num") else ""
        style = f' style="width:{c["width"]}"' if c.get("width") else ""
        ths.append(f'<th{cls}{style}>{pic}{esc(c["label"])}</th>')

    body = []
    for r in rows:
        tds = []
        for c in cols:
            v = r.get(c["key"], "")
            cls = []
            if c.get("num"):
                cls.append("num")
            if c.get("key") == "name":
                cls.append("name")
            attr = f' class="{" ".join(cls)}"' if cls else ""
            tds.append(f"<td{attr}>{v}</td>")
        body.append("<tr>" + "".join(tds) + "</tr>")
    return ("<table><thead><tr>" + "".join(ths) + "</tr></thead><tbody>"
            + "".join(body) + "</tbody></table>")


def foot(left: str = "", right: str = "", tag: str = "") -> str:
    """底部：左说明 — 渐变分隔 — 右胶囊标签。"""
    tag_html = f'<span class="pill">{tag}</span>' if tag else ""
    right_html = f'<span>{right}</span>' if right else ""
    return (f'<div class="foot"><span>{left}</span><span class="sep"></span>'
            f"{right_html}{tag_html}</div>")


def thin() -> str:
    return '<div class="thin"></div>'


# ══════════════════════════════════════════════════════════
#  Minecraft 颜色码（§a 这类），亮底适配
# ══════════════════════════════════════════════════════════

_MC_BASE = {
    "0": "#000000", "1": "#0000aa", "2": "#00aa00", "3": "#00aaaa",
    "4": "#aa0000", "5": "#aa00aa", "6": "#ffaa00", "7": "#aaaaaa",
    "8": "#555555", "9": "#5555ff", "a": "#55ff55", "b": "#55ffff",
    "c": "#ff5555", "d": "#ff55ff", "e": "#ffff55", "f": "#ffffff",
}
# 亮底上白/灰/亮色不可读，换成同色系深色
_MC_LIGHT = {
    "7": "#6b7280", "8": "#4b5563", "f": "#6b7280", "b": "#0e7490",
    "a": "#15803d", "e": "#b45309", "d": "#a21caf", "9": "#1d4ed8",
}
_MC_FMT = set("klmnor")
_MC_RE = re.compile(r"\u00a7([0-9a-fk-or])", re.I)


def mc_text(raw: str) -> str:
    """把 `§a名字` 变成带颜色的 HTML，已转义。"""
    s = "" if raw is None else str(raw)
    if "\u00a7" not in s:
        return esc(s)
    out: list[str] = []
    color = ""
    style = ""
    pos = 0
    for m in _MC_RE.finditer(s):
        chunk = s[pos:m.start()]
        if chunk:
            out.append(_wrap(chunk, color, style))
        pos = m.end()
        code = m.group(1).lower()
        if code in _MC_FMT:
            if code == "r":
                color, style = "", ""
            elif code == "l":
                style += "font-weight:800;"
            elif code == "o":
                style += "font-style:italic;"
            elif code == "n":
                style += "text-decoration:underline;"
        else:
            color = _MC_LIGHT.get(code, _MC_BASE.get(code, ""))
    tail = s[pos:]
    if tail:
        out.append(_wrap(tail, color, style))
    return "".join(out)


def _wrap(text: str, color: str, style: str) -> str:
    css = ""
    if color:
        css += f"color:{color};"
    css += style
    return f'<span style="{css}">{esc(text)}</span>' if css else esc(text)


def rich(value) -> Html:
    """值可能带颜色码：解析成 HTML 片段并标记为安全。"""
    return Html(mc_text(value))


# ══════════════════════════════════════════════════════════
#  数字格式
# ══════════════════════════════════════════════════════════


def num(value, *, digits: int = 2) -> str:
    """数字加千分位、小数截断去尾零；非数字原样返回（按颜色码解析）。

    官网返回的 values 里数字全是字符串，KD 这类是小数，段位/等级名是文本，
    所以这里必须能容忍非数字，不能抛。
    """
    try:
        f = float(value)
    except (TypeError, ValueError):
        return rich(value)
    if f == int(f) and abs(f) < 1e15:
        return f"{int(f):,}"
    s = f"{f:,.{digits}f}".rstrip("0").rstrip(".")
    return s
