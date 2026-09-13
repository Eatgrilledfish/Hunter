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
    treasure_complete: bool = True
    diagnostic: dict = field(default_factory=dict)
    llm_status: str = "idle"
    llm_diagnostic: dict = field(default_factory=dict)
    unresolved: list = field(default_factory=list)

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
        for identity, purchase in list(self.pending_purchases.items()):
            actor=world.ours.get(identity)
            feedback=obj(world.raw.get('lastRoundRoleActionResults'))
            arrived=actor and actor.backpack is not None and actor.inventory[purchase['item']]>purchase['prior']
            failed=world.round==purchase['round']+1 and feedback.get(identity) is False
            if arrived or failed:
                if failed and not arrived:self.treasure_spent-=purchase['reserved_cost']
                self.pending_purchases.pop(identity)
        if self.attempts:
            latest = self.attempts[-1]
            if latest["round"] == world.round-1 and latest.get("result") is None:
                result = world.raw.get("lastSummonTreasureResult")
                if integer(result) and 0 <= result <= 4:
                    latest["result"] = result
                    if result in {1, 4}:
                        self.terminal = "success" if result == 1 else "empty"
        if self.pending:
            raw = world.raw.get("llmResp")
            self.llm_diagnostic = {'sent':self.pending['round'], 'reply_type':type(raw).__name__,
                                   'reply_chars':len(raw) if isinstance(raw,str) else 0}
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
                    data.setdefault('events',[])
                    data.setdefault('treasures',[])
                    if not isinstance(data.get("events"), list) or not isinstance(data.get("treasures"), list):
                        raise ValueError("invalid news collections")
                    self._ingest(world, data, self.pending.get("citation_sources",self.pending["sources"]))
                    self.analyzed.update(self.pending["sources"])
                    if self.pending.get('synthesis_corpus'):
                        self.synthesized_sources.add(self.pending['synthesis_corpus'])
                    self.pending = None
                    self.llm_status = "accepted"
                except (ValueError, TypeError, KeyError) as exc:
                    self.llm_status = "invalid_response"
                    self.llm_diagnostic['reason']=str(exc)[:100]
                    self.pending['rejection']=self.llm_diagnostic.copy()
                    if world.round > self.pending["round"]+2:
                        self.pending = None
            elif world.round > self.pending["round"]+2:
                self.llm_status = 'oversized_response' if isinstance(raw,str) and len(raw)>32768 else 'missing_response'
                if self.pending.get('rejection'):
                    self.llm_status='invalid_response'
                    self.llm_diagnostic=self.pending['rejection']
                self.pending = None
        if session.tasks.active:
            # Active task takes the channel; a late ordinary nonce cannot satisfy
            # any task pending. The consumed ordinary reservation is not refunded.
            self.pending = None
        sources = self.source_parts(session.news)
        self.news_complete = (set(sources) <= self.analyzed and
                              not any(n['truncated_locally'] for n in sources.values()))
        folk={k:v for k,v in sources.items() if v['section']=='folkLegends'}
        self.treasure_complete=(set(folk)<=self.analyzed and not any(n['truncated_locally'] for n in folk.values()))

    def _ingest(self, world, data, sources):
        rejected=Counter()
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
        for candidate in data.get("treasures", [])[:8]:
            if not isinstance(candidate, dict) or support(candidate) is None:
                rejected['unsupported_source']+=1
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
                rejected['position_or_items']+=1
                continue
            if any(not isinstance(x, str) or x not in world.shop for x in items):
                rejected['unknown_shop_item']+=1
                continue
            if not integer(opening, 0) or not integer(closing, opening) or closing-opening > 1300:
                rejected['opening_window']+=1
                continue
            if candidate.get("confidence") != "high" or candidate.get("all_conditions_resolved") is not True:
                rejected['unresolved_conditions']+=1
                continue
            record = {"position": pos, "items": sorted(items), "opening_round": opening, "closing_round": closing,
                      "support": support(candidate), "basis": "model_hypothesis_not_official", "confidence": "high"}
            if not expiry_known:
                record['closing_source'] = 'half_horizon_not_treasure_expiry'
            record["id"] = fingerprint(record)
            if (not any(t["id"] == record["id"] for t in self.treasures) and
                    not any(r['hypothesis_id'] == record['id'] for r in self.rejections)):
                self.treasures.append(record)
        self.events, self.treasures = self.events[-64:], self.treasures[-16:]
        self.llm_diagnostic.update(hypotheses=len(self.treasures),proposed=len(data.get('treasures',[])),
                                   rejected=dict(rejected),unresolved=self.unresolved)

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
        self.diagnostic={'stage':'inactive'}
        if (self.terminal or world.phase_task or not world.phase_task_observed or not policy.treasure_enabled
                or not self.treasure_complete or clock.phases!={'day'}):
            if policy.treasure_enabled and not self.terminal:
                self.diagnostic={'stage':'waiting','reason':'active_task' if world.phase_task else
                    'unread_folk_sources' if not self.treasure_complete else 'night_or_unknown_phase'}
            return []
        self.diagnostic={'stage':'waiting','reason':'no_resolved_hypothesis' if not self.treasures else 'no_feasible_circuit',
                         'hypotheses':len(self.treasures),'unresolved':self.unresolved}
        actor = next((u for u in world.movers if u.kind == "pioneer"), None)
        if actor is None or actor.backpack is None or actor.capacity is None:
            return []
        stands=getattr(world,'pioneer_trade_stands',{})
        if actor.id in stands:home_goals={stands[actor.id]}
        elif getattr(world,'task_side_plan',None):home_goals=set(world.task_side_plan['c_stands'])
        else:
            home_goals=interaction_cells(world,[u.pos for u in world.weapons],actor.pos) if world.weapons else {actor.pos}
        home=distance_field(world,home_goals,actor.pos,deadline)
        start=distance_field(world,[actor.pos],actor.pos,deadline)
        if time.monotonic()>=deadline:return []
        margin=policy.return_buffer+(8 if world.defence_cells else 0)
        result=[];options=[]
        for hypothesis in sorted(self.treasures,key=lambda h:(h["closing_round"],h["opening_round"],h["id"])):
            if world.round>hypothesis['closing_round']:continue
            if any(a['items']==hypothesis['items'] and a.get('result')==3 for a in self.attempts):continue
            if any(a['hypothesis']==hypothesis['id'] for a in self.attempts):continue
            if len(self.attempts)>=policy.treasure_attempt_limit:continue
            needed=Counter(hypothesis['items'])-actor.inventory
            if needed and actor.id in self.pending_purchases:
                self.diagnostic={'stage':'purchase_pending'};continue
            if any(name not in world.shop for name in needed):continue
            if sum(needed.values())+len(actor.backpack)>actor.capacity:continue
            cost=sum(world.shop[name]*n for name,n in needed.items())
            if (self.treasure_spent+cost>policy.treasure_gold_limit or world.gold is None
                    or cost+policy.reserve_gold>world.gold):continue
            altars=interaction_cells(world,[hypothesis['position']],actor.pos)&home.keys()
            for stand in sorted(altars):
                altar=distance_field(world,[stand],actor.pos,deadline)
                if time.monotonic()>=deadline:return []
                if needed:
                    shops=interaction_cells(world,world.zones.get('weaponShop',()),actor.pos)
                    paths=[(start[q]+len(needed)+altar[q],start[q],q) for q in shops if q in start and q in altar]
                else:paths=[(altar[actor.pos],0,None)] if actor.pos in altar else []
                if not paths:continue
                travel,to_shop,shop=min(paths)
                summon=max(world.round+travel,hypothesis['opening_round'])
                # Include every buy, walking leg, opening wait, summon action,
                # and the real return leg. No invisible gate opening is assumed.
                required=summon-world.round+1+home[stand]+margin
                if summon>hypothesis['closing_round'] or required>clock.until_night:continue
                options.append((summon,cost,required,hypothesis['id'],stand,shop,to_shop,altar,hypothesis,needed))
            if options:break  # One complete feasible hypothesis is enough for this turn.
        if not options:return []
        _,cost,required,_,stand,shop,to_shop,altar,hypothesis,needed=min(options,key=lambda o:o[:5])
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
                         'opening':hypothesis['opening_round'],'closing':hypothesis['closing_round']}
        return result

    def finalize(self, world, clock, session, response, policy):
        for identity, action in response["roleCommandMap"].items():
            actor = world.ours[identity]
            if action["action"] == "summonTreasure":
                hypothesis = next((h for h in self.treasures if h["position"] == position(action["targetPos"][0])
                                   and h["items"] == sorted(action["item"])
                                   and h["opening_round"] <= world.round <= h["closing_round"]), None)
                if hypothesis:
                    self.attempts.append({"hypothesis": hypothesis["id"], "items": sorted(action["item"]),
                                          "round": world.round, "result": None})
            if (action['action']=='buy' and actor.kind=='pioneer'
                    and action in getattr(world,'treasure_actions',{}).get(identity,())):
                cost = world.shop[action["name"]]*action.get("num", 1)
                self.treasure_spent += cost  # Conservative reservation; unknown failures do not replenish budget.
                purchase={"round":world.round,"item":action["name"],"reserved_cost":cost,"prior":actor.inventory[action['name']]}
                self.purchases.append(purchase)
                self.pending_purchases[identity]=purchase
        if response["prompt"] or session.tasks.active or self.pending or self.terminal or not policy.treasure_enabled:
            return
        if any(c["action"] in {"acceptTask", "submitAnswer"} for c in response["roleCommandMap"].values()):
            return
        retained = self.source_parts(session.news)
        fresh = {key for key in retained if key not in self.analyzed}
        feedback=[a for a in self.attempts if a.get('result') in (2,3) and a['round'] not in self.reviewed_attempts]
        folk={k:v for k,v in retained.items() if v['section']=='folkLegends'}
        corpus=fingerprint(sorted(folk))
        synthesize=(bool(folk) and not (fresh & folk.keys()) and self.treasure_complete
                    and bool(self.clues) and corpus not in self.synthesized_sources
                    and not any(h['closing_round']>=world.round and not any(a['hypothesis']==h['id'] for a in self.attempts)
                                for h in self.treasures))
        if (not fresh and not feedback and not synthesize) or not session.tasks.budget.reserve():
            return
        sources, used = {}, 0
        # Fresh news triggers analysis; retained old sources supply the missing
        # pieces of cross-day clues. Preserve their original publication metadata.
        ordered = sorted(retained, key=lambda key: (retained[key]["section"]!="folkLegends",key not in fresh,
                         -retained[key]['observed_round'], retained[key].get('offset', 0), key))
        # Reserve context for a fresh fragment before filling the prompt with
        # more new text. Otherwise two full new fragments can permanently hide
        # the sentence crossing the boundary from the previous invocation.
        seed = next((key for key in ordered if key in fresh and not synthesize and
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
        if not sources or (not set(sources).intersection(fresh) and not feedback and not synthesize):
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
                                                  "analysis_stage": "reassess" if feedback else "synthesize" if synthesize else "read_clues",
                                                  "known_clues": known_clues,
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
                        'synthesis_corpus':corpus if synthesize else None}
        self.llm_status = "pending"
        self.reviewed_attempts.update(a['round'] for a in feedback)
        # IDs outside retained news no longer need dedup memory.
        self.analyzed.intersection_update(retained)
