"""
Utility functions for the YouTube Summary System.
Handles configuration loading, logging setup, and filename generation.
"""

import os
import re
import logging
from datetime import datetime
from pathlib import Path
from logging.handlers import RotatingFileHandler

import yaml
from dotenv import load_dotenv


def get_project_root() -> Path:
    """Get the project root directory."""
    return Path(__file__).parent.parent


def load_config() -> dict:
    """Load the main configuration from config.yaml."""
    config_path = get_project_root() / "config" / "config.yaml"
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_channels() -> list:
    """Load the channels list from channels.yaml."""
    channels_path = get_project_root() / "config" / "channels.yaml"
    with open(channels_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data.get("channels", []) or []


def load_env():
    """Load environment variables from .env file."""
    env_path = get_project_root() / "config" / ".env"
    load_dotenv(env_path)


def get_env(key: str, default: str = None) -> str:
    """Get an environment variable value."""
    load_env()
    return os.getenv(key, default)


def setup_logging(name: str = "youtube_summary") -> logging.Logger:
    """Set up logging with file and console handlers."""
    config = load_config()
    log_config = config.get("logging", {})

    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, log_config.get("level", "INFO")))

    # Clear any existing handlers
    logger.handlers.clear()

    # Create log directory if needed
    log_dir = get_project_root() / config["paths"]["logs_dir"]
    log_dir.mkdir(parents=True, exist_ok=True)

    # File handler with rotation
    log_file = log_dir / f"{name}.log"
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=log_config.get("max_file_size_mb", 10) * 1024 * 1024,
        backupCount=log_config.get("backup_count", 5),
        encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)

    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)

    # Formatter
    formatter = logging.Formatter(
        log_config.get("format", "%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    )
    file_handler.setFormatter(formatter)
    console_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    return logger


def sanitize_filename(text: str, max_length: int = 50) -> str:
    """
    Sanitize a string to be used as a filename.
    Removes special characters and limits length.
    """
    # Remove or replace special characters
    sanitized = re.sub(r'[<>:"/\\|?*]', '', text)
    sanitized = re.sub(r'[\s]+', '-', sanitized)
    sanitized = re.sub(r'[^\w\-]', '', sanitized)
    sanitized = sanitized.strip('-')

    # Limit length
    if len(sanitized) > max_length:
        sanitized = sanitized[:max_length].rstrip('-')

    return sanitized.lower() or "untitled"


def generate_summary_filename(
    channel_name: str,
    video_title: str,
    published_date: datetime = None
) -> str:
    """
    Generate a summary filename following the naming convention:
    {date}_{time}_{channel}_{title}.txt

    Example: 2026-01-21_1430_kpunch_video-title.txt
    """
    if published_date is None:
        published_date = datetime.now()

    date_str = published_date.strftime("%Y-%m-%d")
    time_str = published_date.strftime("%H%M")
    channel_clean = sanitize_filename(channel_name, max_length=20)
    title_clean = sanitize_filename(video_title, max_length=50)

    return f"{date_str}_{time_str}_{channel_clean}_{title_clean}.txt"


def get_summaries_dir() -> Path:
    """Get the summaries output directory."""
    config = load_config()
    summaries_dir = get_project_root() / config["paths"]["summaries_dir"]
    summaries_dir.mkdir(parents=True, exist_ok=True)
    return summaries_dir


def get_data_dir() -> Path:
    """Get the data directory."""
    config = load_config()
    data_dir = get_project_root() / config["paths"]["data_dir"]
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def format_summary(
    title: str,
    channel: str,
    published: str,
    url: str,
    overview: str,
    key_points: str,
    quotes: str,
    takeaways: str
) -> str:
    """Format the summary using the template from config."""
    config = load_config()
    template = config.get("summary_template", "")

    return template.format(
        title=title,
        channel=channel,
        published=published,
        url=url,
        overview=overview,
        key_points=key_points,
        quotes=quotes,
        takeaways=takeaways,
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    )
