import os
import logging
import tempfile

logger = logging.getLogger("talos.voice")

_gtts_available = None


def is_gtts_available() -> bool:
    global _gtts_available
    if _gtts_available is not None:
        return _gtts_available
    try:
        from gtts import gTTS
        _gtts_available = True
        logger.info("gTTS available")
    except ImportError:
        logger.warning("gTTS not found. Install: pip install gTTS")
        _gtts_available = False
    return _gtts_available


def text_to_speech(text: str, output_path: str | None = None) -> str | None:
    if not is_gtts_available():
        return None
    
    if not text or not text.strip():
        return None
    
    text = text.strip()
    
    if output_path is None:
        fd, output_path = tempfile.mkstemp(suffix=".mp3", prefix="talos_tts_")
        os.close(fd)
    
    try:
        from gtts import gTTS
        tts = gTTS(text=text, lang='en', slow=False)
        tts.save(output_path)
        
        if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            logger.info(f"TTS generated: {output_path}")
            return output_path
        return None
    except Exception as e:
        logger.error(f"TTS error: {e}")
        return None


def cleanup_audio_file(path: str) -> None:
    try:
        if os.path.exists(path):
            os.unlink(path)
    except Exception:
        pass
