"""生成插件图标。

用法::

    python scripts/make_logo.py

产出（写入仓库根目录）：

- ``logo.png``            256×256，插件市场与 WebUI 卡片用
- ``docs/favicon.png``    128×128，GitHub Pages 站点图标
- ``logo.svg``            矢量源文件，方便日后改色改形

设计意图：一束正在扩散的微光。中心是明亮的光核，外围是三层不同角度的
光弧与几颗轨道上的微尘——对应插件「在安静下来的时候主动亮一下」的性格。
配色沿用面板的暖金 + 紫 + 青，三者互相咬合而不是简单叠加。

之所以用脚本生成而不是直接放一张图片：几何参数、配色、光晕衰减曲线
全部可读可改，日后想换配色只要改常量重跑，不用重新找设计。
"""

from __future__ import annotations

import math
import os

from PIL import Image, ImageChops, ImageDraw, ImageFilter

SIZE = 256
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS_DIR = os.path.join(REPO_ROOT, "docs")

# 配色（与 pages/dashboard/style.css 保持一致）
GOLD = (255, 179, 92)
VIOLET = (185, 140, 255)
CYAN = (90, 209, 255)
BG_TOP = (10, 10, 20)
BG_BOTTOM = (22, 16, 38)

CORNER_RADIUS = 58
CENTER = (SIZE / 2, SIZE / 2)


def lerp(a: int, b: int, t: float) -> int:
    return int(round(a + (b - a) * t))


def gradient_background() -> Image.Image:
    """纵向渐变底。一行一行画，256 行开销可以忽略。"""
    image = Image.new("RGB", (SIZE, SIZE))
    draw = ImageDraw.Draw(image)
    for y in range(SIZE):
        t = y / (SIZE - 1)
        color = tuple(lerp(BG_TOP[i], BG_BOTTOM[i], t) for i in range(3))
        draw.line([(0, y), (SIZE, y)], fill=color)
    return image


def radial_mask(radius: float, peak: float = 1.0, power: float = 2.0, blur: float = 14.0) -> Image.Image:
    """一张中心亮、边缘透明的径向遮罩。

    用「由外向内画同心圆、越往内越亮」的方式逼近径向渐变，
    比逐像素计算快得多，效果对图标来说完全够用。
    """
    mask = Image.new("L", (SIZE, SIZE), 0)
    draw = ImageDraw.Draw(mask)
    steps = 90
    cx, cy = CENTER
    for index in range(steps):
        t = index / steps
        r = radius * (1.0 - t)
        if r <= 0:
            break
        value = int(255 * peak * (t**power))
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=value)
    if blur:
        mask = mask.filter(ImageFilter.GaussianBlur(blur))
    return mask


def screen_layer(base: Image.Image, color: tuple[int, int, int], mask: Image.Image) -> Image.Image:
    """把一层颜色按遮罩叠加到画面上，模拟发光。"""
    layer = Image.new("RGB", (SIZE, SIZE), color)
    brightened = ImageChops.screen(base, layer)
    return Image.composite(brightened, base, mask)


def build() -> Image.Image:
    image = gradient_background()

    # 1) 外层大光晕：让整体有「在发光」的空气感
    image = screen_layer(image, GOLD, radial_mask(radius=124, peak=0.86, power=2.3, blur=24))
    # 2) 紫调次光晕，稍微偏心，避免死对称
    violet_mask = radial_mask(radius=96, peak=0.68, power=2.6, blur=22)
    violet_mask = violet_mask.transform(
        (SIZE, SIZE), Image.AFFINE, (1, 0, -10, 0, 1, 8), resample=Image.BILINEAR
    )
    image = screen_layer(image, VIOLET, violet_mask)
    # 3) 青色补光，落在右下
    cyan_mask = radial_mask(radius=78, peak=0.58, power=2.8, blur=20)
    cyan_mask = cyan_mask.transform(
        (SIZE, SIZE), Image.AFFINE, (1, 0, 16, 0, 1, -12), resample=Image.BILINEAR
    )
    image = screen_layer(image, CYAN, cyan_mask)
    # 4) 光核：先铺一层柔光，再压一个高亮小核
    image = screen_layer(image, (255, 228, 190), radial_mask(radius=46, peak=1.0, power=1.4, blur=10))
    image = screen_layer(image, (255, 250, 240), radial_mask(radius=19, peak=1.0, power=0.9, blur=5))

    # 5) 光弧与微尘，画在独立图层上再整体合成，保证透明度可控
    overlay = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    cx, cy = CENTER

    # 一圈极淡的完整环，给三段光弧一个「轨道」的落脚点
    draw.ellipse([cx - 88, cy - 88, cx + 88, cy + 88], outline=(*GOLD, 40), width=1)

    arcs = (
        (88, 196, 318, GOLD, 255, 4),
        (66, 20, 132, VIOLET, 235, 3),
        (47, 250, 380, CYAN, 215, 3),
    )
    for radius, start, end, color, alpha, width in arcs:
        draw.arc(
            [cx - radius, cy - radius, cx + radius, cy + radius],
            start=start,
            end=end,
            fill=(*color, alpha),
            width=width,
        )

    # 轨道上的微尘：角度避开光弧最亮处，看起来才像在绕行
    dust = (
        (88, 352, 3.8, GOLD, 250),
        (66, 168, 3.0, VIOLET, 225),
        (47, 296, 2.6, CYAN, 215),
        (88, 240, 2.2, GOLD, 170),
        (66, 40, 2.0, VIOLET, 150),
    )
    for radius, angle, size, color, alpha in dust:
        rad = math.radians(angle)
        x = cx + radius * math.cos(rad)
        y = cy + radius * math.sin(rad)
        draw.ellipse([x - size, y - size, x + size, y + size], fill=(*color, alpha))

    # 内核高光
    draw.ellipse([cx - 9, cy - 9, cx + 9, cy + 9], fill=(255, 252, 244, 255))

    image = Image.alpha_composite(image.convert("RGBA"), overlay)

    # 6) 圆角裁切：透明四角，贴在任意背景上都干净
    rounded = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(rounded).rounded_rectangle([0, 0, SIZE - 1, SIZE - 1], radius=CORNER_RADIUS, fill=255)
    rounded = rounded.filter(ImageFilter.GaussianBlur(0.6))
    image.putalpha(rounded)
    return image


def main() -> None:
    icon = build()
    logo_path = os.path.join(REPO_ROOT, "logo.png")
    icon.save(logo_path, "PNG")
    print(f"logo.png  -> {logo_path}")

    os.makedirs(DOCS_DIR, exist_ok=True)
    favicon_path = os.path.join(DOCS_DIR, "favicon.png")
    icon.resize((128, 128), Image.LANCZOS).save(favicon_path, "PNG")
    print(f"favicon   -> {favicon_path}")


if __name__ == "__main__":
    main()
