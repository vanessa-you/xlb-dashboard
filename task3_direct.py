#!/usr/bin/env python3
"""任务3 直调版 v3：不经浏览器直接调集货中心接口 → 写入腾讯文档推单顺序表
布局: B=门店 C=待拣货条数(任务4) D=通道号 E~K=托盘(最多7个/行, 超出在下方插入续行从E列继续)
用法: python3 /tmp/task3_direct.py
认证失效(code 1005等)时运行 /tmp/task3_refresh_auth.sh 借用页面刷新凭证
"""
import json, math, os, subprocess, sys, urllib.request, urllib.error

PY = '/Users/youww/.workbuddy/binaries/python/versions/3.13.12/bin/python3'
TD = '/Users/youww/.workbuddy/plugins/cache/workbuddy-builtin/tencent-docs-plugin/1.0.0/skills/tencent-docs/tencentdocs.py'
FILE_ID = 'DUXpmYVVQcmRneXFL'
SHEET = 'BB08J2'


def _bootstrap_env():
    """自 token provider 取 personal token 注入 env，规避 1.0.0 版只带 Authorization 的 bug。"""
    env = dict(os.environ)
    if env.get('TDOC_OAUTH_ACCESS_TOKEN') or env.get('TDOC_ONEID_ACCESS_TOKEN'):
        return env
    cfg_raw = env.get('CODEBUDDY_MCP_CONFIG')
    if not cfg_raw:
        raise RuntimeError('CODEBUDDY_MCP_CONFIG 缺失')
    cfg = json.loads(cfg_raw)
    servers = cfg.get('mcpServers') or {}
    server = servers.get('connector-proxy') or servers.get('workbuddy') or {}
    gateway_url = server.get('url') or ''
    headers = {k: v for k, v in (server.get('headers') or {}).items()
               if isinstance(k, str) and isinstance(v, str) and v}
    if not (gateway_url.endswith('/mcp') and any(k.lower() == 'authorization' for k in headers)):
        raise RuntimeError('connector-proxy 网关条目不完整')
    token_url = gateway_url + '/internal/tencent-docs/tokens'
    req = urllib.request.Request(token_url, headers=headers, method='GET')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=10) as resp:
        data = json.loads(resp.read().decode('utf-8'))
    personal = data.get('personal') or {}
    if personal.get('available') and personal.get('token'):
        env['TDOC_OAUTH_ACCESS_TOKEN'] = str(personal['token'])
        return env
    raise RuntimeError('personal token 不可用')


_ENV = _bootstrap_env()
AUTH_FILE = os.environ.get('XLB_AUTH', '/tmp/xlb_auth.json')
SNAP_DIR = os.environ.get('XLB_WEB_DIR', '/tmp/xlb_web')
RESP_FILE = os.path.join(SNAP_DIR, 'center_find_direct.json')
STATE_CN = {'DOING': '已排计划', 'FINISH': '拣货完成', 'CHECKED': '已质检', 'LOADING': '装车中'}
MAX_PER_ROW = 7          # E~K 共7列
END_ROW, END_COL = 299, 28  # 读表范围（v3 2026-09-10 新增A劳务列后整体右移1列）

# ---------- 1. 直调接口 ----------
auth = json.load(open(AUTH_FILE))
body = json.dumps(auth['body']).encode()
req = urllib.request.Request(auth['url'], data=body, method='POST')
req.add_header('Content-Type', 'application/json')
req.add_header('Cookie', auth['cookies'])
req.add_header('Access-Token', auth['access_token'])
req.add_header('Origin', 'https://gdp.xlbsoft.com')
req.add_header('Referer', 'https://gdp.xlbsoft.com/')
req.add_header('User-Agent', 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36')
try:
    r = urllib.request.urlopen(req, timeout=20)
    resp = json.loads(r.read().decode())
except urllib.error.HTTPError as e:
    print('HTTPError', e.code, e.read().decode()[:200]); sys.exit(1)
if resp.get('code') != 0:
    print('API失败: code=', resp.get('code'), resp.get('msg'))
    print('>>> 认证可能过期，需借用页面刷新凭证（用户重新登录或页面刷新即可续期）')
    sys.exit(2)
json.dump(resp, open(RESP_FILE, 'w'), ensure_ascii=False)
print('接口OK, 数据量', len(resp['data']), '个区域')

# ---------- 2. 解析（托盘级分组） ----------
# 【2026-09-12 修复】同一门店的托盘可能分布在多个通道（如虚拟通道），
# 逐托盘记录所属通道；主通道=托盘数最多的通道，非主通道托盘标注 (通道名)
api = {}  # client_name -> {'pallets': [(text, state, channel)]}
for region in resp['data']:
    for ch in region.get('details', []):
        for p in ch.get('details', []):
            cn = p.get('client_name')
            if not cn or p.get('state') == 'INIT':
                continue
            s = STATE_CN.get(p['state'], p['state'])
            text = f"{p['pallet_name']}-{s}" if p.get('pallet_name') else s
            e = api.setdefault(cn, {'pallets': []})
            e['pallets'].append((text, p['state'], ch.get('channel', '')))

# 主通道判定：托盘数最多的通道；非主通道托盘文本追加通道标注
from collections import Counter
for cn, e in api.items():
    cnt = Counter(ch for _, _, ch in e['pallets'])
    e['channel'] = cnt.most_common(1)[0][0]
    e['pallets'] = [(t, st, ch) for t, st, ch in e['pallets']]

# ---------- 2.5 快照输出（本地看板用，不影响写表） ----------
import time as _ts
try:
    os.makedirs(SNAP_DIR, exist_ok=True)
    json.dump({'ts': _ts.time(),
               'stores': {cn: {'channel': e['channel'],
                               'pallets': [{'text': t, 'state': st, 'channel': ch}
                                           for t, st, ch in e['pallets']]}
                          for cn, e in api.items()}},
              open(os.path.join(SNAP_DIR, 'data_task3.json'), 'w'), ensure_ascii=False)
except Exception as _e:
    print('snapshot warn:', _e)

# 看板模式（XLB_NOWRITE=1 或 --nowrite）：只取数展示，不读写腾讯表格（2026-09-12 21:20 用户确认）
if os.environ.get('XLB_NOWRITE') == '1' or '--nowrite' in sys.argv:
    print('[看板模式] 仅获取数据，不读写表格')
    sys.exit(0)

# ---------- 工具函数 ----------
def tdoc(tool, args):
    r = subprocess.run([PY, TD, 'tdoc_call', 'sheet-mcp', tool, json.dumps(args)],
                       capture_output=True, text=True, env=_ENV)
    if r.returncode != 0:
        print('ERR', tool, r.stderr[-300:]); sys.exit(3)
    try:
        return json.loads(r.stdout)
    except Exception:
        return {}

def read_rows():
    out = tdoc('get_cell_data', {'file_id': FILE_ID, 'sheet_id': SHEET,
                                 'start_row': 0, 'end_row': END_ROW, 'start_col': 0,
                                 'end_col': END_COL, 'return_csv': True})
    csv_data = out.get('result', {}).get('structuredContent', {}).get('csv_data') or out.get('csv_data')
    rows = []
    for line in csv_data.strip().split('\n'):
        cells = line.split(',')
        cells += [''] * (END_COL + 1 - len(cells))
        rows.append(cells)
    while len(rows) < END_ROW + 1:
        rows.append([''] * (END_COL + 1))
    return rows

def cell(r, c):
    return rows[r][c].strip() if r < len(rows) and c < len(rows[r]) else ''

def match(name):
    for cn in api:
        if cn == name or cn in name or name in cn:
            return cn
    return None

def is_cont_row(rows, i):
    """续行: B列为空 且 C~AB 有内容"""
    if i >= len(rows): return False
    b = rows[i][2].strip() if len(rows[i]) > 2 else ''  # C列=门店（v3）
    rest = [c.strip() for c in rows[i][3:END_COL + 1]]
    return b == '' and any(rest)

def store_list(rows):
    out = []
    for i, row in enumerate(rows):
        if len(row) > 2:
            nm = row[2].strip()  # C列=门店（v3）
            if nm and '门店' not in nm:
                out.append((i, nm))
    return out

def cont_count(rows, ri):
    """门店行正下方连续续行数"""
    n, i = 0, ri + 1
    while i < len(rows) and is_cont_row(rows, i):
        n += 1; i += 1
    return n

# ---------- 3. 结构调整（自底向上，插入/删除续行） ----------
rows = read_rows()
stores = store_list(rows)
for ri, nm in reversed(stores):
    if cell(ri, 4) == '已装车':
        continue  # 已装车离场的门店不再处理（E列，v3）
    cn = match(nm)
    pallets = api[cn]['pallets'] if cn else []
    # 2026-09-10 用户要求：托盘不再换行，全部一行写入；已有续行会被下面逻辑删除
    needed = 0
    # 原有数据判断（D列非空且非已装车，或E~K有托盘）
    d = rows[ri][4].strip() if len(rows[ri]) > 4 else ''
    body_cells = [c.strip() for c in rows[ri][5:END_COL + 1]] if len(rows[ri]) > 5 else []
    had_data = (d not in ('', '已装车')) or any(body_cells)
    if not pallets and not had_data:
        continue  # 从未有数据且接口无 → 不动
    existing = cont_count(rows, ri)
    if needed > existing:
        tdoc('insert_dimension', {'file_id': FILE_ID, 'sheet_id': SHEET,
                                  'dimension_type': 1, 'index': ri + 1, 'count': needed - existing})
        print(f'[插行] {nm}: 下方插入 {needed - existing} 行')
    elif needed < existing:
        tdoc('delete_dimension', {'file_id': FILE_ID, 'sheet_id': SHEET,
                                  'dimension_type': 1, 'index': ri + 1 + needed, 'count': existing - needed})
        print(f'[删行] {nm}: 下方删除 {existing - needed} 行')
    rows = read_rows()  # 行号变了，重读

# ---------- 4. 重新读表生成写入计划 ----------
rows = read_rows()
stores = store_list(rows)
writes, colors = [], []

for ri, nm in stores:
    if cell(ri, 4) == '已装车':
        continue  # 已装车离场的门店不再处理（E列，v3）
    cn = match(nm)
    d = rows[ri][4].strip() if len(rows[ri]) > 4 else ''
    body_cells = [c.strip() for c in rows[ri][5:END_COL + 1]] if len(rows[ri]) > 5 else []
    had_data = (d not in ('', '已装车')) or any(body_cells)
    if cn is None or not api[cn]['pallets']:
        # 整店从接口消失：原有托盘数据 → 清空并标"已装车"；从未有数据 → 不动
        if had_data:
            for c in range(4, END_COL + 1):
                writes.append({'row': ri, 'col': c, 'value': ''})
            writes.append({'row': ri, 'col': 4, 'value': '已装车'})
            # 清空其续行
            i = ri + 1
            while i < len(rows) and is_cont_row(rows, i):
                for c in range(3, END_COL + 1):
                    writes.append({'row': i, 'col': c, 'value': ''})
                i += 1
            print(f'[装车离场] {nm}')
        continue
    e = api[cn]
    writes.append({'row': ri, 'col': 4, 'value': e['channel']})
    chunk = e['pallets']  # 2026-09-10：一行写完，不换行
    n_cols = max(len(chunk), MAX_PER_ROW)
    for j in range(n_cols):
        col = 5 + j
        if j < len(chunk):
            text, st, ch = chunk[j]
            if ch != e['channel']:  # 非主通道托盘：标注实际所属通道
                text = f"{text}({ch})"
            writes.append({'row': ri, 'col': col, 'value': text})
            if st == 'FINISH':
                colors.append((ri, col, 'FFFF0000'))
            elif st == 'CHECKED':
                colors.append((ri, col, 'FFFFA500'))
        else:
            writes.append({'row': ri, 'col': col, 'value': ''})
    # 清掉本行末尾到 END_COL 的旧数据
    for c in range(5 + n_cols, END_COL + 1):
        writes.append({'row': ri, 'col': c, 'value': ''})
    print(f'[更新] {nm}: {e["channel"]} {len(chunk)}托盘 1行 (原{"有" if had_data else "无"}数据)')

import time as _time
plan = {'writes': writes, 'colors': colors, 'ts': _time.time()}
json.dump(plan, open('/tmp/task3_plan.json', 'w'), ensure_ascii=False)
print('计划:', len(writes), '格写入,', len(colors), '格上色')
