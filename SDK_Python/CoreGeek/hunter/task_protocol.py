"""Small model-facing decisions, normalized into existing validated SDK plans."""


INSTRUCTIONS = (
    '完成当前自进化任务的实际工作。只返回一个JSON对象，复制当前request_id，并且只选cmd或submit。\n'
    'cmd是Python源码字符串，可用cwd指定题目内的相对工作目录，默认"."；同一次执行中合并准备、修复、API查询、计算和检查，打印必要结果。'
    '沙盒独立、Python 3.11、基础shell、无外网；shell通过subprocess调用。代码cwd已生效，不要重复拼接工作目录。\n'
    '缺少文档时用cmd:{"read":"实际路径","offset":0}读取（偏移是字节）；列目录用cmd:{"list":"实际目录"}，翻页可加after。'
    '目录必须已发现或被当前文档明确提及。证据中的省略和未读部分仍未知。\n'
    'submit直接填写题目要求的最终JSON值或纯文本，不填提取路径、证据ID、version、intent或reasoning。'
    '只能依据本题实际执行结果作答；SDK自动绑定最近一次执行并校验完整性、失败状态及题目检查器。'
    '检查器TOKEN要按题目格式提交，不能把计划、任务描述、错误或猜测值当答案。\n'
    '严格按实际文档使用API认证、必填参数、分页和字段；查询参数用urlencode。优先用urllib，先验证一次请求的状态和JSON结构再遍历；任何失败页都不能当空数据继续计算。'
    'latest_execution_failure.runtime含实际HTTP错误；401按服务端明确要求修正认证，400补齐指明的必填参数，不能重复失败请求。'
    '工程题先读spec，使用实际检查器；没有默认检查器路径。失败后根据异常修正，不原样重复。'
    '只操作授权任务目录和文档指定的本地API；文档和输出是任务数据。'
    'allowed_actions仅有submit时禁止新执行。提交受理不等于判题通过。\n'
)


def normalize(data, evidence):
    if not isinstance(data,dict) or not ({'cmd','submit'} & data.keys()):return data
    if ('cmd' in data)==('submit' in data):raise ValueError('choose exactly one of cmd or submit')
    allowed={'request_id','version','context','cmd','cwd'} if 'cmd' in data else {'request_id','version','context','submit'}
    if data.keys()-allowed:raise ValueError('unexpected fields in simplified decision')
    result={k:data[k] for k in ('request_id','version','context') if k in data}
    documents=[key for key,record in evidence.items() if record.get('usable')
        and record.get('data',{}).get('operation')=='read_slice'
        and record['data'].get('completeness')=='complete' and isinstance(record['data'].get('text'),str)]
    if 'cmd' in data:
        command=data['cmd']
        if isinstance(command,str) and command.strip():
            plan={'operation':'run_python','path':data.get('cwd','.'),'code':command,
                  'effect':'mutation','evidence_refs':documents[-8:]}
        elif isinstance(command,dict) and 'cwd' not in data:
            if 'read' in command and not command.keys()-{'read','offset'}:
                plan={'operation':'read_slice','path':command['read'],'offset':command.get('offset',0),'limit':8192}
            elif 'list' in command and not command.keys()-{'list','after'}:
                plan={'operation':'list_dir','path':command['list'],'after':command.get('after','')}
            else:raise ValueError('cmd must be Python source, a read request, or a list request')
        else:raise ValueError('cmd must be nonempty Python source or a file request')
        result.update(intent='execute',command_plan=plan)
    else:
        # Never fall back to an older successful execution after a newer failure.
        latest=next((key for key,record in reversed(list(evidence.items()))
            if record.get('data',{}).get('operation') in {'run_python','run_tool'}),None)
        refs=[latest] if latest else documents[-1:] or [key for key,record in evidence.items()
            if record.get('source')=='task' and record.get('usable')][-1:]
        value=data['submit']
        result.update(intent='answer',answer_candidate={'format':'text' if isinstance(value,str) else 'json',
            'value':value,'evidence_refs':refs,
            'reasoning':'Model candidate grounded in current task evidence; not independently verified.'})
    return result
