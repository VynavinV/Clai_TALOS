import os
import logging
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger("talos.voice")

_PIPER_MODEL = os.getenv("PIPER_VOICE", "en_US-lessac-medium")
_PIPER_DATA_DIR = os.getenv("PIPER_DATA_DIR", os.path.expanduser("~/.local/share/piper"))
_PIPER_EXECUTABLE = os.getenv("PIPER_PATH", "piper")

_piper_available = None


def is_piper_available() -> bool:
    global _piper_available
    if _piper_available is not None:
        return _piper_available
    
    try:
        result = subprocess.run(
            [_PIPER_EXECUTABLE, "--version"],
            capture_output=True,
            text=True,
            timeout=5
        )
        _piper_available = result.returncode == 0
        if _piper_available:
            logger.info("Piper TTS available")
        else:
            logger.warning("Piper TTS not found")
        return _piper_available
    except Exception as e:
        logger.warning(f"Piper check failed: {e}")
        _piper_available = False
        return False


def text_to_speech(text: str, output_path: str | None = None) -> str | None:
    if not is_piper_available():
        logger.error("Piper TTS not available")
        return None
    
    if not text or not text.strip():
        logger.error("Empty text provided")
        return None
    
    text = text.strip()
    
    if output_path is None:
        fd, output_path = tempfile.mkstemp(suffix=".wav", prefix="talos_tts_")
        os.close(fd)
    
    try:
        piper_data_dir = Path(_PIPER_DATA_DIR)
        piper_data_dir.mkdir(parents=True, exist_ok=True)
        
        cmd = [
            _PIPER_EXECUTABLE,
            "--model", _PIPER_MODEL,
            "--output_file", output_path,
            "--data-dir", str(piper_data_dir),
            "--download-dir", str(piper_data_dir),
        ]
        
        logger.info(f"Running Piper TTS for {len(text)} chars")
        
        result = subprocess.run(
            cmd,
            input=text,
            capture_output=True,
            text=True,
            timeout=60
        )
        
        if result.returncode != 0:
            logger.error(f"Piper failed: {result.stderr}")
            return None
        
        if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            logger.info(f"TTS generated: {output_path} ({os.path.getsize(output_path)} bytes)")
            return output_path
        else:
            logger.error("Piper produced no output file")
            return None
            
    except subprocess.TimeoutExpired:
        logger.error("Piper TTS timeout")
        return None
    except Exception as e:
        logger.exception(f"TTS error: {e}")
        return None


def cleanup_audio_file(path: str) -> None:
    try:
        if os.path.exists(path):
            os.unlink(path)
            logger.debug(f"Cleaned up audio file: {path}")
    except Exception as e:
        logger.warning(f"Failed to cleanup {path}: {e}")
