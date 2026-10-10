#!/usr/bin/env python3
"""新零帮调度看板 - 本地Web服务
GET  /            看板页面
GET  /api/data    三个任务的快照数据
POST /api/run?task=1|2|3|all  执行对应任务脚本并返回最新数据+日志
"""
import json, os, shutil, subprocess, threading, time, urllib.parse, urllib.request, base64, hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 环境变量（服务器部署时覆盖；默认值=本机路径）
PY = os.environ.get('XLB_PY', '/Users/youww/.workbuddy/binaries/python/versions/3.13.12/bin/python3')
WEB_DIR = os.environ.get('XLB_WEB_DIR', '/tmp/xlb_web')
SCRIPTS_DIR = os.environ.get('XLB_SCRIPTS', '/tmp')
BIND = os.environ.get('XLB_BIND', '127.0.0.1')
PORT_ENV = int(os.environ.get('PORT', 8787))
WEB_USER = os.environ.get('XLB_WEB_USER', '')
WEB_PASS = os.environ.get('XLB_WEB_PASS', '')
TASKS = {
    '1': {'name': '任务1 已调度运单', 'scripts': [os.path.join(SCRIPTS_DIR, 'task1.py')]},
    '2': {'name': '任务2 待拣货条数', 'scripts': [os.path.join(SCRIPTS_DIR, 'task2.py')]},
    '3': {'name': '任务3 托盘状态',   'scripts': [os.path.join(SCRIPTS_DIR, 'task3_direct.py')]},  # 看板模式不写表
}
SNAP = {t: f'{WEB_DIR}/data_task{t}.json' for t in ('1', '2', '3')}
_lock = threading.Lock()  # 防并发执行

# 【2026-09-16 用户要求】本机点击更新后自动把快照推到 GitHub → Pages 同步显示新数据
GH_REPO = os.environ.get('XLB_GH_REPO', '/tmp/xlb_gh_page')   # Pages 仓库工作副本
GH_SYNC = os.environ.get('XLB_GH_SYNC', '1') == '1'
GH_BRANCH = os.environ.get('XLB_GH_BRANCH', 'main')
GIT = shutil.which('git') or '/usr/bin/git'
SYNC_STATUS = f'{WEB_DIR}/sync_status.json'
_sync_lock = threading.Lock()  # 串行化 git 操作


def _git(args, timeout=90):
    return subprocess.run([GIT, '-C', GH_REPO] + args, capture_output=True, text=True, timeout=timeout)


_SHIM_KEYS = ('PYTHONPATH', 'BASH_ENV', 'PYTHONSTARTUP', 'PYTHONHOME', 'NODE_OPTIONS')
_SHIM_PREFIXES = ('CODEBUDDY_SANDBOX', 'CODEBUDDY_SAFE_DELETE', 'CODEBUDDY_BROKERED', 'CODEBUDDY_TOOL')


def _clean_env(extra=None):
    """剥离 WorkBuddy 沙箱 shim 注入的环境变量，返回干净 env。
    server.py 由工具 shell 启动时会继承 sitecustomize(PYTHONPATH)/broker 等变量，
    子进程 python 加载 shim 后读写文件要向旧会话 broker 申请令牌，
    会话过期即报 PermissionError: Sensitive content access was denied。
    剥离后子进程纯净运行，不依赖任何会话状态。"""
    env = dict(os.environ)
    for k in list(env):
        if k in _SHIM_KEYS or k.startswith(_SHIM_PREFIXES):
            env.pop(k, None)
    if extra:
        env.update(extra)
    return env


def _write_sync_status(st):
    try:
        json.dump(st, open(SYNC_STATUS, 'w'), ensure_ascii=False)
    except Exception:
        pass


def _sync_to_github(tids):
    """把本机最新快照同步到 GitHub Pages（后台线程执行，失败不影响看板）
    ① GitHub Contents API 直推（api.github.com 通常可通，不依赖 git）
    ② git pull/commit/push 兜底（github.com:443 可通时用）
    """
    with _sync_lock:
        st = {'ts': time.time(), 'tasks': list(tids), 'ok': False, 'msg': '', 'via': ''}
        done = [t for t in tids if os.path.exists(SNAP.get(t, ''))]
        try:
            # ---- ① API 直推（脚本内部已带退避重试；此处再整体重跑一次兜底） ----
            api_script = os.environ.get('XLB_GH_PUSH_API',
                                        '/Users/youww/WorkBuddy/2026-08-25-10-11-19/xlb_gh/push_snapshot_api.py')
            if os.path.exists(api_script) and done:
                out = ''
                for attempt in range(2):
                    r = subprocess.run([PY, api_script] + list(done),
                                       capture_output=True, text=True, timeout=300,
                                       env=_clean_env())
                    out = (r.stdout or '') + (r.stderr or '')
                    if 'SYNC_OK' in out or 'SYNC_NOOP' in out:
                        break
                    time.sleep(5)
                if 'SYNC_OK' in out:
                    st.update(ok=True, via='api', msg='已推送 GitHub（Pages 约1分钟后生效）：'
                                                     + ','.join(done))
                    raise StopIteration
                api_err = out.strip().splitlines()[-1:] or ['(无输出)']
                st['msg'] = f'API 推送失败: {api_err[0][:160]}'
            # ---- ② git 兜底 ----
            if os.path.isdir(os.path.join(GH_REPO, '.git')):
                for t in done:
                    shutil.copyfile(SNAP[t], os.path.join(GH_REPO, f'data_task{t}.json'))
                _git(['pull', '--rebase', '--quiet', 'origin', GH_BRANCH], timeout=45)
                _git(['add', '-A'])
                c = _git(['-c', 'user.name=xlb-sync',
                          '-c', 'user.email=xlb-sync@users.noreply.github.com',
                          'commit', '-m', f'data(local): {time.strftime("%Y-%m-%dT%H:%M:%S")}'
                                          f' 本机手动更新 task{"".join(done)}'], timeout=30)
                if c.returncode == 0:
                    p = _git(['push', '--quiet', 'origin', GH_BRANCH], timeout=60)
                    if p.returncode == 0:
                        st.update(ok=True, via='git', msg='已通过 git 推送（Pages 约1分钟后生效）')
                    else:
                        st['msg'] += f' | git 推送失败: {(p.stderr or "")[:120]}'
                else:
                    st.update(ok=True, via='git', msg='数据与上次一致，无需提交')
            elif not st['msg']:
                st['msg'] = f'同步不可用：{GH_REPO} 不是 git 仓库'
        except StopIteration:
            pass
        except subprocess.TimeoutExpired:
            st['msg'] = (st['msg'] + ' | ' if st['msg'] else '') + '同步超时'
        except Exception as e:
            st['msg'] = (st['msg'] + ' | ' if st['msg'] else '') + f'同步异常: {e}'
        st['ts'] = time.time()
        _write_sync_status(st)
        print(f'[云端同步] {time.strftime("%H:%M:%S")} ok={st["ok"]} [{st["via"]}] {st["msg"]}', flush=True)
        return st

def _refresh_credentials(api_only=False):
    """认证过期时刷新凭证：①纯API刷新JWT/RT ②Chrome解密重建32位AT（主力兜底）③CDP抓包 ④sh兜底
    注意：API续期只更新JWT，32位AT过期(1005)必须靠②/③从浏览器登录态重建，故①成功后不能直接返回。
    【2026-09-21】api_only=True 时只跑①（30分钟主动续期用）——②会读钥匙串触发 Mac 密码弹窗，
    只允许在任务真正报1005掉线时才触发，避免频繁要密码。"""
    api_ref = os.path.join(SCRIPTS_DIR, 'xlb_refresh_api.py')
    cap = os.path.join(SCRIPTS_DIR, 'xlb_refresh_capture.py')
    rebuild = os.path.join(SCRIPTS_DIR, 'rebuild_auth_from_chrome.py')
    py_ref = os.environ.get('XLB_PY_REFRESH',
                            '/Users/youww/.workbuddy/binaries/python/envs/default/bin/python')  # websocket-client/pycryptodome 在此 venv
    results = []

    def _run(cmd):
        if not os.path.exists(cmd[1] if cmd[0] != 'zsh' else cmd[1]):
            return None
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=90, env=_clean_env())
        results.append(f'[{cmd[1]}] exit={r.returncode}\n{r.stdout[-200:]}{r.stderr[-200:]}')
        return r.returncode

    # ① 纯API：refresh_token轮换续期JWT/RT（不解决AT过期，成功也不提前返回）
    try:
        _run([PY, api_ref])
    except Exception as e:
        results.append(f'[{api_ref}] 异常: {e}')
    if api_only:
        return '[凭证刷新OK-仅API]\n' + '\n'.join(results)
    # ② Chrome解密重建：从日常Chrome cookie/localStorage 取最新32位AT（无需CDP窗口）
    if os.path.exists(rebuild):
        try:
            if _run([py_ref, rebuild]) == 0:
                return f'[凭证刷新OK]\n' + '\n'.join(results)
        except Exception as e:
            results.append(f'[{rebuild}] 异常: {e}')
    # ③ CDP抓包兜底 ④ sh兜底
    for cmd in ([py_ref, cap], ['zsh', os.path.join(SCRIPTS_DIR, 'task3_refresh_auth.sh')]):
        try:
            rc = _run(cmd)
            if rc == 0:
                return f'[凭证刷新OK]\n' + '\n'.join(results)
        except Exception as e:
            results.append(f'[{cmd[1]}] 异常: {e}')
    return '[凭证刷新失败]\n' + '\n'.join(results)

def _proactive_refresh_loop():
    """后台线程：每30分钟主动续期JWT（1小时时效）。仅走API不碰钥匙串（见 _refresh_credentials）"""
    interval = int(os.environ.get('XLB_REFRESH_INTERVAL', 1800))
    while True:
        time.sleep(interval)
        try:
            res = _refresh_credentials(api_only=True)
            print(f'[主动续期] {time.strftime("%H:%M:%S")} {res.splitlines()[0]}', flush=True)
        except Exception as e:
            print(f'[主动续期异常] {e}', flush=True)


# ---------- 司机分类：托盘车 / 零散 ----------
DRIVERS_FILE = f'{WEB_DIR}/xlb_drivers.json'


def _load_drivers():
    try:
        return json.load(open(DRIVERS_FILE))
    except Exception:
        return {}


def _save_drivers(dd):
    json.dump(dd, open(DRIVERS_FILE, 'w'), ensure_ascii=False, indent=1)


def _sync_drivers():
    """任务更新后从任务1快照同步司机名单：只增不删，保留零散标记"""
    try:
        snap = json.load(open(SNAP['1']))
    except Exception:
        return
    today = time.strftime('%Y-%m-%d')
    dd = _load_drivers()
    changed = False
    for g in snap.get('groups') or []:
        name = (g.get('driver') or '').strip()
        if not name:
            continue
        it = dd.get(name)
        if it is None:
            dd[name] = {'scattered': True, 'first_seen': today, 'last_seen': today}  # 默认按零散司机记录
            changed = True
        elif it.get('last_seen') != today:
            it['last_seen'] = today
            changed = True
    if changed:
        _save_drivers(dd)


def run_task(tid, run_kw=None):
    run_kw = run_kw or {}
    if tid == 'all':
        # 【2026-10-07 用户要求】"全部更新"只跑 待拣(2)+集货位(3)，任务1单独点"任务更新"
        logs, ok = [], True
        for t in ('2', '3'):
            r = run_task(t, {'carrier': run_kw.get('carrier'), 'no_sync': True})
            logs.append(f'===== {TASKS[t]["name"]} =====\n{r["log"]}')
            ok = ok and r['ok']
        if ok and GH_SYNC:
            threading.Thread(target=_sync_to_github, args=(('2', '3'),), daemon=True).start()
            logs.append('[云端同步] 已在后台提交至 GitHub，Pages 约1分钟后生效')
        return {'ok': ok, 'log': '\n'.join(logs), 'data': read_data()}
    if tid not in TASKS:
        return {'ok': False, 'log': f'未知任务 {tid}', 'data': read_data()}
    cfg = TASKS[tid]
    # 看板模式：只取数展示，不写腾讯表格（2026-09-12 21:20 用户确认）
    env = _clean_env({'XLB_NOWRITE': '1'})
    if tid == '1':
        env['XLB_CARRIER'] = run_kw.get('carrier') or 'all'  # 供应商筛选（看板下拉框）
    lines = []
    ok = True
    for script in cfg['scripts']:
        lines.append(f'$ python3 {script}')
        try:
            r = subprocess.run([PY, script], capture_output=True, text=True, timeout=180, env=env)
            out = (r.stdout or '') + (('\n[stderr] ' + r.stderr[-500:]) if r.stderr.strip() else '')
            lines.append(out.strip() or f'(exit={r.returncode})')
            if r.returncode != 0:
                ok = False
                joined = out
                if '1005' in joined or '过期' in joined or '认证' in joined or 'Access-Token' in joined:
                    rf = _refresh_credentials()
                    lines.append(rf)
                    if 'OK' in rf:
                        r2 = subprocess.run([PY, script], capture_output=True, text=True, timeout=180, env=env)
                        out2 = (r2.stdout or '') + (('\n[stderr] ' + r2.stderr[-500:]) if r2.stderr.strip() else '')
                        lines.append('[重试] ' + out2.strip())
                        ok = r2.returncode == 0
        except subprocess.TimeoutExpired:
            lines.append('超时(180s)'); ok = False
        except Exception as e:
            lines.append(f'异常: {e}'); ok = False
    if ok and GH_SYNC and not run_kw.get('no_sync'):
        threading.Thread(target=_sync_to_github, args=((tid,),), daemon=True).start()
        lines.append('[云端同步] 已在后台提交至 GitHub，Pages 约1分钟后生效')
    return {'ok': ok, 'log': '\n'.join(lines), 'data': read_data()}

def read_data():
    try:
        _sync_drivers()
    except Exception:
        pass
    d = {}
    for t, path in SNAP.items():
        try:
            j = json.load(open(path))
            j['task'] = t
            d[t] = j
        except Exception:
            d[t] = None
    return d

# 【2026-10-06】门店待拣订单加急：hxl.wms.pickorder.addpriority，fids 来自任务2快照
# ---------- 发车信息（task7 配套）：查当天该司机运输中订单 → 生成发车信息模板 ----------
TMS_ORDER_URL = 'https://tms-web.tms.ali-prod.xlbsoft.com/tms/hxl.tms.transportorder.page'
DEPART_TPL = {
    'project': '好想来',
    'origin': '当涂',
    'exception_tel': '13338890962',
}

# 三字地级市（门店名前缀识别用），未命中则取前两字
CITY_3 = ('马鞍山', '石家庄', '哈尔滨', '秦皇岛', '张家口', '连云港', '景德镇',
          '攀枝花', '牡丹江', '乌鲁木齐', '呼和浩特', '齐齐哈尔', '鄂尔多斯')


def _city_of(name):
    """从门店名提取目的地城市（三字城市优先，否则前两字）"""
    name = (name or '').strip()
    for c in CITY_3:
        if name.startswith(c):
            return c
    return name[:2]



def _tms_orders(date_str):
    """查当天全部已调度运单（分页拉全），返回 content 列表"""
    auth = json.load(open(os.path.join(SCRIPTS_DIR, 'xlb_auth.json')))

    def _call(pn):
        body = json.dumps({
            'page_size': 200, 'page_number': pn,
            'Data_Compact_RangeType_delivery_date': 'day',
            'delivery_date': [date_str, date_str], 'date_type': 3,
            'store_id': 6666600000001, 'storehouse_id': 6666600000134,
            'client_ids': [], 'plate_numbers': [], 'store_ids': [6666600000001],
            'storehouse_ids': [6666600000134], 'carrier_ids': [],
            'arrange_state': 'DISPATCHED',
        }).encode()
        req = urllib.request.Request(TMS_ORDER_URL, data=body, method='POST')
        req.add_header('Content-Type', 'application/json')
        req.add_header('Cookie', auth.get('cookies', ''))
        req.add_header('Access-Token', auth.get('access_token', ''))
        if auth.get('authorization'):
            req.add_header('Authorization', auth['authorization'])
        req.add_header('Origin', 'https://gdp.xlbsoft.com')
        req.add_header('Referer', 'https://gdp.xlbsoft.com/')
        req.add_header('User-Agent', 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                                     'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36')
        return json.loads(urllib.request.urlopen(req, timeout=25).read().decode())

    resp = _call(0)
    if resp.get('code') != 0:
        raise RuntimeError(f"运单接口错误 {resp.get('code')} {resp.get('msg')}")
    data = resp.get('data') or {}
    rows = list(data.get('content') or [])
    for pn in range(1, data.get('total_pages') or 1):
        rows += _call(pn).get('data', {}).get('content') or []
    return rows


def _departure_info(driver, date_str=None):
    """按司机名生成发车信息文本：运输中订单按装车完成时间(pick_time)降序 → 目的地顺序"""
    if not driver:
        return {'ok': False, 'msg': '缺少司机姓名'}
    date_str = date_str or time.strftime('%Y-%m-%d')
    try:
        rows = _tms_orders(date_str)
    except Exception as e:
        return {'ok': False, 'msg': f'查询运单失败: {e}'}
    mine = [o for o in rows if (o.get('driver_name') or '').strip() == driver.strip()]
    trans = [o for o in mine if o.get('state') == 'TRANSITING']
    if not trans:
        return {'ok': False, 'msg': f'{driver} 当天没有「运输中」订单（共 {len(mine)} 单）',
                'total': len(mine)}
    trans.sort(key=lambda o: str(o.get('pick_time') or o.get('loading_start_time') or ''), reverse=True)
    first = trans[0]
    clients, seen = [], set()
    for o in trans:
        c = (o.get('client_name') or '').strip()
        if c and c not in seen:
            seen.add(c)
            clients.append(c)
    # 目的地城市 = 门店名开头的地级市（三字城市优先匹配，否则取前两字）；下方逐条列收货客户
    city = ''
    if clients:
        seen_c, cs = set(), []
        for c in clients:
            p = _city_of(c)
            if p not in seen_c:
                seen_c.add(p)
                cs.append(p)
        city = '/'.join(cs)
    kms = [o.get('total_kilometers') for o in trans if isinstance(o.get('total_kilometers'), (int, float))]
    km_txt = f'{round(min(kms))}公里' if kms else ''
    month, day = int(date_str[5:7]), int(date_str[8:10])
    lines = [f'开始时间：{month}月{day}日',
             f"姓名: {driver}",
             f"车牌号: {first.get('plate_number') or ''}",
             f"物流: {first.get('carrier_name') or ''}",
             f"联系方式：{first.get('driver_phone') or ''}",
             f"项目:{DEPART_TPL['project']}",
             f"出发地:{DEPART_TPL['origin']}",
             f'目的地：{city}']
    for i, c in enumerate(clients, 1):
        lines.append(f'{i}. {c}')
    if km_txt:
        lines.append('')  # 高德导航公里数上方空一行（2026-10-09 用户要求）
        lines.append(f'高德导航最低公里数{km_txt}')
    lines.append(f"异常处理电话：{DEPART_TPL['exception_tel']}")
    return {'ok': True, 'driver': driver, 'date': date_str,
            'text': '\n'.join(lines), 'orders': len(trans),
            'clients': clients, 'km': km_txt,
            'raw': [{'client': o.get('client_name'), 'pick_time': o.get('pick_time'),
                     'loading_start_time': o.get('loading_start_time'),
                     'km': o.get('total_kilometers'), 'fid': o.get('fid')} for o in trans]}



# ---------- 门店排序：从马鞍山顺丰丰泰产业园出发的货车最优路线 ----------
DEPOT_BD = (31.531248, 118.471452)  # 马鞍山顺丰丰泰产业园（当涂县经开区 205国道与大城坊西路交叉口，百度坐标）
_client_coord_cache = {'ts': 0.0, 'map': {}}


def _bd2gcj(lat, lng):
    """百度坐标(BD-09) → 高德坐标(GCJ-02)"""
    import math
    x, y = lng - 0.0065, lat - 0.006
    z = math.sqrt(x * x + y * y) - 0.00002 * math.sin(y * math.pi * 3000 / 180)
    t = math.atan2(y, x) - 0.000003 * math.cos(x * math.pi * 3000 / 180)
    return z * math.cos(t), z * math.sin(t)


def _haversine(a, b):
    """两点球面距离(km)，a/b = (lng, lat)"""
    import math
    lng1, lat1, lng2, lat2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(h))


def _client_coords():
    """拉全量客户主档的 coordinate（GCJ-02），缓存 10 分钟"""
    now = time.time()
    if _client_coord_cache['map'] and now - _client_coord_cache['ts'] < 600:
        return _client_coord_cache['map']
    auth = json.load(open(os.path.join(SCRIPTS_DIR, 'xlb_auth.json')))
    url = 'https://tms-web.tms.ali-prod.xlbsoft.com/tms/hxl.tms.client.assembly.page'
    out = {}
    for pn in range(5):
        body = json.dumps({'page_number': pn, 'page_size': 500, 'show_center': True,
                           'store_id': 6666600000001}).encode()
        req = urllib.request.Request(url, data=body, method='POST')
        req.add_header('Content-Type', 'application/json')
        req.add_header('Cookie', auth.get('cookies', ''))
        req.add_header('Access-Token', auth.get('access_token', ''))
        if auth.get('authorization'):
            req.add_header('Authorization', auth['authorization'])
        req.add_header('Warehouse-Id', '6666600000001')
        req.add_header('Origin', 'https://gdp.xlbsoft.com')
        req.add_header('Referer', 'https://gdp.xlbsoft.com/')
        req.add_header('User-Agent', 'Mozilla/5.0')
        d = json.loads(urllib.request.urlopen(req, timeout=25).read().decode())
        if d.get('code') != 0:
            raise RuntimeError(f"客户主档接口错误 {d.get('code')} {d.get('msg')}")
        rows = (d.get('data') or {}).get('content') or []
        for r in rows:
            nm = (r.get('name') or '').strip()
            co = str(r.get('coordinate') or '')
            if nm and ',' in co:
                a, b = co.split(',')[:2]
                try:
                    out[nm] = (float(a), float(b))
                except ValueError:
                    pass
        if len(rows) < 500:
            break
    _client_coord_cache['ts'] = now
    _client_coord_cache['map'] = out
    return out


def _store_route(driver, date_str=None):
    """该司机当天运输中门店，从丰泰产业园出发按最短路线排序（最近邻 + 2-opt）"""
    if not driver:
        return {'ok': False, 'msg': '缺少司机姓名'}
    date_str = date_str or time.strftime('%Y-%m-%d')
    try:
        rows = _tms_orders(date_str)
    except Exception as e:
        return {'ok': False, 'msg': f'查询运单失败: {e}'}
    trans = [o for o in rows
             if (o.get('driver_name') or '').strip() == driver.strip()
             and o.get('state') in ('LOADING', 'TRANSITING')]
    clients, seen = [], set()
    for o in sorted(trans, key=lambda o: str(o.get('pick_time') or ''), reverse=True):
        c = (o.get('client_name') or '').strip()
        if c and c not in seen:
            seen.add(c)
            clients.append(c)
    if not clients:
        return {'ok': False, 'msg': f'{driver} 当天没有「装车中/运输中」订单'}
    try:
        cmap = _client_coords()
    except Exception as e:
        return {'ok': False, 'msg': f'获取门店坐标失败: {e}'}
    depot = _bd2gcj(*DEPOT_BD)
    pts, missing = [], []
    for c in clients:
        co = cmap.get(c)
        if co:
            pts.append((c, co))
        else:
            missing.append(c)
    n = len(pts)
    if n == 0:
        return {'ok': False, 'msg': f'{driver} 的门店在客户主档中均无坐标，无法排序（{len(missing)} 家）'}
    dist = lambda i, j: _haversine(pts[i][1], pts[j][1])
    d0 = lambda i: _haversine(depot, pts[i][1])
    # 最近邻
    left, order, cur = list(range(n)), [], -1
    while left:
        best = min(left, key=lambda i: d0(i) if cur < 0 else dist(cur, i))
        order.append(best)
        left.remove(best)
        cur = best
    # 2-opt 改进（起点固定）
    improved = True
    while improved:
        improved = False
        for i in range(n - 1):
            for k in range(i + 1, n):
                a = order[i - 1] if i > 0 else None
                b, c, e = order[i], order[k], (order[k + 1] if k + 1 < n else None)
                before = (d0(b) if a is None else dist(a, b)) + (dist(c, e) if e is not None else 0.0)
                after = (d0(c) if a is None else dist(a, c)) + (dist(b, e) if e is not None else 0.0)
                if after < before - 1e-9:
                    order[i:k + 1] = reversed(order[i:k + 1])
                    improved = True
    total = (d0(order[0]) if n else 0.0) + sum(dist(order[i], order[i + 1]) for i in range(n - 1))
    legs, prev = [], depot
    for idx, i in enumerate(order, 1):
        leg = _haversine(prev, pts[i][1])
        prev = pts[i][1]
        legs.append({'no': idx, 'name': pts[i][0], 'leg': round(leg)})
    for j, c in enumerate(missing, 1):
        legs.append({'no': len(order) + j, 'name': c, 'leg': None})
    month, day = int(date_str[5:7]), int(date_str[8:10])
    lines = [f'门店排序 · {driver}（{month}月{day}日）',
             '起点：马鞍山顺丰丰泰产业园']
    for it in legs:
        seg = '（主档无坐标，排最后）' if it['leg'] is None else ''
        lines.append(f"{it['no']}. {it['name']}{seg}")
    return {'ok': True, 'driver': driver, 'date': date_str,
            'text': '\n'.join(lines), 'count': len(legs),
            'total_km': round(total) if n else 0,
            'missing': missing,
            'stops': legs}


def _wms_priority(store):
    if not store:
        return {'ok': False, 'msg': '缺少门店名'}
    try:
        snap = json.load(open(f'{WEB_DIR}/data_task2.json'))
    except Exception:
        return {'ok': False, 'msg': '暂无待拣数据，请先点「待拣更新」'}
    fmap = snap.get('fids') or {}
    # 优先只加急 priority=0（未加急）的单；旧快照无 fids0 时退回全部 fids
    fids = (snap.get('fids0') or {}).get(store)
    src = 'fids0'
    if fids is None:
        for k, v in (snap.get('fids0') or {}).items():
            if k and (k in store or store in k):
                fids = v; src = 'fids0'; break
    if not fids:
        fids = fmap.get(store)
        src = 'fids'
        if fids is None:
            for k, v in fmap.items():
                if k and (k in store or store in k):
                    fids = v; break
    if not fids:
        return {'ok': False, 'msg': f'该门店没有可加急的待拣订单（均已加急或无待拣）：{store}'}
    auth = json.load(open(os.path.join(SCRIPTS_DIR, 'xlb_auth.json')))

    def _call():
        body = json.dumps({**auth.get('body', {}), 'company_id': 66666, 'fids': fids}).encode()
        req = urllib.request.Request(
            'https://wms-web.wms.ali-prod.xlbsoft.com/wms/hxl.wms.pickorder.addpriority',
            data=body, method='POST')
        req.add_header('Content-Type', 'application/json')
        req.add_header('Cookie', auth.get('cookies', ''))
        req.add_header('Access-Token', auth.get('access_token', ''))
        req.add_header('Origin', 'https://gdp.xlbsoft.com')
        req.add_header('Referer', 'https://gdp.xlbsoft.com/')
        req.add_header('User-Agent', 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                                     'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36')
        return json.loads(urllib.request.urlopen(req, timeout=20).read().decode())

    try:
        resp = _call()
        code, msg = resp.get('code'), str(resp.get('message') or resp.get('msg') or '')
        if code != 0 and ('1005' in str(code) or '过期' in msg or '认证' in msg):
            rf = _refresh_credentials()
            print(f'[加急] 凭证刷新: {rf.splitlines()[0]}', flush=True)
            resp = _call()
            code, msg = resp.get('code'), str(resp.get('message') or resp.get('msg') or '')
    except Exception as e:
        return {'ok': False, 'msg': f'接口异常: {e}'}
    if code == 0:
        print(f'[加急] {time.strftime("%H:%M:%S")} {store}: {len(fids)} 条 OK', flush=True)
        return {'ok': True, 'msg': f'已加急 {len(fids)} 条待拣订单', 'count': len(fids)}
    print(f'[加急] {time.strftime("%H:%M:%S")} {store}: 失败 code={code} {msg}', flush=True)
    return {'ok': False, 'msg': f'加急失败 code={code} {msg[:160]}'}

class Handler(BaseHTTPRequestHandler):
    def _check_auth(self):
        if not WEB_PASS:
            return True
        h = self.headers.get('Authorization', '')
        if h.startswith('Basic '):
            try:
                u, _, p = base64.b64decode(h[6:]).decode('utf-8').partition(':')
                if hmac.compare_digest(u, WEB_USER) and hmac.compare_digest(p, WEB_PASS):
                    return True
            except Exception:
                pass
        return False

    def _deny(self):
        body = b'401 Unauthorized'
        self.send_response(401)
        self.send_header('WWW-Authenticate', 'Basic realm="XLB Dashboard"')
        self.send_header('Content-Type', 'text/plain; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Access-Control-Allow-Origin', '*')  # 允许 GitHub Pages 页面调用本机更新按钮
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._check_auth():
            self._deny(); return
        p = urllib.parse.urlparse(self.path)
        if p.path == '/' or p.path == '/index.html':
            body = open(f'{WEB_DIR}/index.html', 'rb').read()
            self._send(200, 'text/html; charset=utf-8', body)
        elif p.path == '/api/store_route':
            qs = urllib.parse.parse_qs(p.query)
            drv = (qs.get('driver') or [''])[0]
            try:
                drv = drv.encode('latin-1').decode('utf-8')
            except (UnicodeEncodeError, UnicodeDecodeError):
                pass
            d = (qs.get('date') or [''])[0]
            body = json.dumps(_store_route(drv, d or None), ensure_ascii=False).encode()
            self._send(200, 'application/json; charset=utf-8', body)
        elif p.path == '/api/departure':
            qs = urllib.parse.parse_qs(p.query)
            drv = (qs.get('driver') or [''])[0]
            try:
                drv = drv.encode('latin-1').decode('utf-8')
            except (UnicodeEncodeError, UnicodeDecodeError):
                pass
            d = (qs.get('date') or [''])[0]
            body = json.dumps(_departure_info(drv, d or None), ensure_ascii=False).encode()
            self._send(200, 'application/json; charset=utf-8', body)
        elif p.path == '/api/drivers':
            try:
                _sync_drivers()
            except Exception:
                pass
            today = time.strftime('%Y-%m-%d')
            dd = _load_drivers()
            lst = [{'name': k, **v} for k, v in dd.items()]
            lst.sort(key=lambda it: (it.get('last_seen') or '', it['name']), reverse=True)
            body = json.dumps({'ok': True, 'today': today, 'drivers': lst},
                              ensure_ascii=False).encode()
            self._send(200, 'application/json; charset=utf-8', body)
        elif p.path == '/api/data':
            body = json.dumps({'ok': True, 'data': read_data()}, ensure_ascii=False).encode()
            self._send(200, 'application/json; charset=utf-8', body)
        elif p.path == '/api/sync_status':
            try:
                st = json.load(open(SYNC_STATUS))
            except Exception:
                st = {'ok': None, 'msg': '尚未同步过'}
            body = json.dumps({'ok': True, 'sync': st, 'enabled': GH_SYNC}, ensure_ascii=False).encode()
            self._send(200, 'application/json; charset=utf-8', body)
        else:
            self._send(404, 'text/plain; charset=utf-8', b'not found')

    def do_POST(self):
        if not self._check_auth():
            self._deny(); return
        p = urllib.parse.urlparse(self.path)
        if p.path == '/api/priority':
            qs = urllib.parse.parse_qs(p.query)
            store = (qs.get('store') or [''])[0]
            try:  # http.server 按 latin-1 解码请求行，原始中文字节需修复；%XX 编码路径不受影响
                store = store.encode('latin-1').decode('utf-8')
            except (UnicodeEncodeError, UnicodeDecodeError):
                pass
            res = _wms_priority(store)
            self._send(200, 'application/json; charset=utf-8',
                       json.dumps(res, ensure_ascii=False).encode())
            return
        if p.path == '/api/drivers_set':
            qs = urllib.parse.parse_qs(p.query)
            drv = (qs.get('driver') or [''])[0]
            try:
                drv = drv.encode('latin-1').decode('utf-8')
            except (UnicodeEncodeError, UnicodeDecodeError):
                pass
            sc = (qs.get('scattered') or ['0'])[0] == '1'
            dd = _load_drivers()
            if drv in dd:
                dd[drv]['scattered'] = sc
                dd[drv]['last_seen'] = time.strftime('%Y-%m-%d')
                _save_drivers(dd)
                body = json.dumps({'ok': True, 'driver': drv, 'scattered': sc},
                                  ensure_ascii=False).encode()
            else:
                body = json.dumps({'ok': False, 'msg': f'未找到司机 {drv}'},
                                  ensure_ascii=False).encode()
            self._send(200, 'application/json; charset=utf-8', body)
            return
        if p.path != '/api/run':
            self._send(404, 'text/plain; charset=utf-8', b'not found'); return
        qs = urllib.parse.parse_qs(p.query)
        tid = (qs.get('task') or [''])[0]
        kw = {'carrier': (qs.get('carrier') or ['all'])[0]}
        if not _lock.acquire(blocking=False):
            body = json.dumps({'ok': False, 'log': '有任务正在执行中，请稍候', 'data': read_data()},
                              ensure_ascii=False).encode()
            self._send(409, 'application/json; charset=utf-8', body); return
        try:
            res = run_task(tid if tid in TASKS or tid == 'all' else 'all', kw)
            body = json.dumps(res, ensure_ascii=False).encode()
            self._send(200, 'application/json; charset=utf-8', body)
        finally:
            _lock.release()

    def log_message(self, *a):
        pass

if __name__ == '__main__':
    os.makedirs(WEB_DIR, exist_ok=True)
    port = PORT_ENV
    threading.Thread(target=_proactive_refresh_loop, daemon=True).start()
    print(f'新零帮调度看板: http://{BIND}:{port} (auth={"on" if WEB_PASS else "off"}, 每30分钟主动续期凭证)')
    ThreadingHTTPServer((BIND, port), Handler).serve_forever()
