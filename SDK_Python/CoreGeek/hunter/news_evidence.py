"""Typed exact citations and deterministic calendar windows for ordinary news."""
import json
import re
from .protocol import fingerprint


class EvidenceError(ValueError):
    def __init__(self, reason, path, source=None):
        self.reason,self.path,self.source=reason,path,source
        super().__init__(reason+': '+path)


def registry(news, descriptions, world):
    result={key:dict(row,evidence_kind='news_fragment') for key,row in news.items()}
    for name,text in descriptions.items():
        if name not in world.shop:continue
        key='catalog:'+name+':'+fingerprint(text)[:12]
        result[key]=dict(text=text,hash=fingerprint(text),evidence_kind='catalog_description',item=name)
    rules=dict(origin='bottom_left',origin_coordinates=[0,0],x_positive='right',y_positive='up',distance='chebyshev',
               width=world.width,height=world.height,station_anchor='top_left',
               bases=[dict(id=u.id,side=side,x=u.pos[0],y=u.pos[1]) for side,units in ((world.side,world.ours),('enemy',world.enemies)) for u in units.values() if u.kind=='station' and u.alive],
               zones={k:sorted(v) for k,v in world.zones.items()})
    text=json.dumps(rules,ensure_ascii=False,sort_keys=True)
    result['map:'+fingerprint(text)[:12]]=dict(text=text,hash=fingerprint(text),evidence_kind='map_rule',
        bases=rules['bases'],origin_coordinates=[0,0],zones=rules['zones'])
    for key,row in result.items():
        # Stable exact spans let the model select evidence instead of copying it.
        row['spans']={f'{key}#{i}':dict(start=m.start(),end=m.end(),text=m.group())
                      for i,m in enumerate(re.finditer(r'[^。！？\n]+[。！？]?|\n',row['text'])) if m.group().strip()}
    return result


def citations(value, sources, path='support', require_news=True):
    refs=value.get('support')
    if not isinstance(refs,list) or not refs:raise EvidenceError('missing_citations',path)
    accepted=[];news=False
    for i,ref in enumerate(refs):
        where=f'{path}[{i}]'
        if not isinstance(ref,dict):raise EvidenceError('invalid_citation_shape',where)
        key=ref.get('source',ref.get('source_id'));entry=sources.get(key)
        if entry is None:raise EvidenceError('unknown_source_id',where,key)
        text=entry['text'];quote=ref.get('quote')
        if 'span' in ref:
            span=entry.get('spans',{}).get(ref['span'])
            if not span:raise EvidenceError('unknown_span_id',where,key)
            quote=text[span['start']:span['end']]
        if 'start' in ref or 'end' in ref:
            start,end=ref.get('start'),ref.get('end')
            if type(start) is not int or type(end) is not int or not 0<=start<end<=len(text):
                raise EvidenceError('invalid_span',where,key)
            actual=text[start:end]
            if quote is not None and quote!=actual:raise EvidenceError('quote_not_exact',where,key)
            quote=actual
        if not isinstance(quote,str) or not quote.strip() or quote not in text:
            raise EvidenceError('quote_not_exact',where,key)
        if ref.get('text_hash') is not None and ref['text_hash']!=fingerprint(quote):
            raise EvidenceError('span_hash_mismatch',where,key)
        news|=entry.get('evidence_kind','news_fragment')=='news_fragment'
        accepted.append(dict(source=key,quote=quote))
    if require_news and not news:raise EvidenceError('news_condition_unproved',path)
    return accepted


def item_evidence(candidate,sources):
    """V2 requires both the news condition and the actual catalog identity."""
    if candidate.get('evidence_version') not in (2,3):return
    rows=candidate.get('item_evidence')
    if not isinstance(rows,list):raise EvidenceError('item_mapping_unproved','item_evidence')
    from collections import Counter
    proved=Counter()
    for i,row in enumerate(rows):
        if not isinstance(row,dict):raise EvidenceError('item_mapping_unproved',f'item_evidence[{i}]')
        name,n=row.get('name'),row.get('quantity')
        if not isinstance(name,str) or type(n) is not int or n<1:raise EvidenceError('item_mapping_unproved',f'item_evidence[{i}]')
        refs=citations(row,sources,f'item_evidence[{i}].support')
        if not any(sources[r['source']].get('item')==name for r in refs):
            raise EvidenceError('catalog_mapping_unproved',f'item_evidence[{i}]')
        proved[name]+=n
    if proved!=Counter(candidate.get('items',[])):raise EvidenceError('item_quantity_mismatch','items')


def time_window(candidate,sources,clock):
    spec=candidate.get('time')
    if not isinstance(spec,dict) or not clock:return None
    refs=citations(spec if spec.get('support') else candidate,sources,'time.support')
    quote=' '.join(r['quote'] for r in refs if sources[r['source']].get('evidence_kind','news_fragment')=='news_fragment')
    day=spec.get('day');anchor=spec.get('anchor','absolute_day')
    if anchor=='publication_day':
        offset=spec.get('day_offset');entries=[sources[r['source']] for r in refs if sources[r['source']].get('evidence_kind','news_fragment')=='news_fragment']
        days={e.get('observed_day') for e in entries if e.get('publication_certain')}
        if type(offset) is not int or not entries or any(not e.get('publication_certain') for e in entries) or len(days)!=1 or None in days:
            raise EvidenceError('publication_day_unknown','time')
        if offset!=1 or not re.search(r'明天|明日|次日|tomorrow',quote,re.I):raise EvidenceError('relative_time_unproved','time')
        day=next(iter(days))+offset
    elif anchor!='absolute_day':raise EvidenceError('unknown_time_anchor','time.anchor')
    if type(day) is not int or not 1<=day<=10:raise EvidenceError('invalid_day','time.day')
    chinese=('一','二','三','四','五','六','七','八','九','十')[day-1]
    if anchor=='absolute_day' and not re.search(r'第(?:'+str(day)+'|'+chinese+r')(?:日|天)|day\s+'+str(day)+r'\b',quote,re.I):
        raise EvidenceError('day_not_in_source','time.day')
    phase=spec.get('phase','day');mode=spec.get('mode',spec.get('relation','within'))
    if phase not in {'day','night','all'} or mode not in {'within','from_start','onward'}:
        raise EvidenceError('invalid_time_semantics','time')
    if phase=='day' and not re.search(r'白昼|白天|daylight|daytime',quote,re.I):raise EvidenceError('phase_not_in_source','time.phase')
    if phase=='night' and not re.search(r'夜|night',quote,re.I):raise EvidenceError('phase_not_in_source','time.phase')
    if mode!='within' and not re.search(r'起|开始|之后|以后|方可|才能|from|onward',quote,re.I):raise EvidenceError('onward_not_in_source','time.mode')
    begin=(day-1)*130+(70 if phase=='night' else 0)
    end=(day-1)*130+(69 if phase=='day' else 129)
    start=max(o+begin for o in clock.offsets)
    stop=min(o+end for o in clock.offsets) if mode=='within' else 1300
    return dict(opening_round=start,execution_window_end=stop,day=day,phase=phase,mode=mode,
                basis='intersection_of_origin_candidates',official_expiry=False,support=refs)


def location_evidence(candidate,sources,world):
    """Validate explicit derivations or a model's evidenced semantic inference.

    V3 accepts inferred units; arithmetic, anchors and map bounds remain strict.
    """
    if candidate.get('evidence_version') not in (2,3):return None
    spec=candidate.get('location')
    if not isinstance(spec,dict):raise EvidenceError('location_derivation_missing','location')
    refs=citations(spec,sources,'location.support')
    text=' '.join(r['quote'] for r in refs if sources[r['source']].get('evidence_kind','news_fragment')=='news_fragment')
    def pairs(text):
        return {(int(m[1]),int(m[2])) for m in re.finditer(r'[（(]\s*(\d+)\s*[,，]\s*(\d+)\s*[）)]',text)}
    def cn(n):
        ones='零一二三四五六七八九'
        return ones[n] if n<10 else ('十' if n==10 else ('' if n//10==1 else ones[n//10])+'十'+(ones[n%10] if n%10 else ''))
    mode=spec.get('mode');point=candidate.get('position',{})
    wanted=(point.get('x'),point.get('y')) if isinstance(point,dict) else None
    if mode=='inferred' and candidate.get('evidence_version')==3:
        reference=spec.get('reference',{})
        if isinstance(reference,str):reference={'kind':reference}
        if not isinstance(reference,dict):raise EvidenceError('reference_unknown','location.reference')
        anchor_refs=citations(reference,sources,'location.reference.support',require_news=False)
        maps=[sources[r['source']] for r in anchor_refs if sources[r['source']].get('evidence_kind')=='map_rule']
        if reference.get('kind')=='map_origin' and any(m.get('origin_coordinates')==[0,0] for m in maps):
            anchor=(0,0)
        else:
            anchor=(reference.get('x'),reference.get('y'))
            mapped=any((b['x'],b['y'])==anchor and b['id']==reference.get('base_id') for m in maps for b in m.get('bases',[]))
            mapped|=any(anchor in [tuple(p) for p in m.get('zones',{}).get(reference.get('zone'),[])] for m in maps)
            quoted=' '.join(r['quote'] for r in anchor_refs if sources[r['source']].get('evidence_kind','news_fragment')=='news_fragment')
            if not mapped and pairs(quoted)!={anchor}:raise EvidenceError('reference_not_unique','location.reference')
        displacement=spec.get('grid_displacement',{})
        if not isinstance(displacement,dict):raise EvidenceError('displacement_unknown','location.grid_displacement')
        east,north=displacement.get('east'),displacement.get('north')
        if any(type(v) is not int for v in (*anchor,east,north)):
            raise EvidenceError('displacement_unknown','location.grid_displacement')
        summary=spec.get('reason_summary')
        if not isinstance(summary,str) or not 1<=len(summary.strip())<=1500 or spec.get('confidence')!='high':
            raise EvidenceError('inference_explanation_required','location')
        resolved=(anchor[0]+east,anchor[1]+north)
        if not world.inside(resolved) or resolved!=wanted:
            raise EvidenceError('coordinate_derivation_mismatch','location')
        alternatives=spec.get('alternatives',[])
        if not isinstance(alternatives,list) or any(isinstance(a,dict) and a.get('confidence')=='high'
                and a.get('position')!=point for a in alternatives):
            raise EvidenceError('competing_location_interpretations','location.alternatives')
        return dict(mode=mode,position=resolved,reference=anchor,east=east,north=north,
                    basis='model_semantic_inference',reason_summary=summary,support=refs,
                    reference_support=anchor_refs,alternatives=alternatives[:4])
    if mode=='absolute':
        positions=pairs(text)
        if positions!={wanted}:raise EvidenceError('absolute_coordinate_not_unique','location')
        return dict(mode=mode,position=wanted,support=refs)
    if mode!='relative':raise EvidenceError('unknown_location_derivation','location.mode')
    reference=spec.get('reference',{})
    anchor=(reference.get('x'),reference.get('y')) if isinstance(reference,dict) else None
    if anchor is None or any(type(v) is not int for v in anchor):raise EvidenceError('reference_unknown','location.reference')
    anchor_refs=citations(reference,sources,'location.reference.support',require_news=False)
    anchor_text=' '.join(r['quote'] for r in anchor_refs if sources[r['source']].get('evidence_kind','news_fragment')=='news_fragment')
    mapped=any(b['id']==reference.get('base_id') and (b['x'],b['y'])==anchor
        for ref in anchor_refs for b in sources[ref['source']].get('bases',[])
        if sources[ref['source']].get('evidence_kind')=='map_rule')
    mapped|=reference.get('kind')=='map_origin' and anchor==(0,0) and any(
        sources[r['source']].get('origin_coordinates')==[0,0] for r in anchor_refs)
    if not mapped and pairs(anchor_text)!={anchor}:raise EvidenceError('reference_not_unique','location.reference')
    east,north=spec.get('east'),spec.get('north');unit=spec.get('unit');scale=spec.get('cells_per_unit')
    if any(type(v) is not int or abs(v)>max(world.width,world.height) for v in (east,north)):
        raise EvidenceError('displacement_unknown','location')
    if unit=='grid':scale=1
    elif unit=='km':
        if type(scale) is not int or not 1<=scale<=max(world.width,world.height):raise EvidenceError('unit_scale_unknown','location.cells_per_unit')
        if not re.search(r'(?:1|一)公里\s*(?:=|等于|对应|为)\s*(?:'+str(scale)+'|'+cn(scale)+r')格',text):
            raise EvidenceError('unit_scale_not_in_source','location.cells_per_unit')
    else:raise EvidenceError('unit_unknown','location.unit')
    for number,positive,negative in ((east,'东','西'),(north,'北','南')):
        if not number:continue
        n=abs(number);direction=positive if number>0 else negative;suffix='格' if unit=='grid' else '公里'
        if not re.search(direction+r'(?:方|边|侧|方向)?\s*(?:'+str(n)+'|'+cn(n)+r')\s*'+suffix,text):
            raise EvidenceError('displacement_not_in_source','location')
    resolved=(anchor[0]+east*scale,anchor[1]+north*scale)
    candidates=[(x,y) for x in range(world.width) for y in range(world.height) if (x,y)==resolved]
    if len(candidates)!=1 or resolved!=wanted:raise EvidenceError('coordinate_derivation_mismatch','location')
    return dict(mode=mode,reference=anchor,east=east,north=north,unit=unit,cells_per_unit=scale,
                candidates=candidates,support=refs,reference_support=anchor_refs)
