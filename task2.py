#!/usr/bin/env python3
"""任务4：查询各门店待拣货条数（pick_states=INIT）→ 写入推单顺序表C列"""
import json, os, subprocess, sys, urllib.request, urllib.error
from collections import Counter

# 看板模式（XLB_NOWRITE=1）：只取数展示，不读写腾讯表格（2026-09-12 21:20 用户确认）
NOWRITE = '--nowrite' in sys.argv or os.environ.get('XLB_NOWRITE') == '1'

PY = '/Users/youww/.workbuddy/binaries/python/versions/3.13.12/bin/python3'
TD = '/Users/youww/.workbuddy/plugins/cache/workbuddy-builtin/tencent-docs-plugin/1.0.0/skills/tencent-docs/tencentdocs.py'
FILE_ID = 'DUXpmYVVQcmRneXFL'
SHEET = 'BB08J2'
URL = 'https://wms-web.wms.ali-prod.xlbsoft.com/wms/hxl.wms.pickorder.page'

# ---------- 1. 调接口（自动翻页） ----------
AUTH_FILE = os.environ.get('XLB_AUTH', '/tmp/xlb_auth.json')
SNAP_DIR = os.environ.get('XLB_WEB_DIR', '/tmp/xlb_web')
auth = json.load(open(AUTH_FILE))
def fetch(page, size=200):
    body = json.dumps({**auth['body'], 'pick_states': ['INIT'], 'page': page, 'size': size}).encode()
    req = urllib.request.Request(URL, data=body, method='POST')
    req.add_header('Content-Type', 'application/json')
    req.add_header('Cookie', auth['cookies'])
    req.add_header('Access-Token', auth['access_token'])
    req.add_header('Origin', 'https://gdp.xlbsoft.com')
    req.add_header('Referer', 'https://gdp.xlbsoft.com/')
    req.add_header('User-Agent', 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36')
    try:
        r = urllib.request.urlopen(req, timeout=20)
        return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        print('HTTPError', e.code, e.read().decode()[:200]); sys.exit(1)

resp = fetch(1)
if resp.get('code') != 0:
    print('API失败:', resp.get('code'), resp.get('msg')); sys.exit(2)
data = resp['data']
content = list(data.get('content') or [])
total = data.get('totalElements') or data.get('total') or len(content)
page_no = 1
while len(content) < total:
    page_no += 1
    r2 = fetch(page_no)
    c2 = r2['data'].get('content') or []
    if not c2: break
    content += c2
print(f'接口OK, 待拣货单共 {total} 条(取回{len(content)})')

# ---------- 2. 按门店计数 ----------
counts = Counter()
for it in content:
    cn = it.get('client_name')
    if cn and it.get('state') == 'INIT':
        counts[cn] += 1

# ---------- 2.5 快照输出（本地看板用，不影响写表） ----------
import time as _t
try:
    import os as _os2
    _os2.makedirs(SNAP_DIR, exist_ok=True)
    json.dump({'ts': _t.time(), 'total': total, 'counts': dict(counts)},
              open(os.path.join(SNAP_DIR, 'data_task2.json'), 'w'), ensure_ascii=False)
except Exception as _e:
    print('snapshot warn:', _e)

if NOWRITE:
    print('[看板模式] 仅获取数据，不写入表格')
    sys.exit(0)

# ---------- 3. 读表格B列 ----------
import os as _os, urllib.request as _ur
def _bootstrap_env():
    env = dict(_os.environ)
    if env.get('TDOC_OAUTH_ACCESS_TOKEN') or env.get('TDOC_ONEID_ACCESS_TOKEN'):
        return env
    cfg_raw = env.get('CODEBUDDY_MCP_CONFIG')
    if not cfg_raw:
        raise RuntimeError('CODEBUDDY_MCP_CONFIG 缺失')
    cfg = json.loads(cfg_raw)
    servers = cfg.get('mcpServers') or {}
    server = servers.get('connector-proxy') or servers.get('workbuddy') or {}
    gw = server.get('url') or ''
    headers = {k: v for k, v in (server.get('headers') or {}).items()
               if isinstance(k, str) and isinstance(v, str) and v}
    if not (gw.endswith('/mcp') and any(k.lower() == 'authorization' for k in headers)):
        raise RuntimeError('connector-proxy 网关条目不完整')
    req = _ur.Request(gw + '/internal/tencent-docs/tokens', headers=headers, method='GET')
    op = _ur.build_opener(_ur.ProxyHandler({}))
    data = json.loads(op.open(req, timeout=10).read().decode('utf-8'))
    personal = data.get('personal') or {}
    if personal.get('available') and personal.get('token'):
        env['TDOC_OAUTH_ACCESS_TOKEN'] = str(personal['token'])
        return env
    raise RuntimeError('personal token 不可用')

_ENV = _bootstrap_env()

def tdoc(tool, args):
    r = subprocess.run([PY, TD, 'tdoc_call', 'sheet-mcp', tool, json.dumps(args)],
                       capture_output=True, text=True, env=_ENV)
    if r.returncode != 0:
        print('ERR', tool, r.stderr[-300:]); sys.exit(3)
    try:
        return json.loads(r.stdout)
    except Exception:
        return {}

out = tdoc('get_cell_data', {'file_id': FILE_ID, 'sheet_id': SHEET, 'start_row': 0, 'end_row': 299, 'start_col': 0, 'end_col': 4, 'return_csv': True})
csv_data = out.get('result', {}).get('structuredContent', {}).get('csv_data') or out.get('csv_data')
rows = []
for line in csv_data.strip().split('\n'):
    cells = line.split(',')
    cells += [''] * (5 - len(cells))  # v3: A劳务列后共5列
    rows.append(cells)

def match(name):
    for cn in counts:
        if cn == name or cn in name or name in cn:
            return cn
    return None

# ---------- 4. 写入C列（D列=已装车 的离场门店跳过；在场门店即使C=0也始终查询，避免漏掉新单） ----------
values, log = [], []
skipped = []
for ri, row in enumerate(rows):
    if len(row) < 2: continue
    nm = row[2].strip()  # C列=门店（v3）
    if not nm or '门店' in nm: continue
    c_val = row[3].strip() if len(row) > 3 else ''
    d_val = row[4].strip() if len(row) > 4 else ''
    if d_val == '已装车':
        skipped.append(nm)
        continue
    cn = match(nm)
    n = counts.get(cn, 0) if cn else 0
    values.append({'row': ri, 'col': 3, 'value_type': 'STRING', 'string_value': str(n)})  # D列（v3）
    log.append(f'{nm}: {n}')

tdoc('set_range_value', {'file_id': FILE_ID, 'sheet_id': SHEET, 'values': values})
print(f'写入 {len(values)} 格, 跳过 {len(skipped)} 家(E列=已装车)')
for s in skipped: print(f'[跳过] {s}')
for l in log: print(l)
print('DONE')
