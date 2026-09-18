"""Lossless, deduplicated ordinary-news audit; independent of compact previews."""
from collections import OrderedDict
import hashlib
import json
from .protocol import fingerprint, obj
from .llm_trace import payload, decision


class NewsAudit:
    def __init__(self):
        self.sessions=OrderedDict()
        self.blobs=OrderedDict()

    def prompt(self, logger, text, data, **metadata):
        """Compact logs retain exact prompts through reusable text blocks."""
        start=text.find('{"request_id":')
        body=json.dumps(data,ensure_ascii=False)
        if logger.mode!='compact' or start<0 or text[start:]!=body:
            logger.archive('news_prompt',text,**metadata)
            return
        def block(value):
            digest=hashlib.sha256(value.encode('utf-8')).hexdigest()
            if digest not in self.blobs:
                logger.archive('news_blob',value,blob_id=digest)
            self.blobs[digest]=True
            self.blobs.move_to_end(digest)
            while len(self.blobs)>512:self.blobs.popitem(last=False)
            return ['ref',digest]
        def value_node(value):
            encoded=json.dumps(value,ensure_ascii=False)
            return block(encoded) if len(encoded)>=384 else ['value',value]
        shared_maps={'sources','evidence_sources','evidence_index','field_state',
                     'resolved_fields','runtime_state'}
        fields=[]
        for name,value in data.items():
            if name in shared_maps and isinstance(value,dict):
                node=['object',[[k,value_node(v)] for k,v in value.items()]]
            elif isinstance(value,list) and len(json.dumps(value,ensure_ascii=False))>=384:
                node=['array',[value_node(v) for v in value]]
            else:node=value_node(value)
            fields.append([name,node])
        manifest=dict(format='news_prompt_refs_v1',prefix=block(text[:start]),fields=fields,
            prompt_sha256=hashlib.sha256(text.encode('utf-8')).hexdigest(),total_chars=len(text))
        logger.archive('news_prompt_ref',json.dumps(manifest,ensure_ascii=False,separators=(',',':')),**metadata)

    def state(self, raw):
        team=obj(raw.get('teamOur'))
        key=(str(team.get('teamId','?')),str(team.get('type','?')))
        state=self.sessions.setdefault(key,dict(seen=set(),requests=set(),replies=set(),pending=None))
        self.sessions.move_to_end(key)
        while len(self.sessions)>4:self.sessions.popitem(last=False)
        return key,state

    def observe(self, logger, raw, response=None):
        if not isinstance(raw,dict):return
        key,state=self.state(raw)
        if response is None:
            for section in ('officialNews','folkLegends'):
                text=obj(raw.get('worldNews')).get(section)
                if not isinstance(text,str) or not text:continue
                identity=(section,fingerprint(text))
                if identity in state['seen']:continue
                logger.archive('news_source',text,team=key,section=section,source_hash=identity[1],
                               first_observed_round=raw.get('roundNo'))
                state['seen'].add(identity)
            text=raw.get('llmResp');pending=state.get('pending')
            if pending and isinstance(text,str) and text:
                decoded=decision(text)
                matched=decoded.get('request_id')==pending['request_id'] or (
                    decoded.get('context')==pending['context'] and decoded.get('version')==1)
                # A task response may carry private checker credentials. Only
                # dump an ordinary response or its malformed non-task reply.
                if matched or not raw.get('phaseTask'):
                    identity=(pending['request_id'],fingerprint(text))
                    if identity not in state['replies']:
                        logger.archive('news_reply',text,team=key,request_id=pending['request_id'],
                            cycle_id=pending.get('cycle_id'),matched=matched,sent_round=pending['round'])
                        state['replies'].add(identity)
                    if matched:state['pending']=None
            return
        text=response.get('prompt')
        if not isinstance(text,str) or not text:return
        data=payload(text)
        if obj(data.get('context')).get('purpose')!='news_and_treasure':
            state['pending']=None
            return
        identity=data.get('request_id')
        if identity not in state['requests']:
            self.prompt(logger,text,data,team=key,request_id=identity,
                cycle_id=data.get('cycle_id'),day=data.get('day'),analysis_stage=data.get('analysis_stage'))
            state['requests'].add(identity)
        state['pending']=data


def restore_prompt(manifest, blobs):
    """Restore an exact prompt; missing/corrupt blocks fail explicitly."""
    if manifest.get('format')!='news_prompt_refs_v1':raise ValueError('unknown prompt format')
    def read_block(digest):
        text=blobs[digest]
        if hashlib.sha256(text.encode('utf-8')).hexdigest()!=digest:raise ValueError('corrupt news block')
        return text
    def decode(node):
        kind,value=node
        if kind=='ref':return json.loads(read_block(value))
        if kind=='value':return value
        if kind=='object':return {k:decode(v) for k,v in value}
        if kind=='array':return [decode(v) for v in value]
        raise ValueError('unknown prompt node')
    text=read_block(manifest['prefix'][1])+json.dumps(
        {k:decode(v) for k,v in manifest['fields']},ensure_ascii=False)
    if (len(text)!=manifest['total_chars'] or
            hashlib.sha256(text.encode('utf-8')).hexdigest()!=manifest['prompt_sha256']):
        raise ValueError('prompt reconstruction mismatch')
    return text


def write_parts(logger, event, text, metadata):
    """Bypass fit/brief: offsets and SHA256 let logs reconstruct exact input."""
    if logger.mode=='off':return
    digest=hashlib.sha256(text.encode('utf-8')).hexdigest()
    parts=[text[i:i+2400] for i in range(0,len(text),2400)] or ['']
    with logger.output_lock:
        logger.sequence+=1
        for i,part in enumerate(parts):
            record=dict(schema=3,run=logger.run_id[:8],sdk=getattr(logger,'sdk_fingerprint',None),
                seq=logger.sequence,round=getattr(logger.local,'round',None),event=event,
                part=i+1,parts=len(parts),offset=i*2400,total_chars=len(text),
                sha256=digest,encoding='utf-8',text=part,**metadata)
            print('HUNTER '+json.dumps(record,ensure_ascii=False,separators=(',',':')),flush=True)
