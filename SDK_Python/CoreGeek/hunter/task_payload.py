"""Bounded, explicitly covered evidence for the cross-turn model request."""
import json
import re


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
        # Persist the interface shape, never carry a previous city's record
        # samples or pagination totals into the next task as current evidence.
        if event.get('kind')=='json_shape':
            event={k:v for k,v in event.items() if k not in {'record_samples','pagination','records_on_page','responses_observed',
                'temporal_value_counts','records_observed','temporal_values_partial','pagination_coverage','pagination_datasets'}}
        if event not in result:result.append(event)
    return result[-16:]


def size(value):
    return len(json.dumps(value, ensure_ascii=False))


def statistics_review(task, answer_only=False):
    statements=[task.text]+[e.get('data',{}).get('text','') or '' for e in task.evidence.values()
        if e.get('usable') and e.get('data',{}).get('operation')=='read_slice'
        and e['data'].get('path')==task.statement_path]
    text='\n'.join(statements)
    if not re.search(r'API|统计|count|aggregate|分页',text,re.I):return None
    result = {
        'oldest_era':'先读取本题对 oldest_era 的完整定义，确认返回的是年代值还是对应记录名称，不能仅凭字段名或历史城市答案推断。按本城市完整记录的明确时间字段排序，核对 temporal_value_counts 中全部已观察年代、实际数值年份及所需排序依据；响应顺序和字符串 min 不能证明最早。复核最早候选的原始时间值、名称及最终输出字段，缺失年代必须单独处理，不能用猜测年份、空值默认值或别的城市年代代入。',
        'execution':'用简短程序一次完成分页、断言、统计；同题已有完整计算结果时直接提交，不要重复查询。最终答案赋给全局HUNTER_ANSWER；也可让stdout只输出题目要求的一个完整JSON对象；不能夹杂调试、首页响应或说明文字，否则无法提取答案。计算最终答案时必须在cmd同级加 submit_output:true，省去最后一次模型往返。统计缺字段应明确失败。',
        'feedback':'收到官方某字段不符时，定位该字段的记录和转换逻辑并重新运行；保留其他实际计算值，不凭记忆改答案。',
        'coverage':'temporal_value_counts 为有界当前执行观察，标记 partial；须由程序根据实际分页确认全部数据。'}
    if 'oldest_era' not in text:result.pop('oldest_era')
    result['audit']='统计程序计算完成后，将本次计算的 field、definition、records_count、pages_complete、raw_time、sort_key、selected_name、output_value 写入全局字典 HUNTER_STATISTICS_AUDIT；值必须来自当前题目和实际计算，不填猜测值。运行包装器会单独记录它，不要自行打印审计字典。它是程序的自报依据，不代替完整数据断言；stdout 仍只输出题目答案。'
    if answer_only:
        result.pop('execution')
        result.pop('audit')
        result['feedback'] = '现在只可根据当前已观察的执行结果修正并提交。不能请求重新运行或提出代码计划。'
        result['coverage'] = '检查当前执行观察是否覆盖全部记录；不能从首页或采样记录猜测完整统计。'
    return result


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
