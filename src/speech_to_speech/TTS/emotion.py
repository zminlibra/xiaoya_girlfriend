"""Keyword-driven emotion detection for Qwen3-TTS VoiceDesign/CustomVoice.

Qwen3-TTS has no ``emotion=`` parameter: the only way to colour delivery is the
``instruct`` prompt. This module maps an utterance to a short style suffix that
gets appended to the base voice instruction, so the same speaker sounds happy,
shy, sad and so on.

The base instruction is always kept intact — only the style suffix changes.
Appending (rather than replacing) is what keeps the *voice identity* stable
across emotions; the CustomVoice speaker embedding pins the timbre while the
suffix only moves prosody.
"""

from __future__ import annotations

# Ordered most-specific first: the first emotion with a keyword hit wins, so
# put the ones whose cues are least ambiguous at the top.
# Style suffixes deliberately lead with *tempo and tone of voice* rather than
# pitch words. Measured on Qwen3-TTS: stronger pitch-flavoured wording ("音调明显
# 上扬", "声音明亮") collapsed the happy/sad pitch gap from 76Hz to 36Hz and pushed
# timbre around, whereas tempo/tone wording shifts delivery while keeping the
# speaker recognisable. Keep these short and avoid stacking several pitch words.
EMOTION_STYLES: dict[str, str] = {
    "shy": "，语气害羞腼腆，语速稍慢，尾音软糯",
    "sad": "，语气低落，语速放慢",
    "angry": "，语气带着小情绪，语速稍快，咬字偏重",
    "surprised": "，语气惊讶，语速略快",
    "happy": "，语气开心雀跃，语速稍快",
    "neutral": "",
}

EMOTION_KEYWORDS: dict[str, tuple[str, ...]] = {
    "shy": ("害羞", "脸红", "讨厌啦", "羞", "不好意思", "心动", "人家"),
    "sad": ("难过", "伤心", "呜呜", "委屈", "寂寞", "想念", "失落", "唉", "不开心"),
    "angry": ("哼", "生气", "不理你", "气死", "讨厌你", "烦", "过分"),
    "surprised": ("哇", "天哪", "竟然", "真的吗", "惊讶", "没想到", "居然", "不是吧"),
    "happy": ("开心", "高兴", "嘻嘻", "哈哈", "嘿嘿", "喜欢", "好呀", "棒", "兴奋", "好玩", "可爱"),
}


def detect_emotion(text: str) -> str:
    """Return the emotion label for ``text``.

    ``neutral`` when nothing matches. Keyword matching is deliberately simple:
    the goal is audible variation in delivery, not sentiment accuracy, and a
    wrong guess only shifts prosody slightly.
    """
    if not text:
        return "neutral"
    for emotion, words in EMOTION_KEYWORDS.items():
        if any(word in text for word in words):
            return emotion
    return "neutral"


def apply_emotion(base_instruct: str | None, text: str) -> str | None:
    """Append the detected emotion's style suffix to ``base_instruct``.

    Returns ``base_instruct`` unchanged for neutral text so the common case
    produces byte-identical prompts (and therefore identical audio under
    deterministic decoding).
    """
    emotion = detect_emotion(text)
    suffix = EMOTION_STYLES.get(emotion, "")
    if not suffix:
        return base_instruct
    if not base_instruct:
        return suffix.lstrip("，")
    return f"{base_instruct}{suffix}"
