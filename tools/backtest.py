# -*- coding: utf-8 -*-
"""
バックテスト: Get_NAVWARN / Get_LAUNCHES の Git 履歴で毎日ジョブを過去に遡って再現し、
score.py の全パイプライン(予測→凍結→答え合わせ→成績)を実データで検証する。

使い方:
  python tools/backtest.py <NAVWARNクローン> <LAUNCHESクローン> <satcat.json> <tle_recent.json>
出力: tools/backtest_ledger.json / backtest_report.txt (リポジトリにはコミットしない)
"""
import sys, os, json, subprocess, datetime, bisect, io

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import score as S

NAVWARN_REPO, LAUNCHES_REPO, SATCAT_PATH, TLE_PATH = sys.argv[1:5]
OUTDIR = os.path.dirname(os.path.abspath(__file__))
FILES = ['DailyMemIV.txt', 'DailyMemXII.txt', 'DailyMemLAN.txt', 'DailyMemPAC.txt', 'DailyMemARC.txt']

def git_out(repo, args):
    return subprocess.run(['git', '-C', repo] + args, capture_output=True,
                          text=True, encoding='utf-8', errors='replace').stdout

def commit_list(repo):
    """[(datetime, hash)] 古い順"""
    out = []
    for line in git_out(repo, ['log', '--reverse', '--format=%H %cI']).splitlines():
        h, iso = line.split()
        out.append((datetime.datetime.fromisoformat(iso).astimezone(datetime.timezone.utc), h))
    return out

def show_at(repo, commits, when, path):
    """when以前の最新コミットのファイル内容 (無ければ None)"""
    times = [c[0] for c in commits]
    i = bisect.bisect_right(times, when) - 1
    if i < 0:
        return None
    r = subprocess.run(['git', '-C', repo, 'show', '%s:%s' % (commits[i][1], path)],
                       capture_output=True, text=True, encoding='utf-8', errors='replace')
    return r.stdout if r.returncode == 0 else None

def main():
    nav_commits = commit_list(NAVWARN_REPO)
    lau_commits = commit_list(LAUNCHES_REPO)
    start = max(nav_commits[0][0], lau_commits[0][0]).date() + datetime.timedelta(days=1)
    today = datetime.datetime.now(datetime.timezone.utc)

    ledger_by_year = {2026: {'year': 2026, 'records': {}}}
    day = start
    n_days = 0
    while True:
        now = datetime.datetime(day.year, day.month, day.day, 3, 15, tzinfo=datetime.timezone.utc)
        if now > today:
            break
        lj = show_at(LAUNCHES_REPO, lau_commits, now, 'data/launches.json')
        if lj:
            try:
                launches = json.loads(lj)
                launches = launches.get('launches', launches.get('data')) if isinstance(launches, dict) else launches
            except Exception:
                launches = None
            if launches:
                warnings = []
                for f in FILES:
                    txt = show_at(NAVWARN_REPO, nav_commits, now, 'data/' + f)
                    if txt:
                        warnings += S.parse_warnings(txt, (now.year - 1, now.year, now.year + 1))
                S.step_predict(ledger_by_year, launches, warnings, now)
                S.step_freeze(ledger_by_year, launches, now)
                n_days += 1
        day += datetime.timedelta(days=1)

    # 答え合わせ: fetch をローカルファイルに差し替え(オフライン)
    satcat_txt = io.open(SATCAT_PATH, encoding='utf-8').read()
    tle_txt = io.open(TLE_PATH, encoding='utf-8').read()
    real_fetch = S.fetch
    S.fetch = lambda url, timeout=60: satcat_txt if 'satcat' in url else (tle_txt if 'tle_recent' in url else real_fetch(url, timeout))
    n_ans = S.step_answer(ledger_by_year, today)
    stats = S.step_stats(ledger_by_year)

    led = ledger_by_year[2026]
    io.open(os.path.join(OUTDIR, 'backtest_ledger.json'), 'w', encoding='utf-8').write(
        json.dumps(led, ensure_ascii=False, indent=1, sort_keys=True))

    # ── レポート ──
    lines = []
    lines.append('バックテスト: %s 〜 %s (%d日ぶん再現) / 答え合わせ %d件' % (start, today.date(), n_days, n_ans))
    lines.append('')
    lines.append('%-44s %-6s %7s %7s %7s %9s %9s %8s %s' % (
        'launch', 'site', 'i_pred', 'i_act', 'd_incl', 'raan_pred', 'raan_act', 'd_raan', 'flags'))
    scored = []
    for key, rec in sorted(led['records'].items(), key=lambda kv: kv[1].get('liftoff') or ''):
        a = rec.get('ans')
        p = rec.get('pred') or {}
        if not a or not a.get('identified'):
            continue
        site = (p.get('site_ref') or '?')[:6]
        lines.append('%-44s %-6s %7s %7s %7s %9s %9s %8s %s' % (
            (rec['name'] or '')[:44], site,
            p.get('incl'), a.get('incl'), a.get('d_incl'),
            a.get('raan_pred'), a.get('raan'), a.get('d_raan'),
            ','.join(rec['flags'])))
        if a.get('d_incl') is not None:
            scored.append(rec)
    lines.append('')
    di = [r['ans']['d_incl'] for r in scored if r['ans']['d_incl'] is not None]
    dr = [r['ans']['d_raan'] for r in scored if r['ans']['d_raan'] is not None]
    if di:
        lines.append('傾斜角: n=%d MAE=%.2f° 平均=%+.2f°' % (len(di), sum(abs(x) for x in di)/len(di), sum(di)/len(di)))
    if dr:
        lines.append('RAAN  : n=%d MAE=%.2f° 平均=%+.2f°' % (len(dr), sum(abs(x) for x in dr)/len(dr), sum(dr)/len(dr)))
    lines.append('')
    lines.append('=== stats.json (ロケット×射場) ===')
    for k, b in stats['by_rocket_site'].items():
        lines.append('%s : n=%d incl=%s raan=%s' % (k, b['n'], b['incl'], b['raan']))
    lines.append('')
    nf = [ (k, r) for k, r in led['records'].items() if r.get('frozen_at') and (not r.get('ans') or not r['ans'].get('identified')) ]
    lines.append('未採点(凍結済み) %d件: %s' % (len(nf), ', '.join(('%s[%s]' % ((r['name'] or '')[:30], ','.join(r['flags']))) for k, r in nf[:40])))
    rep = '\n'.join(lines)
    io.open(os.path.join(OUTDIR, 'backtest_report.txt'), 'w', encoding='utf-8').write(rep)
    print(rep)

if __name__ == '__main__':
    main()
