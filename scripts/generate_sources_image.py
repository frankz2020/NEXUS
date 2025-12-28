#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
资料来源图片生成器
==================

用法：
    # 直接编辑模式 - 修改下面的 URLS 列表然后运行
    python scripts/generate_sources_image.py
    
    # 命令行模式
    python scripts/generate_sources_image.py --school NYU --urls "url1" "url2" "url3"

支持的学校：
    NYU, USC, EMORY, UCD (UC DAVIS), UBC, EDINBURGH
"""

import sys
import os
import json
import tempfile
import argparse
from pathlib import Path
from datetime import datetime

# Add project root to path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from news_bot.processing.image_generator import make_reference_image_from_reports

# 学校品牌配色
SCHOOL_CONFIG = {
    "NYU": {"brand_color": "#57068c", "folder": "NYU_Weekly"},
    "USC": {"brand_color": "#990000", "folder": "USC_Weekly"},
    "EMORY": {"brand_color": "#222c66", "folder": "EMORY_Weekly"},
    "UCD": {"brand_color": "#022851", "folder": "UCD_Weekly"},
    "UC DAVIS": {"brand_color": "#022851", "folder": "UCD_Weekly"},
    "UBC": {"brand_color": "#002145", "folder": "UBC_Weekly"},
    "EDINBURGH": {"brand_color": "#041e42", "folder": "EDIN_Weekly"},
}


def generate_sources_image(
    urls: list,
    school: str = "NYU",
    output_path: str = None,
    output_dir: str = "wechat_images",
    page_width: int = 540,
    device_scale: int = 4,
) -> str:
    """
    根据 URL 列表生成资料来源图片。
    
    Args:
        urls: URL 列表
        school: 学校名称
        output_path: 输出路径（可选）
        output_dir: 输出目录
        page_width: 图片宽度
        device_scale: 缩放比例
    
    Returns:
        生成的图片路径
    """
    # 标准化学校名称
    school_upper = school.upper().strip()
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
        final_output = output_path
    else:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        final_output = str(out_dir / f"{timestamp}_资料来源.png")
    
    print(f"🎨 学校: {school_upper}")
    print(f"🎨 品牌色: {brand_color}")
    print(f"📋 URL 数量: {len(urls)}")
    print(f"💾 输出: {final_output}")
    print()
    
    # 打印 URL 列表
    for i, url in enumerate(urls, 1):
        print(f"  {i}. {url[:60]}...")
    print()
    
    # 创建临时 JSON 文件
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False, encoding='utf-8') as f:
        json.dump([{"source_url": url} for url in urls], f, ensure_ascii=False, indent=2)
        temp_json = f.name
    
    try:
        # 生成图片
        result = make_reference_image_from_reports(
            sorted_json_path=temp_json,
            output_dir=str(out_dir),
            filename=Path(final_output).name,
            top_n=len(urls),
            page_width=page_width,
            device_scale=device_scale,
            brand_color=brand_color,
        )
        print(f"✅ 资料来源图片生成成功: {result}")
        return result
    finally:
        # 删除临时文件
        try:
            os.remove(temp_json)
        except Exception:
            pass


def main():
    parser = argparse.ArgumentParser(
        description="资料来源图片生成器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--school", "-s", default="NYU",
                        help="学校名称 (NYU, USC, EMORY, UCD, UBC, EDINBURGH)")
    parser.add_argument("--urls", "-u", nargs="+",
                        help="URL 列表")
    parser.add_argument("--output", "-o",
                        help="输出文件路径（可选）")
    parser.add_argument("--output-dir", default="wechat_images",
                        help="输出目录")
    
    args = parser.parse_args()
    
    if not args.urls:
        print("❌ 错误: 必须指定 --urls 参数")
        sys.exit(1)
    
    generate_sources_image(
        urls=args.urls,
        school=args.school,
        output_path=args.output,
        output_dir=args.output_dir,
    )


if __name__ == "__main__":
    import sys
    
    # ===================================================================
    # 🎯 直接编辑模式 - 在这里填写 URL 列表，然后运行脚本
    # ===================================================================
    
    DIRECT_MODE = {
        "enabled": True,  # 设为 True 启用直接模式
        
        "school": "EDINBURGH",  # NYU, USC, EMORY, UCD, UBC, EDINBURGH
        
        "urls": [
            "https://www.edinburghlive.co.uk/news/edinburgh-news/edinburgh-hit-snow-new-year-33129421",
            "https://www.ed.ac.uk/news/shrinking-ai-memory-boosts-accuracy",
        ],
        
        "output": None,  # 输出路径（None = 自动生成）
    }
    
    # ===================================================================
    
    # 判断运行模式
    if len(sys.argv) == 1 and DIRECT_MODE["enabled"]:
        # 直接模式
        print("=" * 60)
        print("📋 资料来源图片生成器 - 直接编辑模式")
        print("=" * 60)
        
        try:
            output_path = generate_sources_image(
                urls=DIRECT_MODE["urls"],
                school=DIRECT_MODE["school"],
                output_path=DIRECT_MODE.get("output"),
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

