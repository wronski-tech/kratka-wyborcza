"""Speech to text for spoken questions (OpenAI transcription).

The transcript is only the question text: the UI shows it for checking and then asks it through
``/ask``, so a spoken question passes the same routing, SQL guards and answer checks as a typed one.
"""

from __future__ import annotations


from api.services.config import OPENAI_TRANSCRIBE_MODEL
from api.services.llm_client import llm_client

# Vocabulary hint: election terms and party names the model would otherwise misspell.
PROMPT = ("Pytanie o polskie wybory: Sejm, Senat, prezydent, Parlament Europejski, sejmik, wójt, burmistrz, gmina, "
          "powiat, województwo, frekwencja, mandaty, PKW, KO, PiS, Lewica, Trzecia Droga, PSL, Konfederacja.")
MAX_CHARS = 1000


def transcribe(audio: bytes, content_type: str) -> str:
    """Text of a recorded question (language detected by the model), whitespace collapsed."""
    result = llm_client().audio.transcriptions.create(
        model=OPENAI_TRANSCRIBE_MODEL, file=("question.wav", audio, content_type), prompt=PROMPT,
    )
    return " ".join(result.text.split())[:MAX_CHARS]
