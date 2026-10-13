#!/usr/bin/env python3
"""云端批量拉数入口（GitHub Actions 用）：依次跑 task1/2/3 的 NOWRITE 模式。

凭证策略（2026-10-12 改造，解决静态凭证过期问题）：
  1. 若注入了 secret XLB_AUTH_B64（/tmp/xlb_auth.json 的 base64），先落盘并探活；
  2. 探活失败或无注入时，用 secrets XLB_ACCOUNT/XLB_COMPANY_ID/XLB_PASSWORD
     走纯 API 密码登录（pwd.user.find → user.account.login）拿全新 32位AT，
     凭证永续，不再依赖浏览器刷新；
  3. 仍失败则报错退出（exit 2），Actions 显示红色可察觉。

快照 data_task{1,2,3}.json 写到仓库根目录，由 Pages 直接展示。
"""
import os, sys, json, base64, subprocess, time, datetime
import urllib.error, urllib.parse, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ['XLB_NOWRITE'] = '1'
os.environ['XLB_WEB_DIR'] = HERE
os.environ.setdefault('XLB_AUTH', os.path.join(HERE, 'auth.json'))
AUTH = os.environ['XLB_AUTH']

GDP = os.environ.get('XLB_GDP', 'https://gdp.xlbsoft.com')
UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36')
WMS_PROBE = 'https://wms-web.wms.ali-prod.xlbsoft.com/wms/hxl.wms.pickorder.page'
PROBE_BODY = {
    'operator_store_id': 6666600000001, 'company_id': 66666, 'client_ids': [],
    'storehouse_id': 6666600000134, 'store_id': 6666600000001, 'region_ids': [],
    'pick_states': ['INIT'], 'page': 1, 'size': 1,
}
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 禁代理


def _log(msg):
    print(f'[{time.strftime("%H:%M:%S")}] {msg}', flush=True)


def _post(path_or_url, body, extra_headers=None, timeout=25):
    url = path_or_url if path_or_url.startswith('http') else GDP + path_or_url
    h = {'Content-Type': 'application/json', 'Origin': GDP, 'Referer': GDP + '/',
         'User-Agent': UA, 'Accept': 'application/json, text/plain, */*'}
    h.update(extra_headers or {})
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=h, method='POST')
    try:
        with OPENER.open(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except Exception:
            return {'code': e.code, 'msg': f'HTTP {e.code}'}
    except Exception as e:
        return {'code': -1, 'msg': str(e)}


def probe_at(at):
    """用 32 位 AT 真实调 WMS 探活：code=0 即有效"""
    j = _post(WMS_PROBE, PROBE_BODY, {'Access-Token': at})
    return j.get('code') == 0, f'code={j.get("code")} msg={j.get("msg")}'


def probe_at_retry(at, attempts=3, delay=4):
    """探活带重试：网络抖动（code=-1/超时）不算凭证过期，避免误触发登录；
    只有明确 1005/登陆已过期才立即判定失效"""
    info = ''
    for i in range(attempts):
        ok, info = probe_at(at)
        if ok:
            return True, info
        if '1005' in info or '登陆' in info or '登录' in info:
            return False, info
        if i < attempts - 1:
            _log(f'探活异常（{info}），{delay}s 后重试')
            time.sleep(delay)
    return False, info


def password_login(acc, company_id, pwd):
    """纯 API 密码登录，返回 dict(access_token, jwt, refresh_token, token_type, user)
    2026-10-13：登录接口从海外 runner 访问国内服务偶发超时（urlopen timed out），
    故单次调用超时放宽到 45s；上层再做整体重试"""
    ja = _post('/erp-mdm/hxl.erp.account.pwd.user.find',
               {'account': acc, 'company_id': company_id, 'pwd': pwd}, timeout=45)
    if ja.get('code') != 0:
        raise RuntimeError(f'密码校验失败: code={ja.get("code")} msg={ja.get("msg")}')
    d = ja.get('data') or {}
    lt = d.get('login_token')
    ulist = d.get('account_user_list') or []
    uid = (ulist[0] or {}).get('id') if ulist else None
    if not lt:
        raise RuntimeError('未返回 login_token')
    _log(f'A. 密码校验通过 login_token={lt[:18]}… 用户={((ulist[0] or {}).get("name") if ulist else "?")}')

    jb = _post('/erp-mdm/hxl.erp.user.account.login',
               {'account': acc, 'company_id': company_id, 'source': 'WEB',
                'user_id': uid, 'login_source': 'WEB'},
               {'Access-token': lt}, timeout=45)
    if jb.get('code') != 0:
        raise RuntimeError(f'换取凭据失败: code={jb.get("code")} msg={jb.get("msg")}')
    ud = jb.get('data') or {}
    at = ud.get('access_token')
    if not at:
        raise RuntimeError('未返回 32 位 access_token')
    oa = ud.get('oauth2') or {}
    _log(f'B. 换取凭据成功 AT={at}')
    return {'access_token': at,
            'jwt': oa.get('access_token') or '',
            'refresh_token': oa.get('refresh_token') or ud.get('refresh_token') or '',
            'token_type': oa.get('token_type') or 'Bearer',
            'user': ud, 'user_id': uid, 'company_id': company_id, 'account': acc}


def write_auth(res):
    ck = {}
    if os.path.exists(AUTH):
        try:
            for part in (json.load(open(AUTH)).get('cookies') or '').split(';'):
                if '=' in part:
                    k, v = part.strip().split('=', 1)
                    ck[k] = v
        except Exception:
            pass
    q = urllib.parse.quote
    if res['jwt']:
        ck['xlb_token'] = q(f'{res["token_type"]} {res["jwt"]}')
    if res['refresh_token']:
        ck['xlb_refresh_token'] = res['refresh_token']
    ck['xlb_profile_access_token'] = res['access_token']
    ck['production_qiankun_token'] = q(json.dumps(
        {'account': res['account'], 'companyId': res['company_id'],
         'accessToken': res['access_token']}, ensure_ascii=False, separators=(',', ':')))
    if res.get('user_id'):
        ck['User-Id'] = str(res['user_id'])
    ck['account'] = str(res['account'])
    ck['company_id'] = str(res['company_id'])
    ck['xlb_client_secret'] = '31869922-f120-2d61-e2fb-b5f5f668928f'
    auth = {
        'access_token': res['access_token'],
        'authorization': (f'{res["token_type"]} {res["jwt"]}' if res['jwt'] else 'Bearer'),
        'refresh_token': res['refresh_token'],
        'cookies': '; '.join(f'{k}={v}' for k, v in ck.items()),
        # url/task3、body/task2 依赖的字段（与浏览器抓包的 /tmp/xlb_auth.json 结构一致）
        'url': WMS_DISTRIBUTE_URL,
        'body': dict(BODY_BASE),
        'saved_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'source': 'cloud_password_login',
    }
    json.dump(auth, open(AUTH, 'w'), ensure_ascii=False, indent=1)
    _log(f'D. 凭证已写入 {AUTH}')


WMS_DISTRIBUTE_URL = 'https://wms-web.wms.ali-prod.xlbsoft.com/wms/hxl.wms.distribute.center.find'
BODY_BASE = {'operator_store_id': 6666600000001, 'company_id': 66666, 'client_ids': [],
             'storehouse_id': 6666600000134, 'store_id': 6666600000001, 'region_ids': []}


def _normalize_auth(d):
    """修复旧版本 write_auth 写入的 body/url 结构（task2 依赖 dict body，task3 依赖 wms-web url）"""
    fixed = False
    if not isinstance(d.get('body'), dict) or not d['body']:
        d['body'] = dict(BODY_BASE); fixed = True
    if 'wms-web' not in str(d.get('url') or ''):
        d['url'] = WMS_DISTRIBUTE_URL; fixed = True
    if fixed:
        json.dump(d, open(AUTH, 'w'), ensure_ascii=False, indent=1)
        _log('已规范化 auth.json 的 body/url 结构')


def login_with_retry(acc, comp, pwd, attempts=3, delay=8):
    """密码登录带重试：云端到国内接口偶发超时，重试可显著降低 run 失败率"""
    last = None
    for i in range(attempts):
        try:
            res = password_login(acc, comp, pwd)
            if i:
                _log(f'（第 {i + 1} 次尝试成功）')
            return res
        except Exception as e:
            last = e
            _log(f'密码登录第 {i + 1}/{attempts} 次失败: {e}')
            if i < attempts - 1:
                time.sleep(delay * (i + 1))     # 8s / 16s 退避
    raise RuntimeError(f'密码登录重试 {attempts} 次仍失败: {last}')


def ensure_credentials():
    """确保 auth.json 存在且 AT 有效；必要时密码登录重建
    2026-10-13：auth.json 由 workflow 的 actions/cache 跨 run 复用（关键！），
    因此正常情况大部分 run 直接命中缓存凭证、跳过登录；只有缓存过期才登录"""
    b64 = os.environ.get('XLB_AUTH_B64', '')
    if b64 and not os.path.exists(AUTH):
        try:
            open(AUTH, 'wb').write(base64.b64decode(b64))
        except Exception as e:
            _log(f'XLB_AUTH_B64 解码失败: {e}')

    # 2) 已有凭证探活（优先缓存里的 auth.json）
    if os.path.exists(AUTH):
        try:
            d = json.load(open(AUTH))
            at = d.get('access_token')
            if at:
                ok, info = probe_at_retry(at)
                if ok:
                    _log(f'已有凭证有效（AT尾号…{at[-6:]}），跳过登录')
                    _normalize_auth(d)   # 修复旧版本写入的 body/url 结构
                    return True
                _log(f'已有凭证失效（{info}），尝试重新获取')
        except Exception as e:
            _log(f'读取凭证失败: {e}')

    # 3) 密码登录（带重试）
    acc = os.environ.get('XLB_ACCOUNT')
    comp = os.environ.get('XLB_COMPANY_ID')
    pwd = os.environ.get('XLB_PASSWORD')
    if not (acc and comp and pwd):
        _log('错误: 凭证失效且缺少 XLB_ACCOUNT/XLB_COMPANY_ID/XLB_PASSWORD secrets')
        return False
    try:
        res = login_with_retry(acc.strip(), int(comp), pwd)
    except Exception as e:
        _log(f'密码登录失败: {e}')
        return False
    ok, info = probe_at(res['access_token'])
    if not ok:
        _log(f'警告: 新 AT 探活异常（{info}），仍落盘继续尝试')
    write_auth(res)
    return True


def main():
    if not ensure_credentials():
        print('错误: 无法获得有效凭证'); sys.exit(2)

    fails = 0
    for label, script in [('任务1调度', 'task1.py'), ('任务2待拣', 'task2.py'), ('任务3集货位', 'task3_direct.py')]:
        try:
            r = subprocess.run([sys.executable, os.path.join(HERE, script)],
                               capture_output=True, text=True, timeout=240, cwd=HERE)
            lines = [l for l in (r.stdout + r.stderr).strip().splitlines() if l.strip()]
            print(f'{label}: exit={r.returncode} | ' + (lines[-1] if lines else '(无输出)'))
            if r.returncode != 0:
                for l in lines[-5:]: print('   ', l)
                fails += 1
        except Exception as e:
            print(f'{label}: 异常 {e}'); fails += 1

    print('--- 快照校验 ---')
    for f in ('data_task1.json', 'data_task2.json', 'data_task3.json'):
        try:
            d = json.load(open(os.path.join(HERE, f)))
            print(f, 'ts=', time.strftime('%m-%d %H:%M:%S', time.localtime(d['ts'])),
                  'total=', d.get('total'), 'groups=', len(d.get('groups') or d.get('stores') or {}))
        except Exception as e:
            print(f, '缺失:', e); fails += 1
    sys.exit(1 if fails else 0)


if __name__ == '__main__':
    main()
