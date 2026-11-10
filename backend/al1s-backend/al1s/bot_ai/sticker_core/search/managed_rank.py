"""Stealer's text-only smart-search recall and scoring for scoped entries."""

import re

from .aliases import ALIASES
from .text_similarity import BM25, _extract_words, _has_negation_prefix, calculate_hybrid_similarity, tokenize_for_bm25
from ..processing.semantic_schema import build_meme_search_text


def _labels(value):
    if isinstance(value, str):
        return [item.strip().lower() for item in value.split(',') if item.strip()]
    return [str(item).lower() for item in value if item] if isinstance(value, list) else []


def _characters(entry, info):
    key = str(entry.get('character') or '').strip()
    if not key:
        return []
    value = info.get(key) if isinstance(info, dict) else None
    name = str(value.get('name') or '').strip() if isinstance(value, dict) else ''
    return [item.lower() for item in (key, name) if item]


def _recall(query, entries, character_info):
    found, fusion = {}, {}

    def add(paths):
        seen = set()
        for index, path in enumerate(paths, 1):
            if path in seen:
                continue
            seen.add(path)
            found.setdefault(path, path)
            fusion[path] = fusion.get(path, 0.0) + 1.0 / (60 + index)

    for field in ('overlay_text', 'character', 'scenes'):
        matches = []
        for path, entry in entries.items():
            if field == 'character':
                values = _characters(entry, character_info)
            elif field == 'overlay_text':
                values = [str(entry.get(field) or '').lower()]
            else:
                values = _labels(entry.get(field, []))
            if any(len(value) >= 2 and (value in query or any(
                    len(piece) >= 2 and piece in query for piece in re.split(r'[\s,，。！？!?、~～]+', value)))
                    for value in values):
                matches.append(path)
            if len(matches) >= 16:
                break
        add(matches)
    documents, paths = [], []
    for path, entry in entries.items():
        tokens = tokenize_for_bm25(build_meme_search_text(entry, bm25=True))
        if tokens:
            paths.append(path)
            documents.append(tokens)
    if documents:
        add([paths[index] for index, _ in BM25(documents).get_top_k(tokenize_for_bm25(query), k=48)])
    return sorted(found, key=lambda path: -fusion[path])[:48]


def _score(query, entry, character_info):
    category = str(entry.get('category') or '').lower()
    tags, scenes = _labels(entry.get('tags')), _labels(entry.get('scenes'))
    overlay = str(entry.get('overlay_text') or '').strip().lower()
    if overlay and (_has_negation_prefix(query, overlay) or any(
            _has_negation_prefix(overlay, token) for token in query.split())):
        return 0.0
    description = str(entry.get('desc') or '').lower()
    words = _extract_words(query)
    desc_score = calculate_hybrid_similarity(query, description)
    if desc_score < 0.25:
        hits = sum(1 for word in words & _extract_words(description) if len(word) >= 2)
        desc_score = max(desc_score, min(1.0, hits * 0.25))
    tag_words = set().union(*(_extract_words(tag) for tag in tags)) if tags else set()
    tag_score = min(1.0, sum(tag in query for tag in tags) / len(tags)) if tags else 0.0
    if any(len(word) >= 2 for word in words & tag_words):
        tag_score = min(1.0, tag_score + 0.3)
    scene_words = set().union(*(_extract_words(scene) for scene in scenes)) if scenes else set()
    scene_score = min(1.0, sum(scene in query for scene in scenes) / len(scenes)) if scenes else 0.0
    phrases = {word for word in scene_words if len(word) >= 2}
    if phrases & words:
        scene_score = max(scene_score, 0.35 + 0.65 * len(phrases & words) / len(phrases))
    overlay_score = 1.0 if overlay and overlay in query else 0.0
    if overlay and not overlay_score:
        pieces = [piece for piece in re.split(r'[\s,，。！？!?、~～]+', overlay) if len(piece) >= 2]
        hits = sum(piece in query for piece in pieces)
        if hits:
            overlay_score = min(1.0, hits / max(len(pieces), 1) + 0.35)
    tokens = set(tokenize_for_bm25(query))
    entry_tokens = set(tokenize_for_bm25(' '.join([overlay, category, description, *tags, *scenes])))
    fast_score = len(tokens & entry_tokens) / max(1, len(tokens)) if tokens and entry_tokens else 0.0
    character_score = float(any(len(name) >= 2 and name in query for name in _characters(entry, character_info)))
    base = (overlay_score * 0.28 + desc_score * 0.18 + tag_score * 0.05 + scene_score * 0.25
            + character_score * 0.24 + fast_score * 0.2)
    if base < 0.15:
        return 0.0
    return base + min(0.02, int(entry.get('use_count', 0) or 0) * 0.002) + (0.04 if entry.get('is_favorite') else 0)


def rank_entries(query, entries, character_info, limit):
    folded = query.casefold()
    categories = []
    for alias, category in sorted(ALIASES.items(), key=lambda item: len(item[0]), reverse=True):
        term = alias.casefold()
        exact = len(term) <= 1 or term.isascii()
        if (folded == term if exact else term in folded) and not _has_negation_prefix(folded, term):
            if category not in categories:
                categories.append(category)
    expanded = ' '.join([folded, *categories])
    ranked = [(path, _score(expanded, entries[path], character_info))
              for path in _recall(expanded, entries, character_info)]
    return [path for path, score in sorted(ranked, key=lambda item: -item[1]) if score > 0.1][:limit]
