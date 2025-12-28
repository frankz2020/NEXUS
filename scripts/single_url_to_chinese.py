#!/usr/bin/env python3
"""
单URL中文新闻生成器
================

用法：
    python scripts/single_url_to_chinese.py <新闻URL>
    
    # 也可以指定自定义标题
    python scripts/single_url_to_chinese.py <新闻URL> --title "自定义标题"

示例：
    python scripts/single_url_to_chinese.py "https://www.nyu.edu/news/example-article"

功能：
    1. 抓取网页内容
    2. 生成英文摘要 (使用 Gemini)
    3. 翻译并优化成中文新闻文稿

输出：
    - 中文标题
    - 中文新闻正文
    - 英文摘要（可选）
"""

import sys
import os
import argparse
import json
from datetime import datetime

# Add project root to path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from news_bot.core import config
from news_bot.processing import article_handler
from news_bot.generation import summarizer
from news_bot.localization import translator
from news_bot.reporting import google_docs_exporter


# Default school profile for single URL processing (generic Chinese student audience)
DEFAULT_SCHOOL_PROFILE = {
    "school_name": "General",
    "school_location": "the United States",
    "prompt_context": {
        "audience_en": "Chinese international students and Chinese-speaking readers"
    }
}


def process_single_url(url: str, custom_title: str = None, show_english: bool = False, school_profile: dict = None) -> dict:
    """
    处理单个URL并生成中文新闻文稿。
    
    Args:
        url: 新闻文章的URL
        custom_title: 可选的自定义标题（用于参考）
        show_english: 是否在输出中显示英文摘要
        school_profile: 可选的学校配置（影响受众定义）
    
    Returns:
        包含中文标题和正文的字典
    """
    school = school_profile or DEFAULT_SCHOOL_PROFILE
    
    print("=" * 60)
    print("单URL中文新闻生成器")
    print("=" * 60)
    print(f"URL: {url}")
    if custom_title:
        print(f"参考标题: {custom_title}")
    print()
    
    # Step 1: 抓取文章内容
    print(">>> Step 1: 抓取网页内容...")
    article_text = article_handler.fetch_and_extract_text(url)
    
    if not article_text:
        print("错误: 无法抓取网页内容")
        return None
    
    print(f"成功抓取内容 ({len(article_text)} 字符, 约 {len(article_text.split())} 单词)")
    print()
    
    # Step 2: 生成英文摘要
    print(">>> Step 2: 生成英文摘要...")
    english_summary = summarizer.generate_summary_with_gemini(
        school=school,
        article_text=article_text,
        article_url=url,
        article_title=custom_title or "N/A"
    )
    
    if not english_summary or "failed" in english_summary.lower():
        print("❌ 错误: 无法生成英文摘要")
        return None
    
    print(f"✅ 英文摘要生成成功 ({len(english_summary)} 字符)")
    print()
    
    # Step 3: 翻译成中文
    print(">>> Step 3: 翻译并优化成中文...")
    translation_input = {
        "summary": english_summary,
        "source_url": url,
        "reported_publication_date": datetime.now().strftime("%Y-%m-%d"),
        "original_title": custom_title or "N/A"
    }
    
    translation_output = translator.translate_and_restyle_to_chinese(translation_input)
    
    if not translation_output:
        print("错误: 翻译失败")
        return None
    
    chinese_title = translation_output.get("chinese_title", "标题生成失败")
    chinese_report = translation_output.get("refined_chinese_news_report", "翻译失败")
    
    if "失败" in chinese_title or "失败" in chinese_report:
        print("⚠️ 警告: 翻译可能不完整")
    else:
        print("✅ 中文新闻文稿生成成功！")
    
    print()
    print("=" * 60)
    print("📰 生成结果")
    print("=" * 60)
    print()
    print(f"【标题】{chinese_title}")
    print()
    print("【正文】")
    print(chinese_report)
    print()
    
    if show_english:
        print("-" * 40)
        print("【英文摘要】(参考)")
        print(english_summary)
        print()
    
    print("=" * 60)
    print(f"来源: {url}")
    print("=" * 60)
    
    return {
        "url": url,
        "chinese_title": chinese_title,
        "chinese_report": chinese_report,
        "english_summary": english_summary,
        "generated_at": datetime.now().isoformat()
    }


def main():
    parser = argparse.ArgumentParser(
        description="单URL中文新闻生成器 - 将英文新闻转换为中文文稿",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
    %(prog)s "https://www.nyu.edu/news/article"
    %(prog)s "https://example.com/news" --title "原始标题"
    %(prog)s "https://example.com/news" --show-english --save
        """
    )
    
    parser.add_argument("url", help="新闻文章的URL")
    parser.add_argument("--title", "-t", help="可选的原始标题（用于参考）", default=None)
    parser.add_argument("--show-english", "-e", action="store_true", help="显示英文摘要")
    parser.add_argument("--save", "-s", action="store_true", help="保存结果到JSON文件")
    parser.add_argument("--output", "-o", help="输出文件路径 (默认: news_reports/single_url_*.json)")
    parser.add_argument("--gdoc", "-g", action="store_true", help="导出到 Google Doc")
    
    args = parser.parse_args()
    
    # Validate URL
    if not args.url.startswith("http"):
        print("错误: URL必须以 http:// 或 https:// 开头")
        sys.exit(1)
    
    # Validate config
    try:
        config.validate_config()
    except ValueError as e:
        print(f"配置错误: {e}")
        sys.exit(1)
    
    # Process URL
    result = process_single_url(
        url=args.url,
        custom_title=args.title,
        show_english=args.show_english
    )
    
    if not result:
        print("❌ 处理失败")
        sys.exit(1)
    
    # Save if requested
    if args.save:
        output_dir = os.path.join(PROJECT_ROOT, config.DEFAULT_OUTPUT_DIR)
        os.makedirs(output_dir, exist_ok=True)
        
        if args.output:
            output_path = args.output
        else:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_path = os.path.join(output_dir, f"single_url_{timestamp}.json")
        
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        
        print(f"\n💾 结果已保存到: {output_path}")
    
    # Export to Google Doc if requested
    if args.gdoc:
        print("\n>>> 导出到 Google Doc...")
        # Prepare report data in the format expected by google_docs_exporter
        report_data = [{
            "chinese_title": result["chinese_title"],
            "refined_chinese_news_report": result["chinese_report"],
            "source_url": result["url"]
        }]
        
        today = datetime.now().date()
        doc_url = google_docs_exporter.update_or_create_news_document(
            school=DEFAULT_SCHOOL_PROFILE,
            reports_data=report_data,
            week_start_date=today,
            week_end_date=today,
            is_email=True  # Use breaking news format for single article
        )
        
        if doc_url:
            print(f"\nGoogle Doc 链接: {doc_url}")
        else:
            print("\n导出到 Google Doc 失败")
    
    print("\n完成！")


if __name__ == "__main__":
    main()

