#!/usr/bin/env python3
import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections import defaultdict, deque
from functools import wraps
from hashlib import sha256
from pathlib import Path
from flask import Flask, render_template, request, jsonify, send_file, Response, g
from werkzeug.utils import secure_filename

# Initialize logger
logger = logging.getLogger(__name__)

ALLOWED_VIDEO_EXTENSIONS = ('.mp4', '.avi', '.mov', '.mkv')


class FixedWindowRateLimiter:
    """Small in-memory limiter for API access tokens."""

    def __init__(self, limit=30, window_seconds=60):
        self.limit = limit
        self.window_seconds = window_seconds
        self._events = defaultdict(deque)
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls):
        raw_limit = os.environ.get("VIDEO_ANALYZER_API_RATE_LIMIT", "30/minute")
        limit, window_seconds = cls._parse_limit(raw_limit)
        return cls(limit=limit, window_seconds=window_seconds)

    @staticmethod
    def _parse_limit(raw_limit):
        raw_limit = raw_limit.strip().lower()
        windows = {
            "second": 1,
            "sec": 1,
            "s": 1,
            "minute": 60,
            "min": 60,
            "m": 60,
            "hour": 3600,
            "hr": 3600,
            "h": 3600,
        }

        if "/" not in raw_limit:
            return max(1, int(raw_limit)), 60

        limit_text, window_text = raw_limit.split("/", 1)
        limit = max(1, int(limit_text.strip()))
        window_seconds = windows.get(window_text.strip())
        if window_seconds is None:
            raise ValueError(
                "VIDEO_ANALYZER_API_RATE_LIMIT must look like '30/minute', '100/hour', or an integer per minute"
            )
        return limit, window_seconds

    def check(self, key):
        now = time.time()
        cutoff = now - self.window_seconds

        with self._lock:
            events = self._events[key]
            while events and events[0] <= cutoff:
                events.popleft()

            if len(events) >= self.limit:
                retry_after = max(1, int(events[0] + self.window_seconds - now))
                return False, retry_after

            events.append(now)
            return True, 0

class VideoAnalyzerUI:
    def __init__(self, host='localhost', port=5000, dev_mode=False):
        self.app = Flask(__name__)
        self.host = host
        self.port = port
        self.dev_mode = dev_mode
        self.sessions = {}
        self.api_jobs = {}
        self.api_jobs_lock = threading.Lock()
        self.api_keys = self._load_api_keys()
        self.api_rate_limiter = FixedWindowRateLimiter.from_env()
        max_upload_mb = int(os.environ.get("VIDEO_ANALYZER_MAX_UPLOAD_MB", "512"))
        self.app.config["MAX_CONTENT_LENGTH"] = max_upload_mb * 1024 * 1024
        
        # Ensure tmp directories exist
        self.tmp_root = Path(tempfile.gettempdir()) / 'video-analyzer-ui'
        self.uploads_dir = self.tmp_root / 'uploads'
        self.results_dir = self.tmp_root / 'results'
        self.api_uploads_dir = self.tmp_root / 'api_uploads'
        self.api_results_dir = self.tmp_root / 'api_results'
        self.uploads_dir.mkdir(parents=True, exist_ok=True)
        self.results_dir.mkdir(parents=True, exist_ok=True)
        self.api_uploads_dir.mkdir(parents=True, exist_ok=True)
        self.api_results_dir.mkdir(parents=True, exist_ok=True)
        
        self.setup_routes()

    def _load_api_keys(self):
        raw_keys = os.environ.get("VIDEO_ANALYZER_API_KEYS") or os.environ.get("VIDEO_ANALYZER_API_KEY", "")
        return {key.strip() for key in raw_keys.split(",") if key.strip()}

    def _token_id(self, token):
        return sha256(token.encode("utf-8")).hexdigest()[:16]

    def _authenticate_api_request(self):
        if not self.api_keys:
            return None, (jsonify({
                'error': 'API authentication is not configured',
                'detail': 'Set VIDEO_ANALYZER_API_KEY or VIDEO_ANALYZER_API_KEYS before exposing /api/v1.'
            }), 503)

        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            response = jsonify({'error': 'Missing bearer token'})
            response.headers["WWW-Authenticate"] = 'Bearer realm="video-analyzer-api"'
            return None, (response, 401)

        token = header[len("Bearer "):].strip()
        if token not in self.api_keys:
            response = jsonify({'error': 'Invalid bearer token'})
            response.headers["WWW-Authenticate"] = 'Bearer realm="video-analyzer-api"'
            return None, (response, 401)

        return self._token_id(token), None

    def _get_request_token_id(self):
        return getattr(g, "api_token_id", None)

    def _api_job_response(self, job):
        return {
            'id': job['id'],
            'status': job['status'],
            'filename': job['filename'],
            'created_at': job['created_at'],
            'updated_at': job['updated_at'],
            'returncode': job.get('returncode'),
            'error': job.get('error'),
            'log_tail': job.get('log_tail', []),
            'links': {
                'self': f"/api/v1/analyses/{job['id']}",
                'result': f"/api/v1/analyses/{job['id']}/result",
            }
        }

    def _get_api_job(self, job_id):
        with self.api_jobs_lock:
            job = self.api_jobs.get(job_id)

        if not job:
            return None, (jsonify({'error': 'Analysis job not found'}), 404)

        if job.get('owner') != self._get_request_token_id():
            return None, (jsonify({'error': 'Analysis job not found'}), 404)

        return job, None

    def _update_api_job(self, job_id, **changes):
        changes['updated_at'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        with self.api_jobs_lock:
            if job_id in self.api_jobs:
                self.api_jobs[job_id].update(changes)

    def _request_value(self, *names):
        for name in names:
            value = request.form.get(name)
            if value not in (None, ""):
                return value
        return None

    def _request_bool(self, *names):
        value = self._request_value(*names)
        if value is None:
            return False
        return value.lower() in ("1", "true", "yes", "on")

    def _build_analysis_command(self, video_path, output_dir):
        cmd = ['video-analyzer', str(video_path), '--output', str(output_dir)]

        string_options = {
            'ollama_url': '--ollama-url',
            'api_key': '--api-key',
            'api_url': '--api-url',
            'model': '--model',
            'whisper_model': '--whisper-model',
            'prompt': '--prompt',
            'language': '--language',
            'device': '--device',
        }
        choice_options = {
            'client': ('--client', {'ollama', 'openai_api'}),
        }
        int_options = {
            'max_frames': '--max-frames',
        }
        float_options = {
            'duration': '--duration',
            'temperature': '--temperature',
        }

        for name, cli_name in string_options.items():
            value = self._request_value(name, name.replace("_", "-"))
            if value is not None:
                cmd.extend([cli_name, value])

        for name, (cli_name, allowed_values) in choice_options.items():
            value = self._request_value(name, name.replace("_", "-"))
            if value is not None:
                if value not in allowed_values:
                    allowed = ", ".join(sorted(allowed_values))
                    raise ValueError(f"{name} must be one of: {allowed}")
                cmd.extend([cli_name, value])

        for name, cli_name in int_options.items():
            value = self._request_value(name, name.replace("_", "-"))
            if value is not None:
                try:
                    number = int(value)
                    if number < 1:
                        raise ValueError
                except ValueError:
                    raise ValueError(f"{name} must be a positive integer")
                cmd.extend([cli_name, str(number)])

        for name, cli_name in float_options.items():
            value = self._request_value(name, name.replace("_", "-"))
            if value is not None:
                try:
                    number = float(value)
                    if number < 0:
                        raise ValueError
                except ValueError:
                    raise ValueError(f"{name} must be a non-negative number")
                cmd.extend([cli_name, str(number)])

        if self._request_bool('keep_frames', 'keep-frames'):
            cmd.append('--keep-frames')

        return cmd

    def _run_api_analysis_job(self, job_id, cmd, result_path):
        self._update_api_job(job_id, status='running', log_tail=[])
        log_tail = deque(maxlen=100)

        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True,
                bufsize=1
            )

            if process.stdout:
                for line in process.stdout:
                    line = line.strip()
                    if line:
                        log_tail.append(line)
                        self._update_api_job(job_id, log_tail=list(log_tail))

            process.wait()
            if process.returncode == 0 and result_path.exists():
                self._update_api_job(
                    job_id,
                    status='completed',
                    returncode=process.returncode,
                    log_tail=list(log_tail)
                )
            else:
                self._update_api_job(
                    job_id,
                    status='failed',
                    returncode=process.returncode,
                    error='Analysis process failed or did not produce analysis.json',
                    log_tail=list(log_tail)
                )
        except Exception as e:
            logger.exception("API analysis job failed")
            self._update_api_job(job_id, status='failed', error=str(e), log_tail=list(log_tail))
        
    def setup_routes(self):
        @self.app.route('/')
        def index():
            return render_template('index.html')

        def require_api_access(handler):
            @wraps(handler)
            def wrapper(*args, **kwargs):
                token_id, auth_error = self._authenticate_api_request()
                if auth_error:
                    return auth_error

                limiter_key = f"{token_id}:{request.remote_addr or 'unknown'}"
                allowed, retry_after = self.api_rate_limiter.check(limiter_key)
                if not allowed:
                    response = jsonify({
                        'error': 'Rate limit exceeded',
                        'retry_after_seconds': retry_after,
                    })
                    response.headers['Retry-After'] = str(retry_after)
                    return response, 429

                g.api_token_id = token_id
                return handler(*args, **kwargs)

            return wrapper

        @self.app.route('/api/v1/health')
        @require_api_access
        def api_health():
            return jsonify({
                'status': 'ok',
                'auth': 'enabled',
                'rate_limit': {
                    'limit': self.api_rate_limiter.limit,
                    'window_seconds': self.api_rate_limiter.window_seconds,
                },
            })

        @self.app.route('/api/v1/analyses', methods=['POST'])
        @require_api_access
        def api_create_analysis():
            if 'video' not in request.files:
                return jsonify({'error': 'No video file provided'}), 400

            file = request.files['video']
            if file.filename == '':
                return jsonify({'error': 'No selected file'}), 400

            if not file.filename.lower().endswith(ALLOWED_VIDEO_EXTENSIONS):
                return jsonify({'error': 'Invalid file type'}), 400

            upload_dir = None
            result_dir = None
            try:
                job_id = str(uuid.uuid4())
                upload_dir = self.api_uploads_dir / job_id
                result_dir = self.api_results_dir / job_id
                upload_dir.mkdir(parents=True)
                result_dir.mkdir(parents=True)

                filename = secure_filename(file.filename) or "video"
                video_path = upload_dir / filename
                file.save(video_path)

                cmd = self._build_analysis_command(video_path, result_dir)
                result_path = result_dir / 'analysis.json'
                now = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
                job = {
                    'id': job_id,
                    'owner': self._get_request_token_id(),
                    'status': 'queued',
                    'filename': filename,
                    'created_at': now,
                    'updated_at': now,
                    'upload_dir': str(upload_dir),
                    'result_dir': str(result_dir),
                    'result_path': str(result_path),
                    'returncode': None,
                    'error': None,
                    'log_tail': [],
                }

                with self.api_jobs_lock:
                    self.api_jobs[job_id] = job

                worker = threading.Thread(
                    target=self._run_api_analysis_job,
                    args=(job_id, cmd, result_path),
                    daemon=True
                )
                worker.start()

                return jsonify(self._api_job_response(job)), 202
            except ValueError as e:
                if upload_dir:
                    shutil.rmtree(upload_dir, ignore_errors=True)
                if result_dir:
                    shutil.rmtree(result_dir, ignore_errors=True)
                return jsonify({'error': str(e)}), 400
            except Exception as e:
                if upload_dir:
                    shutil.rmtree(upload_dir, ignore_errors=True)
                if result_dir:
                    shutil.rmtree(result_dir, ignore_errors=True)
                logger.error(f"API upload error: {e}")
                return jsonify({'error': str(e)}), 500

        @self.app.route('/api/v1/analyses/<job_id>')
        @require_api_access
        def api_get_analysis(job_id):
            job, error = self._get_api_job(job_id)
            if error:
                return error
            return jsonify(self._api_job_response(job))

        @self.app.route('/api/v1/analyses/<job_id>/result')
        @require_api_access
        def api_get_analysis_result(job_id):
            job, error = self._get_api_job(job_id)
            if error:
                return error

            if job['status'] != 'completed':
                return jsonify({
                    'error': 'Analysis result is not ready',
                    'status': job['status'],
                }), 409

            result_path = Path(job['result_path'])
            if not result_path.exists():
                return jsonify({'error': 'Analysis result file not found'}), 404

            try:
                with open(result_path) as f:
                    return jsonify(json.load(f))
            except Exception as e:
                logger.error(f"Error reading API analysis result: {e}")
                return jsonify({'error': f'Error reading analysis result: {str(e)}'}), 500

        @self.app.route('/api/v1/analyses/<job_id>', methods=['DELETE'])
        @require_api_access
        def api_delete_analysis(job_id):
            job, error = self._get_api_job(job_id)
            if error:
                return error

            if job['status'] in ('queued', 'running'):
                return jsonify({'error': 'Cannot delete an active analysis job'}), 409

            shutil.rmtree(job['upload_dir'], ignore_errors=True)
            shutil.rmtree(job['result_dir'], ignore_errors=True)

            with self.api_jobs_lock:
                self.api_jobs.pop(job_id, None)

            return jsonify({'message': 'Analysis job deleted'})
            
        @self.app.route('/upload', methods=['POST'])
        def upload_file():
            if 'video' not in request.files:
                return jsonify({'error': 'No video file provided'}), 400
                
            file = request.files['video']
            if file.filename == '':
                return jsonify({'error': 'No selected file'}), 400
                
            if not file.filename.lower().endswith(ALLOWED_VIDEO_EXTENSIONS):
                return jsonify({'error': 'Invalid file type'}), 400
                
            try:
                # Create session
                session_id = str(uuid.uuid4())
                session_upload_dir = self.uploads_dir / session_id
                session_results_dir = self.results_dir / session_id
                session_upload_dir.mkdir(parents=True)
                session_results_dir.mkdir(parents=True)
                
                # Save file
                filename = secure_filename(file.filename)
                filepath = session_upload_dir / filename
                file.save(filepath)
                
                self.sessions[session_id] = {
                    'video_path': str(filepath),
                    'results_dir': str(session_results_dir),
                    'filename': filename
                }
                
                return jsonify({
                    'session_id': session_id,
                    'message': 'File uploaded successfully'
                })
                
            except Exception as e:
                logger.error(f"Upload error: {e}")
                return jsonify({'error': str(e)}), 500
                
        @self.app.route('/analyze/<session_id>', methods=['POST'])
        def analyze(session_id):
            if session_id not in self.sessions:
                return jsonify({'error': 'Invalid session'}), 404
                
            session = self.sessions[session_id]
            
            # Build command
            cmd = ['video-analyzer', session['video_path']]
            
            # Add optional parameters
            for param, value in request.form.items():
                if value:  # Only add parameters with values
                    if param in ['keep-frames', 'dev']:  # Flags without values
                        cmd.append(f'--{param}')
                    else:
                        cmd.extend([f'--{param}', value])
                        
            # Create output directory if it doesn't exist
            results_dir = Path(session['results_dir'])
            results_dir.mkdir(parents=True, exist_ok=True)
            
            # Add output directory
            cmd.extend(['--output', str(results_dir)])
            
            # Store output directory in session for later use
            session['output_dir'] = str(results_dir)
            logger.debug(f"Set output directory to: {results_dir}")
            
            # Store command in session for streaming
            session['cmd'] = cmd
            
            return jsonify({'message': 'Analysis started'})
            
        @self.app.route('/analyze/<session_id>/stream')
        def stream_output(session_id):
            if session_id not in self.sessions:
                return jsonify({'error': 'Invalid session'}), 404
                
            session = self.sessions[session_id]
            if 'cmd' not in session:
                return jsonify({'error': 'Analysis not started'}), 400
                
            def generate_output():
                logger.debug(f"Starting analysis with command: {' '.join(session['cmd'])}")
                try:
                    process = subprocess.Popen(
                        session['cmd'],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        universal_newlines=True,
                        bufsize=1
                    )
                    
                    for line in process.stdout:
                        line = line.strip()
                        if line:  # Only send non-empty lines
                            logger.debug(f"Output: {line}")
                            yield f"data: {line}\n\n"
                    
                    process.wait()
                    if process.returncode == 0:
                        logger.info("Analysis completed successfully")
                        yield f"data: Analysis completed successfully\n\n"
                    else:
                        logger.error(f"Analysis failed with code {process.returncode}")
                        yield f"data: Analysis failed with code {process.returncode}\n\n"
                except Exception as e:
                    logger.error(f"Error during analysis: {e}")
                    yield f"data: Error during analysis: {str(e)}\n\n"
                    yield f"data: Analysis failed\n\n"
                    
            return Response(
                generate_output(),
                mimetype='text/event-stream',
                headers={
                    'Cache-Control': 'no-cache',
                    'Connection': 'keep-alive'
                }
            )
            
        @self.app.route('/results/<session_id>')
        def get_results(session_id):
            if session_id not in self.sessions:
                return jsonify({'error': 'Invalid session'}), 404
                
            session = self.sessions[session_id]
            results_dir = Path(session['results_dir'])
            logger.debug(f"Looking for results in: {results_dir}")
            
            if not results_dir.exists():
                logger.error(f"Results directory not found: {results_dir}")
                return jsonify({'error': 'Results directory not found'}), 404
            
            # List all files in results directory for debugging
            logger.debug("Files in results directory:")
            for file in results_dir.glob('**/*'):
                logger.debug(f"- {file}")
            
            # Check both the results directory and the default 'output' directory
            analysis_file = results_dir / 'analysis.json'
            default_output = Path('output/analysis.json')
            
            if default_output.exists():
                logger.debug(f"Found analysis file in default output directory: {default_output}")
                try:
                    # Move the file to our results directory
                    default_output.rename(analysis_file)
                    logger.debug(f"Moved analysis file to: {analysis_file}")
                except Exception as e:
                    logger.error(f"Error moving analysis file: {e}")
                    # If move fails, try to copy the content
                    try:
                        analysis_file.write_text(default_output.read_text())
                        logger.debug("Copied analysis file content")
                        default_output.unlink()
                    except Exception as copy_error:
                        logger.error(f"Error copying analysis file: {copy_error}")
                        return jsonify({'error': 'Error accessing analysis file'}), 500
            if not analysis_file.exists():
                logger.error(f"Analysis file not found: {analysis_file}")
                return jsonify({'error': 'Analysis file not found'}), 404
                
            try:
                return send_file(
                    analysis_file,
                    mimetype='application/json',
                    as_attachment=True,
                    download_name=f"analysis_{session['filename']}.json"
                )
            except Exception as e:
                logger.error(f"Error sending file: {e}")
                return jsonify({'error': f'Error sending file: {str(e)}'}), 500
            
        @self.app.route('/cleanup/<session_id>', methods=['POST'])
        def cleanup_session(session_id):
            if session_id not in self.sessions:
                return jsonify({'error': 'Invalid session'}), 404
                
            try:
                session = self.sessions[session_id]
                # Clean up upload directory
                upload_dir = Path(session['video_path']).parent
                if upload_dir.exists():
                    for file in upload_dir.glob('*'):
                        file.unlink()
                    upload_dir.rmdir()
                
                # Clean up results directory
                results_dir = Path(session['results_dir'])
                if results_dir.exists():
                    for file in results_dir.glob('**/*'):
                        if file.is_file():
                            file.unlink()
                    for dir_path in sorted(results_dir.glob('**/*'), reverse=True):
                        if dir_path.is_dir():
                            dir_path.rmdir()
                    results_dir.rmdir()
                
                # Clean up default output directory if it exists
                default_output_dir = Path('output')
                if default_output_dir.exists():
                    for file in default_output_dir.glob('**/*'):
                        if file.is_file():
                            file.unlink()
                    for dir_path in sorted(default_output_dir.glob('**/*'), reverse=True):
                        if dir_path.is_dir():
                            dir_path.rmdir()
                    default_output_dir.rmdir()
                
                del self.sessions[session_id]
                return jsonify({'message': 'Session cleaned up successfully'})
                
            except Exception as e:
                logger.error(f"Cleanup error: {e}")
                return jsonify({'error': str(e)}), 500
    
    def run(self):
        self.app.run(
            host=self.host,
            port=self.port,
            debug=self.dev_mode
        )

def main():
    parser = argparse.ArgumentParser(description="Video Analyzer UI Server")
    parser.add_argument('--host', default='localhost', help='Host to bind to')
    parser.add_argument('--port', type=int, default=5000, help='Port to listen on')
    parser.add_argument('--dev', action='store_true', help='Enable development mode')
    parser.add_argument('--log-file', help='Log file path')
    
    args = parser.parse_args()
    
    # Configure logging
    log_config = {
        'level': logging.DEBUG if args.dev else logging.INFO,
        'format': '%(asctime)s - %(levelname)s - %(message)s',
    }
    if args.log_file:
        log_config['filename'] = args.log_file
    logging.basicConfig(**log_config)
    
    try:
        # Check if video-analyzer is installed
        subprocess.run(['video-analyzer', '--help'], capture_output=True, check=True)
    except subprocess.CalledProcessError:
        logger.error("video-analyzer command not found. Please install video-analyzer package.")
        sys.exit(1)
    except FileNotFoundError:
        logger.error("video-analyzer command not found. Please install video-analyzer package.")
        sys.exit(1)
    
    # Start server
    server = VideoAnalyzerUI(
        host=args.host,
        port=args.port,
        dev_mode=args.dev
    )
    server.run()

if __name__ == '__main__':
    main()
