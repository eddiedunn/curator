"""Text chunking utilities for content processing."""

import re
from typing import List, Dict, Any, Optional


def count_tokens(text: str) -> int:
    """Estimate token count using word-based approximation.

    Args:
        text: Text to count tokens for

    Returns:
        Estimated token count (roughly 1.3 tokens per word)
    """
    if not text:
        return 0
    # Simple word-based estimate: ~1.3 tokens per word on average
    words = len(text.split())
    return int(words * 1.3)


def find_sentence_boundaries(text: str) -> List[int]:
    """Find sentence boundaries in text.

    Args:
        text: Text to analyze

    Returns:
        List of character offsets where sentences end
    """
    if not text:
        return []

    # Pattern for sentence endings: . ! ? followed by space/newline or end of string
    # Avoid splitting on common abbreviations
    pattern = r'(?<!\w\.\w.)(?<![A-Z][a-z]\.)(?<=\.|\!|\?)\s+'

    boundaries = []
    for match in re.finditer(pattern, text):
        boundaries.append(match.start())

    # Add end of text as final boundary
    if text and (not boundaries or boundaries[-1] < len(text)):
        boundaries.append(len(text))

    return boundaries


def merge_small_chunks(chunks: List[Dict], min_tokens: int = 100) -> List[Dict]:
    """Merge chunks that are below minimum token count.

    Args:
        chunks: List of chunk dictionaries
        min_tokens: Minimum token count for a chunk

    Returns:
        List of merged chunks
    """
    if not chunks:
        return []

    merged = []
    current_chunk = None

    for chunk in chunks:
        token_count = chunk.get('metadata', {}).get('token_count', 0)

        if current_chunk is None:
            current_chunk = chunk.copy()
            current_chunk['metadata'] = chunk['metadata'].copy()
        elif token_count < min_tokens or current_chunk['metadata'].get('token_count', 0) < min_tokens:
            # Merge with current chunk
            current_chunk['content'] += ' ' + chunk['content']
            current_chunk['end_offset'] = chunk['end_offset']
            current_chunk['metadata']['token_count'] = count_tokens(current_chunk['content'])

            # Merge timestamps if present
            if 'end_time' in chunk['metadata']:
                current_chunk['metadata']['end_time'] = chunk['metadata']['end_time']
        else:
            # Current chunk is large enough, save it and start new one
            merged.append(current_chunk)
            current_chunk = chunk.copy()
            current_chunk['metadata'] = chunk['metadata'].copy()

    # Add final chunk
    if current_chunk:
        merged.append(current_chunk)

    # Re-index chunks
    for i, chunk in enumerate(merged):
        chunk['metadata']['chunk_index'] = i

    return merged


def chunk_by_sentences(
    text: str,
    target_tokens: int = 500,
    overlap_tokens: int = 50,
) -> List[Dict]:
    """Split text into chunks by sentence boundaries.

    Args:
        text: Text to chunk
        target_tokens: Target size of each chunk in tokens
        overlap_tokens: Number of tokens to overlap between chunks

    Returns:
        List of chunk dictionaries with content and metadata
    """
    if not text:
        return []

    # Find sentence boundaries
    boundaries = find_sentence_boundaries(text)

    # Extract sentences with their positions
    sentences = []
    start = 0
    for end in boundaries:
        sentence_text = text[start:end].strip()
        if sentence_text:
            sentences.append({
                'text': sentence_text,
                'start': start,
                'end': end,
                'tokens': count_tokens(sentence_text)
            })
        start = end

    if not sentences:
        return []

    chunks = []
    current_sentences = []
    current_tokens = 0
    chunk_start = sentences[0]['start']

    i = 0
    while i < len(sentences):
        sentence = sentences[i]

        # Check if adding this sentence exceeds target
        if current_tokens + sentence['tokens'] > target_tokens and current_sentences:
            # Create chunk from accumulated sentences
            chunk_end = current_sentences[-1]['end']
            chunk_text = text[chunk_start:chunk_end].strip()

            chunks.append({
                'content': chunk_text,
                'start_offset': chunk_start,
                'end_offset': chunk_end,
                'metadata': {
                    'chunk_index': len(chunks),
                    'token_count': current_tokens,
                }
            })

            # Start new chunk with overlap
            # Find sentences to include in overlap
            overlap_start_idx = len(current_sentences) - 1
            overlap_accumulated = 0
            while overlap_start_idx >= 0 and overlap_accumulated < overlap_tokens:
                overlap_accumulated += current_sentences[overlap_start_idx]['tokens']
                overlap_start_idx -= 1
            overlap_start_idx += 1

            # Reset for next chunk
            if overlap_start_idx < len(current_sentences):
                current_sentences = current_sentences[overlap_start_idx:]
                current_tokens = sum(s['tokens'] for s in current_sentences)
                chunk_start = current_sentences[0]['start']
            else:
                current_sentences = []
                current_tokens = 0
                chunk_start = sentence['start']

            # Don't increment i, re-process this sentence
            continue

        current_sentences.append(sentence)
        current_tokens += sentence['tokens']
        i += 1

    # Add remaining sentences as final chunk
    if current_sentences:
        chunk_end = current_sentences[-1]['end']
        chunk_text = text[chunk_start:chunk_end].strip()
        chunks.append({
            'content': chunk_text,
            'start_offset': chunk_start,
            'end_offset': chunk_end,
            'metadata': {
                'chunk_index': len(chunks),
                'token_count': current_tokens,
            }
        })

    return chunks


def chunk_by_paragraphs(
    text: str,
    max_tokens: int = 1000,
) -> List[Dict]:
    """Split text into chunks by paragraph boundaries.

    Keeps paragraph boundaries intact. Combines small paragraphs
    and splits large ones.

    Args:
        text: Text to chunk
        max_tokens: Maximum size of each chunk in tokens

    Returns:
        List of chunk dictionaries with content and metadata
    """
    if not text:
        return []

    # Split by double newlines (paragraph breaks)
    paragraphs = []
    current_pos = 0

    # Find all paragraph breaks
    para_pattern = r'\n\s*\n'
    matches = list(re.finditer(para_pattern, text))

    if not matches:
        # Single paragraph
        paragraphs.append({
            'text': text.strip(),
            'start': 0,
            'end': len(text),
            'tokens': count_tokens(text)
        })
    else:
        # First paragraph
        first_para = text[:matches[0].start()].strip()
        if first_para:
            paragraphs.append({
                'text': first_para,
                'start': 0,
                'end': matches[0].start(),
                'tokens': count_tokens(first_para)
            })

        # Middle paragraphs
        for i, match in enumerate(matches):
            start = match.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            para_text = text[start:end].strip()
            if para_text:
                paragraphs.append({
                    'text': para_text,
                    'start': start,
                    'end': end,
                    'tokens': count_tokens(para_text)
                })

    if not paragraphs:
        return []

    chunks = []
    current_paras = []
    current_tokens = 0

    for para in paragraphs:
        # If paragraph alone exceeds max, split it by sentences
        if para['tokens'] > max_tokens:
            # Save accumulated paragraphs first
            if current_paras:
                chunk_start = current_paras[0]['start']
                chunk_end = current_paras[-1]['end']
                chunk_text = text[chunk_start:chunk_end].strip()
                chunks.append({
                    'content': chunk_text,
                    'start_offset': chunk_start,
                    'end_offset': chunk_end,
                    'metadata': {
                        'chunk_index': len(chunks),
                        'token_count': current_tokens,
                    }
                })
                current_paras = []
                current_tokens = 0

            # Split large paragraph by sentences
            para_chunks = chunk_by_sentences(para['text'], target_tokens=max_tokens, overlap_tokens=0)
            for pc in para_chunks:
                # Adjust offsets to original text
                pc['start_offset'] = para['start'] + pc['start_offset']
                pc['end_offset'] = para['start'] + pc['end_offset']
                pc['metadata']['chunk_index'] = len(chunks)
                chunks.append(pc)

        # Check if adding this paragraph exceeds max
        elif current_tokens + para['tokens'] > max_tokens and current_paras:
            # Create chunk from accumulated paragraphs
            chunk_start = current_paras[0]['start']
            chunk_end = current_paras[-1]['end']
            chunk_text = text[chunk_start:chunk_end].strip()
            chunks.append({
                'content': chunk_text,
                'start_offset': chunk_start,
                'end_offset': chunk_end,
                'metadata': {
                    'chunk_index': len(chunks),
                    'token_count': current_tokens,
                }
            })

            # Start new chunk with current paragraph
            current_paras = [para]
            current_tokens = para['tokens']
        else:
            # Add paragraph to current chunk
            current_paras.append(para)
            current_tokens += para['tokens']

    # Add remaining paragraphs as final chunk
    if current_paras:
        chunk_start = current_paras[0]['start']
        chunk_end = current_paras[-1]['end']
        chunk_text = text[chunk_start:chunk_end].strip()
        chunks.append({
            'content': chunk_text,
            'start_offset': chunk_start,
            'end_offset': chunk_end,
            'metadata': {
                'chunk_index': len(chunks),
                'token_count': current_tokens,
            }
        })

    return chunks


def chunk_by_semantic(
    text: str,
    target_tokens: int = 500,
    overlap_tokens: int = 50,
) -> List[Dict]:
    """Default chunking strategy balancing size and semantic coherence.

    Uses paragraph-aware sentence chunking for best results.

    Args:
        text: Text to chunk
        target_tokens: Target size of each chunk in tokens
        overlap_tokens: Number of tokens to overlap between chunks

    Returns:
        List of chunk dictionaries with content and metadata
    """
    if not text:
        return []

    # Use sentence-based chunking as the default semantic strategy
    # This balances between maintaining semantic units and target size
    return chunk_by_sentences(text, target_tokens=target_tokens, overlap_tokens=overlap_tokens)


def chunk_with_timestamps(
    text: str,
    segments: List[Dict[str, Any]],
    target_tokens: int = 500,
) -> List[Dict]:
    """Chunk text with timestamp alignment for transcripts.

    Args:
        text: Full transcript text
        segments: List of timestamp segments with format:
                 [{"text": str, "start": float, "end": float}, ...]
        target_tokens: Target size of each chunk in tokens

    Returns:
        List of chunk dictionaries with timestamp metadata
    """
    if not text or not segments:
        return []

    chunks = []
    current_segments = []
    current_tokens = 0
    text_start = 0

    for segment in segments:
        segment_text = segment.get('text', '').strip()
        segment_tokens = count_tokens(segment_text)

        # Check if adding this segment exceeds target
        if current_tokens + segment_tokens > target_tokens and current_segments:
            # Create chunk from accumulated segments
            chunk_text = ' '.join(s.get('text', '').strip() for s in current_segments)
            chunk_end = text_start + len(chunk_text)

            chunks.append({
                'content': chunk_text,
                'start_offset': text_start,
                'end_offset': chunk_end,
                'metadata': {
                    'chunk_index': len(chunks),
                    'token_count': current_tokens,
                    'start_time': current_segments[0].get('start', 0.0),
                    'end_time': current_segments[-1].get('end', 0.0),
                }
            })

            # Reset for next chunk
            text_start = chunk_end + 1  # +1 for space
            current_segments = []
            current_tokens = 0

        current_segments.append(segment)
        current_tokens += segment_tokens

    # Add remaining segments as final chunk
    if current_segments:
        chunk_text = ' '.join(s.get('text', '').strip() for s in current_segments)
        chunk_end = text_start + len(chunk_text)

        chunks.append({
            'content': chunk_text,
            'start_offset': text_start,
            'end_offset': chunk_end,
            'metadata': {
                'chunk_index': len(chunks),
                'token_count': current_tokens,
                'start_time': current_segments[0].get('start', 0.0),
                'end_time': current_segments[-1].get('end', 0.0),
            }
        })

    return chunks


# Legacy functions for backward compatibility
def chunk_text(
    text: str,
    chunk_size: int = 512,
    overlap: int = 50,
) -> List[str]:
    """Split text into overlapping chunks (legacy function).

    Args:
        text: Text to chunk
        chunk_size: Target size of each chunk in characters
        overlap: Number of characters to overlap between chunks

    Returns:
        List of text chunks
    """
    if not text:
        return []

    chunks = []
    start = 0

    while start < len(text):
        end = min(start + chunk_size, len(text))
        chunk = text[start:end]
        chunks.append(chunk)

        # Move start position by chunk_size minus overlap
        start += chunk_size - overlap

    return chunks
