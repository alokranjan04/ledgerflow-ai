"""Conversation language: English, Hindi (Devanagari) and Hinglish (Hindi in Latin script).

`detect` decides from the customer's own words; the session keeps the language once
chosen (the lock), because noisy phone audio and code-mixed speech otherwise make
the model flip languages mid-conversation (a lesson from the Mydoot phone agent).
"""
from __future__ import annotations

import re

LANGUAGES = {"en": "English", "hi": "हिन्दी", "hi-Latn": "Hinglish"}
DEVANAGARI = re.compile(r"[ऀ-ॿ]")
LATIN = re.compile(r"[A-Za-z]")
# Frequent Hinglish function words; two or more in a message mark it as Hinglish.
HINGLISH = {"kya", "mera", "meri", "mere", "hai", "hain", "kitna", "kitni", "kab", "kyun", "kyon", "nahi", "nahin",
            "karna", "karo", "kar", "chahiye", "batao", "bataiye", "paise", "paisa", "kaise", "aur", "toh", "bhi",
            "maine", "mujhe", "aap", "apna", "abhi", "wala", "wali", "gaya", "gayi", "hua", "hoga", "sakta", "sakte"}


def detect(text: str) -> str:
    """Best guess for one message: 'hi' (Devanagari), 'hi-Latn' (Hinglish) or 'en'."""
    text = text or ""
    dev, lat = len(DEVANAGARI.findall(text)), len(LATIN.findall(text))
    if dev and dev >= 0.3 * (dev + lat):
        return "hi"
    words = re.findall(r"[a-z]+", text.lower())
    if sum(w in HINGLISH for w in words) >= 2:
        return "hi-Latn"
    return "en"


def instruction(language: str, mode: str) -> str:
    """The language block of the system prompt."""
    spoken = mode in ("voice", "phone")
    keep = ("Keep amounts as digits with ₹ and Indian grouping exactly as tools return them (₹17,323.49); "
            "keep IDs, clause IDs, product names and banking terms such as EMI, loan, prepayment and foreclosure "
            "in English.") if not spoken else (
            "Say amounts in Indian style in the chosen language (for example 'sattrah hazaar teen sau' or "
            "'सत्रह हज़ार तीन सौ' rupees); keep IDs and clause IDs as letters and digits; banking terms such as "
            "EMI, loan and prepayment may stay in English.")
    lock = ("Once the language is set, keep it in BOTH directions for the rest of the conversation. Speech that "
            "sounds like another language is unclear audio, not a language change; switch only if the customer "
            "clearly asks.") if spoken else "Keep that language unless the customer switches or asks."
    if language == "hi":
        return f"LANGUAGE: Reply in simple, everyday Hindi in Devanagari script. {keep} {lock}"
    if language == "hi-Latn":
        return (f"LANGUAGE: Reply in Hinglish: Hindi written in Latin script, the way the customer writes. {keep} "
                f"{lock}")
    if language == "en":
        return f"LANGUAGE: Reply in English. {lock}"
    return ("LANGUAGE: Reply in the customer's language: English, Hindi (Devanagari) or Hinglish (Hindi in Latin "
            f"script), matching how they write or speak. {keep} {lock}")
