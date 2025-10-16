
from flask import Flask, request, jsonify, send_file, render_template_string, Response
from flask_cors import CORS
import base64
import os
import time
import json
from datetime import datetime
from google import genai
from google.genai import types
from moviepy.editor import VideoFileClip, concatenate_videoclips
import threading
import uuid
from uuid import uuid4
from dotenv import load_dotenv
import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor
import io
from urllib.parse import urlparse
from decimal import Decimal

load_dotenv()

app = Flask(__name__)
CORS(app)

# Configuration
UPLOAD_FOLDER = os.getenv('UPLOAD_FOLDER', 'uploads')
VIDEO_FOLDER = os.getenv('VIDEO_FOLDER', 'generated')
METADATA_FOLDER = os.getenv('METADATA_FOLDER', 'metadata')
LOG_FILE = os.getenv('LOG_FILE', 'generation_log.txt')
GEMINI_API_KEY = "AIzaSyAWg2HFQ1td6Y6LUU816-KJbi5S6CL9iCk"

# PostgreSQL Configuration
DATABASE_URL = os.getenv('DATABASE_URL', "postgresql://realyai:Realyai@2025@34.135.55.17:5432/realyai")
print(DATABASE_URL)
# Parse database URL for configuration
def get_db_config():
    if DATABASE_URL:
        parsed = urlparse(DATABASE_URL)
        return {
            'dbname': parsed.path.lstrip('/').split('?')[0] or 'realyai',
            'user': parsed.username or 'realyai',
            'password': parsed.password or 'Realyai@2025',
            'host': parsed.hostname or '34.135.55.17',
            'port': parsed.port or 5432
        }
    else:
        return {
            'dbname': os.getenv('DB_NAME', 'realyai'),
            'user': os.getenv('DB_USER', 'postgres'),
            'password': os.getenv('DB_PASSWORD', 'root123'),
            'host': os.getenv('DB_HOST', '172.22.66.252'),
            'port': os.getenv('DB_PORT', '5432')
        }

DB_CONFIG = get_db_config()

# Create folders
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(VIDEO_FOLDER, exist_ok=True)
os.makedirs(METADATA_FOLDER, exist_ok=True)

# Initialize connection pool
try:
    postgreSQL_pool = psycopg2.pool.ThreadedConnectionPool(
        minconn=1,
        maxconn=10,
        **DB_CONFIG
    )
    log_to_file = lambda msg: None  # Temporary
    log_to_file("✅ PostgreSQL connection pool created successfully")
except Exception as e:
    print(f"❌ Error creating connection pool: {str(e)}")
    postgreSQL_pool = None

# Log function
def log_to_file(message):
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with open(LOG_FILE, 'a', encoding='utf-8') as f:
        f.write(f"[{timestamp}] {message}\n")
    print(f"[{timestamp}] {message}")

# Get database connection
def get_db_connection():
    if postgreSQL_pool:
        return postgreSQL_pool.getconn()
    return None

# Release database connection
def release_db_connection(conn):
    if postgreSQL_pool and conn:
        postgreSQL_pool.putconn(conn)

# Save video to PostgreSQL as base64 (once generated)
def save_video_to_db(user_id, prompt, video_path, metadata):
    try:
        conn = get_db_connection()
        if not conn:
            log_to_file("❌ Cannot get database connection")
            return False

        cursor = conn.cursor()

        # Read video file as binary and encode to base64
        with open(video_path, 'rb') as video_file:
            video_data = video_file.read()
            video_data_b64 = base64.b64encode(video_data).decode('utf-8')

        video_size = len(video_data)

        # Update video record with generated video
        cursor.execute("""
            UPDATE video_reels SET
                "videoData" = %s,
                "videoFormat" = %s,
                "videoSize" = %s,
                "videoDuration" = %s,
                "estimatedCost" = %s,
                status = %s,
                "generationTime" = %s,
                updated_at = %s
            WHERE user_id = %s
            AND prompt = %s
            AND status = 'PROCESSING'
            AND id = (
                SELECT id FROM video_reels
                WHERE user_id = %s AND prompt = %s AND status = 'PROCESSING'
                ORDER BY created_at DESC LIMIT 1
            )
        """, (
            video_data_b64,
            metadata.get('video_format', 'mp4'),
            video_size,
            Decimal(str(metadata.get('video_duration', 0))),
            Decimal(str(metadata.get('estimated_cost', 0))),
            'COMPLETED',
            datetime.now(),
            datetime.now(),
            user_id,
            prompt,
            user_id,
            prompt
        ))

        if cursor.rowcount == 0:
            log_to_file("⚠️  No matching video record found to update")

        conn.commit()
        cursor.close()
        release_db_connection(conn)

        log_to_file(f"✅ Video saved to database for user: {user_id}")

        # Delete local file after saving to DB
        if os.path.exists(video_path):
            os.remove(video_path)
            log_to_file(f"🗑️  Removed local video file: {video_path}")

        return True

    except Exception as e:
        log_to_file(f"❌ Error saving video to database: {str(e)}")
        if conn:
            conn.rollback()
            release_db_connection(conn)
        return False

# Update video status in database
def update_video_status_db(user_id, prompt, status, error_message=None):
    try:
        conn = get_db_connection()
        if not conn:
            return False

        cursor = conn.cursor()

        if status == 'FAILED':
            cursor.execute("""
                UPDATE video_reels SET
                    status = %s,
                    error_message = %s,
                    updated_at = %s
                WHERE user_id = %s 
                AND prompt = %s
                AND status = 'PROCESSING'
                AND id = (
                    SELECT id FROM video_reels
                    WHERE user_id = %s AND prompt = %s AND status = 'PROCESSING'
                    ORDER BY created_at DESC LIMIT 1
                )
            """, (status, error_message, datetime.now(), user_id, prompt, user_id, prompt))
        else:
            cursor.execute("""
                UPDATE video_reels SET
                    status = %s,
                    updated_at = %s
                WHERE user_id = %s
                AND prompt = %s
                AND status = 'PROCESSING'
                AND id = (
                    SELECT id FROM video_reels
                    WHERE user_id = %s AND prompt = %s AND status = 'PROCESSING'
                    ORDER BY created_at DESC LIMIT 1
                )
            """, (status, datetime.now(), user_id, prompt, user_id, prompt))

        conn.commit()
        cursor.close()
        release_db_connection(conn)

        return True

    except Exception as e:
        log_to_file(f"❌ Error updating video status: {str(e)}")
        if conn:
            conn.rollback()
            release_db_connection(conn)
        return False

# Get video from database by id
def get_video_from_db(video_id):
    try:
        conn = get_db_connection()
        if not conn:
            return None

        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("""
            SELECT id, user_id, prompt, status, "videoFormat", "videoDuration",
                   "estimatedCost", created_at, updated_at, "errorMessage", "videoData"
            FROM video_reels WHERE id = %s
        """, (video_id,))
        video = cursor.fetchone()

        cursor.close()
        release_db_connection(conn)

        return video

    except Exception as e:
        log_to_file(f"❌ Error retrieving video from database: {str(e)}")
        if conn:
            release_db_connection(conn)
        return None

# Get all videos for user
def get_user_videos_db(user_id):
    try:
        conn = get_db_connection()
        if not conn:
            return []

        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("""
            SELECT id, user_id, prompt, status, "videoFormat", "videoDuration",
                   "estimatedCost", created_at, updated_at, "errorMessage"
            FROM video_reels
            WHERE user_id = %s
            ORDER BY created_at DESC
        """, (user_id,))

        videos = cursor.fetchall()

        cursor.close()
        release_db_connection(conn)

        # Convert Decimal to float for JSON serialization
        for video in videos:
            if video.get('video_duration'):
                video['video_duration'] = float(video['video_duration'])
            if video.get('estimated_cost'):
                video['estimated_cost'] = float(video['estimated_cost'])

        return videos

    except Exception as e:
        log_to_file(f"❌ Error getting user videos: {str(e)}")
        if conn:
            release_db_connection(conn)
        return []

# Save base64 image to file
def save_base64_image(base64_string, filename):
    try:
        if ',' in base64_string:
            base64_string = base64_string.split(',')[1]
        
        image_data = base64.b64decode(base64_string)
        image_path = os.path.join(UPLOAD_FOLDER, filename)
        
        with open(image_path, 'wb') as f:
            f.write(image_data)
        
        return image_path
    except Exception as e:
        log_to_file(f"❌ Error saving base64 image: {str(e)}")
        return None

# Generate video using Veo 3 Fast
def generate_video_veo3(api_key, images, prompt, aspect_ratio="9:16", user_id=""):
    try:
        client = genai.Client(api_key=api_key)
        clip_files = []
        
        log_to_file(f"🎬 Starting Veo 3 generation for user: {user_id}")
        
        # Generate clips for each image
        for idx, img_path in enumerate(images):
            log_to_file(f"🎥 Processing scene {idx+1}/{len(images)}...")
            
            with open(img_path, 'rb') as f:
                img_bytes = f.read()
            
            mime_type = "image/jpeg"
            if img_path.endswith('.png'):
                mime_type = "image/png"
            
            img_obj = types.Image(
                image_bytes=img_bytes,
                mime_type=mime_type
            )
            
            config = types.GenerateVideosConfig(aspect_ratio=aspect_ratio)
            
            log_to_file(f"⚙️  Generating scene {idx+1} with Veo 3 Fast...")
            operation = client.models.generate_videos(
                model="veo-3.0-generate-001",
                prompt=prompt,
                image=img_obj,
                config=config
            )
            
            # Poll for completion
            poll_count = 0
            while not operation.done and poll_count < 60:
                poll_count += 1
                log_to_file(f"⏳ Scene {idx+1} polling... {poll_count*10}s")
                time.sleep(10)
                operation = client.operations.get(operation)
            
            if not operation.done:
                log_to_file(f"⏱️  Scene {idx+1} timeout")
                continue
            
            # Download video
            video = operation.response.generated_videos[0]
            client.files.download(file=video.video)
            
            scene_filename = f"scene_{user_id}_{idx+1}_{int(time.time())}.mp4"
            scene_path = os.path.join(VIDEO_FOLDER, scene_filename)
            video.video.save(scene_path)
            clip_files.append(scene_path)
            
            log_to_file(f"✅ Scene {idx+1} completed: {scene_filename}")
        
        # Concatenate clips
        if len(clip_files) == len(images):
            log_to_file("🔗 Concatenating clips into final video...")
            
            clips = []
            for cf in clip_files:
                clip = VideoFileClip(cf)
                trimmed = clip.subclip(0, min(clip.duration, 5.0))
                clips.append(trimmed)
            
            final = concatenate_videoclips(clips, method="compose")
            
            final_filename = f"final_video_{user_id}_{int(time.time())}.mp4"
            final_path = os.path.join(VIDEO_FOLDER, final_filename)
            
            final.write_videofile(
                final_path,
                codec="libx264",
                audio_codec="aac",
                temp_audiofile="temp-audio.m4a",
                remove_temp=True,
                logger=None
            )
            
            duration = final.duration
            
            # Cleanup
            for clip in clips:
                clip.close()
            final.close()
            
            # Remove scene files
            for cf in clip_files:
                if os.path.exists(cf):
                    os.remove(cf)
            
            log_to_file(f"🎉 Final video created: {final_filename} ({duration}s)")
            
            return {
                'success': True,
                'filename': final_filename,
                'path': final_path,
                'duration': duration
            }
        else:
            return {
                'success': False,
                'error': 'Not all clips generated'
            }
            
    except Exception as e:
        log_to_file(f"❌ Error in video generation: {str(e)}")
        return {
            'success': False,
            'error': str(e)
        }

# Background task for video generation
def background_video_generation(api_key, image_files, prompt, aspect_ratio, user_id):
    try:
        log_to_file(f"🚀 Background task started for user: {user_id}")
        
        # Update status to PROCESSING
        update_video_status_db(user_id, prompt, 'PROCESSING')
        
        # Generate video
        result = generate_video_veo3(api_key, image_files, prompt, aspect_ratio, user_id)
        
        if result['success']:
            # Calculate cost
            cost_estimate = result['duration'] * 0.15
            
            metadata = {
                'video_format': 'mp4',
                'video_duration': result['duration'],
                'estimated_cost': cost_estimate
            }
            
            # Save video to PostgreSQL database
            save_video_to_db(user_id, prompt, result['path'], metadata)
            
            log_to_file(f"✅ Background task completed successfully for user: {user_id}")
        else:
            # Update with failure
            update_video_status_db(user_id, prompt, 'FAILED', result['error'])
            log_to_file(f"❌ Background task failed for user: {user_id}")
            
    except Exception as e:
        error_msg = f"Background task exception: {str(e)}"
        log_to_file(error_msg)
        update_video_status_db(user_id, prompt, 'FAILED', error_msg)
    finally:
        # Cleanup uploaded image files
        for img_file in image_files:
            try:
                if os.path.exists(img_file):
                    os.remove(img_file)
                    log_to_file(f"🗑️  Removed temp file: {img_file}")
            except Exception as e:
                log_to_file(f"⚠️  Error removing temp file {img_file}: {str(e)}")

# HTML template for video gallery
VIDEO_GALLERY_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Video Gallery</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            padding: 20px;
        }
        .container { max-width: 1200px; margin: 0 auto; }
        h1 {
            color: white;
            text-align: center;
            margin-bottom: 30px;
            font-size: 2.5em;
            text-shadow: 2px 2px 4px rgba(0,0,0,0.3);
        }
        .user-input {
            background: white;
            padding: 20px;
            border-radius: 10px;
            margin-bottom: 30px;
            box-shadow: 0 10px 30px rgba(0,0,0,0.2);
        }
        .input-group { display: flex; gap: 10px; align-items: center; }
        input[type="text"] {
            flex: 1;
            padding: 12px;
            border: 2px solid #ddd;
            border-radius: 5px;
            font-size: 16px;
        }
        button {
            padding: 12px 24px;
            background: #667eea;
            color: white;
            border: none;
            border-radius: 5px;
            cursor: pointer;
            font-size: 16px;
            transition: background 0.3s;
        }
        button:hover { background: #5568d3; }
        .video-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(350px, 1fr));
            gap: 25px;
        }
        .video-card {
            background: white;
            border-radius: 10px;
            overflow: hidden;
            box-shadow: 0 10px 30px rgba(0,0,0,0.2);
            transition: transform 0.3s, box-shadow 0.3s;
        }
        .video-card:hover {
            transform: translateY(-5px);
            box-shadow: 0 15px 40px rgba(0,0,0,0.3);
        }
        .video-wrapper {
            position: relative;
            width: 100%;
            padding-top: 177.78%;
            background: #000;
        }
        video {
            position: absolute;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            object-fit: contain;
        }
        .video-info { padding: 15px; }
        .video-title {
            font-size: 16px;
            font-weight: bold;
            margin-bottom: 8px;
            color: #333;
        }
        .video-meta {
            font-size: 13px;
            color: #666;
            margin-bottom: 5px;
        }
        .status {
            display: inline-block;
            padding: 4px 12px;
            border-radius: 20px;
            font-size: 12px;
            font-weight: bold;
            margin-top: 8px;
        }
        .status.completed { background: #d4edda; color: #155724; }
        .status.processing { background: #fff3cd; color: #856404; }
        .status.failed { background: #f8d7da; color: #721c24; }
        .no-videos {
            text-align: center;
            color: white;
            font-size: 1.2em;
            padding: 40px;
            background: rgba(255,255,255,0.1);
            border-radius: 10px;
        }
        .loading {
            text-align: center;
            padding: 40px;
            color: white;
            font-size: 1.2em;
        }
        .error {
            background: #f8d7da;
            color: #721c24;
            padding: 15px;
            border-radius: 5px;
            margin-bottom: 20px;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>🎬 Video Gallery</h1>
        <div class="user-input">
            <div class="input-group">
                <input type="text" id="userId" placeholder="Enter User ID" value="user123">
                <button onclick="loadVideos()">Load Videos</button>
                <button onclick="refreshVideos()">🔄 Refresh</button>
            </div>
        </div>
        <div id="error" class="error" style="display:none;"></div>
        <div id="loading" class="loading" style="display:none;">Loading videos...</div>
        <div id="videoGrid" class="video-grid"></div>
    </div>
    <script>
        let currentUserId = 'user123';
        function showError(message) {
            const errorDiv = document.getElementById('error');
            errorDiv.textContent = message;
            errorDiv.style.display = 'block';
            setTimeout(() => { errorDiv.style.display = 'none'; }, 5000);
        }
        function formatDate(dateString) {
            if (!dateString) return 'N/A';
            const date = new Date(dateString);
            return date.toLocaleString();
        }
        function formatDuration(seconds) {
            if (!seconds) return 'N/A';
            return `${parseFloat(seconds).toFixed(1)}s`;
        }
        function getStatusClass(status) {
            if (!status) return '';
            return status.toLowerCase();
        }
        async function loadVideos() {
            const userId = document.getElementById('userId').value.trim();
            if (!userId) {
                showError('Please enter a User ID');
                return;
            }
            currentUserId = userId;
            const loading = document.getElementById('loading');
            const videoGrid = document.getElementById('videoGrid');
            loading.style.display = 'block';
            videoGrid.innerHTML = '';
            try {
                const response = await fetch(`/videos/${userId}`);
                const data = await response.json();
                loading.style.display = 'none';
                if (data.videos && data.videos.length > 0) {
                    videoGrid.innerHTML = data.videos.map(video => `
                        <div class="video-card">
                            ${video.status === 'COMPLETED' ? `
                               <div class="video-wrapper">
                                   <video controls preload="metadata">
                                       <source src="/video/${video.id}" type="video/${video.videoFormat || 'mp4'}">
                                       Your browser does not support the video tag.
                                   </video>
                               </div>
                           ` : `
                               <div class="video-wrapper" style="display:flex; align-items:center; justify-content:center; background:#f0f0f0;">
                                   <span style="font-size:48px;">${video.status === 'PROCESSING' ? '⏳' : '❌'}</span>
                               </div>
                           `}
                           <div class="video-info">
                               <div class="video-title">Video #${video.id}</div>
                               <div class="video-meta">📝 ${video.prompt || 'No prompt'}</div>
                               <div class="video-meta">⏱️ Duration: ${formatDuration(video.videoDuration)}</div>
                               <div class="video-meta">💰 Cost: $${(video.estimatedCost || 0).toFixed(4)}</div>
                               <div class="video-meta">📅 ${formatDate(video.created_at)}</div>
                               ${video.errorMessage ? `<div class="video-meta" style="color: #721c24;">❌ ${video.errorMessage}</div>` : ''}
                               <span class="status ${getStatusClass(video.status)}">${video.status || 'UNKNOWN'}</span>
                           </div>
                        </div>
                    `).join('');
                } else {
                    videoGrid.innerHTML = '<div class="no-videos">No videos found for this user</div>';
                }
            } catch (error) {
                loading.style.display = 'none';
                showError('Error loading videos: ' + error.message);
                console.error('Error:', error);
            }
        }
        function refreshVideos() { loadVideos(); }
        window.addEventListener('load', () => { loadVideos(); });
        setInterval(() => {
            const videoGrid = document.getElementById('videoGrid');
            if (videoGrid.innerHTML.includes('PROCESSING')) { loadVideos(); }
        }, 30000);
    </script>
</body>
</html>
"""

# API Endpoints
@app.route('/')
def index():
    return render_template_string(VIDEO_GALLERY_HTML)

@app.route('/health', methods=['GET'])
def health_check():
    db_status = 'connected' if postgreSQL_pool else 'disconnected'
    return jsonify({
        'status': 'healthy',
        'timestamp': datetime.now().isoformat(),
        'storage': 'postgresql',
        'database': db_status
    })

@app.route('/generate-video', methods=['POST'])
def generate_video():
    try:
        data = request.json

        # Save input JSON to file for debugging/logging
        if data:
            timestamp = int(time.time())
            debug_filename = f"request_{timestamp}.json"
            debug_path = os.path.join(METADATA_FOLDER, debug_filename)

            try:
                with open(debug_path, 'w', encoding='utf-8') as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
                log_to_file(f"📝 Request data saved to: {debug_filename}")
            except Exception as e:
                log_to_file(f"⚠️  Failed to save request data: {str(e)}")

        if not data:
            return jsonify({'error': 'No data provided'}), 400
        
        api_key = GEMINI_API_KEY or data.get('api_key')
        user_id = data.get('user_id', 'anonymous')
        prompt = data.get('prompt', 'Professional branding video with smooth transitions')
        aspect_ratio = data.get('aspect_ratio', '9:16')
        images_base64 = data.get('images', [])
        
        if not api_key:
            return jsonify({'error': 'API key not configured'}), 400
        
        if len(images_base64) != 3:
            return jsonify({'error': 'Exactly 3 images required'}), 400
        
        log_to_file(f"📨 Request from user: {user_id}")

        # Save base64 images to disk
        image_files = []

        for idx, img_b64 in enumerate(images_base64):
            timestamp = int(time.time())
            filename = f"{user_id}_img_{idx+1}_{timestamp}.jpg"
            img_path = save_base64_image(img_b64, filename)
            if img_path:
                image_files.append(img_path)

        if len(image_files) != 3:
            return jsonify({'error': 'Failed to process images'}), 400

        # Create initial database record - only essential columns to avoid schema issues
        try:
            conn = get_db_connection()
            cursor = conn.cursor()

            # Generate UUID for id
            video_id = str(uuid4())

            cursor.execute("""
                INSERT INTO video_reels (
                    id, user_id, prompt, status, updated_at
                ) VALUES (%s, %s, %s, %s, %s)
            """, (
                video_id,
                user_id,  # This is firebase_uid which matches users.firebase_uid
                prompt,
                'PROCESSING',
                datetime.now()
            ))

            conn.commit()
            cursor.close()
            release_db_connection(conn)

        except Exception as e:
            log_to_file(f"❌ Error creating initial record: {str(e)}")
            return jsonify({'error': 'Database error'}), 500
        
        # Start background thread for video generation
        thread = threading.Thread(
            target=background_video_generation,
            args=(api_key, image_files, prompt, aspect_ratio, user_id)
        )
        thread.daemon = True
        thread.start()
        
        log_to_file(f"🎬 Background thread started for user: {user_id}")
        
        return jsonify({
            'success': True,
            'video_id': video_id,
            'status': 'PROCESSING',
            'message': 'Video generation started. Use video_id to check status',
            'estimated_time': '3-5 minutes'
        }), 202
        
    except Exception as e:
        log_to_file(f"❌ API Error: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/video-status/<int:video_id>', methods=['GET'])
def get_video_status(video_id):
    try:
        video = get_video_from_db(video_id)
        
        if video:
            response = dict(video)
            # Remove binary data from response
            response.pop('video_data', None)
            response.pop('image1', None)
            response.pop('image2', None)
            response.pop('image3', None)
            
            # Convert Decimal to float
            if response.get('video_duration'):
                response['video_duration'] = float(response['video_duration'])
            if response.get('estimated_cost'):
                response['estimated_cost'] = float(response['estimated_cost'])
            
            return jsonify(response)
        else:
            return jsonify({'error': 'Video reel not found'}), 404
            
    except Exception as e:
        log_to_file(f"❌ Error getting video status: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/videos/<user_id>', methods=['GET'])
def list_user_videos(user_id):
    try:
        videos = get_user_videos_db(user_id)
        
        return jsonify({
            'user_id': user_id,
            'count': len(videos),
            'videos': [dict(v) for v in videos]
        })
        
    except Exception as e:
        log_to_file(f"❌ Error listing videos: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/video/<int:video_id>', methods=['GET'])
def download_video(video_id):
    try:
        video = get_video_from_db(video_id)
        
        if not video:
            return jsonify({'error': 'Video reel not found'}), 404
        
        status = video.get('status')
        
        if status == 'PROCESSING':
            return jsonify({
                'error': 'Video is still processing',
                'status': 'PROCESSING'
            }), 202
        
        if status == 'FAILED':
            return jsonify({
                'error': 'Video generation failed',
                'message': video.get('error_message')
            }), 500
        
        if video.get('videoData'):
            video_data_b64 = video['videoData']
            video_bytes = base64.b64decode(video_data_b64)
            video_filename = f"video_{video_id}.mp4"

            return Response(
                video_bytes,
                mimetype='video/mp4',
                headers={
                    'Content-Disposition': f'inline; filename="{video_filename}"',
                    'Accept-Ranges': 'bytes'
                }
            )
        else:
            return jsonify({'error': 'Video data not found'}), 404
            
    except Exception as e:
        log_to_file(f"❌ Error downloading video: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/delete-video/<int:video_id>', methods=['DELETE'])
def delete_video(video_id):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute("DELETE FROM video_reels WHERE id = %s", (video_id,))

        if cursor.rowcount > 0:
            conn.commit()
            cursor.close()
            release_db_connection(conn)

            log_to_file(f"🗑️  Deleted video: {video_id}")

            return jsonify({
                'success': True,
                'message': 'Video deleted successfully'
            })
        else:
            cursor.close()
            release_db_connection(conn)
            return jsonify({'error': 'Video not found'}), 404

    except Exception as e:
        log_to_file(f"❌ Error deleting video: {str(e)}")
        if conn:
            conn.rollback()
            release_db_connection(conn)
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    log_to_file("=" * 60)
    log_to_file("🚀 SERVER STARTING - POSTGRESQL STORAGE")
    log_to_file("=" * 60)
    app.run(debug=True, host='0.0.0.0', port=8080)
