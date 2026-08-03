#!/usr/bin/env python3
# 为静态新闻页生成响应式 WebP 图片，并自动更新 HTML。

from __future__ import annotations

import argparse
import html as html_lib
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from PIL import Image, ImageOps


@dataclass(frozen=True)
class ImageRule:
    filename: str
    widths: tuple[int, ...]
    quality: int
    sizes: str
    priority: str


RULES: tuple[ImageRule, ...] = (
    ImageRule(
        "award.png",
        (640, 960, 1440, 1920),
        82,
        "(max-width: 820px) calc(100vw - 32px), 1180px",
        "high",
    ),
    ImageRule(
        "venue.jpg",
        (480, 720, 960, 1440),
        80,
        "(max-width: 820px) calc(100vw - 32px), 576px",
        "low",
    ),
    ImageRule(
        "presentation.jpg",
        (480, 720, 960, 1440),
        80,
        "(max-width: 820px) calc(100vw - 32px), 576px",
        "low",
    ),
    ImageRule(
        "ceremony.jpg",
        (640, 960, 1440, 1920),
        80,
        "(max-width: 820px) calc(100vw - 32px), 1180px",
        "low",
    ),
    ImageRule(
        "media-report.png",
        (480, 720, 960, 1440),
        78,
        "(max-width: 820px) calc(100vw - 84px), 620px",
        "low",
    ),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="生成响应式 WebP 并更新新闻页 HTML")
    parser.add_argument("--html", default="index.html", help="HTML 文件路径")
    parser.add_argument("--assets", default="assets", help="图片目录")
    parser.add_argument(
        "--in-place",
        action="store_true",
        help="覆盖原 HTML；覆盖前创建 .bak 备份",
    )
    parser.add_argument(
        "--quality-offset",
        type=int,
        default=0,
        help="统一调整 WebP 质量，例如 -5",
    )
    return parser.parse_args()


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def normalize_mode(image: Image.Image) -> Image.Image:
    if image.mode in ("RGBA", "LA"):
        return image.convert("RGBA")
    if image.mode == "P" and "transparency" in image.info:
        return image.convert("RGBA")
    return image.convert("RGB")


def target_widths(original_width: int, requested: Iterable[int]) -> list[int]:
    widths = sorted({w for w in requested if 0 < w <= original_width})
    if not widths:
        widths = [original_width]
    elif widths[-1] < original_width and original_width < widths[-1] * 1.35:
        widths.append(original_width)
    return widths


def optimize_one(
    source: Path,
    widths: Iterable[int],
    quality: int,
) -> tuple[list[tuple[int, Path]], tuple[int, int], int]:
    original_bytes = source.stat().st_size

    with Image.open(source) as opened:
        image = ImageOps.exif_transpose(opened)
        image.load()
        image = normalize_mode(image)
        original_width, original_height = image.size

        outputs: list[tuple[int, Path]] = []
        for width in target_widths(original_width, widths):
            height = round(original_height * width / original_width)
            resized = (
                image
                if width == original_width
                else image.resize((width, height), Image.Resampling.LANCZOS)
            )

            output = source.with_name(f"{source.stem}-{width}.webp")
            resized.save(
                output,
                format="WEBP",
                quality=max(45, min(92, quality)),
                method=6,
                optimize=True,
            )
            outputs.append((width, output))

    return outputs, (original_width, original_height), original_bytes


ATTR_RE = re.compile(
    r"([:\w-]+)\s*=\s*(?:\"([^\"]*)\"|'([^']*)')",
    re.DOTALL,
)


def parse_attrs(tag: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    for match in ATTR_RE.finditer(tag):
        name = match.group(1).lower()
        value = match.group(2) if match.group(2) is not None else match.group(3)
        attrs[name] = value
    return attrs


def esc(value: str) -> str:
    return html_lib.escape(value, quote=True)


def build_picture(
    original_src: str,
    attrs: dict[str, str],
    variants: list[tuple[int, Path]],
    original_size: tuple[int, int],
    rule: ImageRule,
) -> str:
    srcset = ", ".join(
        f"assets/{path.name} {width}w" for width, path in variants
    )

    alt = attrs.get("alt", "")
    css_class = attrs.get("class")
    width, height = original_size

    loading = "eager" if rule.priority == "high" else "lazy"
    fetchpriority = "high" if rule.priority == "high" else "low"

    class_attr = f' class="{esc(css_class)}"' if css_class else ""

    return (
        '<picture class="responsive-picture">\n'
        f'  <source type="image/webp" srcset="{esc(srcset)}" sizes="{esc(rule.sizes)}">\n'
        f'  <img src="{esc(original_src)}" alt="{esc(alt)}"{class_attr} '
        f'width="{width}" height="{height}" '
        f'loading="{loading}" decoding="async" fetchpriority="{fetchpriority}">\n'
        '</picture>'
    )


def replace_img(
    document: str,
    rule: ImageRule,
    variants: list[tuple[int, Path]],
    original_size: tuple[int, int],
) -> tuple[str, bool]:
    original_src = f"assets/{rule.filename}"

    # 支持重复执行：若当前图片已经存在响应式 WebP srcset，则不再嵌套 picture。
    already_optimized = re.compile(
        rf'<picture\b[^>]*class\s*=\s*(["\'])[^"\']*responsive-picture[^"\']*\1[^>]*>'
        rf'.*?{re.escape(Path(rule.filename).stem)}-\d+\.webp'
        rf'.*?<img\b[^>]*\bsrc\s*=\s*(["\']){re.escape(original_src)}\2[^>]*>'
        rf'.*?</picture>',
        re.IGNORECASE | re.DOTALL,
    )
    if already_optimized.search(document):
        return document, True

    pattern = re.compile(
        rf'<img\b[^>]*\bsrc\s*=\s*(["\']){re.escape(original_src)}\1[^>]*>',
        re.IGNORECASE | re.DOTALL,
    )
    match = pattern.search(document)
    if not match:
        return document, False

    tag = match.group(0)
    attrs = parse_attrs(tag)
    picture = build_picture(original_src, attrs, variants, original_size, rule)
    return document[: match.start()] + picture + document[match.end() :], True


def ensure_preload(
    document: str,
    variants: list[tuple[int, Path]],
    sizes: str,
) -> str:
    marker = 'data-news-image-preload="award"'
    if marker in document:
        return document

    srcset = ", ".join(
        f"assets/{path.name} {width}w" for width, path in variants
    )
    preferred = variants[-1][1].name
    preload = (
        f'  <link rel="preload" as="image" type="image/webp" '
        f'href="assets/{preferred}" '
        f'imagesrcset="{esc(srcset)}" imagesizes="{esc(sizes)}" '
        f'fetchpriority="high" {marker}>\n'
    )
    return document.replace("</head>", preload + "</head>", 1)


def ensure_css(document: str) -> str:
    marker = "news-image-performance"
    if marker in document:
        return document

    css = (
        '\n  <style id="news-image-performance">\n'
        '    .responsive-picture {\n'
        '      display: block;\n'
        '      width: 100%;\n'
        '    }\n\n'
        '    .responsive-picture > img {\n'
        '      display: block;\n'
        '      width: 100%;\n'
        '      height: auto;\n'
        '    }\n\n'
        '    .lead-photo,\n'
        '    .photo-grid figure,\n'
        '    .wide-photo,\n'
        '    .media-shot {\n'
        '      overflow: hidden;\n'
        '    }\n'
        '  </style>\n'
    )
    return document.replace("</head>", css + "\n</head>", 1)


def main() -> int:
    args = parse_args()
    html_path = Path(args.html)
    assets_dir = Path(args.assets)

    if not html_path.is_file():
        print(f"错误：找不到 HTML 文件：{html_path}", file=sys.stderr)
        return 2
    if not assets_dir.is_dir():
        print(f"错误：找不到图片目录：{assets_dir}", file=sys.stderr)
        return 2

    document = html_path.read_text(encoding="utf-8")
    generated: dict[str, tuple[list[tuple[int, Path]], tuple[int, int]]] = {}
    total_original = 0
    total_webp = 0

    print("开始生成响应式 WebP：")
    for rule in RULES:
        source = assets_dir / rule.filename
        if not source.is_file():
            print(f"  跳过：{source} 不存在")
            continue

        quality = rule.quality + args.quality_offset
        variants, original_size, original_bytes = optimize_one(
            source,
            rule.widths,
            quality,
        )
        generated[rule.filename] = (variants, original_size)
        total_original += original_bytes
        total_webp += sum(path.stat().st_size for _, path in variants)

        variant_info = ", ".join(
            f"{width}px/{human_size(path.stat().st_size)}"
            for width, path in variants
        )
        print(
            f"  {rule.filename}: 原图 {original_size[0]}×{original_size[1]} "
            f"({human_size(original_bytes)}) -> {variant_info}"
        )

    if not generated:
        print("没有找到可处理的目标图片，HTML 未修改。", file=sys.stderr)
        return 3

    for rule in RULES:
        item = generated.get(rule.filename)
        if not item:
            continue
        variants, original_size = item
        document, replaced = replace_img(
            document,
            rule,
            variants,
            original_size,
        )
        if not replaced:
            print(f"  提示：HTML 中未找到 assets/{rule.filename} 的 <img> 标签")

    award = generated.get("award.png")
    if award:
        award_variants, _ = award
        award_rule = next(rule for rule in RULES if rule.filename == "award.png")
        document = ensure_preload(document, award_variants, award_rule.sizes)

    document = ensure_css(document)

    if args.in_place:
        backup = html_path.with_suffix(html_path.suffix + ".bak")
        shutil.copy2(html_path, backup)
        output_path = html_path
        print(f"已备份原 HTML：{backup}")
    else:
        output_path = html_path.with_name(
            f"{html_path.stem}.optimized{html_path.suffix}"
        )

    output_path.write_text(document, encoding="utf-8")
    print(f"已生成 HTML：{output_path}")

    print("\n优化结果：")
    print("  1. 首图使用 eager、fetchpriority=high，并预加载 WebP。")
    print("  2. 其余图片使用 lazy、decoding=async、fetchpriority=low。")
    print("  3. 图片写入 width/height，降低页面跳动。")
    print("  4. 原 PNG/JPG 保留为兼容回退。")
    print(
        f"  5. 原始目标图片合计：{human_size(total_original)}；"
        f"全部 WebP 档位合计：{human_size(total_webp)}。"
    )
    print("     浏览器只会下载每张图片最合适的一个档位。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
