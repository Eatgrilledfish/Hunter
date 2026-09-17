"""Source-backed news hypotheses and explicitly budgeted treasure attempts."""
from collections import Counter
from dataclasses import dataclass, field
import json
import re
import time

from .arbitration import Candidate
from .economy import movement
from .navigation import route, distance_field, interaction_cells, neighbours
from .protocol import obj, integer, position, pos_json, fingerprint, strict_json
from .tasks import llm_service_failure
from .news_cycle import NewsCycle
from .rules import Policy


# Taskbook 4.6.3 descriptions. The live shop controls availability and price.
OFFERING_DESCRIPTIONS = {
    'AcientTablet':'古符石板：灰白色石板，刻有无法辨认的远古文字，触摸时发出微弱嗡鸣。',
    'StarSand':'星辰之沙：银白色细沙，在暗处自行闪烁冷光。',
    'FlameBreath':'烈焰之息：透明晶石瓶，内封一团橙红色雾气，轻轻摇晃时发出微弱光亮。',
    'FrostPotion':'寒霜药剂：深蓝色粘稠液体，瓶口凝结薄霜，靠近时有刺骨寒意。',
    'ThornAmulet':'荆棘护符：活藤蔓编织的圆形护符，表面带细小尖刺，散发草木气味。',
    'IronWhistle':'回音铁哨：生铁小哨，锈色斑驳，晃动时有金属碎片轻响。',
}


@dataclass
class Intelligence:
    cycle: NewsCycle = field(default_factory=NewsCycle)
    preparations: list = field(default_factory=list)
    preparation_trip: dict = field(default_factory=dict)
    return_plan: dict = field(default_factory=dict)
    pending: dict | None = None
    analyzed: set = field(default_factory=set)
    events: list = field(default_factory=list)
    treasures: list = field(default_factory=list)
    attempts: list = field(default_factory=list)
    purchases: list = field(default_factory=list)
    treasure_spent: int = 0
    terminal: str | None = None
    seq: int = 0
    news_complete: bool = True
    rejections: list = field(default_factory=list)
    clues: list = field(default_factory=list)
    pending_purchases: dict = field(default_factory=dict)
    reviewed_attempts: set = field(default_factory=set)
    synthesized_sources: set = field(default_factory=set)
    reviewed_sources: set = field(default_factory=set)
    review_attempts: dict = field(default_factory=dict)
    treasure_complete: bool = True
    diagnostic: dict = field(default_factory=dict)
    llm_status: str = "idle"
    llm_diagnostic: dict = field(default_factory=dict)
    unresolved: list = field(default_factory=list)
    execution: dict = field(default_factory=dict)
    offered_plan: dict = field(default_factory=dict)
    execution_blockers: dict = field(default_factory=dict)
    invalid_candidates: list = field(default_factory=list)
    validation_reviews: dict = field(default_factory=dict)

    @staticmethod
    def news_id(entry):
        return f"news:{entry['observed_round']}:{entry['section']}:{entry['hash'][:12]}"

    @classmethod
    def source_parts(cls, entries):
        sources = {}
        for entry in entries:
            if entry['text'] in {'今日无重大新闻', '无', ''}:
                continue
            identity = cls.news_id(entry)
            text = entry['text']
            if len(text) <= 8000:
                sources[identity] = entry
                continue
            # Exact, disjoint source slices; offsets permit reconstructing the
            # retained original. No generated summary replaces counterevidence.
            for offset in range(0, len(text), 8000):
                sources[f'{identity}:{offset}'] = dict(entry, text=text[offset:offset+8000],
                    parent_source=identity, offset=offset, retained_chars=len(text),
                    is_fragment=True)
        return sources

    def reconcile(self, world, clock, session):
        policy = getattr(world, 'strategy_policy', Policy())
        trip = self.preparation_trip
        carrier = world.ours.get(trip.get('actor'))
        if (trip and trip.get('phase') not in {'completed','handed_over'}
                and carrier and carrier.alive and carrier.backpack is not None
                and carrier.id not in self.pending_purchases
                and carrier.pos == tuple(trip['home'])
                and not (Counter(trip['items'])-carrier.inventory)):
            trip.update(phase='completed',completed_round=world.round)
            if self.execution.get('phase') == 'prepare':self.execution.clear()
        if policy.news_daily_enabled:
            self.cycle.entries(session.news, clock)
        for identity, purchase in list(self.pending_purchases.items()):
            actor=world.ours.get(identity)
            feedback=obj(world.raw.get('lastRoundRoleActionResults'))
            delta = max(0, actor.inventory[purchase['item']]-purchase['prior']) if actor and actor.backpack is not None else 0
            requested = purchase.get('requested_num',1)
            arrived=delta >= requested
            failed=world.round==purchase['round']+1 and feedback.get(identity) is False
            settled=world.round==purchase['round']+1 and feedback.get(identity) is True
            purchase.update(observed_delta=delta,settlement_status='complete' if arrived else
                            'partial' if delta else 'failed' if failed else 'unknown')
            if settled and 0 < delta < requested:
                self.treasure_spent -= purchase['reserved_cost']*(requested-delta)//requested
                self.pending_purchases.pop(identity)
                continue
            if arrived or failed:
                if failed and not arrived and not delta:self.treasure_spent-=purchase['reserved_cost']
                self.pending_purchases.pop(identity)
        if self.attempts:
            latest = self.attempts[-1]
            if latest["round"] == world.round-1 and latest.get("result") is None:
                result = world.raw.get("lastSummonTreasureResult")
                if integer(result) and 0 <= result <= 4:
                    latest["result"] = result
                    if result in {1, 4}:
                        self.terminal = "success" if result == 1 else "empty"
                        if policy.news_daily_enabled:
                            self.cycle.close(self,clock)
        if self.pending:
            raw = world.raw.get("llmResp")
            self.llm_diagnostic = {'sent':self.pending['round'], 'reply_type':type(raw).__name__,
                                   'reply_chars':len(raw) if isinstance(raw,str) else 0,
                                   'analysis_stage':self.pending.get('analysis_stage','read_clues')}
            if isinstance(raw, str) and raw and len(raw) <= 32768:
                try:
                    try:data = strict_json(raw.strip())
                    except ValueError:
                        blocks=re.findall(r'```(?:json)?[ \t]*\n(.*?)\n```',raw,re.S)
                        if len(blocks)!=1:raise ValueError('not one JSON object or fenced block')
                        data=strict_json(blocks[0])
                    if not isinstance(data,dict):raise ValueError('response is not an object')
                    if 'context' in data:
                        matched=(data['context']==self.pending['context'] and type(data.get('version')) is int
                                 and data['version']==1 and ('request_id' not in data or
                                 data['request_id']==fingerprint(self.pending['context']['nonce'])[:16]))
                    else:
                        matched=data.get('request_id')==fingerprint(self.pending['context']['nonce'])[:16]
                    if not matched:
                        raise ValueError("news response mismatch")
                    if self.pending.get('cycle_id', self.cycle.number) != self.cycle.number:
                        raise ValueError('closed news cycle')
                    data.setdefault('events',[])
                    data.setdefault('treasures',[])
                    if not isinstance(data.get("events"), list) or not isinstance(data.get("treasures"), list):
                        raise ValueError("invalid news collections")
                    if policy.news_daily_enabled:
                        if data.get('decision','WAIT_INFO') not in {'WAIT_INFO','BUY_READY','DIG_READY'}:
                            raise ValueError('invalid news decision')
                        # Each new accepted analysis revises the actionable plan;
                        # an empty conclusion cannot leave yesterday's plan live.
                        self.treasures.clear()
                        self.preparations.clear()
                    self._ingest(world, data, self.pending.get("citation_sources",self.pending["sources"]))
                    trip=self.preparation_trip
                    if (policy.news_daily_enabled and not self.preparations and not self.treasures
                            and trip and trip.get('phase') not in {'completed','handed_over'}):
                        self.return_plan=dict(actor=trip['actor'],home=trip['home'])
                        trip.update(phase='handed_over',reason='preparation withdrawn; retain physical return')
                        if self.execution.get('phase')=='prepare':self.execution.clear()
                    self.analyzed.update(self.pending["sources"])
                    self.reviewed_sources.update(self.pending.get('review_sources',()))
                    if self.pending.get('synthesis_corpus'):
                        self.synthesized_sources.add(self.pending['synthesis_corpus'])
                    reviewed=set(self.pending.get('validation_ids',()))
                    self.invalid_candidates=[v for v in self.invalid_candidates if v['id'] not in reviewed]
                    self.pending = None
                    if policy.news_daily_enabled:
                        self.cycle.status = ('dig_ready' if self.treasures else 'buy_ready' if self.preparations else 'wait_info')
                    self.llm_status = ('accepted_without_plan' if self.llm_diagnostic.get('rejected')
                                       and not self.llm_diagnostic.get('progress',{}).get('new_hypotheses') else 'accepted')
                except (ValueError, TypeError, KeyError) as exc:
                    self.llm_status = "invalid_response"
                    self.llm_diagnostic['reason']=str(exc)[:100]
                    self.pending['rejection']=self.llm_diagnostic.copy()
                    if world.round > self.pending["round"]+2:
                        self.pending = None
            elif not raw and llm_service_failure(world,self.pending):
                self.llm_status='service_failure'
                self.llm_diagnostic['reason']='platform LLM request failed; reservation remains consumed'
                self.pending=None
            elif world.round > self.pending["round"]+2:
                self.llm_status = 'oversized_response' if isinstance(raw,str) and len(raw)>32768 else 'missing_response'
                if self.pending.get('rejection'):
                    self.llm_status='invalid_response'
                    self.llm_diagnostic=self.pending['rejection']
                self.pending = None
        if session.tasks.active:
            # Active task takes the channel; a late ordinary nonce cannot satisfy
            # any task pending. The consumed ordinary reservation is not refunded.
            if self.pending:
                self.cycle.retired.add(self.pending['context']['nonce'])
                self.llm_status='interrupted'
            self.pending = None
        sources = self.source_parts(self.cycle.entries(session.news,clock) if policy.news_daily_enabled else session.news)
        self.news_complete = (set(sources) <= self.analyzed and
                              not any(n['truncated_locally'] for n in sources.values()))
        folk={k:v for k,v in sources.items() if v['section']=='folkLegends'}
        self.treasure_complete=(set(folk)<=self.analyzed and not any(n['truncated_locally'] for n in folk.values()))

    def _ingest(self, world, data, sources):
        prior_clues={c['id'] for c in self.clues}
        prior_hypotheses={h['id'] for h in self.treasures}
        rejected=Counter()
        expected={
            'unsupported_source':'support[].source must be a supplied source ID; quote must be an exact substring of that source',
            'position_or_items':'position is an in-bounds integer {x,y}; items is a nonempty exact quantity list, at most 40',
            'unknown_shop_item':'items must use exact IDs from the current shop and offering descriptions',
            'opening_window':'opening_round is an absolute integer round; optional closing_round >= opening_round, horizon <=1300',
            'unresolved_conditions':'confidence must be high and all_conditions_resolved true, only when supported by original clues'}
        def reject(candidate,reason):
            rejected[reason]+=1
            encoded=json.dumps(candidate,ensure_ascii=False)
            sample=(candidate if isinstance(candidate,dict) and len(encoded)<=6000 else
                    dict(type=type(candidate).__name__,sample=encoded[:600],truncated=len(encoded)>600))
            record=dict(id=fingerprint(candidate),candidate=sample,reason=reason,
                expected=expected[reason],round=world.round)
            self.invalid_candidates=[v for v in self.invalid_candidates if v['id']!=record['id']]
            self.invalid_candidates=(self.invalid_candidates+[record])[-4:]

        unresolved=data.get('unresolved',[])
        if isinstance(unresolved,list):
            self.unresolved=[s[:200] for s in unresolved[:6] if isinstance(s,str)]
        def support(value):
            citations = value.get("support")
            if not isinstance(citations, list) or not citations:
                return None
            accepted = []
            for citation in citations:
                if not isinstance(citation, dict):
                    return None
                entry = sources.get(citation.get("source"))
                quote = citation.get("quote")
                if entry is None or not isinstance(quote, str) or not quote.strip() or quote not in entry["text"]:
                    return None
                accepted.append(citation)
            return accepted
        clues=data.get('clues',[])
        for clue in (clues[:32] if isinstance(clues,list) else []):
            if not isinstance(clue,dict):continue
            refs=support(clue);text=clue.get('text');kind=clue.get('kind')
            if (not refs or not isinstance(text,str) or not 1<=len(text)<=500
                    or kind not in {'location','items','time','condition','contradiction'}):continue
            record={'kind':kind,'text':text,'support':refs}
            record['id']=fingerprint(record)
            if not any(c['id']==record['id'] for c in self.clues):self.clues.append(record)
        if not getattr(world,'strategy_policy',Policy()).news_daily_enabled:
            self.clues=self.clues[-64:]
        for rejection in data.get('rejections', [])[:16]:
            if not isinstance(rejection, dict):
                continue
            refs = support(rejection)
            identity = rejection.get('hypothesis_id')
            if refs and any(h.get('id') == identity for h in self.treasures + self.events):
                self.rejections.append({'hypothesis_id': identity, 'support': refs,
                                        'basis': 'model_counterevidence'})
                self.treasures = [h for h in self.treasures if h['id'] != identity]
                self.events = [e for e in self.events if e.get('id') != identity]
        self.rejections = self.rejections[-32:]
        for event in data.get("events", [])[:16]:
            if not isinstance(event, dict) or event.get("resource") not in {"stone", "iron", "copper"}:
                continue
            refs = support(event)
            start, end = event.get("start_offset"), event.get("end_offset")
            if refs is None or not integer(start, 0) or not integer(end, 0) or end < start or end > 10:
                continue
            if event.get("effect") not in {"closed", "restored", "price_up", "price_down"}:
                continue
            publications = {sources[c["source"]]["observed_day"] for c in refs if sources[c["source"]]["publication_certain"]}
            all_certain = all(sources[c["source"]]["publication_certain"] for c in refs)
            day = next(iter(publications)) if all_certain and len(publications) == 1 else None
            record = {"resource": event["resource"], "effect": event["effect"], "start_offset": start,
                      "end_offset": end, "start_day": day+start if day else None,
                      "end_day": day+end if day else None, "support": refs, "basis": "model_hypothesis"}
            record['id'] = fingerprint(record)
            if (record not in self.events and
                    not any(r['hypothesis_id'] == record['id'] for r in self.rejections)):
                self.events.append(record)
        for candidate in (data.get("treasures", [])[:8] if data.get('decision') in (None,'DIG_READY') else []):
            if not isinstance(candidate, dict) or support(candidate) is None:
                reject(candidate,'unsupported_source')
                continue
            pos, items = position(candidate.get("position")), candidate.get("items")
            opening = candidate.get("opening_round")
            # The taskbook specifies an opening condition, not a mandatory
            # expiry. 1300 is the half's maximum horizon, never a claimed
            # treasure closing time. Preserve an explicitly supplied expiry.
            closing = candidate.get('closing_round')
            expiry_known = closing is not None
            if not expiry_known:
                closing = 1300
            if pos is None or not world.inside(pos) or not isinstance(items, list) or not items or len(items) > 40:
                reject(candidate,'position_or_items')
                continue
            if any(not isinstance(x, str) or x not in world.shop for x in items):
                reject(candidate,'unknown_shop_item')
                continue
            if not integer(opening, 0) or not integer(closing, opening) or closing-opening > 1300:
                reject(candidate,'opening_window')
                continue
            if candidate.get("confidence") != "high" or candidate.get("all_conditions_resolved") is not True:
                reject(candidate,'unresolved_conditions')
                continue
            record = {"position": pos, "items": sorted(items), "opening_round": opening, "closing_round": closing,
                      "support": support(candidate), "basis": "model_hypothesis_not_official", "confidence": "high"}
            if not expiry_known:
                record['closing_source'] = 'half_horizon_not_treasure_expiry'
            record["id"] = fingerprint(record)
            if (not any(t["id"] == record["id"] for t in self.treasures) and
                    not any(r['hypothesis_id'] == record['id'] for r in self.rejections)):
                self.treasures.append(record)
                self.invalid_candidates=[]
        for candidate in (data.get('purchases',[])[:8] if data.get('decision') in (None,'BUY_READY') else []):
            if not isinstance(candidate,dict) or support(candidate) is None:
                reject(candidate,'unsupported_source');continue
            items=candidate.get('items')
            if not isinstance(items,list) or not 1<=len(items)<=40:
                reject(candidate,'position_or_items');continue
            if any(not isinstance(x,str) or x not in world.shop for x in items):
                reject(candidate,'unknown_shop_item');continue
            if candidate.get('confidence')!='high' or candidate.get('all_item_conditions_resolved') is not True:
                reject(candidate,'unresolved_conditions');continue
            record=dict(items=sorted(items),support=support(candidate),basis='model_hypothesis_not_official')
            record['id']=fingerprint(record)
            if record not in self.preparations:self.preparations.append(record)
        self.events, self.treasures = self.events[-64:], self.treasures[-16:]
        self.llm_diagnostic.update(hypotheses=len(self.treasures),proposed=len(data.get('treasures',[])),
                                   rejected=dict(rejected),unresolved=self.unresolved,
                                   progress=dict(new_clues=len({c['id'] for c in self.clues}-prior_clues),
                                       new_hypotheses=len({h['id'] for h in self.treasures}-prior_hypotheses)))

    def constraint_state(self, sources):
        """Attributed extraction evidence, explicitly distinct from a solved plan."""
        terms={'location':r'地点|坐标|方位|位置|location|position|coordinate',
               'items':r'祭品|物品|数量|星辰|石板|items?|offering',
               'time':r'时间|开启|黎明|夜晚|日落|日期|time|opening|day|night',
               'condition':r'条件|禁止|不得|不能|condition|forbid|unless'}
        unresolved=' '.join(self.unresolved)
        result={}
        for kind,pattern in terms.items():
            clues=[c for c in self.clues if c['kind']==kind]
            spans=[]
            for clue in clues:
                for ref in clue['support']:
                    entry=sources.get(ref['source'])
                    if not entry or ref['quote'] not in entry['text']:continue
                    offset=entry.get('offset',0)+entry['text'].index(ref['quote'])
                    spans.append(dict(source=ref['source'],parent_source=entry.get('parent_source',ref['source']),
                        offset=offset,end=offset+min(512,len(ref['quote'])),quote=ref['quote'][:512],
                        excerpt=len(ref['quote'])>512,derivation=clue['text'][:200]))
            missing=not clues or bool(re.search(pattern,unresolved,re.I))
            result[kind]=dict(status=('unresolved' if self.treasure_complete else 'unread') if missing else 'evidence_collected',
                              evidence_spans=spans[-3:])
        return result,terms

    def hold_ore(self, mineral, clock):
        if not self.news_complete or clock.day is None:
            return False
        for event in self.events:
            if (event['resource'] != mineral or event['effect'] != 'price_up' or
                    event['start_day'] is None or not clock.day < event['start_day'] <= clock.day+2):
                continue
            # Keep both evidence records. Unknown timing cannot establish that
            # an opposing forecast is disjoint; no unsupported tie-breaking.
            conflicting = any(other['resource'] == mineral and other['effect'] == 'price_down' and
                (other['start_day'] is None or other['end_day'] is None or
                 max(event['start_day'], other['start_day']) <= min(event['end_day'], other['end_day']))
                for other in self.events)
            if not conflicting:
                return True
        return False

    def candidates(self, world, clock, policy, deadline):
        clear_night=clock.phases=={'night'} and getattr(world,'own_wave_cleared',False)
        remaining=min(130-(world.round-o)%130 for o in clock.offsets)
        work_remaining=(remaining+(70 if clock.day is not None and clock.day<10 else 0)
                        if clear_night else clock.until_night)
        self.offered_plan={}
        self.diagnostic={'stage':'inactive','retained_plan':dict(self.execution)}
        if self.return_plan and not world.phase_task and world.phase_task_observed:
            actor=world.ours.get(self.return_plan['actor'])
            if actor and actor.alive:
                goal=tuple(self.return_plan['home'])
                if actor.pos==goal:
                    self.return_plan={}
                else:
                    back=distance_field(world,{goal},actor.pos,deadline)
                    steps=[p for p in neighbours(actor.pos) if back.get(p,float('inf'))<back.get(actor.pos,0)]
                    self.diagnostic=dict(stage='return',actor=actor.id,cost=0,home=goal)
                    return movement(actor,steps,220,'complete treasure return obligation')
            else:
                self.return_plan={}
        if policy.news_daily_enabled and getattr(world,'news_task_hold',False):
            self.diagnostic=dict(stage='waiting',reason='daily_analysis_or_return')
            return []
        if (self.terminal or world.phase_task or not world.phase_task_observed or not policy.treasure_enabled
                or not self.treasure_complete or (clock.phases!={'day'} and not clear_night)):
            if policy.treasure_enabled and not self.terminal:
                self.diagnostic.update(stage='waiting',reason='active_task' if world.phase_task else
                    'unread_folk_sources' if not self.treasure_complete else 'night_or_unknown_phase')
            return []
        self.diagnostic={'stage':'waiting','reason':'no_resolved_hypothesis' if not self.treasures else 'no_feasible_circuit',
                         'hypotheses':len(self.treasures),'unresolved':self.unresolved,
                         'retained_plan':dict(self.execution),'rejections':[]}
        if policy.news_daily_enabled and len({(t['position'],tuple(t['items']),t['opening_round'],t['closing_round'])
                                               for t in self.treasures})>1:
            self.diagnostic['reason']='conflicting_treasure_hypotheses';return []
        actor = next((u for u in world.movers if u.kind == "pioneer"), None)
        if actor is None or actor.backpack is None or actor.capacity is None:
            self.diagnostic['reason']='actor_or_personal_inventory_unknown'
            return []
        if getattr(world,'critical_base_ids',()):
            self.diagnostic['reason']='urgent_defence_preempts_treasure'
            return []
        if clear_night:
            from copy import copy
            from .robot_threats import active
            threats=active(world)
            if any(r.attack_range is None or r.attack_power is None for r in threats):
                self.diagnostic['reason']='unknown_night_route_threat';return []
            view=copy(world);view.occupied=set(world.occupied)
            for r in threats:
                if r.attack_power<=0:continue
                radius=r.attack_range+1
                view.occupied.update((x,y) for x in range(max(0,r.pos[0]-radius),min(world.width,r.pos[0]+radius+1))
                    for y in range(max(0,r.pos[1]-radius),min(world.height,r.pos[1]+radius+1)))
            world=view
        stands=getattr(world,'pioneer_trade_stands',{})
        trip=self.preparation_trip
        if (not self.treasures and trip.get('actor')==actor.id
                and trip.get('phase') not in {'completed','handed_over'} and trip.get('home')):
            home_goals={tuple(trip['home'])}
        elif actor.id in stands:home_goals={stands[actor.id]}
        elif getattr(world,'task_side_plan',None):home_goals=set(world.task_side_plan['c_stands'])
        elif self.execution.get('home'):home_goals={tuple(self.execution['home'])}
        else:
            home_goals=interaction_cells(world,[u.pos for u in world.weapons],actor.pos) if world.weapons else {actor.pos}
        home=distance_field(world,home_goals,actor.pos,deadline)
        start=distance_field(world,[actor.pos],actor.pos,deadline)
        if time.monotonic()>=deadline:return []
        margin=policy.return_buffer+(8 if world.defence_cells else 0)
        result=[];options=[];preparations=[]
        def rejected(h, reason):
            self.execution_blockers[h['id']]=dict(reason=reason,observed_round=world.round)
            if len(self.diagnostic['rejections'])<8:
                self.diagnostic['rejections'].append(dict(hypothesis=h['id'][:12],reason=reason))
        for hypothesis in sorted(self.treasures,key=lambda h:(h['id']!=self.execution.get('hypothesis'),h["closing_round"],h["opening_round"],h["id"])):
            if world.round>hypothesis['closing_round']:continue
            if any(a['items']==hypothesis['items'] and a.get('result')==3 for a in self.attempts):continue
            if any(a['hypothesis']==hypothesis['id'] for a in self.attempts):continue
            if len(self.attempts)>=policy.treasure_attempt_limit:continue
            needed=Counter(hypothesis['items'])-actor.inventory
            if needed and actor.id in self.pending_purchases:
                self.diagnostic.update(stage='purchase_pending',actor=actor.id);continue
            if any(name not in world.shop for name in needed):
                rejected(hypothesis,'offering_not_in_current_shop');continue
            if sum(needed.values())+len(actor.backpack)>actor.capacity:
                rejected(hypothesis,'personal_capacity');continue
            cost=sum(world.shop[name]*n for name,n in needed.items())
            cash_reserve=policy.reserve_gold
            if getattr(world,'staged_walls',False):
                from .wall_policy import investment_fund
                cash_reserve=max(cash_reserve,investment_fund(world)[0])
            if needed and (self.treasure_spent+cost>policy.treasure_gold_limit or world.gold is None
                    or cost+cash_reserve>world.gold):
                rejected(hypothesis,'observed_gold_or_attempt_budget');continue
            altars=interaction_cells(world,[hypothesis['position']],actor.pos)&home.keys()
            if not altars:rejected(hypothesis,'altar_or_return_unreachable')
            for stand in sorted(altars):
                altar=distance_field(world,[stand],actor.pos,deadline)
                if time.monotonic()>=deadline:return []
                if needed:
                    shops=interaction_cells(world,world.zones.get('weaponShop',()),actor.pos)
                    paths=[(start[q]+len(needed)+altar[q],start[q],q) for q in shops if q in start and q in altar]
                else:paths=[(altar[actor.pos],0,None)] if actor.pos in altar else []
                if not paths:
                    rejected(hypothesis,'shop_or_altar_unreachable');continue
                travel,to_shop,shop=min(paths)
                summon=max(world.round+travel,hypothesis['opening_round'])
                # Include every buy, walking leg, opening wait, summon action,
                # and the real return leg. No invisible gate opening is assumed.
                required=summon-world.round+1+home[stand]+margin
                if summon>hypothesis['closing_round'] or required>work_remaining:
                    rejected(hypothesis,'summon_and_return_exceed_today')
                    if clear_night:continue
                    # Prepare on a prior day only if the current map also has
                    # a complete future daylight execution window. This is a
                    # route estimate, revalidated against actual walls later.
                    future_start=world.round+clock.until_night+60
                    future_walk=min((altar[p] for p in home_goals if p in altar),default=None)
                    future_ok=False
                    if future_walk is not None and clock.day is not None:
                        for day_offset in range(1,11-clock.day):
                            begin=future_start+(day_offset-1)*130
                            moment=max(begin+future_walk,hypothesis['opening_round'])
                            if (moment<=hypothesis['closing_round']
                                    and moment+1+home[stand]+margin<=begin+70):
                                future_ok=True;break
                    if not future_ok:continue
                    if needed:
                        prep_paths=[(start[q]+len(needed)+home[q]+margin,start[q],q)
                            for q in shops & start.keys() & home.keys()
                            if start[q]+len(needed)+home[q]+margin<=clock.until_night]
                    else:
                        prep_paths=[(home[actor.pos]+margin,0,None)] if actor.pos in home else []
                    if prep_paths:
                        prep_required,prep_walk,prep_shop=min(prep_paths)
                        preparations.append((hypothesis['closing_round'],cost,prep_required,hypothesis['id'],
                            stand,prep_shop,prep_walk,home,hypothesis,needed))
                    continue
                options.append((summon,cost,required,hypothesis['id'],stand,shop,to_shop,altar,hypothesis,needed))
            if options:break  # One complete feasible hypothesis is enough for this turn.
        preparing=not options and bool(preparations)
        if preparing:options=preparations
        if not options and self.preparations and clock.phases=={'day'}:
            return self._prepare_purchase(world,clock,policy,deadline,actor,start,home,home_goals,margin)
        if not options:
            if time.monotonic()>=deadline:self.diagnostic['reason']='planning_budget_exhausted'
            return []
        _,cost,required,_,stand,shop,to_shop,altar,hypothesis,needed=min(options,
            key=lambda o:(o[3]!=self.execution.get('hypothesis'),o[:5]))
        self.execution_blockers.pop(hypothesis['id'],None)
        reason='priority treasure circuit fits offerings, opening time and defence return'
        if needed:
            if to_shop==0:
                name,count=sorted(needed.items())[0]
                result=[Candidate(actor.id,{'action':'buy','name':name,'num':count},220,reason,
                                  gold_reserve=policy.reserve_gold)]
            else:
                field=distance_field(world,[shop],actor.pos,deadline)
                steps=[p for p in neighbours(actor.pos) if field.get(p,float('inf'))<field.get(actor.pos,0)]
                result=movement(actor,steps,220,reason)
            stage='procure'
        elif preparing:
            if home.get(actor.pos)==0:
                self.execution=dict(hypothesis=hypothesis['id'],actor=actor.id,phase='prepare',
                    stage='prepared_observed',observed_round=world.round,
                    opening=hypothesis['opening_round'],closing=hypothesis['closing_round'])
            prepared=(self.execution.get('hypothesis')==hypothesis['id']
                      and self.execution.get('stage')=='prepared_observed')
            steps=[] if prepared else [p for p in neighbours(actor.pos)
                if home.get(p,float('inf'))<home.get(actor.pos,0)]
            result=movement(actor,steps,220,'return with confirmed offerings; resume in a later daylight window')
            stage='prepare_return' if result else 'wait_future_day'
        elif actor.pos==stand and world.round>=hypothesis['opening_round']:
            result=[Candidate(actor.id,{'action':'summonTreasure','targetPos':[pos_json(hypothesis['position'])],
                                        'item':hypothesis['items']},220,reason)]
            stage='summon'
        elif actor.pos==stand:
            stage='wait_open'
        else:
            steps=[p for p in neighbours(actor.pos) if altar.get(p,float('inf'))<altar.get(actor.pos,0)]
            result=movement(actor,steps,220,reason);stage='travel'
        if time.monotonic()>=deadline:return []
        self.diagnostic={'stage':stage,'actor':actor.id,'hypothesis':hypothesis['id'][:12],
                         'target':hypothesis['position'],'items':hypothesis['items'],'required':required,'cost':cost,
                         'opening':hypothesis['opening_round'],'closing':hypothesis['closing_round'],
                         'phase':'prepare' if preparing else 'execute',
                         'held_offerings':dict(Counter(hypothesis['items']) & actor.inventory),
                         'retained_plan':dict(self.execution)}
        self.offered_plan=dict(hypothesis=hypothesis['id'],actor=actor.id,stage=stage,
            phase=self.diagnostic['phase'],opening=hypothesis['opening_round'],closing=hypothesis['closing_round'],
            home=min(home_goals,key=lambda p:(start.get(p,float('inf')),p)))
        return result

    def _prepare_purchase(self,world,clock,policy,deadline,actor,start,home,home_goals,margin):
        """A resolved offering list can own one bounded purchase/return trip."""
        if len(self.attempts)>=policy.treasure_attempt_limit:return []
        if actor.id in self.pending_purchases:
            self.diagnostic=dict(stage='purchase_pending',actor=actor.id,cost=0)
            return []
        lists={tuple(p['items']) for p in self.preparations}
        if len(lists)!=1:
            self.diagnostic['reason']='conflicting_offering_lists';return []
        plan=self.preparations[0]
        if any(a.get('result')==3 and a['items']==plan['items'] for a in self.attempts):return []
        needed=Counter(plan['items'])-actor.inventory
        if any(n not in world.shop for n in needed) or sum(needed.values())+len(actor.backpack)>actor.capacity:return []
        cost=sum(world.shop[n]*count for n,count in needed.items())
        reserve=policy.reserve_gold
        if getattr(world,'staged_walls',False):
            from .wall_policy import investment_fund
            reserve=max(reserve,investment_fund(world)[0])
        if needed and (world.gold is None or cost+reserve>world.gold or self.treasure_spent+cost>policy.treasure_gold_limit):return []
        goal=min(home_goals,key=lambda p:(start.get(p,float('inf')),p))
        if not needed:
            active_trip=(self.preparation_trip.get('actor')==actor.id
                and self.preparation_trip.get('phase') not in {None,'completed','handed_over'})
            if not active_trip or home.get(actor.pos)==0:
                self.diagnostic=dict(stage='prepared',actor=actor.id,cost=0)
                if active_trip:
                    self.preparation_trip.update(phase='completed',completed_round=world.round)
                if self.execution.get('phase')=='prepare':self.execution.clear()
                return []
            if actor.pos not in home or home[actor.pos]+margin>clock.until_night:return []
            steps=[p for p in neighbours(actor.pos) if home.get(p,float('inf'))<home[actor.pos]]
            result=movement(actor,steps,220,'return with confirmed treasure supplies')
            stage='prepare_return'
        else:
            shops=interaction_cells(world,world.zones.get('weaponShop',()),actor.pos)
            choices=[(start[p]+len(needed)+home[p]+margin,start[p],p) for p in shops & start.keys() & home.keys()
                     if start[p]+len(needed)+home[p]+margin<=clock.until_night]
            if not choices:return []
            required,walk,shop=min(choices)
            if not walk:
                name,count=sorted(needed.items())[0]
                result=[Candidate(actor.id,dict(action='buy',name=name,num=count),220,
                        'purchase source-resolved treasure supplies',gold_reserve=reserve)]
            else:
                field=distance_field(world,{shop},actor.pos,deadline)
                result=movement(actor,[p for p in neighbours(actor.pos) if field.get(p,float('inf'))<field.get(actor.pos,0)],220,
                                'purchase source-resolved treasure supplies')
            stage='procure'
        if time.monotonic()>=deadline:return []
        self.diagnostic=dict(stage=stage,actor=actor.id,phase='prepare',cost=cost,items=plan['items'])
        self.offered_plan=dict(actor=actor.id,hypothesis=plan['id'],phase='prepare',stage=stage,home=goal,
                              items=sorted(plan['items']))
        return result

    def finalize(self, world, clock, session, response, policy):
        identity=self.offered_plan.get('actor')
        if (identity in getattr(world,'treasure_actions',{}) and
                response['roleCommandMap'].get(identity) in world.treasure_actions[identity]):
            self.execution=dict(self.offered_plan,selected_round=world.round)
            if self.offered_plan.get('phase')=='prepare' and self.offered_plan.get('items'):
                self.preparation_trip=dict(actor=identity,items=self.offered_plan['items'],
                    requirement=fingerprint([self.cycle.number,identity,self.offered_plan['items']]),
                    home=self.offered_plan['home'],phase=self.offered_plan['stage'],selected_round=world.round)
            elif self.offered_plan.get('phase')!='prepare' and self.preparation_trip:
                self.preparation_trip.update(phase='handed_over',completed_round=world.round)
        for identity, action in response["roleCommandMap"].items():
            actor = world.ours[identity]
            if action["action"] == "summonTreasure":
                hypothesis = next((h for h in self.treasures if h["position"] == position(action["targetPos"][0])
                                   and h["items"] == sorted(action["item"])
                                   and h["opening_round"] <= world.round <= h["closing_round"]), None)
                if hypothesis:
                    self.attempts.append({"hypothesis": hypothesis["id"], "items": sorted(action["item"]),
                                          "round": world.round, "result": None})
                    if policy.news_daily_enabled and self.offered_plan.get('home'):
                        self.return_plan=dict(actor=identity,home=self.offered_plan['home'])
            if (action['action']=='buy' and actor.kind=='pioneer'
                    and action in getattr(world,'treasure_actions',{}).get(identity,())):
                cost = world.shop[action["name"]]*action.get("num", 1)
                self.treasure_spent += cost  # Conservative reservation; unknown failures do not replenish budget.
                purchase={"round":world.round,"item":action["name"],"reserved_cost":cost,"prior":actor.inventory[action['name']],
                          'requested_num':action.get('num',1),'settlement_status':'pending'}
                self.purchases.append(purchase)
                self.pending_purchases[identity]=purchase
        if policy.news_daily_enabled:
            self.cycle.emit(self,world,clock,session,response,policy,OFFERING_DESCRIPTIONS)
            return
        if response["prompt"] or session.tasks.active or self.pending or self.terminal or not policy.treasure_enabled:
            return
        if any(c["action"] in {"acceptTask", "submitAnswer"} for c in response["roleCommandMap"].values()):
            return
        self.execution_blockers={k:v for k,v in self.execution_blockers.items()
                                 if any(h['id']==k and h['closing_round']>=world.round for h in self.treasures)}
        retained = self.source_parts(session.news)
        fresh = {key for key in retained if key not in self.analyzed}
        feedback=[a for a in self.attempts if a.get('result') in (2,3) and a['round'] not in self.reviewed_attempts]
        folk={k:v for k,v in retained.items() if v['section']=='folkLegends'}
        corpus=fingerprint(sorted(folk))
        executable=any(h['closing_round']>=world.round and h['id'] not in self.execution_blockers and not any(a['hypothesis']==h['id'] for a in self.attempts)
                       for h in self.treasures)
        awaiting_result=any(a.get('result') is None and world.round<=a['round']+2 for a in self.attempts)
        synthesize=(bool(folk) and not (fresh & folk.keys()) and self.treasure_complete
                    and bool(self.clues) and corpus not in self.synthesized_sources
                    and not executable and not awaiting_result)
        # "Read" is source coverage, not proof that its constraints were
        # extracted. Revisit unresolved original evidence once; a lost or
        # interrupted review gets at most one retry, within the daily quota.
        reviewable={k for k in folk if k not in self.reviewed_sources
                    and self.review_attempts.get(k,0)<2 and not folk[k]['truncated_locally']}
        review=bool(reviewable and not fresh and not feedback and not synthesize
                    and self.treasure_complete and not executable and not awaiting_result)
        repair_validation=bool(self.invalid_candidates and not fresh and not executable
            and not awaiting_result and self.validation_reviews.get(corpus,0)<1)
        if repair_validation:synthesize=review=False
        if (not fresh and not feedback and not synthesize and not review and not repair_validation) or not session.tasks.budget.reserve():
            return
        constraints,constraint_terms=self.constraint_state(retained)
        missing=[k for k,v in constraints.items() if v['status'] in {'unread','unresolved'}]
        def relevance(key):
            return sum(bool(re.search(constraint_terms[k],retained[key]['text'],re.I)) for k in missing)
        sources, used = {}, 0
        # Fresh news triggers analysis; retained old sources supply the missing
        # pieces of cross-day clues. Preserve their original publication metadata.
        ordered = sorted(retained, key=lambda key: (retained[key]["section"]!="folkLegends",key not in fresh,
                         -retained[key]['observed_round'], retained[key].get('offset', 0), key))
        if review:
            # Old unresolved constraints must not repeatedly lose the entire
            # raw-text allowance to the newest announcement.
            ordered=sorted(retained,key=lambda k:(k not in reviewable,-relevance(k),retained[k]['observed_round'],
                                                 retained[k].get('offset',0),k))
        # Reserve context for a fresh fragment before filling the prompt with
        # more new text. Otherwise two full new fragments can permanently hide
        # the sentence crossing the boundary from the previous invocation.
        seed = next((key for key in ordered if key in (reviewable if review else fresh) and not synthesize and
                     not retained[key]['truncated_locally']), None)
        adjacent = []
        if seed is not None and retained[seed].get('parent_source'):
            entry = retained[seed]
            adjacent = sorted((key for key in ordered if key not in fresh and
                retained[key].get('parent_source') == entry['parent_source'] and
                abs(retained[key].get('offset', -16000)-entry['offset']) == 8000),
                key=lambda key: retained[key]['offset'])
        prioritized = ([seed] if seed is not None else []) + adjacent + ordered
        for key in prioritized:
            if key in sources:
                continue
            entry = retained[key]
            if used+len(entry["text"]) <= 16000 and not entry["truncated_locally"]:
                sources[key] = entry
                used += len(entry["text"])
        if not sources or (not set(sources).intersection(fresh) and not feedback and not synthesize and not review and not repair_validation):
            # Reserve was only tentative; no response was issued.
            session.tasks.budget.cancel_unissued()
            return
        self.seq += 1
        context = {"task_instance": None, "nonce": f"news:{session.epoch}:{self.seq}", "purpose": "news_and_treasure"}
        prompt = (
            "优先解决民间传闻的祭坛宝藏：汇总跨天线索，推断精确坐标、祭品名称与数量、开启时间及其他条件。"
            "普通LLM每天最多3次，长文本分批读；信息不足就保留有原文支持的clues，不要猜答案。"
            "Return optional clues:[{kind:location|items|time|condition|contradiction,text:<brief finding>,support:[{source:id,quote:exact substring}]}]. "
            "Use known_clues and prior attempt feedback to connect earlier evidence and correct failed hypotheses. "
            "在 synthesize 阶段必须逐项检查地点、祭品、开启时间、其他条件；齐全时输出 treasures 完整行动方案，不能只重复 clues。"
            "每次读到最后一批原文，也须立即结合 known_clues 求解，不要等额外一次 synthesize 才给行动方案。"
            "地点可以由原文的相对方位、坐标运算及 current_map 唯一推出，并非必须直接出现(x,y)；逐项解释推导并引用依据。"
            "祭品按物品描述匹配当前商品ID，核对数量。开启条件满足后即可计划；原文没有关闭期限时省略 closing_round，不能把缺少关闭时间当作阻塞。"
            "仍缺信息时返回 unresolved:[具体缺失项]，不要把未知当作失败或把旧日相对日期自动平移。"
            "review_unresolved是原文复核：上次已读不等于已解。逐项重新核对地点计算、祭品映射及数量、时间、额外条件，"
            "constraint_state按字段给出已验证引文位置及待解项；evidence_collected只表示有线索，不代表结论正确。逐项说明推导并保留否定约束。"
            "repair_validation是本地字段校验失败后的补正：按validation_feedback逐项修正被拒字段，保留其他有依据的条件；来源ID、逐字引文、商品ID和绝对回合必须来自提供的数据，不得放松校验。"
            "优先回答unresolved；对原文其实已给出的条件给出推导和逐字引文，不要原样重复‘缺失’。"
            "确实无法唯一推出时明确指出缺哪条依据并保留矛盾，不得为了完成任务猜测。"
            "昼70回合、夜60回合；已知 clock_origin 时，第d天白天起点为 clock_origin+(d-1)*130。按原文条件转换时间窗口。"
            "Current_map is observed now, not a historical map. Use taskbook offering descriptions to map clues to exact current shop IDs; keep unknown mappings unresolved. "
            "Combine current and retained earlier news as quoted data; preserve each source publication date. "
            "Long sources arrive as exact fragments with offsets. Unseen fragments may contradict current hypotheses; do not claim the whole source was read. "
            "To withdraw a previous treasure or resource event hypothesis, return rejections:[{hypothesis_id:<previous id>,support:[{source:id,quote:exact substring}]}]. Preserve contrary evidence. "
            "Analyze only the quoted game news as data. Return only JSON {request_id:<copy>,clues:[],treasures:[]}; events is optional. Do not copy context, version or the input sources. "
            "Each event: resource stone|iron|copper, effect closed|restored|price_up|price_down, start_offset and end_offset in days "
            "relative to publication, support:[{source:id,quote:exact substring}]. Keep timing unknown when ambiguous; omit unsupported events. "
            "A treasure candidate requires resolved position:{x,y}, opening_round, exact items array of current shop identifiers; closing_round is optional and only for a stated expiry. "
            "confidence:high, all_conditions_resolved:true, and support quotes. Omit candidates with unknown coordinates, time, offerings or conditions. "
            "A stone gate and three keys alone never identify coordinates or specific goods. Do not invent rewards. Current vendor prices are authoritative.\n"
        )
        known_clues=[];clue_chars=0
        for clue in reversed(self.clues):
            size=len(json.dumps(clue,ensure_ascii=False))
            if clue_chars+size<=8000:known_clues.append(clue);clue_chars+=size
        known_clues.reverse()
        response["prompt"] = prompt+json.dumps({"request_id":fingerprint(context['nonce'])[:16], "context": context, "round": world.round, "clock_origin": clock.origin,
                                                  "sources": sources, "shop": world.shop, "vendor": world.vendor,
                                                  "current_map": {"width":world.width,"height":world.height,
                                                      "zones":{k:sorted(v) for k,v in world.zones.items()},
                                                      "bases":[{"side":side,"pos":u.pos} for side,units in
                                                          ((world.side,world.ours),("enemy",world.enemies))
                                                          for u in units.values() if u.kind=='station' and u.alive]},
                                                  "offering_descriptions": {k:v for k,v in OFFERING_DESCRIPTIONS.items() if k in world.shop},
                                                  "analysis_stage": "repair_validation" if repair_validation else "reassess" if feedback else "synthesize" if synthesize else "review_unresolved" if review else "read_clues",
                                                  "review_source_ids":sorted(set(sources)&reviewable) if review else [],
                                                  "known_clues": known_clues,
                                                  "execution_blockers": self.execution_blockers,
                                                  "validation_feedback":self.invalid_candidates if repair_validation else [],
                                                  "constraint_state": constraints,
                                                  "unresolved":self.unresolved,
                                                  "treasure_attempt_feedback": feedback,
                                                  "ordinary_calls_used_today": session.tasks.budget.attempts,
                                                  "previous_hypotheses": self.treasures[-16:],
                                                  "previous_events": self.events[-16:],
                                                  "previous_rejections": self.rejections[-16:],
                                                  "remaining_source_parts": len(fresh-set(sources))}, ensure_ascii=False)
        citation_sources=dict(sources)
        for clue in self.clues:
            for ref in clue['support']:
                if ref['source'] in retained:citation_sources[ref['source']]=retained[ref['source']]
        self.pending = {"round": world.round, "context": context, "sources": sources,"citation_sources":citation_sources,
                        'synthesis_corpus':corpus if synthesize else None,
                        'analysis_stage': 'repair_validation' if repair_validation else 'reassess' if feedback else 'synthesize' if synthesize else 'review_unresolved' if review else 'read_clues',
                        'review_sources':sorted(set(sources)&reviewable) if review else []}
        self.pending['validation_ids']=[v['id'] for v in self.invalid_candidates] if repair_validation else []
        if repair_validation:self.validation_reviews[corpus]=self.validation_reviews.get(corpus,0)+1
        self.validation_reviews=dict(list(self.validation_reviews.items())[-10:])
        for key in self.pending['review_sources']:
            self.review_attempts[key]=self.review_attempts.get(key,0)+1
        self.llm_status = "pending"
        self.reviewed_attempts.update(a['round'] for a in feedback)
        # IDs outside retained news no longer need dedup memory.
        self.analyzed.intersection_update(retained)
        self.reviewed_sources.intersection_update(retained)
        self.review_attempts={k:n for k,n in self.review_attempts.items() if k in retained}
