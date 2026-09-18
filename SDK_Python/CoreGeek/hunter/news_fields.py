"""Incremental, independently validated facts for one treasure cycle."""
from copy import deepcopy
from .news_evidence import citations, item_evidence, location_evidence, time_window, EvidenceError
from .protocol import position

FIELDS=('items','location','time','condition')


def merge(intel, world, data, sources, reject):
    updates=data.get('field_updates',{})
    if not isinstance(updates,dict):
        reject({'field_updates':updates},'invalid_field_updates');return set()
    changed=set()
    revisions=data.get('revisions',[])
    for row in revisions if isinstance(revisions,list) else []:
        if not isinstance(row,dict) or row.get('field') not in FIELDS:continue
        try:refs=citations(row,sources,'revisions.support')
        except EvidenceError as exc:reject(row,exc.reason);continue
        if not isinstance(row.get('reason'),str) or not row['reason'].strip():continue
        name=row['field']
        intel.resolved_fields.pop(name,None)
        intel.treasures=[]
        if name=='items':intel.preparations=[]
        intel.rejections.append(dict(field=name,support=refs,reason=row['reason'],round=world.round))
    for name,spec in updates.items():
        if name not in FIELDS or not isinstance(spec,dict):continue
        try:
            refs=citations(spec,sources,f'field_updates.{name}.support')
            if spec.get('confidence')!='high':raise EvidenceError('unresolved_conditions',name)
            candidate=dict(spec,evidence_version=3)
            if name=='items':
                value=spec.get('items')
                if not isinstance(value,list) or not 1<=len(value)<=40 or any(not isinstance(v,str) or v not in world.shop for v in value):
                    raise EvidenceError('unknown_shop_item',name)
                item_evidence(candidate,sources);value=sorted(value)
            elif name=='location':
                candidate['location']=spec
                value=location_evidence(candidate,sources,world)
            elif name=='time':
                value=time_window(dict(time=spec),sources,world.strategy_clock)
            else:
                if spec.get('resolved') is not True:raise EvidenceError('unresolved_conditions',name)
                value=True
            old=intel.resolved_fields.get(name)
            if old and old['value']!=value:
                # Citation wording may change without changing the conclusion.
                old_value=old['value']
                equivalent=(name=='location' and tuple(old_value.get('position',()))==tuple(value.get('position',()))
                    or name=='time' and all(old_value.get(k)==value.get(k) for k in ('opening_round','execution_window_end','phase','official_expiry')))
                if not equivalent:raise EvidenceError('field_revision_requires_counterevidence',name)
            intel.resolved_fields[name]=dict(value=value,spec=deepcopy(spec),evidence=refs,
                status='inferred' if name=='location' and spec.get('mode')=='inferred' else 'validated_hypothesis',
                version=world.round)
            changed.add(name)
        except (EvidenceError,TypeError,ValueError) as exc:
            reject(dict(field=name,field_update=spec),exc.reason if isinstance(exc,EvidenceError) else 'invalid_field_shape')
    return changed


def candidate(intel):
    facts=intel.resolved_fields
    if not all(k in facts for k in FIELDS):return None
    loc=facts['location'];items=facts['items'];timing=facts['time']
    refs=[]
    for fact in facts.values():
        for ref in fact['evidence']:
            if ref not in refs:refs.append(ref)
    return dict(evidence_version=3,position=dict(zip(('x','y'),loc['value']['position'])),
        items=list(items['value']),item_evidence=deepcopy(items['spec']['item_evidence']),
        location=deepcopy(loc['spec']),time=deepcopy(timing['spec']),support=refs,
        confidence='high',all_conditions_resolved=True)


def adopt(intel, world, candidate, *, location=None, window=None):
    """Retain the proofs already checked by the legacy full-plan parser."""
    if candidate.get('item_evidence'):
        intel.resolved_fields['items']=dict(value=sorted(candidate['items']),
            spec=dict(items=sorted(candidate['items']),item_evidence=deepcopy(candidate['item_evidence']),
                      support=deepcopy(candidate['support']),confidence='high'),
            evidence=deepcopy(candidate['support']),status='validated_hypothesis',version=world.round)
    if location and candidate.get('location'):
        intel.resolved_fields['location']=dict(value=dict(location,position=tuple(position(candidate['position']))),
            spec=dict(deepcopy(candidate['location']),position=deepcopy(candidate['position']),confidence='high'),
            evidence=deepcopy(location['support']),status='inferred' if location['mode']=='inferred' else 'validated_hypothesis',version=world.round)
    if window and candidate.get('time'):
        intel.resolved_fields['time']=dict(value=window,spec=dict(deepcopy(candidate['time']),confidence='high'),
            evidence=deepcopy(window['support']),status='validated_hypothesis',version=world.round)
    if location and window:
        intel.resolved_fields['condition']=dict(value=True,spec=dict(resolved=True,support=deepcopy(candidate['support']),confidence='high'),
            evidence=deepcopy(candidate['support']),status='validated_hypothesis',version=world.round)


def refresh(intel, world):
    values={'items':[p['items'] for p in intel.treasures or intel.preparations],
            'location':[p['position'] for p in intel.treasures],
            'time':[p.get('time_window',dict(opening_round=p['opening_round'],closing_round=p['closing_round'])) for p in intel.treasures],
            'condition':[True] if intel.treasures else []}
    result={}
    for name in FIELDS:
        fact=intel.resolved_fields.get(name)
        result[name]=(dict(status=fact['status'],value=fact['value'],candidates=[fact['value']],
                          evidence=fact['evidence'],missing_reason=[],version=fact['version']) if fact else
            dict(status='validated_hypothesis' if values[name] else 'unresolved',
                 value=values[name][0] if len(values[name])==1 else None,candidates=values[name],
                 evidence=[c for c in intel.clues if c['kind']==name],
                 missing_reason=[] if values[name] else intel.unresolved_fields.get(name,['No supported field yet']),version=world.round))
    intel.field_state=result
