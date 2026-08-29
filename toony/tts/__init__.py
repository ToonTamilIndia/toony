from .base import TTS, TTSError, Speech
from .factory import build_tts
from .speaker import Speaker

__all__ = ["TTS", "TTSError", "Speech", "build_tts", "Speaker"]
