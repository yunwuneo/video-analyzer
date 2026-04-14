# Video Analyzer UI

A lightweight web interface for the video-analyzer tool.

## Features
- Simple, intuitive interface for video analysis
- Real-time command output streaming
- Drag-and-drop video upload
- Results visualization and download
- Session management and cleanup

## Prerequisites
- Python 3.8 or higher
- video-analyzer package installed
- FFmpeg (required by video-analyzer)

## Installation

```bash
pip install video-analyzer-ui
```

## Quick Start

1. Start the server:
   ```bash
   video-analyzer-ui
   ```

2. Open in browser:
   http://localhost:5000

## Usage

### Development Mode
```bash
video-analyzer-ui --dev
```
- Auto-reload on code changes
- Debug logging
- Development error pages

### Production Mode
```bash
video-analyzer-ui --host 0.0.0.0 --port 5000
```
- Optimized for performance
- Error logging to file
- Production-ready security

### Command Line Options
- `--dev`: Enable development mode
- `--host`: Bind address (default: localhost)
- `--port`: Port number (default: 5000)
- `--log-file`: Log file path
- `--config`: Custom config file path

## External API

The UI server also exposes an authenticated JSON API under `/api/v1`. API access is disabled until at least one bearer token is configured.

### Configuration

```bash
export VIDEO_ANALYZER_API_KEY="replace-with-a-long-random-token"
# or multiple comma-separated tokens:
export VIDEO_ANALYZER_API_KEYS="token-one,token-two"

# Optional, defaults to 30/minute per token and client IP.
export VIDEO_ANALYZER_API_RATE_LIMIT="30/minute"

# Optional, defaults to 512 MB.
export VIDEO_ANALYZER_MAX_UPLOAD_MB="512"

video-analyzer-ui --host 0.0.0.0 --port 5000
```

Supported rate-limit windows are `second`, `minute`, and `hour`, for example `5/minute` or `100/hour`.

### Start an Analysis

```bash
curl -X POST http://localhost:5000/api/v1/analyses \
  -H "Authorization: Bearer $VIDEO_ANALYZER_API_KEY" \
  -F "video=@/path/to/video.mp4" \
  -F "client=ollama" \
  -F "model=llama3.2-vision" \
  -F "max_frames=5" \
  -F "prompt=Describe the main actions"
```

The response is `202 Accepted` with a job ID:

```json
{
  "id": "8fd9f9f1-4f02-4d6f-9b5d-6be905438ea1",
  "status": "queued",
  "links": {
    "self": "/api/v1/analyses/8fd9f9f1-4f02-4d6f-9b5d-6be905438ea1",
    "result": "/api/v1/analyses/8fd9f9f1-4f02-4d6f-9b5d-6be905438ea1/result"
  }
}
```

### Poll Status

```bash
curl http://localhost:5000/api/v1/analyses/<job-id> \
  -H "Authorization: Bearer $VIDEO_ANALYZER_API_KEY"
```

Jobs move through `queued`, `running`, `completed`, or `failed`. The status response includes a small `log_tail` for troubleshooting without exposing the internal command.

### Fetch Result

```bash
curl http://localhost:5000/api/v1/analyses/<job-id>/result \
  -H "Authorization: Bearer $VIDEO_ANALYZER_API_KEY"
```

The result endpoint returns the same `analysis.json` structure produced by the CLI. If the job is not finished, it returns `409 Conflict`.

### Cleanup

```bash
curl -X DELETE http://localhost:5000/api/v1/analyses/<job-id> \
  -H "Authorization: Bearer $VIDEO_ANALYZER_API_KEY"
```

Cleanup removes the uploaded video and generated results for completed or failed jobs. Running jobs cannot be deleted.

### Accepted Analysis Parameters

The API accepts the uploaded `video` file and a safe whitelist of CLI-compatible fields: `client`, `ollama_url`, `api_key`, `api_url`, `model`, `whisper_model`, `prompt`, `language`, `device`, `max_frames`, `duration`, `temperature`, and `keep_frames`. Use underscores or hyphens in field names, for example `max_frames` or `max-frames`.

## Development Setup

1. Clone the repository:
   ```bash
   git clone https://github.com/username/video-analyzer-ui.git
   cd video-analyzer-ui
   ```

2. Create virtual environment:
   ```bash
   python -m venv venv
   source venv/bin/activate  # or venv\Scripts\activate on Windows
   ```

3. Install dependencies:
   ```bash
   pip install -e .
   ```

4. Run development server:
   ```bash
   video-analyzer-ui --dev
   ```

## How It Works

1. Upload Video:
   - Drag and drop or select a video file
   - File is temporarily stored in a session-specific directory

2. Configure Analysis:
   - Required fields are marked with *
   - Optional parameters can be left empty
   - Real-time command preview shows what will be executed

3. Run Analysis:
   - Progress is shown in real-time
   - Output is streamed as it becomes available
   - Results are stored in session directory

4. View Results:
   - Download analysis.json
   - View extracted frames (if kept)
   - Access transcripts and other outputs

## License

Apache License
