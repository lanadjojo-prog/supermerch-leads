"""Enforce the public SuperMerch sender identity at every Zoho output boundary."""
import re

SENDER_NAME = 'Chris'
_OLD_NAME = r'Giovanni(?:\s+Djojowardi)?'


def normalize_sender_identity(text: str) -> str:
    """Repair self-introductions/signatures; reject ambiguous leftover mentions.

    Never silently rename a recipient or company that happens to use the old
    name. Such copy needs review instead of becoming a wrong personalized draft.
    """
    # Imported historical mail copy sometimes stores escaped line separators.
    value = str(text or '').replace(r'\r\n', '\n').replace(r'\n', '\n').replace(r'\r', '\n')
    value = re.sub(r'\b(ik\s+ben|mijn\s+naam\s+is|hier\s+is)\s+' + _OLD_NAME + r'\b',
                   lambda match: match[1] + ' ' + SENDER_NAME, value, flags=re.I)
    value = re.sub(r'\b' + _OLD_NAME + r'\b(?=\s*(?:van\s+SuperMerch\b|\|\s*SuperMerch\b))',
                   SENDER_NAME, value, flags=re.I)
    value = re.sub(r'^[ \t]*' + _OLD_NAME + r'[ \t]*$', SENDER_NAME, value, flags=re.I | re.M)
    if re.search(r'\bGiovanni\b', value, flags=re.I):
        raise ValueError('Controleer de afzendernaam in de SuperMerch-tekst.')
    return value
