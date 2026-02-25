# app.py - NEXUS News Production Studio
# A professional web interface for news content generation

#redeployment test 2
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
import time
from datetime import datetime
from pathlib import Path
from queue import Queue
from typing import Dict, Any, Optional
import tempfile
import zipfile

from flask import Flask, render_template, request, jsonify, Response, send_file, redirect, url_for
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[logging.StreamHandler(sys.stderr)]
)
logger = logging.getLogger('nexus')

REDIS_URL = (
    os.environ.get('REDIS_URL')
    or os.environ.get('REDIS_PRIVATE_URL')
    or os.environ.get('REDIS_TLS_URL')
    or os.environ.get('REDIS_CONNECTION_URL')
    or ''
).strip()
REDIS_TASKS_HASH_KEY = 'nexus:tasks'
REDIS_TASKS_ZSET_KEY = 'nexus:tasks:created'
redis_client = None
if REDIS_URL:
    logger.info("Redis configured for task persistence.")
else:
    logger.warning("Redis not configured; tasks are in-memory only.")


def redis_enabled():
    return bool(REDIS_URL)


def get_redis_client():
    global redis_client
    if not REDIS_URL:
        return None
    if redis_client is None:
        import redis
        redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
        redis_client.ping()
        logger.info("Redis connection OK.")
    return redis_client


def redis_write_task(task, set_created=False, created_ts=None):
    client = get_redis_client()
    assert client is not None
    task_id = task.get("id")
    assert task_id
    client.hset(REDIS_TASKS_HASH_KEY, task_id, json.dumps(task))
    if set_created:
        if created_ts is None:
            created_ts = time.time()
        client.zadd(REDIS_TASKS_ZSET_KEY, {task_id: created_ts})


def redis_get_task(task_id: str):
    client = get_redis_client()
    assert client is not None
    raw = client.hget(REDIS_TASKS_HASH_KEY, task_id)
    if not raw:
        return None
    return json.loads(raw)


def redis_list_tasks():
    client = get_redis_client()
    assert client is not None
    task_ids = client.zrevrange(REDIS_TASKS_ZSET_KEY, 0, -1)
    if not task_ids:
        return []
    raw_tasks = client.hmget(REDIS_TASKS_HASH_KEY, task_ids)
    tasks_list = []
    for raw in raw_tasks:
        if raw:
            tasks_list.append(json.loads(raw))
    return tasks_list


def redis_delete_task(task_id: str):
    client = get_redis_client()
    assert client is not None
    client.hdel(REDIS_TASKS_HASH_KEY, task_id)
    client.zrem(REDIS_TASKS_ZSET_KEY, task_id)


# Add project root to path
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# Ensure Google credentials exist from environment variables
def restore_credentials_from_env():
    """Restore Google OAuth credentials from environment variables if files are missing."""
    try:
        # Import config to get paths
        from news_bot.core import config
        
        logger.info("Attempting to restore credentials from environment variables...")
        logger.info(f"Checking for GOOGLE_OAUTH_CREDENTIALS_JSON in env: {'GOOGLE_OAUTH_CREDENTIALS_JSON' in os.environ}")
        logger.info(f"Checking for GOOGLE_OAUTH_TOKEN_PICKLE_BASE64 in env: {'GOOGLE_OAUTH_TOKEN_PICKLE_BASE64' in os.environ}")
        
        # 1. Restore credentials.json
        if not os.path.exists(config.OAUTH_CREDENTIALS_FILE):
            creds_json = os.environ.get('GOOGLE_OAUTH_CREDENTIALS_JSON')
            if creds_json:
                logger.info(f"Restoring credentials.json from env var to {config.OAUTH_CREDENTIALS_FILE} (length: {len(creds_json)})")
                try:
                    with open(config.OAUTH_CREDENTIALS_FILE, 'w') as f:
                        f.write(creds_json)
                    logger.info("Successfully wrote credentials.json")
                except Exception as e:
                    logger.error(f"Failed to write credentials.json: {e}")
            else:
                logger.warning("GOOGLE_OAUTH_CREDENTIALS_JSON env var not set (credentials.json missing)")
        else:
             logger.info(f"credentials.json already exists at {config.OAUTH_CREDENTIALS_FILE}")
        
        # 2. Restore token.pickle
        if not os.path.exists(config.OAUTH_TOKEN_PICKLE_FILE):
            token_base64 = os.environ.get('GOOGLE_OAUTH_TOKEN_PICKLE_BASE64')
            if token_base64:
                import base64
                logger.info(f"Restoring token.pickle from env var to {config.OAUTH_TOKEN_PICKLE_FILE} (length: {len(token_base64)})")
                try:
                    with open(config.OAUTH_TOKEN_PICKLE_FILE, 'wb') as f:
                        f.write(base64.b64decode(token_base64))
                    logger.info("Successfully wrote token.pickle")
                except Exception as e:
                    logger.error(f"Failed to write token.pickle: {e}")
            else:
                logger.warning("GOOGLE_OAUTH_TOKEN_PICKLE_BASE64 env var not set (token.pickle missing)")
        else:
            logger.info(f"token.pickle already exists at {config.OAUTH_TOKEN_PICKLE_FILE}")
                
    except Exception as e:
        logger.error(f"Error restoring credentials: {e}")

# Try to restore credentials on startup
restore_credentials_from_env()

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
# AUTHENTICATION SYSTEM
# ============================================================================

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'
login_manager.login_message = 'Please log in to access the NEXUS Studio.'
login_manager.login_message_category = 'info'

# User activity log - stores {user_id: [{action, timestamp, details}]}
user_activity_log: Dict[str, list] = {}
activity_log_lock = threading.Lock()

def log_user_activity(user_id: str, action: str, details: dict = None):
    """Log user activity for tracking."""
    with activity_log_lock:
        if user_id not in user_activity_log:
            user_activity_log[user_id] = []
        
        entry = {
            "timestamp": datetime.now().isoformat(),
            "action": action,
            "details": details or {},
            "ip": request.remote_addr if request else None
        }
        user_activity_log[user_id].append(entry)
        
        # Keep only last 1000 entries per user
        if len(user_activity_log[user_id]) > 1000:
            user_activity_log[user_id] = user_activity_log[user_id][-1000:]
        
        # Also log to console for Railway logs
        logger.info(f"[USER ACTIVITY] {user_id}: {action} - {details}")

class User(UserMixin):
    """User model for Flask-Login."""
    def __init__(self, id, username, password_hash, display_name=None, role='user'):
        self.id = id
        self.username = username
        self.password_hash = password_hash
        self.display_name = display_name or username
        self.role = role
    
    def check_password(self, password):
        return check_password_hash(self.password_hash, password)
    
    def to_dict(self):
        return {
            "id": self.id,
            "username": self.username,
            "display_name": self.display_name,
            "role": self.role
        }

def load_users_from_env():
    """
    Load users from environment variables.
    Format: NEXUS_USERS='username1:password1:DisplayName1:role1,username2:password2:DisplayName2:role2'
    Or use individual variables:
    NEXUS_USER_1='username:password:DisplayName:role'
    NEXUS_USER_2='username:password:DisplayName:role'
    
    Fallback to NEXUS_ADMIN_USER and NEXUS_ADMIN_PASS for simple setup.
    """
    users = {}
    
    # Method 1: Bulk user definition
    users_str = os.environ.get('NEXUS_USERS', '')
    if users_str:
        for i, user_def in enumerate(users_str.split(',')):
            parts = user_def.strip().split(':')
            if len(parts) >= 2:
                username = parts[0].strip()
                password = parts[1].strip()
                display_name = parts[2].strip() if len(parts) > 2 else username
                role = parts[3].strip() if len(parts) > 3 else 'user'
                
                users[username] = User(
                    id=str(i + 1),
                    username=username,
                    password_hash=generate_password_hash(password),
                    display_name=display_name,
                    role=role
                )
    
    # Method 2: Individual user variables
    for i in range(1, 20):  # Support up to 20 individual users
        user_str = os.environ.get(f'NEXUS_USER_{i}', '')
        if user_str:
            parts = user_str.split(':')
            if len(parts) >= 2:
                username = parts[0].strip()
                password = parts[1].strip()
                display_name = parts[2].strip() if len(parts) > 2 else username
                role = parts[3].strip() if len(parts) > 3 else 'user'
                
                users[username] = User(
                    id=str(100 + i),
                    username=username,
                    password_hash=generate_password_hash(password),
                    display_name=display_name,
                    role=role
                )
    
    # Method 3: Simple admin setup (fallback)
    admin_user = os.environ.get('NEXUS_ADMIN_USER', '')
    admin_pass = os.environ.get('NEXUS_ADMIN_PASS', '')
    if admin_user and admin_pass and admin_user not in users:
        users[admin_user] = User(
            id='admin',
            username=admin_user,
            password_hash=generate_password_hash(admin_pass),
            display_name='Administrator',
            role='admin'
        )
    
    # Development fallback - only if no users configured
    if not users:
        dev_password = os.environ.get('NEXUS_DEV_PASS', 'nexus2024')
        users['admin'] = User(
            id='dev-admin',
            username='admin',
            password_hash=generate_password_hash(dev_password),
            display_name='Dev Admin',
            role='admin'
        )
        logger.warning("⚠️  No users configured! Using default dev credentials. Set NEXUS_ADMIN_USER and NEXUS_ADMIN_PASS in Railway.")
    
    return users

# Load users on startup
USERS = load_users_from_env()
logger.info(f"Loaded {len(USERS)} user(s): {list(USERS.keys())}")

@login_manager.user_loader
def load_user(user_id):
    """Load user by ID for Flask-Login."""
    for user in USERS.values():
        if user.id == user_id:
            return user
    return None

def get_user_by_username(username):
    """Get user by username."""
    return USERS.get(username)

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
    created_at = datetime.now().isoformat()
    created_ts = time.time()
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
            "created_at": created_at,
            "updated_at": created_at,
        }
        sse_queues[task_id] = Queue()
        if redis_enabled():
            redis_write_task(tasks[task_id], set_created=True, created_ts=created_ts)
    return task_id

def update_task(task_id: str, status: str = None, progress: int = None, 
                message: str = None, result: Any = None, error: str = None):
    """Update task status and broadcast to SSE."""
    with task_lock:
        task = tasks.get(task_id)
        if not task and redis_enabled():
            task = redis_get_task(task_id)
            if task:
                tasks[task_id] = task
        if not task:
            return
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
        if redis_enabled():
            redis_write_task(task)
        
        # Broadcast to SSE queue
        if task_id in sse_queues:
            sse_queues[task_id].put(json.dumps(task))

def get_task(task_id: str) -> Optional[dict]:
    """Get task by ID."""
    if redis_enabled():
        task = redis_get_task(task_id)
        return task.copy() if task else None
    with task_lock:
        return tasks.get(task_id, {}).copy() if task_id in tasks else None

def list_tasks() -> list:
    """List all tasks, newest first."""
    if redis_enabled():
        return redis_list_tasks()
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
        
        # Check if credential files exist OR env vars are present (Cloud-native approach)
        from news_bot.core import config
        
        # We no longer strictly enforce file existence here, as google_docs_exporter now supports env vars
        # But we'll still log debug info
        app_dir = os.path.dirname(os.path.abspath(__file__))
        
        # Try to restore credentials if missing (best effort for local compatibility)
        if not os.path.exists(config.OAUTH_CREDENTIALS_FILE) or not os.path.exists(config.OAUTH_TOKEN_PICKLE_FILE):
             try:
                 restore_credentials_from_env()
             except Exception:
                 pass # Ignore errors here, rely on exporter logic
        
        # Simplified Check: Just assume it will work if we have env vars or files
        has_creds = os.path.exists(config.OAUTH_CREDENTIALS_FILE) or 'GOOGLE_OAUTH_CREDENTIALS_JSON' in os.environ
        has_token = os.path.exists(config.OAUTH_TOKEN_PICKLE_FILE) or 'GOOGLE_OAUTH_TOKEN_PICKLE_BASE64' in os.environ
        
        if not has_creds and not has_token:
             # Only error if we have ABSOLUTELY NOTHING
             doc_error = "Missing Google Credentials (neither files nor env vars found)"
        
        if not doc_error:
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
            "school_code": school,  # Unified school code for batch download
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
            "school_code": school,  # Unified school code for batch download
            "url_count": len(urls),
        }
        
        update_task(task_id, status="completed", progress=100,
                   message="Sources image generated", result=result)
        
    except Exception as e:
        logger.error(f"Error in worker_sources_image: {e}\n{traceback.format_exc()}")
        update_task(task_id, error=str(e))

def worker_full_pipeline(task_id: str, school_code: str):
    """
    Run the full news collection pipeline:
    1. main_orchestrator: discover + process articles -> JSON
    2. coordinator: process reports -> individual Google Docs
    3. Create sub-tasks for each generated doc
    """
    try:
        update_task(task_id, status="running", progress=2, message="Initializing pipeline...")
        
        # Import modules
        from news_bot.core import school_config
        from news_bot.main_orchestrator import run_news_bot_for_school
        from news_bot.processing.coordinator import process_reports_individually
        
        # Get school profile
        school_profile = school_config.SCHOOL_PROFILES.get(school_code.lower())
        if not school_profile:
            update_task(task_id, error=f"Unknown school: {school_code}")
            return
        
        school_name = school_profile.get("school_name", school_code)
        update_task(task_id, progress=5, message=f"Starting pipeline for {school_name}...")
        
        # Step 1: Run main_orchestrator (5-60%)
        def orchestrator_progress(pct, msg):
            # Map 0-100 to 5-60
            mapped_pct = 5 + int(pct * 0.55)
            update_task(task_id, progress=mapped_pct, message=f"[Discovery] {msg}")
        
        json_path, reports = run_news_bot_for_school(school_profile, orchestrator_progress)
        
        if not reports:
            update_task(task_id, status="completed", progress=100, 
                       message="No articles found", result={
                           "school": school_name,
                           "article_count": 0,
                           "docs": []
                       })
            return
        
        update_task(task_id, progress=62, message=f"Found {len(reports)} articles, creating Google Docs...")
        
        # Step 2: Run coordinator to create individual Google Docs (62-95%)
        def coordinator_progress(pct, msg):
            # Map 0-100 to 62-95
            mapped_pct = 62 + int(pct * 0.33)
            update_task(task_id, progress=mapped_pct, message=f"[Export] {msg}")
        
        doc_results = process_reports_individually(school_profile, reports, coordinator_progress)
        
        # Step 3: Create sub-tasks for each doc (so they appear in doc queue)
        update_task(task_id, progress=96, message="Creating task entries...")
        
        created_tasks = []
        for doc_result in doc_results:
            if doc_result.get("doc_link"):
                # Create a completed url_to_doc task for each doc
                sub_task_id = str(uuid.uuid4())[:8]
                created_at = datetime.now().isoformat()
                created_ts = time.time()
                with task_lock:
                    tasks[sub_task_id] = {
                        "id": sub_task_id,
                        "type": "url_to_doc",
                        "status": "completed",
                        "progress": 100,
                        "message": "Created via pipeline",
                        "params": {
                            "url": doc_result.get("source_url", ""),
                            "title": doc_result.get("original_title", ""),
                            "pipeline_task": task_id
                        },
                        "result": {
                            "title": doc_result.get("chinese_title"),
                            "chinese_title": doc_result.get("chinese_title"),
                            "chinese_report": doc_result.get("chinese_report"),
                            "english_summary": doc_result.get("english_summary"),
                            "source_url": doc_result.get("source_url"),
                            "doc_link": doc_result.get("doc_link"),
                        },
                        "error": None,
                        "created_at": created_at,
                        "updated_at": created_at,
                    }
                    sse_queues[sub_task_id] = Queue()
                    if redis_enabled():
                        redis_write_task(tasks[sub_task_id], set_created=True, created_ts=created_ts)
                created_tasks.append({
                    "task_id": sub_task_id,
                    "title": doc_result.get("chinese_title"),
                    "doc_link": doc_result.get("doc_link")
                })
        
        # Complete the pipeline task
        result = {
            "school": school_name,
            "article_count": len(reports),
            "docs_created": len(created_tasks),
            "json_path": json_path,
            "created_tasks": created_tasks
        }
        
        update_task(task_id, status="completed", progress=100,
                   message=f"Pipeline completed: {len(created_tasks)} docs created",
                   result=result)
        
    except Exception as e:
        logger.error(f"Error in worker_full_pipeline: {e}\n{traceback.format_exc()}")
        update_task(task_id, error=str(e))


def worker_gdoc_to_images(task_id: str, doc_id: str, school: str = None, need_images: bool = True):
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
        
        # Match CLI --no-images semantics:
        # - need_images=True: include embedded doc images and source-url fetching
        # - need_images=False: disable all cover-image collection
        items = parse_news_from_doc(doc, extract_images=need_images)
        if not need_images:
            # Hard-disable cover blocks in final renders even if source data
            # unexpectedly carries image fields.
            for it in items:
                it["cover_image"] = ""
        
        if not items:
            update_task(task_id, error="No articles found in document")
            return
        
        # Detect school and brand color
        auto_color, detected_school = pick_brand_from_title(doc_title)
        
        # Use manually selected school if specified, otherwise use auto-detected
        if school and school in SCHOOLS:
            school_code = school
            school_name = SCHOOLS[school]["name"]  # Use full name for folder_for_school
            brand_color = SCHOOLS[school]["color"]
        else:
            school_name = detected_school
            brand_color = auto_color
            # Reverse lookup: find school_code from detected school name
            # Aliases handle variations like "University of California, Davis" vs "UC Davis"
            SCHOOL_ALIASES = {
                "NYU": ["nyu", "new york university"],
                "USC": ["usc", "southern california"],
                "EMORY": ["emory"],
                "UCD": ["ucd", "uc davis", "davis", "california, davis"],
                "UBC": ["ubc", "british columbia"],
                "EDINBURGH": ["edinburgh"],
            }
            school_code = None
            if detected_school:
                detected_lower = detected_school.lower()
                for code, aliases in SCHOOL_ALIASES.items():
                    if any(alias in detected_lower for alias in aliases):
                        school_code = code
                        break
        
        update_task(task_id, progress=50, message=f"Generating {len(items)} images...")
        
        # Determine output directory
        out_dir = "wechat_images"
        school_dir = folder_for_school(school_name) if school_name else ""
        
        # Generate images
        generated_files = render_to_images(
            items,
            doc_title=doc_title,
            out_dir=out_dir,
            page_width=540,
            device_scale=4,  # Reduced from 4 for faster rendering
            brand_color=brand_color,
            title_size=22.5,
            body_size=22.5,
            top_n=10,
            skip_image_fetch=not need_images,
            school_name=school_name,
        )
        
        # Determine final output directory for response
        output_path = Path(out_dir) / school_dir if school_dir else Path(out_dir)
        
        # Collect source URLs from all parsed items
        source_urls = []
        seen_urls = set()
        for item in items:
            # Get all source URLs from each item
            item_urls = item.get('source_urls') or []
            if not item_urls and item.get('source_url'):
                item_urls = [item.get('source_url')]
            for url in item_urls:
                url = (url or '').strip()
                if url and url not in seen_urls:
                    seen_urls.add(url)
                    source_urls.append(url)
        
        result = {
            "title": doc_title,
            "output_dir": str(output_path),
            "files": generated_files,
            "article_count": len(items),
            "school": school_name,
            "school_code": school_code,  # School code for API calls (works for both manual and auto-detected)
            "source_urls": source_urls,  # Include source URLs for Sources Reference Image
        }
        
        update_task(task_id, status="completed", progress=100,
                   message=f"Generated {len(items)} images", result=result)
        
    except Exception as e:
        logger.error(f"Error in worker_gdoc_to_images: {e}\n{traceback.format_exc()}")
        update_task(task_id, error=str(e))

# ============================================================================
# ROUTES
# ============================================================================

# --- Authentication Routes ---

@app.route('/login', methods=['GET', 'POST'])
def login():
    """Login page and handler."""
    # If already logged in, redirect to home
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    
    error = None
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        
        user = get_user_by_username(username)
        
        if user and user.check_password(password):
            login_user(user, remember=True)
            log_user_activity(user.id, 'login', {
                'username': username,
                'user_agent': request.headers.get('User-Agent', '')[:100]
            })
            
            # Redirect to requested page or home
            next_page = request.args.get('next')
            if next_page and next_page.startswith('/'):
                return redirect(next_page)
            return redirect(url_for('index'))
        else:
            error = 'Invalid username or password'
            logger.warning(f"Failed login attempt for user: {username} from IP: {request.remote_addr}")
    
    return render_template('login.html', error=error)

@app.route('/logout')
@login_required
def logout():
    """Logout the current user."""
    if current_user.is_authenticated:
        log_user_activity(current_user.id, 'logout', {'username': current_user.username})
    logout_user()
    return redirect(url_for('login'))

# --- Protected Routes ---

@app.route('/')
@login_required
def index():
    """Render the main page."""
    log_user_activity(current_user.id, 'page_view', {'page': 'index'})
    return render_template('index.html', schools=SCHOOLS, user=current_user)

@app.route('/guide')
@login_required
def guide():
    """Render the interactive operation guide."""
    log_user_activity(current_user.id, 'page_view', {'page': 'guide'})
    return render_template('guide.html', user=current_user)

@app.route('/health')
def health():
    """Health check endpoint (public for Railway monitoring)."""
    return jsonify({"status": "ok", "timestamp": datetime.now().isoformat()})

# --- Task Management ---

@app.route('/api/tasks', methods=['GET'])
@login_required
def api_list_tasks():
    """List all tasks."""
    return jsonify({"tasks": list_tasks()})

@app.route('/api/tasks/<task_id>', methods=['GET'])
@login_required
def api_get_task(task_id):
    """Get task status."""
    task = get_task(task_id)
    if not task:
        return jsonify({"error": "Task not found"}), 404
    return jsonify(task)

@app.route('/api/tasks/<task_id>', methods=['DELETE'])
@login_required
def api_delete_task(task_id):
    """Delete/cancel a task."""
    log_user_activity(current_user.id, 'task_delete', {'task_id': task_id})
    with task_lock:
        if task_id in tasks:
            del tasks[task_id]
        if task_id in sse_queues:
            del sse_queues[task_id]
        if redis_enabled():
            redis_delete_task(task_id)
    return jsonify({"success": True})

@app.route('/api/tasks/<task_id>/stream')
@login_required
def api_task_stream(task_id):
    """SSE stream for task updates."""
    def generate():
        queue = sse_queues.get(task_id)
        if not queue:
            if not redis_enabled():
                yield f"data: {json.dumps({'error': 'Task not found'})}\n\n"
                return
            task = get_task(task_id)
            if not task:
                yield f"data: {json.dumps({'error': 'Task not found'})}\n\n"
                return
            last_payload = None
            while True:
                task = get_task(task_id)
                if not task:
                    yield f"data: {json.dumps({'error': 'Task not found'})}\n\n"
                    return
                payload = json.dumps(task)
                if payload != last_payload:
                    yield f"data: {payload}\n\n"
                    last_payload = payload
                if task.get("status") in ["completed", "error"]:
                    break
                time.sleep(2)
            return
        
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
@login_required
def api_url_to_doc():
    """Start URL to Chinese news task."""
    data = request.json
    url = data.get('url', '').strip()
    title = data.get('title', '').strip() or None
    
    if not url:
        return jsonify({"error": "URL is required"}), 400
    
    task_id = create_task("url_to_doc", {"url": url, "title": title, "user": current_user.username})
    log_user_activity(current_user.id, 'task_create', {'type': 'url_to_doc', 'task_id': task_id, 'url': url})
    
    thread = threading.Thread(target=worker_url_to_doc, args=(task_id, url, title))
    thread.daemon = True
    thread.start()
    
    return jsonify({"task_id": task_id, "message": "Task started"})

# --- Text to Image ---

@app.route('/api/text-to-image', methods=['POST'])
@login_required
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
        "school": school, "title": title, "content": content[:100] + "...", "user": current_user.username
    })
    log_user_activity(current_user.id, 'task_create', {'type': 'text_to_image', 'task_id': task_id, 'school': school})
    
    thread = threading.Thread(
        target=worker_text_to_image, 
        args=(task_id, school, title, content, source_url, cover_image)
    )
    thread.daemon = True
    thread.start()
    
    return jsonify({"task_id": task_id, "message": "Task started"})

# --- Sources Image ---

@app.route('/api/sources-image', methods=['POST'])
@login_required
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
    
    task_id = create_task("sources_image", {"school": school, "url_count": len(urls), "user": current_user.username})
    log_user_activity(current_user.id, 'task_create', {'type': 'sources_image', 'task_id': task_id, 'school': school, 'url_count': len(urls)})
    
    thread = threading.Thread(target=worker_sources_image, args=(task_id, school, urls))
    thread.daemon = True
    thread.start()
    
    return jsonify({"task_id": task_id, "message": "Task started"})

# --- Full Pipeline (News Collection) ---

@app.route('/api/full-pipeline', methods=['POST'])
@login_required
def api_full_pipeline():
    """Run the full news collection pipeline for a school."""
    data = request.json
    school = data.get('school', '').lower()
    
    # Import school_config to validate
    from news_bot.core import school_config
    
    if school not in school_config.SCHOOL_PROFILES:
        valid_schools = list(school_config.SCHOOL_PROFILES.keys())
        return jsonify({"error": f"Invalid school. Choose from: {valid_schools}"}), 400
    
    school_name = school_config.SCHOOL_PROFILES[school].get("school_name", school)
    
    task_id = create_task("full_pipeline", {
        "school": school,
        "school_name": school_name,
        "user": current_user.username
    })
    log_user_activity(current_user.id, 'task_create', {
        'type': 'full_pipeline',
        'task_id': task_id,
        'school': school
    })
    
    thread = threading.Thread(target=worker_full_pipeline, args=(task_id, school))
    thread.daemon = True
    thread.start()
    
    return jsonify({"task_id": task_id, "message": f"Pipeline started for {school_name}"})


# --- Google Doc to Images ---

@app.route('/api/gdoc-to-images', methods=['POST'])
@login_required
def api_gdoc_to_images():
    """Generate WeChat images from Google Doc."""
    data = request.json
    doc_url = data.get('doc_url', '').strip()
    school = data.get('school', '').strip().upper() or None
    need_images = data.get('need_images', True)
    if isinstance(need_images, str):
        need_images = need_images.strip().lower() not in ('false', '0', 'no', 'off')
    else:
        need_images = bool(need_images)
    
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
    
    task_id = create_task("gdoc_to_images", {
        "doc_id": doc_id[:20] + "...",
        "school": school,
        "need_images": need_images,
        "user": current_user.username
    })
    log_user_activity(current_user.id, 'task_create', {
        'type': 'gdoc_to_images',
        'task_id': task_id,
        'school': school,
        'need_images': need_images
    })
    
    thread = threading.Thread(target=worker_gdoc_to_images, args=(task_id, doc_id, school, need_images))
    thread.daemon = True
    thread.start()
    
    return jsonify({"task_id": task_id, "message": "Task started"})

# --- File Downloads ---

@app.route('/api/download/<path:filepath>')
@login_required
def api_download_file(filepath):
    """Download a generated file."""
    from urllib.parse import unquote
    # URL decode the filepath (handles Chinese characters)
    decoded_path = unquote(filepath)
    # Security: only allow files from wechat_images directory
    safe_path = Path('wechat_images') / decoded_path.replace('..', '')
    if not safe_path.exists():
        return jsonify({"error": f"File not found: {safe_path}"}), 404
    log_user_activity(current_user.id, 'file_download', {'file': str(safe_path)})
    return send_file(safe_path, as_attachment=True)

@app.route('/api/preview/<path:filepath>')
@login_required
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
@login_required
def api_download_folder(folderpath):
    """Download folder as ZIP."""
    safe_path = Path('wechat_images') / folderpath.replace('..', '')
    if not safe_path.exists() or not safe_path.is_dir():
        return jsonify({"error": "Folder not found"}), 404
    
    log_user_activity(current_user.id, 'folder_download', {'folder': str(safe_path)})
    
    # Create ZIP
    temp_zip = tempfile.NamedTemporaryFile(delete=False, suffix='.zip')
    with zipfile.ZipFile(temp_zip.name, 'w', zipfile.ZIP_DEFLATED) as zf:
        for f in safe_path.glob('*.png'):
            zf.write(f, f.name)
    
    return send_file(temp_zip.name, as_attachment=True, 
                    download_name=f'{safe_path.name}.zip')

@app.route('/api/download-files', methods=['POST'])
@login_required
def api_download_files():
    """Download specific files as ZIP (for current session images only)."""
    data = request.json
    files = data.get('files', [])
    name = data.get('name', 'images')
    
    if not files:
        return jsonify({"error": "No files specified"}), 400
    
    log_user_activity(current_user.id, 'batch_download', {'file_count': len(files), 'name': name})
    
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
@login_required
def api_schools():
    """Get available schools."""
    return jsonify({"schools": SCHOOLS})

# --- User Info & Activity Logs (Admin only) ---

@app.route('/api/me', methods=['GET'])
@login_required
def api_current_user():
    """Get current user info."""
    return jsonify({"user": current_user.to_dict()})

@app.route('/api/activity-logs', methods=['GET'])
@login_required
def api_activity_logs():
    """Get activity logs (admin only, or own logs for regular users)."""
    user_id = request.args.get('user_id')
    
    # Non-admins can only see their own logs
    if current_user.role != 'admin':
        user_id = current_user.id
    
    with activity_log_lock:
        if user_id:
            logs = user_activity_log.get(user_id, [])[-100:]  # Last 100 entries
        else:
            # Admin can see all logs
            all_logs = []
            for uid, entries in user_activity_log.items():
                for entry in entries[-50:]:  # Last 50 per user
                    entry_copy = entry.copy()
                    entry_copy['user_id'] = uid
                    all_logs.append(entry_copy)
            logs = sorted(all_logs, key=lambda x: x['timestamp'], reverse=True)[:200]
    
    return jsonify({"logs": logs})

@app.route('/api/users', methods=['GET'])
@login_required
def api_list_users():
    """List all users (admin only)."""
    if current_user.role != 'admin':
        return jsonify({"error": "Admin access required"}), 403
    
    users_list = [u.to_dict() for u in USERS.values()]
    return jsonify({"users": users_list})

# ============================================================================
# MAIN
# ============================================================================

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 3050))
    debug = os.environ.get('FLASK_ENV', 'development') != 'production'
    
    print("=" * 60)
    print("NEXUS News Production Studio")
    print("=" * 60)
    print(f"Server: http://0.0.0.0:{port}")
    print("=" * 60)
    
    app.run(host='0.0.0.0', port=port, debug=debug, threaded=True)
