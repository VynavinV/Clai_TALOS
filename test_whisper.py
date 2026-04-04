#!/usr/bin/env python3
import whisper
import tempfile
import os

print("Testing Whisper transcription...")

model = whisper.load_model("base")

with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
    tmp_path = tmp.name

import numpy as np
sample_rate = 16000
duration = 3
audio = np.sin(2 * np.pi * sample_rate * duration).astype(np.float32)
with open(tmp_path, 'wb') as f:
    f.write(audio.tobytes())

result = model.transcribe(tmp_path, language="en")
text = result["text"].strip()

print(f"Transcription result: {text}")

os.unlink(tmp_path)
print("Test passed!")
