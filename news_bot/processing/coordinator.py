import json
import os
import re
from typing import List, Dict, Optional, Callable, Tuple
from datetime import datetime, timedelta, date

from news_bot.core import config, school_config
from news_bot.reporting import google_docs_exporter


def _sort_key(report: Dict) -> Tuple:
    """Sort key for reports: newest first by publication date."""
    date_str = report.get("reported_publication_date", "")
    try:
        if date_str and date_str != "N/A" and date_str != "Date not found":
            date_obj = datetime.strptime(date_str, "%Y-%m-%d").date()
            return (-date_obj.toordinal(), report.get("processing_timestamp", ""))
    except (ValueError, AttributeError):
        pass
    return (0, report.get("processing_timestamp", ""))


def process_reports_individually(
    school_profile: dict,
    reports: List[Dict],
    progress_callback: Optional[Callable[[int, str], None]] = None
) -> List[Dict]:
    """
    Process reports and export each to an individual Google Doc.
    
    Args:
        school_profile: School configuration dict
        reports: List of report dicts from main_orchestrator
        progress_callback: Optional callback function(progress_pct, message) for UI updates
    
    Returns:
        List of result dicts, each containing:
        - chinese_title
        - chinese_report (refined_chinese_news_report)
        - english_summary
        - source_url
        - doc_link
    """
    def update_progress(pct: int, msg: str):
        if progress_callback:
            progress_callback(pct, msg)
        print(f"[Coordinator {pct}%] {msg}")
    
    if not reports:
        update_progress(100, "No reports to process")
        return []
    
    update_progress(5, f"Processing {len(reports)} reports...")
    
    # Set default relevance values
    for report in reports:
        report["relevance_score"] = 10
        report["relevance_reason"] = "已通过Step 1相关性筛选"
    
    # Sort by publication date (newest first)
    sorted_reports = sorted(reports, key=_sort_key)
    
    # Extract intro
    apply_intro_extraction(sorted_reports)
    
    update_progress(10, "Reports sorted and processed")
    
    # Get date range for doc title
    start_date, end_date = config.get_news_date_range()
    
    # Export each report individually
    results = []
    total = len(sorted_reports)
    
    for i, report in enumerate(sorted_reports):
        # Calculate progress: 10-95% for exporting
        progress_pct = 10 + int((i / total) * 85)
        
        chinese_title = report.get("chinese_title", "Untitled")
        update_progress(progress_pct, f"Exporting {i+1}/{total}: {chinese_title[:30]}...")
        
        try:
            # Export single report to Google Doc
            doc_link = google_docs_exporter.update_or_create_news_document(
                school=school_profile,
                reports_data=[report],  # Single report
                week_start_date=start_date,
                week_end_date=end_date,
                is_email=False
            )
            
            results.append({
                "chinese_title": chinese_title,
                "chinese_report": report.get("refined_chinese_news_report", ""),
                "english_summary": report.get("english_summary", ""),
                "source_url": report.get("source_url", ""),
                "doc_link": doc_link,
                "original_title": report.get("original_title", ""),
                "reported_publication_date": report.get("reported_publication_date", "")
            })
            
            if doc_link:
                update_progress(progress_pct, f"Created doc for: {chinese_title[:30]}...")
            else:
                update_progress(progress_pct, f"Failed to create doc for: {chinese_title[:30]}...")
                
        except Exception as e:
            print(f"Error exporting report {i+1}: {e}")
            results.append({
                "chinese_title": chinese_title,
                "chinese_report": report.get("refined_chinese_news_report", ""),
                "english_summary": report.get("english_summary", ""),
                "source_url": report.get("source_url", ""),
                "doc_link": None,
                "doc_error": str(e),
                "original_title": report.get("original_title", ""),
                "reported_publication_date": report.get("reported_publication_date", "")
            })
    
    update_progress(100, f"Completed: {len([r for r in results if r.get('doc_link')])} docs created")
    return results


# --------------------------
# Step 1: 提取第一句作为 intro (Refinement is now done during translation)
# Note: Relevance filtering is trusted from Step 1 discovery phase, so no additional scoring needed
# --------------------------

def apply_intro_extraction(reports: List[Dict]) -> None:
    """
    Extracts the first sentence from refined Chinese news reports as intro.
    Refinement is now done during translation, so we only extract the intro here.
    """
    for report in reports:
        refined_text = report.get("refined_chinese_news_report", "")
        if not refined_text.strip():
            continue

        # 提取第一句作为 intro（以句号或换行符切分）
        first_sentence = ""
        split_by_punc = re.split(r'(?<=[。！？])\s*', refined_text.strip())
        for sentence in split_by_punc:
            if sentence:
                first_sentence = sentence.strip()
                break

        report["gemini_generated_intro"] = first_sentence

# --------------------------
# Step 4: 主流程
# --------------------------

def process_news_report(choosen_school: dict[str, str], input_path: str, output_path: str):
    if not os.path.exists(input_path):
        print(f"文件不存在: {input_path}")
        return

    with open(input_path, 'r', encoding='utf-8') as f:
        reports = json.load(f)

    print(f"正在处理 {len(reports)} 篇新闻...")
    
    # Trust Step 1 filtering - no need for additional relevance scoring
    # Set default relevance values for backward compatibility
    for report in reports:
        report["relevance_score"] = 10  # All articles passed Step 1 filtering, so assume high relevance
        report["relevance_reason"] = "已通过Step 1相关性筛选 (Passed Step 1 relevance filter)"
    
    # Sort by publication date (newest first)
    sorted_reports = sorted(reports, key=_sort_key)

    # Extract intro from already-refined reports (refinement now happens during translation)
    apply_intro_extraction(sorted_reports)

    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(sorted_reports, f, ensure_ascii=False, indent=2)

    print(f"已保存至: {output_path}")

    # --- Export sorted to Google Doc ---
    print("开始导出排序后的结果到 Google Doc...")
    
    # Get the configured date range
    start_date, end_date = config.get_news_date_range()
    
    gdoc_url = google_docs_exporter.update_or_create_news_document(choosen_school, sorted_reports, start_date, end_date, is_email=False)
    if gdoc_url:
        print(f"导出成功: {gdoc_url}")
    else:
        print("导出失败，请检查 API 设置与授权。")

# --------------------------
# Step 5: 执行入口
# --------------------------

if __name__ == "__main__":
    # Get configured date range to find the correct file
    start_date, end_date = config.get_news_date_range()
    
    # Look for files with the date range in the filename
    input_file = None
    reports_dir = "news_reports"
    
    # Pick school to collect news from
    print(f"=== Please pick a school to collect news from: ===")
    schools_dict = school_config.SCHOOL_PROFILES
    for school, info in schools_dict.items():
        print(f"  {info['id']}: {info['school_name']}")
    choosen_school_id = int(input("Please enter the ID of the school you want to collect news from: "))
    choosen_school = list(schools_dict.values())[choosen_school_id - 1]   
    if not os.path.exists(reports_dir):
        print(f"Reports directory '{reports_dir}' does not exist.")
        exit(1)
    
    # Pattern to match files with date range
    pattern = f"weekly_student_news_report_{start_date}_{end_date}"
    pattern_email = f"breaking_news_report_{start_date}"
    
    # Find the most recent file matching the pattern
    for fname in sorted(os.listdir(reports_dir), reverse=True):
        if fname.startswith(pattern) and fname.endswith(".json") and "_sorted" not in fname:
            input_file = os.path.join(reports_dir, fname)
            break
    
    # Fallback to today's date pattern if custom date range file not found
    if input_file is None:
        today = datetime.now().strftime("%Y-%m-%d")
        pattern = f"weekly_student_news_report_{today}"
        
        for fname in sorted(os.listdir(reports_dir), reverse=True):
            if fname.startswith(pattern) and fname.endswith(".json") and "_sorted" not in fname:
                input_file = os.path.join(reports_dir, fname)
                break
    
    # Final fallback to any recent file
    if input_file is None:
        for fname in sorted(os.listdir(reports_dir), reverse=True):
            if fname.startswith("weekly_student_news_report_") and fname.endswith(".json") and "_sorted" not in fname:
                input_file = os.path.join(reports_dir, fname)
                print(f"Warning: Using fallback file (may not match configured date range): {fname}")
                break
    
    if input_file is None:
        print(f"未找到日期范围 {start_date} 到 {end_date} 的新闻摘要文件。")
        print("请先运行 main_orchestrator.py。")
    else:
        output_file = input_file.replace(".json", "_sorted.json")
        print(f"读取输入文件: {input_file}")
        print(f"处理日期范围: {start_date} 到 {end_date}")
        process_news_report(choosen_school, input_file, output_file)