# app.py - NEXUS News Production Studio
# A professional web interface for news content generation

import sys
import os

# Fix SSL certificate loading issue on macOS before any other imports
# This must be done BEFORE importing requests or any module that uses it
try:
    import certifi
    os.environ['SSL_CERT_FILE'] = certifi.where()
    os.environ['REQUESTS_CA_BUNDLE'] = certifi.where()
except ImportError:
    pass

# Workaround for requests SSL preloading issue on macOS
# Patch the SSL context before requests is imported
try:
    import ssl
    ssl._create_default_https_context = ssl._create_unverified_context
except Exception:
    pass

import json
import uuid
import threading
import traceback
import logging
from datetime import datetime
from pathlib import Path
from queue import Queue
from typing import Dict, Any, Optional
import tempfile
import zipfile

from flask import Flask, render_template, request, jsonify, Response, send_file

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[logging.StreamHandler(sys.stderr)]
)
logger = logging.getLogger('nexus')

# Add project root to path
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# Pre-import modules (optional, will be imported in workers if this fails)
try:
    import requests
    from news_bot.processing import article_handler
    from news_bot.generation import summarizer
    from news_bot.localization import translator
    from news_bot.reporting import google_docs_exporter
    from news_bot.utils import prompt_logger
    from scripts.text_to_image import generate_news_image as _gen_img
    from scripts.generate_sources_image import generate_sources_image as _gen_src
    logger.info("All modules pre-imported successfully")
except Exception as e:
    logger.warning(f"Some modules failed to pre-import (will retry in workers): {e}")

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'nexus-studio-secret')

# ============================================================================
# TASK QUEUE SYSTEM
# ============================================================================
# Each task has: id, type, status, progress, result, error, created_at, updated_at

tasks: Dict[str, Dict[str, Any]] = {}
task_lock = threading.Lock()
sse_queues: Dict[str, Queue] = {}  # task_id -> queue for SSE updates

def create_task(task_type: str, params: dict) -> str:
    """Create a new task and return its ID."""
    task_id = str(uuid.uuid4())[:8]
    with task_lock:
        tasks[task_id] = {
            "id": task_id,
            "type": task_type,
            "status": "pending",
            "progress": 0,
            "message": "Queued",
            "params": params,
            "result": None,
            "error": None,
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
        }
        sse_queues[task_id] = Queue()
    return task_id

def update_task(task_id: str, status: str = None, progress: int = None, 
                message: str = None, result: Any = None, error: str = None):
    """Update task status and broadcast to SSE."""
    with task_lock:
        if task_id not in tasks:
            return
        task = tasks[task_id]
        if status:
            task["status"] = status
        if progress is not None:
            task["progress"] = progress
        if message:
            task["message"] = message
        if result is not None:
            task["result"] = result
        if error:
            task["error"] = error
            task["status"] = "error"
        task["updated_at"] = datetime.now().isoformat()
        
        # Broadcast to SSE queue
        if task_id in sse_queues:
            sse_queues[task_id].put(json.dumps(task))

def get_task(task_id: str) -> Optional[dict]:
    """Get task by ID."""
    with task_lock:
        return tasks.get(task_id, {}).copy() if task_id in tasks else None

def list_tasks() -> list:
    """List all tasks, newest first."""
    with task_lock:
        return sorted(tasks.values(), key=lambda x: x["created_at"], reverse=True)

# ============================================================================
# SCHOOL CONFIG
# ============================================================================
SCHOOLS = {
    "NYU": {"name": "New York University", "color": "#57068c", "folder": "NYU_Weekly"},
    "USC": {"name": "University of Southern California", "color": "#990000", "folder": "USC_Weekly"},
    "EMORY": {"name": "Emory University", "color": "#222c66", "folder": "EMORY_Weekly"},
    "UCD": {"name": "UC Davis", "color": "#022851", "folder": "UCD_Weekly"},
    "UBC": {"name": "University of British Columbia", "color": "#002145", "folder": "UBC_Weekly"},
    "EDINBURGH": {"name": "University of Edinburgh", "color": "#041e42", "folder": "EDIN_Weekly"},
}

# ============================================================================
# BACKGROUND WORKERS
# ============================================================================

def worker_url_to_doc(task_id: str, url: str, title: str = None):
    """Process URL to generate Chinese news and export to Google Doc."""
    try:
        update_task(task_id, status="running", progress=5, message="Fetching article...")
        
        # Import modules (needed if pre-import failed)
        from news_bot.processing import article_handler
        from news_bot.generation import summarizer
        from news_bot.localization import translator
        from news_bot.reporting import google_docs_exporter
        from news_bot.utils import prompt_logger
        
        # Initialize prompt log
        prompt_logger.initialize_prompt_log()
        
        # Step 1: Fetch article
        update_task(task_id, progress=10, message="Extracting content...")
        article_text = article_handler.fetch_and_extract_text(url)
        if not article_text:
            update_task(task_id, error="Failed to fetch article content")
            return
        
        # Step 2: Generate English summary
        update_task(task_id, progress=30, message="Generating English summary...")
        school_profile = {
            "school_name": "General",
            "school_location": "Global",
            "prompt_context": {"audience_en": "Chinese readers"}
        }
        english_summary = summarizer.generate_summary_with_gemini(
            school_profile, article_text, url, title or "Article"
        )
        if not english_summary or "failed" in english_summary.lower():
            update_task(task_id, error="Failed to generate summary")
            return
        
        # Step 3: Translate to Chinese
        update_task(task_id, progress=60, message="Translating to Chinese...")
        english_report_data = {
            "summary": english_summary,
            "source_url": url,
            "reported_publication_date": datetime.now().strftime("%Y-%m-%d"),
            "original_title": title or "Article"
        }
        translation_output = translator.translate_and_restyle_to_chinese(english_report_data)
        
        if not translation_output:
            update_task(task_id, error="Translation failed")
            return
        
        chinese_title = translation_output.get("chinese_title", "Untitled")
        chinese_report = translation_output.get("refined_chinese_news_report", "")
        
        # Step 4: Export to Google Doc
        update_task(task_id, progress=85, message="Exporting to Google Doc...")
        doc_link = None
        doc_error = None
        try:
            doc_link = google_docs_exporter.update_or_create_news_document(
                school=school_profile,
                reports_data=[{
                    "chinese_title": chinese_title,
                    "refined_chinese_news_report": chinese_report,
                    "source_url": url
                }],
                week_start_date=datetime.now().date(),
                week_end_date=datetime.now().date(),
                is_email=False
            )
            if doc_link:
                logger.info(f"Google Doc created successfully: {doc_link}")
            else:
                doc_error = "Google Docs exporter returned None (check credentials)"
                logger.warning(doc_error)
        except Exception as e:
            doc_error = f"Google Docs export failed: {e}"
            logger.warning(doc_error)
            logger.warning(traceback.format_exc())
        
        result = {
            "title": chinese_title,  # For sidebar display
            "chinese_title": chinese_title,
            "chinese_report": chinese_report,
            "english_summary": english_summary,
            "source_url": url,
            "doc_link": doc_link,
            "doc_error": doc_error  # Include error for debugging
        }
        
        update_task(task_id, status="completed", progress=100, 
                   message="Completed", result=result)
        
    except Exception as e:
        logger.error(f"Error in worker_url_to_doc: {e}\n{traceback.format_exc()}")
        update_task(task_id, error=str(e))

def worker_text_to_image(task_id: str, school: str, title: str, content: str, 
                         source_url: str = None, cover_image: str = None):
    """Generate news image from text."""
    try:
        update_task(task_id, status="running", progress=10, message="Preparing content...")
        
        from scripts.text_to_image import generate_news_image
        
        update_task(task_id, progress=30, message="Rendering image...")
        
        output_path = generate_news_image(
            school=school,
            title=title,
            content=content,
            source_url=source_url or "",
            cover_image=cover_image or "",
        )
        
        result = {
            "image_path": output_path,
            "school": school,
            "title": title,
            "source_url": source_url,
        }
        
        update_task(task_id, status="completed", progress=100,
                   message="Image generated", result=result)
        
    except Exception as e:
        logger.error(f"Error in worker_text_to_image: {e}\n{traceback.format_exc()}")
        update_task(task_id, error=str(e))

def worker_sources_image(task_id: str, school: str, urls: list):
    """Generate sources reference image."""
    try:
        update_task(task_id, status="running", progress=10, message="Preparing sources...")
        
        from scripts.generate_sources_image import generate_sources_image
        
        update_task(task_id, progress=40, message="Rendering image...")
        
        output_path = generate_sources_image(
            urls=urls,
            school=school,
        )
        
        result = {
            "image_path": output_path,
            "school": school,
            "url_count": len(urls),
        }
        
        update_task(task_id, status="completed", progress=100,
                   message="Sources image generated", result=result)
        
    except Exception as e:
        logger.error(f"Error in worker_sources_image: {e}\n{traceback.format_exc()}")
        update_task(task_id, error=str(e))

def worker_gdoc_to_images(task_id: str, doc_id: str, school: str = None):
    """Generate WeChat images from Google Doc."""
    try:
        update_task(task_id, status="running", progress=10, message="Fetching document...")
        
        from scripts.gdoc_to_wechat_images import (
            fetch_doc, parse_news_from_doc, render_to_images, 
            pick_brand_from_title, _extract_doc_id, folder_for_school
        )
        from pathlib import Path
        
        # Extract doc ID if URL was provided
        actual_doc_id = _extract_doc_id(doc_id)
        
        # Fetch document
        doc = fetch_doc(actual_doc_id)
        if not doc:
            update_task(task_id, error="Failed to fetch Google Doc")
            return
        
        doc_title = (doc.get('title') or 'Untitled').strip()
        update_task(task_id, progress=30, message="Parsing document...")
        
        # Parse articles from doc
        items = parse_news_from_doc(doc, extract_images=True)
        
        if not items:
            update_task(task_id, error="No articles found in document")
            return
        
        # Detect school and brand color
        auto_color, detected_school = pick_brand_from_title(doc_title)
        school_name = school or detected_school
        brand_color = auto_color
        
        update_task(task_id, progress=50, message=f"Generating {len(items)} images...")
        
        # Determine output directory
        out_dir = "wechat_images"
        school_dir = folder_for_school(school_name) if school_name else ""
        
        # Generate images
        render_to_images(
            items,
            doc_title=doc_title,
            out_dir=out_dir,
            page_width=540,
            device_scale=2,  # Reduced from 4 for faster rendering
            brand_color=brand_color,
            title_size=22.5,
            body_size=22.5,
            top_n=10,
            skip_image_fetch=False,
            school_name=school_name,
        )
        
        # Find generated files
        output_path = Path(out_dir) / school_dir if school_dir else Path(out_dir)
        generated_files = [str(f) for f in output_path.glob("*.png")] if output_path.exists() else []
        
        result = {
            "title": doc_title,
            "output_dir": str(output_path),
            "files": generated_files[-len(items)-1:],  # Most recent files
            "article_count": len(items),
            "school": school_name,
        }
        
        update_task(task_id, status="completed", progress=100,
                   message=f"Generated {len(items)} images", result=result)
        
    except Exception as e:
        logger.error(f"Error in worker_gdoc_to_images: {e}\n{traceback.format_exc()}")
        update_task(task_id, error=str(e))

# ============================================================================
# ROUTES
# ============================================================================

@app.route('/')
def index():
    """Render the main page."""
    return render_template('index.html', schools=SCHOOLS)

@app.route('/health')
def health():
    """Health check endpoint."""
    return jsonify({"status": "ok", "timestamp": datetime.now().isoformat()})

# --- Task Management ---

@app.route('/api/tasks', methods=['GET'])
def api_list_tasks():
    """List all tasks."""
    return jsonify({"tasks": list_tasks()})

@app.route('/api/tasks/<task_id>', methods=['GET'])
def api_get_task(task_id):
    """Get task status."""
    task = get_task(task_id)
    if not task:
        return jsonify({"error": "Task not found"}), 404
    return jsonify(task)

@app.route('/api/tasks/<task_id>', methods=['DELETE'])
def api_delete_task(task_id):
    """Delete/cancel a task."""
    with task_lock:
        if task_id in tasks:
            del tasks[task_id]
        if task_id in sse_queues:
            del sse_queues[task_id]
    return jsonify({"success": True})

@app.route('/api/tasks/<task_id>/stream')
def api_task_stream(task_id):
    """SSE stream for task updates."""
    def generate():
        if task_id not in sse_queues:
            yield f"data: {json.dumps({'error': 'Task not found'})}\n\n"
            return
        
        queue = sse_queues[task_id]
        # Send current state first
        task = get_task(task_id)
        if task:
            yield f"data: {json.dumps(task)}\n\n"
        
        # Stream updates
        while True:
            try:
                update = queue.get(timeout=30)
                yield f"data: {update}\n\n"
                data = json.loads(update)
                if data.get("status") in ["completed", "error"]:
                    break
            except:
                # Heartbeat
                yield f"data: {json.dumps({'type': 'heartbeat'})}\n\n"
                task = get_task(task_id)
                if task and task.get("status") in ["completed", "error"]:
                    break
    
    return Response(generate(), mimetype='text/event-stream', headers={
        'Cache-Control': 'no-cache',
        'Connection': 'keep-alive',
        'X-Accel-Buffering': 'no'
    })

# --- URL to Chinese News ---

@app.route('/api/url-to-doc', methods=['POST'])
def api_url_to_doc():
    """Start URL to Chinese news task."""
    data = request.json
    url = data.get('url', '').strip()
    title = data.get('title', '').strip() or None
    
    if not url:
        return jsonify({"error": "URL is required"}), 400
    
    task_id = create_task("url_to_doc", {"url": url, "title": title})
    
    thread = threading.Thread(target=worker_url_to_doc, args=(task_id, url, title))
    thread.daemon = True
    thread.start()
    
    return jsonify({"task_id": task_id, "message": "Task started"})

# --- Text to Image ---

@app.route('/api/text-to-image', methods=['POST'])
def api_text_to_image():
    """Generate image from text."""
    data = request.json
    school = data.get('school', 'NYU').upper()
    title = data.get('title', '').strip()
    content = data.get('content', '').strip()
    source_url = data.get('source_url', '').strip() or None
    cover_image = data.get('cover_image', '').strip() or None
    
    if not title or not content:
        return jsonify({"error": "Title and content are required"}), 400
    
    if school not in SCHOOLS:
        return jsonify({"error": f"Invalid school. Choose from: {list(SCHOOLS.keys())}"}), 400
    
    task_id = create_task("text_to_image", {
        "school": school, "title": title, "content": content[:100] + "..."
    })
    
    thread = threading.Thread(
        target=worker_text_to_image, 
        args=(task_id, school, title, content, source_url, cover_image)
    )
    thread.daemon = True
    thread.start()
    
    return jsonify({"task_id": task_id, "message": "Task started"})

# --- Sources Image ---

@app.route('/api/sources-image', methods=['POST'])
def api_sources_image():
    """Generate sources reference image."""
    data = request.json
    school = data.get('school', 'NYU').upper()
    urls = data.get('urls', [])
    
    if not urls or not isinstance(urls, list):
        return jsonify({"error": "URLs list is required"}), 400
    
    # Clean URLs
    urls = [u.strip() for u in urls if u.strip()]
    if not urls:
        return jsonify({"error": "At least one valid URL is required"}), 400
    
    if school not in SCHOOLS:
        return jsonify({"error": f"Invalid school. Choose from: {list(SCHOOLS.keys())}"}), 400
    
    task_id = create_task("sources_image", {"school": school, "url_count": len(urls)})
    
    thread = threading.Thread(target=worker_sources_image, args=(task_id, school, urls))
    thread.daemon = True
    thread.start()
    
    return jsonify({"task_id": task_id, "message": "Task started"})

# --- Google Doc to Images ---

@app.route('/api/gdoc-to-images', methods=['POST'])
def api_gdoc_to_images():
    """Generate WeChat images from Google Doc."""
    data = request.json
    doc_url = data.get('doc_url', '').strip()
    school = data.get('school', '').strip().upper() or None
    
    if not doc_url:
        return jsonify({"error": "Google Doc URL is required"}), 400
    
    # Extract doc ID from URL
    import re
    match = re.search(r'/d/([a-zA-Z0-9_-]+)', doc_url)
    if match:
        doc_id = match.group(1)
    else:
        doc_id = doc_url  # Assume it's already an ID
    
    if school and school not in SCHOOLS:
        return jsonify({"error": f"Invalid school. Choose from: {list(SCHOOLS.keys())}"}), 400
    
    task_id = create_task("gdoc_to_images", {"doc_id": doc_id[:20] + "...", "school": school})
    
    thread = threading.Thread(target=worker_gdoc_to_images, args=(task_id, doc_id, school))
    thread.daemon = True
    thread.start()
    
    return jsonify({"task_id": task_id, "message": "Task started"})

# --- File Downloads ---

@app.route('/api/download/<path:filepath>')
def api_download_file(filepath):
    """Download a generated file."""
    from urllib.parse import unquote
    # URL decode the filepath (handles Chinese characters)
    decoded_path = unquote(filepath)
    # Security: only allow files from wechat_images directory
    safe_path = Path('wechat_images') / decoded_path.replace('..', '')
    if not safe_path.exists():
        return jsonify({"error": f"File not found: {safe_path}"}), 404
    return send_file(safe_path, as_attachment=True)

@app.route('/api/preview/<path:filepath>')
def api_preview_file(filepath):
    """Preview a generated image (inline display, not download)."""
    from urllib.parse import unquote
    # URL decode the filepath (handles Chinese characters)
    decoded_path = unquote(filepath)
    # Security: only allow files from wechat_images directory
    safe_path = Path('wechat_images') / decoded_path.replace('..', '')
    logger.info(f"Preview request: filepath={filepath}, decoded={decoded_path}, safe_path={safe_path}, exists={safe_path.exists()}")
    if not safe_path.exists():
        return jsonify({"error": f"File not found: {safe_path}"}), 404
    return send_file(safe_path, mimetype='image/png')

@app.route('/api/download-folder/<path:folderpath>')
def api_download_folder(folderpath):
    """Download folder as ZIP."""
    safe_path = Path('wechat_images') / folderpath.replace('..', '')
    if not safe_path.exists() or not safe_path.is_dir():
        return jsonify({"error": "Folder not found"}), 404
    
    # Create ZIP
    temp_zip = tempfile.NamedTemporaryFile(delete=False, suffix='.zip')
    with zipfile.ZipFile(temp_zip.name, 'w', zipfile.ZIP_DEFLATED) as zf:
        for f in safe_path.glob('*.png'):
            zf.write(f, f.name)
    
    return send_file(temp_zip.name, as_attachment=True, 
                    download_name=f'{safe_path.name}.zip')

@app.route('/api/download-files', methods=['POST'])
def api_download_files():
    """Download specific files as ZIP (for current session images only)."""
    data = request.json
    files = data.get('files', [])
    name = data.get('name', 'images')
    
    if not files:
        return jsonify({"error": "No files specified"}), 400
    
    # Create ZIP with only the specified files
    temp_zip = tempfile.NamedTemporaryFile(delete=False, suffix='.zip')
    with zipfile.ZipFile(temp_zip.name, 'w', zipfile.ZIP_DEFLATED) as zf:
        for filepath in files:
            # Handle both full paths and relative paths
            if filepath.startswith('wechat_images/'):
                safe_path = Path(filepath)
            else:
                safe_path = Path('wechat_images') / filepath.replace('..', '')
            
            if safe_path.exists() and safe_path.is_file():
                zf.write(safe_path, safe_path.name)
            else:
                logger.warning(f"File not found for download: {safe_path}")
    
    return send_file(temp_zip.name, as_attachment=True, 
                    download_name=f'{name}.zip')

# --- Schools Info ---

@app.route('/api/schools', methods=['GET'])
def api_schools():
    """Get available schools."""
    return jsonify({"schools": SCHOOLS})

# ============================================================================
# MAIN
# ============================================================================

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 3000))
    debug = os.environ.get('FLASK_ENV', 'development') != 'production'
    
    print("=" * 60)
    print("NEXUS News Production Studio")
    print("=" * 60)
    print(f"Server: http://0.0.0.0:{port}")
    print("=" * 60)
    
    app.run(host='0.0.0.0', port=port, debug=debug, threaded=True)
