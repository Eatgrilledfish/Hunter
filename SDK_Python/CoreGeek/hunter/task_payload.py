"""Bounded, explicitly covered evidence for the cross-turn model request."""
import json


def api_scope(task):
    manual=next((e['data'].get('file_sha256') for e in reversed(list(task.evidence.values()))
                 if e.get('usable') and e.get('data',{}).get('path')=='API_DOCS.md'
                 and e['data'].get('completeness')=='complete'),None)
    return (task.environment.get('root'),manual) if task.environment and manual else None


def api_observations(task, remembered=()):
    result=[]
    for event in list(remembered)+[v for e in task.evidence.values()
            for v in e.get('data',{}).get('runtime_events',[])]:
        if event.get('kind') not in {'http','json_shape','exception'}:continue
        if event not in result:result.append(event)
    return result[-16:]


def size(value):
    return len(json.dumps(value, ensure_ascii=False))


def pack_evidence(task, budget=24000):
    selected, seen_paths = [], set()
    for index, (key, record) in enumerate(reversed(list(task.evidence.items()))):
        data = dict(record.get('data', {}))
        data.pop('context', None)
        operation = data.get('operation')
        document = operation == 'read_slice'
        if document:
            path = data.get('path')
            if path in seen_paths:
                continue  # Earlier versions/slices must not crowd out current content.
            seen_paths.add(path)
        if isinstance(data.get('text'), str):
            data.pop('bytes_base64', None)
            data.pop('slice_text', None)  # Already included in text (whole or slice).
        item = {'id': key, **{k: v for k, v in record.items() if k not in ('nonce', 'data')}, 'data': data}
        priority = (0 if record.get('source') == 'task' or document and data.get('path') == task.statement_path
                    else 1 if operation in ('run_tool', 'run_python')
                    else 2 if document else 3)
        selected.append((priority, index, item))
    evidence, coverage, used = [], {}, 0
    for _, _, item in sorted(selected, key=lambda x: x[:2]):
        data = item['data']
        document = data.get('operation') == 'read_slice'
        text = data.get('text')
        available = budget-used
        amount = size(item)
        if amount > available and isinstance(text, str) and available >= 800:
            # Keep both ends and expose exactly what was sent. The source can be
            # fully verified while its prompt representation remains partial.
            # No byte offset is guessed from character offsets (UTF-8 differs).
            base = {k: v for k, v in data.items() if k != 'text'}
            original = task.evidence[item['id']].get('data', {})
            recent = original.get('slice_text')
            # Re-reading a middle slice must actually expose that slice even
            # when the ledger assembles the full (oversized) document again.
            # Keep the wrapper's byte coordinates rather than converting them.
            if (document and isinstance(recent, str) and len(recent) <= 8192
                    and type(original.get('slice_offset')) is int
                    and type(original.get('slice_next_byte')) is int):
                base['recent_requested_slice'] = {'start_byte': original['slice_offset'],
                    'end_byte': original['slice_next_byte'], 'text': recent}
            def clipped(count):
                head, tail = (count+1)//2, count//2
                ranges = [[0, head], [len(text)-tail, len(text)]] if tail else [[0, head]]
                return {**item, 'data': {**base, 'text': None,
                        'text_chars_in_evidence': len(text), 'prompt_completeness': 'partial',
                        'sent_char_ranges': ranges,
                        'text_segments': [{'start_char': a, 'end_char': b, 'text': text[a:b]} for a,b in ranges]}}
            low, high = 0, len(text)
            while low < high:
                middle = (low+high+1)//2
                if size(clipped(middle)) <= available:
                    low = middle
                else:
                    high = middle-1
            item = clipped(low)
            amount = size(item)
        if amount <= available:
            evidence.append(item)
            used += amount
        if document:
            sent = amount <= available
            coverage[item['id']] = {'path': data.get('path'), 'file_sha256': data.get('file_sha256'),
                'source_completeness': data.get('completeness'),
                'prompt_completeness': (item['data'].get('prompt_completeness', data.get('completeness'))
                                        if sent else 'omitted'),
                'sent_char_ranges': (item['data'].get('sent_char_ranges', [[0, len(text)]])
                                     if sent and isinstance(text, str) else []),
                'recent_requested_byte_range': ([item['data']['recent_requested_slice']['start_byte'],
                    item['data']['recent_requested_slice']['end_byte']]
                    if sent and 'recent_requested_slice' in item['data'] else None)}
    return evidence, coverage
