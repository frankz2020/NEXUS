#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文字转图片生成器
================

用法：
    # 命令行模式
    python scripts/text_to_image.py --school NYU --title "标题" --content "文章内容"
    
    # 从文件读取内容
    python scripts/text_to_image.py --school USC --title "标题" --content-file article.txt
    
    # Python 代码调用
    from scripts.text_to_image import generate_news_image
    generate_news_image(school="NYU", title="标题", content="内容")

支持的学校：
    NYU, USC, EMORY, UCD (UC DAVIS), UBC, EDINBURGH
"""

import sys
import os
import re
import argparse
from pathlib import Path
from datetime import datetime

try:
    import requests
    from urllib.parse import urljoin
    from bs4 import BeautifulSoup
    HAS_WEB_DEPS = True
except ImportError:
    HAS_WEB_DEPS = False

# Add project root to path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from news_bot.processing.image_generator import generate_image_from_article


# =================== 从URL抓取封面图片 ===================
def fetch_cover_from_url(page_url: str, timeout: int = 12) -> str:
    """
    从来源页面抓取封面图片（og:image, twitter:image 等）
    
    Args:
        page_url: 新闻文章的URL
        timeout: 请求超时时间
    
    Returns:
        图片URL，如果没找到则返回空字符串
    """
    if not page_url:
        return ""
    
    if not HAS_WEB_DEPS:
        print("⚠️  缺少 requests/bs4 依赖，无法抓取封面图片")
        return ""
    
    print(f"🔍 从URL抓取封面图片: {page_url[:60]}...")
    
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X) Chrome/124 Safari/537.36"}
        r = requests.get(page_url, headers=headers, timeout=timeout)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        def _abs(u: str) -> str:
            return urljoin(page_url, (u or "").strip())

        def _looks_like_image_url(url: str) -> bool:
            if not url:
                return False
            bad = (".svg", ".gif", "data:image/svg", "sprite", "logo", "icon")
            u = url.lower()
            return not any(b in u for b in bad)

        # 优先尝试 og:image, twitter:image 等 meta 标签
        for sel, attr in [
            ('meta[property="og:image"]', "content"),
            ('meta[name="og:image"]', "content"),
            ('meta[property="twitter:image"]', "content"),
            ('meta[name="twitter:image"]', "content"),
            ('meta[itemprop="image"]', "content"),
            ('link[rel="image_src"]', "href"),
        ]:
            tag = soup.select_one(sel)
            if tag and tag.get(attr):
                url = _abs(tag.get(attr))
                if _looks_like_image_url(url):
                    print(f"✅ 找到封面图片: {url[:60]}...")
                    return url

        # 如果没有找到 meta 标签，尝试找页面中的大图
        for img in soup.find_all("img"):
            src = _abs(img.get("src") or "")
            if not _looks_like_image_url(src):
                continue
            # 尝试获取图片尺寸
            w = None
            h = None
            try:
                w = int(str(img.get("width", "")).strip())
            except (ValueError, AttributeError):
                pass
            try:
                h = int(str(img.get("height", "")).strip())
            except (ValueError, AttributeError):
                pass
            # 只选择足够大的图片（避免 logo 等小图）
            if (w and w < 240) or (h and h < 160):
                continue
            print(f"✅ 找到封面图片: {src[:60]}...")
            return src
            
        print("⚠️  未能从URL抓取到封面图片")
    except Exception as e:
        print(f"⚠️  抓取封面图片失败: {e}")
    
    return ""

# 学校品牌配色
SCHOOL_CONFIG = {
    "NYU": {
        "brand_color": "#57068c",
        "folder": "NYU_Weekly",
        "full_name": "New York University"
    },
    "USC": {
        "brand_color": "#990000",
        "folder": "USC_Weekly",
        "full_name": "University of Southern California"
    },
    "EMORY": {
        "brand_color": "#222c66",
        "folder": "EMORY_Weekly",
        "full_name": "Emory University"
    },
    "UCD": {
        "brand_color": "#022851",
        "folder": "UCD_Weekly",
        "full_name": "UC Davis",
        "alt_color": "#FFBF00"  # 黄色备用
    },
    "UC DAVIS": {
        "brand_color": "#022851",
        "folder": "UCD_Weekly",
        "full_name": "UC Davis",
        "alt_color": "#FFBF00"
    },
    "UBC": {
        "brand_color": "#002145",
        "folder": "UBC_Weekly",
        "full_name": "University of British Columbia"
    },
    "EDINBURGH": {
        "brand_color": "#041e42",
        "folder": "EDIN_Weekly",
        "full_name": "University of Edinburgh"
    },
}


def _normalize_content(content: str, min_chars_per_paragraph: int = 50) -> str:
    """
    标准化内容格式，确保分段正确。
    规则：每50字以上 + 句号自动分段
    
    Args:
        content: 原始内容
        min_chars_per_paragraph: 每段最小字符数（默认50）
    """
    # 移除所有换行和多余空格，合并为单行
    text = content.strip()
    text = re.sub(r'\s+', ' ', text)  # 所有空白变成单个空格
    
    # 按句号分段（每50字以上+句号）
    result = []
    current_paragraph = ""
    
    # 分割成句子（保留句号）
    sentences = re.split(r'(。)', text)
    
    i = 0
    while i < len(sentences):
        part = sentences[i]
        
        # 如果是句号，附加到当前段落
        if part == '。':
            current_paragraph += part
            # 检查是否需要分段：当前段落超过 min_chars_per_paragraph
            if len(current_paragraph) >= min_chars_per_paragraph:
                result.append(current_paragraph.strip())
                current_paragraph = ""
        else:
            current_paragraph += part
        
        i += 1
    
    # 处理剩余内容
    if current_paragraph.strip():
        result.append(current_paragraph.strip())
    
    # 用双换行连接段落
    return '\n\n'.join(result)


def generate_news_image(
    school: str,
    title: str,
    content: str,
    output_path: str = None,
    output_dir: str = "wechat_images",
    page_width: int = 540,
    device_scale: int = 4,
    title_size: float = 22.5,
    body_size: float = 20.0,
    cover_image: str = "",
    source_url: str = "",
    left_bar_color: str = None,
) -> str:
    """
    根据学校、标题、内容生成新闻图片。
    
    Args:
        school: 学校名称 (NYU, USC, EMORY, UCD, UBC, EDINBURGH)
        title: 中文新闻标题
        content: 中文新闻正文（用空行分段）
        output_path: 输出文件路径（可选，不指定则自动生成）
        output_dir: 输出目录（默认 wechat_images）
        page_width: 图片宽度
        device_scale: 缩放比例（越高越清晰）
        title_size: 标题字号
        body_size: 正文字号
        cover_image: 封面图片URL（可选，直接指定）
        source_url: 新闻来源URL（可选，会自动抓取封面图片）
        left_bar_color: 左侧色条颜色（可选，覆盖学校配色）
    
    Returns:
        生成的图片路径
    """
    # 标准化内容格式
    content = _normalize_content(content)
    
    # 如果没有指定封面图片但有来源URL，尝试从URL抓取
    if not cover_image and source_url:
        cover_image = fetch_cover_from_url(source_url)
    
    # 标准化学校名称
    school_upper = school.upper().strip()
    
    # 获取学校配置
    if school_upper not in SCHOOL_CONFIG:
        print(f"⚠️  未知学校 '{school}'，使用默认配色 (NYU)")
        school_upper = "NYU"
    
    config = SCHOOL_CONFIG[school_upper]
    brand_color = config["brand_color"]
    folder = config["folder"]
    
    # 创建输出目录
    out_dir = Path(output_dir) / folder
    out_dir.mkdir(parents=True, exist_ok=True)
    
    # 生成输出文件名
    if output_path:
        out_path = Path(output_path)
    else:
        # 使用时间戳 + 标题前缀生成文件名
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_title = title[:30].replace('/', '_').replace('\\', '_').replace(':', '_').replace(' ', '_')
        out_path = out_dir / f"{timestamp}_{safe_title}.png"
    
    # 确保输出目录存在
    out_path.parent.mkdir(parents=True, exist_ok=True)
    
    # 使用指定的左侧色条颜色，或使用学校配色
    bar_color = left_bar_color or brand_color
    
    print(f"🎨 学校: {config['full_name']} ({school_upper})")
    print(f"🎨 品牌色: {brand_color}")
    print(f"📝 标题: {title[:50]}...")
    print(f"📄 内容: {len(content)} 字符")
    print(f"💾 输出: {out_path}")
    
    # 生成图片
    generate_image_from_article(
        title=title,
        content=content,
        output_path=str(out_path),
        credits="",  # 不显示来源
        cover_image=cover_image,
        cover_caption="",
        page_width=page_width,
        device_scale=device_scale,
        title_size=title_size,
        body_size=body_size,
        brand_color=brand_color,
        left_bar_color=bar_color,
    )
    
    print(f"✅ 图片生成成功: {out_path}")
    return str(out_path)


def main():
    parser = argparse.ArgumentParser(
        description="文字转图片生成器 - 指定学校、标题、内容生成新闻图片",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
支持的学校:
    NYU       - 紫色 (#57068c)
    USC       - 红色 (#990000)
    EMORY     - 深蓝 (#222c66)
    UCD       - 蓝色 (#022851)
    UBC       - 深蓝 (#002145)
    EDINBURGH - 藏青 (#041e42)

示例:
    %(prog)s --school NYU --title "纽大新闻" --content "这是内容..."
    %(prog)s --school USC --title "USC新闻" --content-file article.txt
    %(prog)s --school UBC --title "UBC新闻" --content "内容" --output my_image.png
        """
    )
    
    parser.add_argument("--school", "-s", required=True, 
                        help="学校名称 (NYU, USC, EMORY, UCD, UBC, EDINBURGH)")
    parser.add_argument("--title", "-t", required=True, 
                        help="新闻标题")
    parser.add_argument("--content", "-c", 
                        help="新闻内容（与 --content-file 二选一）")
    parser.add_argument("--content-file", "-f", 
                        help="从文件读取内容")
    parser.add_argument("--output", "-o", 
                        help="输出文件路径（可选）")
    parser.add_argument("--output-dir", default="wechat_images", 
                        help="输出目录（默认: wechat_images）")
    parser.add_argument("--cover", 
                        help="封面图片URL（可选，直接指定）")
    parser.add_argument("--url", "-u",
                        help="新闻来源URL（可选，自动抓取封面图片）")
    parser.add_argument("--page-width", type=int, default=540, 
                        help="图片宽度（默认: 540）")
    parser.add_argument("--device-scale", type=int, default=4, 
                        help="缩放比例（默认: 4）")
    parser.add_argument("--title-size", type=float, default=22.5, 
                        help="标题字号（默认: 22.5）")
    parser.add_argument("--body-size", type=float, default=20.0, 
                        help="正文字号（默认: 20.0）")
    
    args = parser.parse_args()
    
    # 获取内容
    if args.content:
        content = args.content
    elif args.content_file:
        with open(args.content_file, 'r', encoding='utf-8') as f:
            content = f.read()
    else:
        print("❌ 错误: 必须指定 --content 或 --content-file")
        sys.exit(1)
    
    # 生成图片
    try:
        output_path = generate_news_image(
            school=args.school,
            title=args.title,
            content=content,
            output_path=args.output,
            output_dir=args.output_dir,
            page_width=args.page_width,
            device_scale=args.device_scale,
            title_size=args.title_size,
            body_size=args.body_size,
            cover_image=args.cover or "",
            source_url=args.url or "",
        )
        print(f"\n🎉 完成！图片已保存到: {output_path}")
    except Exception as e:
        print(f"❌ 生成失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    import sys
    
    # ===================================================================
    # 🎯 直接编辑模式 - 在这里填写内容，然后运行脚本
    # ===================================================================
    # 如果不带任何命令行参数，会使用下面的内容生成图片
    # 用法: python scripts/text_to_image.py
    # ===================================================================
    
    DIRECT_MODE = {
        "enabled": True,  # 设为 True 启用直接模式
        
        "school": "NYU",  # NYU, USC, EMORY, UCD, UBC, EDINBURGH
        
        "title": "暴雪席卷美国东北部，上万航班延误取消",
        
        "content": """从上周五晚间至周六，一场严重的冬季风暴袭击了五大湖及美国东北部地区，导致交通和公用事业服务严重受阻。全美范围内有超过10,000架次航班延误，另有1,000多架次航班被取消，纽约拉瓜迪亚 (LaGuardia) 和纽瓦克 (Newark) 等主要枢纽机场均受降雪影响。
纽约部分地区积雪深度超过11英寸，康涅狄格州积雪达到8英寸，密歇根州则有超过3万名用户因冰雪压垮树木而断电。纽约中央公园记录到自2022年初以来的最高降雪量，实测数值超过4英寸。
美国国家气象局 (National Weather Service) 警告称，第二场风暴将从周日开始为中西部上游和东北部带来更多降雪、强风和冻雨。随后，这些地区将迎来极寒气温，相关部门提醒民众持续关注天气预警。""",
        
        "source_url": "https://edition.cnn.com/2025/12/26/weather/winter-storm-snow-northeast-nyc-climate",  # 新闻来源URL（自动抓取封面）
        "cover_image": "",  # 封面图片URL（可选，直接指定则优先使用）
        "output": None,     # 输出路径（None = 自动生成）
    }
    
    # ===================================================================
    
    # 判断运行模式
    if len(sys.argv) == 1 and DIRECT_MODE["enabled"]:
        # 直接模式 - 使用上面定义的内容
        print("=" * 60)
        print("📝 直接编辑模式")
        print("=" * 60)
        
        try:
            output_path = generate_news_image(
                school=DIRECT_MODE["school"],
                title=DIRECT_MODE["title"],
                content=DIRECT_MODE["content"],
                output_path=DIRECT_MODE.get("output"),
                cover_image=DIRECT_MODE.get("cover_image", ""),
                source_url=DIRECT_MODE.get("source_url", ""),
            )
            print(f"\n🎉 完成！图片已保存到: {output_path}")
        except Exception as e:
            print(f"❌ 生成失败: {e}")
            import traceback
            traceback.print_exc()
            sys.exit(1)
    else:
        # 命令行模式
        main()

