# Video Analyzer Usage Guide

This guide covers all configuration options and command line arguments for the video analyzer tool, along with practical examples for different use cases.

## Table of Contents
- [Basic Usage](#basic-usage)
- [Command Line Arguments](#command-line-arguments)
- [Configuration System](#configuration-system)
- [Common Use Cases](#common-use-cases)
- [Advanced Examples](#advanced-examples)

## Basic Usage

### Local Analysis with Ollama (Default)
```bash
video-analyzer path/to/video.mp4
```

### Using OpenAI-Compatible API (OpenRouter/OpenAI)
```bash
video-analyzer path/to/video.mp4 --client openai_api --api-key your-key --api-url https://openrouter.ai/api/v1
```

## Command Line Arguments

| Argument | Description | Default | Example |
|----------|-------------|---------|---------|
| `video_path` | Path to the input video file | (Required) | `video.mp4` |
| `--config` | Path to configuration directory | config/ | `--config /path/to/config/` |
| `--output` | Output directory for analysis results | output/ | `--output ./results/` |
| `--client` | Client to use (ollama or openai_api) | ollama | `--client openai_api` |
| `--ollama-url` | URL for the Ollama service | http://localhost:11434 | `--ollama-url http://localhost:11434` |
| `--api-key` | API key for OpenAI-compatible service | None | `--api-key sk-xxx...` |
| `--api-url` | API URL for OpenAI-compatible API | None | `--api-url https://openrouter.ai/api/v1` |
| `--model` | Name of the vision model to use | llama3.2-vision | `--model gpt-4-vision-preview` |
| `--duration` | Duration in seconds to process | None (full video) | `--duration 60` |
| `--keep-frames` | Keep extracted frames after analysis | False | `--keep-frames` |
| `--whisper-model` | Whisper model size or model path | medium | `--whisper-model large` |
| `--start-stage` | Stage to start processing from (1-3) | 1 | `--start-stage 2` |
| `--max-frames` | Maximum number of frames to process. When specified, frames are sampled evenly across the video duration rather than just taking the first N frames. | sys.maxsize | `--max-frames 100` |
| `--log-level` | Set logging level | INFO | `--log-level DEBUG` |
| `--prompt` | Question to ask about the video | "" | `--prompt "What activities are shown?"` |
| `--language` | Set language for transcription | None (auto-detect) | `--language en` |
| `--device` | Select device for Whisper model | cpu | `--device cuda` |
| `--temperature` | Temperature for LLM generation | 0.2 | `--temperature 0.2` |

### Processing Stages
The `--start-stage` argument allows you to begin processing from a specific stage:
1. Frame and Audio Processing
2. Frame Analysis
3. Video Reconstruction

## Configuration System

The tool uses a cascading configuration system with the following priority:
1. Command line arguments (highest priority)
2. User config (config/config.json)
3. Default config (config/default_config.json)

### Configuration File Structure

```json
{
  "clients": {
    "default": "ollama",
    "temperature": 0.2,
    "ollama": {
      "url": "http://localhost:11434",
      "model": "llama3.2-vision"
    },
    "openai_api": {
      "api_key": "",
      "api_url": "https://openrouter.ai/api/v1",
      "model": "meta-llama/llama-3.2-11b-vision-instruct:free"
    }
  },
  "prompt_dir": "",
  "output_dir": "output",
  "frames": {
    "per_minute": 10,
    "analysis_threshold": 10.0,
    "min_difference": 5.0,
    "max_count": 30
  },
  "response_length": {
    "frame": 256,
    "reconstruction": 512,
    "narrative": 1024
  },
  "audio": {
    "sample_rate": 16000,
    "channels": 1,
    "quality_threshold": 0.5,
    "chunk_length": 30,
    "language_confidence_threshold": 0.5,
    "language": null
  },
  "keep_frames": false,
  "prompt": ""
}
```

### Configuration Options Explained

#### Client Settings
- `clients.default`: Default LLM client (ollama/openai_api)
- `clients.temperature`: Temperature for LLM generation (0.0-1.0, higher values = more creative)
- `clients.ollama.url`: Ollama service URL
- `clients.ollama.model`: Vision model for Ollama
- `clients.openai_api.api_key`: API key for OpenAI-compatible services
- `clients.openai_api.api_url`: API endpoint URL
- `clients.openai_api.model`: Vision model for API service

#### Frame Analysis Settings
- `frames.per_minute`: Target frames to extract per minute
- `frames.analysis_threshold`: Threshold for key frame detection
- `frames.min_difference`: Minimum difference between frames
- `frames.max_count`: Maximum frames to extract

#### Response Length Settings
- `response_length.frame`: Max length for frame analysis
- `response_length.reconstruction`: Max length for video reconstruction
- `response_length.narrative`: Max length for enhanced narrative

#### Audio Processing Settings
- `audio.sample_rate`: Audio sample rate in Hz
- `audio.channels`: Number of audio channels
- `audio.quality_threshold`: Minimum quality for transcription
- `audio.chunk_length`: Audio chunk processing length
- `audio.language_confidence_threshold`: Language detection confidence
- `audio.language`: Force specific language (null for auto-detect)

#### General Settings
- `prompt_dir`: Custom prompt directory path
- `output_dir`: Analysis output directory
- `keep_frames`: Retain extracted frames
- `prompt`: Custom analysis prompt

## Common Use Cases

### Quick Local Analysis
```bash
video-analyzer video.mp4
```

### High-Quality Cloud Analysis with Custom Prompt
```bash
video-analyzer video.mp4 \
    --client openai_api \
    --api-key your-key \
    --api-url https://openrouter.ai/api/v1 \
    --model meta-llama/llama-3.2-11b-vision-instruct:free \
    --whisper-model large \
    --prompt "What activities are happening in this video?"
```

### Resume from Frame Analysis Stage
```bash
video-analyzer video.mp4 \
    --start-stage 2 \
    --max-frames 50 \
    --keep-frames
```

### Analyze Video with Evenly Sampled Frames
```bash
video-analyzer video.mp4 \
    --max-frames 5 \
    --keep-frames
```
This will extract frames evenly spaced across the video duration. For example, in a 5-minute video, it would sample approximately one frame per minute rather than taking the first 5 frames.

### Batch Analyze a Video Directory

`video-analyzer-batch` recursively scans a source directory, runs `video-analyzer`
for each video, and mirrors the source folder structure into a result directory.
Each result is saved as `<video_stem>_analysis.json`.

The batch command can be configured with `.env` files or environment variables,
so local paths and API keys do not need to be written into scripts.

It automatically loads these files from the current directory when present:

- `.env`
- `.env.batch`
- `video-analyzer-batch.env`

Existing system environment variables override values from files. To load one
or more explicit files, set `VIDEO_ANALYZER_BATCH_ENV_FILE`. Multiple paths are
separated with `;` on Windows or `:` on macOS/Linux.

Example `.env.batch`:
```dotenv
VIDEO_ANALYZER_BATCH_INPUT_DIR=C:\Users\zewen\repos\nodeSpider_tiktok\download\Neo
VIDEO_ANALYZER_BATCH_OUTPUT_DIR=.\dy
VIDEO_ANALYZER_BATCH_CLIENT=openai_api
VIDEO_ANALYZER_BATCH_API_KEY=your-key
VIDEO_ANALYZER_BATCH_API_URL=https://api.openai.com/v1
VIDEO_ANALYZER_BATCH_MODEL=gpt-4o
VIDEO_ANALYZER_BATCH_DEVICE=cuda
VIDEO_ANALYZER_BATCH_MAX_FRAMES=20
```

Then run:
```bash
video-analyzer-batch
```

PowerShell:
```powershell
$env:VIDEO_ANALYZER_BATCH_INPUT_DIR = "C:\Users\zewen\repos\nodeSpider_tiktok\download\Neo"
$env:VIDEO_ANALYZER_BATCH_OUTPUT_DIR = ".\dy"
$env:VIDEO_ANALYZER_BATCH_CLIENT = "openai_api"
$env:VIDEO_ANALYZER_BATCH_API_KEY = "your-key"
$env:VIDEO_ANALYZER_BATCH_API_URL = "https://api.openai.com/v1"
$env:VIDEO_ANALYZER_BATCH_MODEL = "gpt-4o"
$env:VIDEO_ANALYZER_BATCH_DEVICE = "cuda"
$env:VIDEO_ANALYZER_BATCH_MAX_FRAMES = "20"

video-analyzer-batch
```

Bash/Zsh:
```bash
export VIDEO_ANALYZER_BATCH_INPUT_DIR="/path/to/videos"
export VIDEO_ANALYZER_BATCH_OUTPUT_DIR="./dy"
export VIDEO_ANALYZER_BATCH_CLIENT="openai_api"
export VIDEO_ANALYZER_BATCH_API_KEY="your-key"
export VIDEO_ANALYZER_BATCH_API_URL="https://api.openai.com/v1"
export VIDEO_ANALYZER_BATCH_MODEL="gpt-4o"
export VIDEO_ANALYZER_BATCH_DEVICE="cuda"
export VIDEO_ANALYZER_BATCH_MAX_FRAMES="20"

video-analyzer-batch
```

Useful optional variables:

- `VIDEO_ANALYZER_BATCH_EXTENSIONS`: comma-separated extensions, default `.mp4,.mkv,.avi,.ts`
- `VIDEO_ANALYZER_BATCH_TEMP_OUTPUT_DIR`: temporary analyzer output directory, default `./output`
- `VIDEO_ANALYZER_BATCH_ENV_FILE`: extra `.env` file path, or multiple paths separated by the OS path separator
- `VIDEO_ANALYZER_BATCH_SKIP_EXISTING`: skip videos with an existing result, default `true`
- `VIDEO_ANALYZER_BATCH_OVERWRITE_EXISTING`: replace existing result files, default `true`
- `VIDEO_ANALYZER_BATCH_CLEAN_TEMP`: clean temporary analyzer files after each video, default `true`
- `VIDEO_ANALYZER_BATCH_EXTRA_ARGS`: additional shell-style arguments passed to `video-analyzer`
- `video-analyzer-batch --help-env`: print the full environment variable reference

### Specific Language Processing
```bash
video-analyzer video.mp4 \
    --language es \
    --whisper-model large
```

### GPU-Accelerated Processing
```bash
video-analyzer video.mp4 \
    --device cuda \
    --whisper-model large
```

## Advanced Examples

### Full Configuration with OpenRouter
```bash
video-analyzer video.mp4 \
    --config custom_config.json \
    --output ./analysis_results \
    --client openai_api \
    --api-key your-key \
    --api-url https://openrouter.ai/api/v1 \
    --model meta-llama/llama-3.2-11b-vision-instruct:free \
    --duration 120 \
    --whisper-model large \
    --keep-frames \
    --log-level DEBUG \
    --prompt "Focus on the interactions between people"
```

### Local Processing with Frame Limits
```bash
video-analyzer video.mp4 \
    --client ollama \
    --ollama-url http://localhost:11434 \
    --model llama3.2-vision \
    --max-frames 30 \
    --whisper-model medium \
    --device cuda \
    --language en
```

### Resume Analysis from Specific Stage
```bash
video-analyzer video.mp4 \
    --start-stage 2 \
    --output ./custom_output \
    --keep-frames \
    --max-frames 50 \
    --prompt "Describe the main events"
```

### Using Local Whisper Model
```bash
video-analyzer video.mp4 \
    --whisper-model /path/to/whisper/model \
    --device cuda \
    --start-stage 1
