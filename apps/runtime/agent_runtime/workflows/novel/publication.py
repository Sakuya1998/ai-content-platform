"""Translate internal novel graph vocabulary at the generic publication boundary."""

FIELDS = {
    "novel_id": "project_id",
    "chapter_number": "block_index",
    "chapters": "blocks",
    "chapter_versions": "content_versions",
    "book_audits": "audits",
    "current_chapter": "current_block",
    "total_chapters": "block_count",
    "chapter_summaries": "block_summaries",
    "book_audit_completed": "audit_completed",
    "book_summary": "content_summary",
    "book_revision_mode": "revision_mode",
    "chapter_version": "content_version",
    "chapter_plan": "block_plan",
    "max_chapter_words": "max_block_words",
}


def _translate(value, fields):
    if isinstance(value, list):
        return [_translate(item, fields) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for name, item in value.items():
        key = fields.get(name, name)
        translated = _translate(item, fields)
        if key in result and result[key] != translated:
            raise ValueError("conflicting publication field: " + key)
        result[key] = translated
    return result


def project_envelope(value):
    return _translate(value, FIELDS)


def novel_envelope(value):
    return _translate(value, {public: internal for internal, public in FIELDS.items()})
