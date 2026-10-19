"""Comparing two readings of the same page and settling where they differ.

Pure text functions, no API calls: the converter sends the prompt built here and
checks the answer with validate_final().
"""
import re
from difflib import SequenceMatcher

DIACRITICS = re.compile(r"[ً-ْٰـ]")
DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")


def norm_word(word):
    """Spelling-blind form of a word: the two readers often differ only in hamza
    forms, final ى/ي or ة/ه, which are not real disagreements about the page."""
    w = DIACRITICS.sub("", word).translate(DIGITS)
    w = re.sub("[إأآٱا]", "ا", w)
    w = w.replace("ى", "ي").replace("ة", "ه").replace("ؤ", "و").replace("ئ", "ي")
    return re.sub(r"[^ء-ي0-9A-Za-z]", "", w)


def words(text):
    """[(normalised, original)] for every word that has letters or digits."""
    out = []
    for raw in (text or "").split():
        n = norm_word(raw)
        if n:
            out.append((n, raw))
    return out


def compare(a, b):
    """Similarity of two readings and the places where they differ.

    Returns (ratio, diffs) where diffs is a list of (words_in_a, words_in_b)."""
    wa, wb = words(a), words(b)
    m = SequenceMatcher(None, [w for w, _ in wa], [w for w, _ in wb], autojunk=False)
    diffs = []
    for op, i1, i2, j1, j2 in m.get_opcodes():
        if op != "equal":
            diffs.append((" ".join(r for _, r in wa[i1:i2]), " ".join(r for _, r in wb[j1:j2])))
    return m.ratio(), diffs


RECONCILE_PROMPT = """أمامك صورة صفحة من كتاب عربي ممسوح ضوئياً، ونسختان لنصها كتبهما قارئان آليان مختلفان: (أ) و(ب).
المطلوب نسخة نهائية واحدة مطابقة للصورة حرفاً بحرف.
- في كل موضع تختلف فيه النسختان انظر في الصورة واكتب المطبوع فعلاً، وقد يكون الصواب غير الاثنين.
- إن كان في الصورة سطر أو كلمات سقطت من النسختين فأضفها في مكانها.
- لا تحذف شيئاً مطبوعاً ولا تضف ما ليس في الصورة، ولا تصحح أخطاء المؤلف أو الطبعة.
- اكتب الحروف كما هي مطبوعة، ومنها «ى» و«ي» في آخر الكلمة.

قواعد الإخراج (Markdown):
<<RULES>>

مواضع الاختلاف بين النسختين (للتنبيه فقط، والحكم للصورة):
<<DIFFS>>
<<MISSING>>
النسخة (أ):
<<<
<<A>>
>>>

النسخة (ب):
<<<
<<B>>
>>>

اكتب النسخة النهائية فقط، بلا أي تعليق."""

MISSING_HINT = """
أسطر يبدو أنها سقطت من النسختين أو من إحداهما (من قارئ ثالث فيه أخطاء حروف، فاعتمد على الصورة):
<<LINES>>
"""


def reconcile_prompt(rules, a, b, diffs, missing):
    """The settling request. Placeholders are replaced one by one (not str.format),
    because page text may contain braces."""
    shown = diffs[:40]
    listed = "\n".join(f"- (أ): {x[:80] or '—'} | (ب): {y[:80] or '—'}" for x, y in shown)
    if len(diffs) > len(shown):
        listed += f"\n- … و{len(diffs) - len(shown)} موضعاً آخر"
    hint = MISSING_HINT.replace("<<LINES>>", "\n".join(f"- {l}" for l in missing[:5])) if missing else ""
    return (RECONCILE_PROMPT
            .replace("<<RULES>>", rules)
            .replace("<<DIFFS>>", listed or "- لا توجد")
            .replace("<<MISSING>>", hint)
            .replace("<<A>>", a)
            .replace("<<B>>", b or "(لا توجد نسخة ثانية؛ راجع النسخة (أ) على الصورة)"))


def validate_final(final, a, b):
    """True when the settled text is a plausible version of the page: close to at
    least one reading and not missing a large part of it."""
    if not final:
        return False
    nf = len(words(final))
    sizes = [len(words(t)) for t in (a, b) if t]
    if not sizes or nf < 0.9 * min(sizes):
        return False
    return any(compare(final, t)[0] >= 0.85 for t in (a, b) if t)
