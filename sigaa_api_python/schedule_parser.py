import re
_GROUP_RE = re.compile('\\d[MTN]\\d+', re.IGNORECASE)

def parse_schedule_code(code: str) -> int:
    if not code:
        return 1
    code = code.strip()
    groups = _GROUP_RE.findall(code)
    if not groups:
        return 1
    total_slots = 0
    for group in groups:
        slots = group[2:]
        total_slots += len(slots)
    return max(1, total_slots)