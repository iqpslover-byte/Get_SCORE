# -*- coding: utf-8 -*-
"""
Get_SCORE: 打上げ予測の台帳（予測→凍結→答え合わせ）を毎日更新する。

流れ:
  1. Get_LAUNCHES の launches.json と Get_NAVWARN の電文5ファイルを取得
  2. 予定打上げごとに警報を突合 → 傾斜角推定(H方式=OPs_LAB_Maps v3.13.307と同一) → 予測を台帳へ
  3. リフトオフ確認(status成功/失敗 or netを6時間経過)で予測を凍結
  4. 凍結済み・未採点のレコードに答えを付ける:
       - satcat.json で COSPAR グループを同定(打上げ日+射場コード)
       - tle_recent.json の TLE から実測傾斜角/RAAN
       - RAANはJ2歳差でリフトオフ時刻へ巻き戻し、予測は実リフトオフ時刻で再計算して比較
  5. data/ledger_YYYY.json (追記・凍結後の予測は不変) と data/stats.json を出力

計算はアプリ(OPs_LAB_Maps.html)と同一:
  - 傾斜角: orbInclFromZones = 各ゾーン重心方位をコリドー折返し→最遠50%集約→平均 (無補正acos式)
  - RAAN:   _raanVizNewBasis/_raanVizNewCompute (v3.24.8 軌道法線方式) + gstime(Vallado)
外部ライブラリ依存なし(標準ライブラリのみ)。
"""
import json, math, re, os, io, sys, urllib.request, datetime

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, 'data')

URL_LAUNCHES = 'https://raw.githubusercontent.com/iqpslover-byte/Get_LAUNCHES/main/data/launches.json'
URL_SATCAT   = 'https://raw.githubusercontent.com/iqpslover-byte/Get_TLE/main/data/satcat.json'
URL_TLE      = 'https://raw.githubusercontent.com/iqpslover-byte/Get_TLE/main/data/tle_recent.json'
URL_NAVWARN  = 'https://iqpslover-byte.github.io/Get_NAVWARN/data/DailyMem%s.txt'
NAVWARN_AREAS = ['IV', 'XII', 'LAN', 'PAC', 'ARC']

MODEL_VERSION = 'v2'   # v2(2026-07-16): fold誤反転修正・重心アンラップ修正・頂点緯度法(デブリ警報ペアリング)
D2R = math.pi / 180
R2D = 180 / math.pi

# ── 射場コリドー(打上げ許容方位) : アプリ ORB_PRESET_SITES と同一値 ──
PRESET_SITES = [
    {'name': 'Rocket Lab LC-1 (NZL)',            'lat': -39.2626,  'lon': 177.8648,   'az0': 30,  'az1': 200},
    {'name': 'Rocket Lab LC-2 , LC-3 (USA)',     'lat': 37.834056, 'lon': -75.483306, 'az0': 38,  'az1': 142},
    {'name': 'Cape Canaveral SFS (USA)',         'lat': 28.488889, 'lon': -80.577778, 'az0': 35,  'az1': 120},
    {'name': 'Kennedy Space Center (USA)',       'lat': 28.583333, 'lon': -80.65,     'az0': 35,  'az1': 120},
    {'name': 'Starbase (USA)',                   'lat': 25.991389, 'lon': -97.183611, 'az0': 90,  'az1': 130},
    {'name': 'Vandenberg SFB (USA)',             'lat': 34.7325,   'lon': -120.568056,'az0': 153, 'az1': 240},
    {'name': '種子島宇宙センター (JPN)',          'lat': 30.4,      'lon': 130.97,     'az0': 80,  'az1': 200},
    {'name': '北海道スペースポート (JPN)',        'lat': 42.5,      'lon': 143.441667, 'az0': 80,  'az1': 170},
    {'name': 'Guiana Space Centre (FRA)',        'lat': 5.239,     'lon': -52.7683,   'az0': 349, 'az1': 90},
    {'name': 'Satish Dhawan Space Centre (IND)', 'lat': 13.733,    'lon': 80.235,     'az0': 90,  'az1': 200},
    {'name': 'Baikonur Cosmodrome (KAZ)',        'lat': 45.965,    'lon': 63.305,     'az0': 347, 'az1': 65},
    {'name': '酒泉衛星発射センター (CHN)',        'lat': 40.958,    'lon': 100.291,    'az0': 130, 'az1': 160},
    {'name': '太原衛星発射センター (CHN)',        'lat': 38.849,    'lon': 111.608,    'az0': 160, 'az1': 200},
    {'name': '西昌衛星発射センター (CHN)',        'lat': 28.246,    'lon': 102.026,    'az0': 97,  'az1': 104},
    {'name': '中国文昌航天発射場 (CHN)',          'lat': 19.614,    'lon': 110.951,    'az0': 90,  'az1': 190},
    {'name': '海南商業宇宙発射場 (CHN)',          'lat': 19.60081,  'lon': 110.93575,  'az0': 90,  'az1': 190},
    {'name': 'Andøya Spaceport (NOR)',           'lat': 69.294167, 'lon': 16.020833,  'az0': 300, 'az1': 5},
]

# ── satcat SITEコード → 座標 (同定の射場照合用・確度の高いもののみ) ──
SATCAT_SITES = {
    'AFETR': (28.5, -80.57),    # Cape Canaveral / KSC (東部試験場)
    'AFWTR': (34.73, -120.57),  # Vandenberg (西部試験場)
    'WLPIS': (37.83, -75.49),   # Wallops
    'RLLB':  (-39.26, 177.86),  # Rocket Lab LC-1 (Mahia)
    'TTMTR': (45.97, 63.31),    # Baikonur
    'PLMSC': (62.93, 40.57),    # Plesetsk
    'VOSTO': (51.88, 128.33),   # Vostochny
    'FRGUI': (5.24, -52.77),    # Kourou
    'SRILR': (13.73, 80.24),    # Satish Dhawan
    'TANSC': (30.4, 130.97),    # 種子島
    'TNSTA': (30.4, 130.97),    # 種子島(別表記)
    'KSCUT': (31.25, 131.08),   # 内之浦
    'JSC':   (40.96, 100.29),   # 酒泉
    'TAISC': (38.85, 111.61),   # 太原
    'XICLF': (28.25, 102.03),   # 西昌
    'WSC':   (19.61, 110.95),   # 文昌
    'KODAK': (57.44, -152.34),  # Kodiak
    'SEMLS': (35.23, 53.92),    # Semnan
    'YAVNE': (31.88, 34.68),    # Palmachim
    'NSC':   (34.43, 127.54),   # Naro
}

# ════════════════════════ 汎用ジオメトリ (アプリ/match.pyと同式) ════════════════════════

def haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    dla = (lat2 - lat1) * D2R
    dlo = (lon2 - lon1) * D2R
    a = math.sin(dla/2)**2 + math.cos(lat1*D2R)*math.cos(lat2*D2R)*math.sin(dlo/2)**2
    return 2 * R * math.asin(min(1, math.sqrt(a)))

def bearing_deg(lat1, lon1, lat2, lon2):
    dL = (lon2 - lon1) * D2R
    y = math.sin(dL) * math.cos(lat2*D2R)
    x = math.cos(lat1*D2R)*math.sin(lat2*D2R) - math.sin(lat1*D2R)*math.cos(lat2*D2R)*math.cos(dL)
    return (math.atan2(y, x) * R2D + 360) % 360

def incl_from_az(lat, az):
    """無補正・絶対値なし: i = acos(cos(緯度)×sin(方位)) 。逆行は90-180°で出る"""
    c = max(-1, min(1, math.cos(lat*D2R) * math.sin(az*D2R)))
    return math.acos(c) * R2D

def zone_centroid(site_lon, zone):
    """ゾーン重心。経度はゾーン先頭頂点基準でアンラップしてから平均 (アプリ orbZoneCentroid v3.34.7 相当)。
       v2: 射場基準だと射場の対蹠経度をまたぐ遠方ゾーン(例:Starbase×インド洋デブリ区域)で
       頂点が+50°/-250°など別表現に割れ平均が壊れる。ゾーンは連続領域なので先頭頂点基準が安全。
       site_lon 引数は互換のため残置(未使用)。"""
    cla = sum(p[0] for p in zone) / len(zone)
    ref = zone[0][1]
    s = 0.0
    for p in zone:
        d = p[1] - ref
        while d > 180: d -= 360
        while d < -180: d += 360
        s += ref + d
    clo = s / len(zone)
    while clo > 180: clo -= 360
    while clo < -180: clo += 360
    return (cla, clo)

# ── 打上げコリドー (アプリ _azInRange/_azCenter/_azDist/_azFoldToCorridor と同一) ──

def _az_in_range(a, a0, a1):
    a = a % 360
    if a0 <= a1:
        return a0 <= a <= a1
    return a >= a0 or a <= a1   # 0°跨ぎ

def _az_center(a0, a1):
    span = (a1 - a0) % 360
    return (a0 + span/2) % 360

def _az_dist(a, b):
    d = abs(a - b) % 360
    return min(d, 360 - d)

def az_fold_to_corridor(az, a0, a1):
    """傾斜角を保つ4等価解{az,180-az,360-az,180+az}のうちコリドー内(無ければ中心に最近)を採用。
       v2(アプリv3.34.2): 生方位が既にコリドー内ならそのまま。中心近傍優先だと南南東打上げが
       ミラーの南南西へ誤反転する(VSFB 170.6°→189.4°)。折り返しはコリドー外の救済に限定。"""
    if a0 is None or a1 is None:
        return az
    if _az_in_range(az % 360, a0, a1):
        return az % 360
    cand = [x % 360 for x in (az, 180-az, 360-az, 180+az)]
    c = _az_center(a0, a1)
    inside = [a for a in cand if _az_in_range(a, a0, a1)]
    pool = inside if inside else cand
    return min(pool, key=lambda a: _az_dist(a, c))

def nearest_preset_site(lat, lon):
    """打上げパッド座標に最も近いプリセット射場(コリドー取得用)。100km超は該当なし"""
    best, bd = None, 1e9
    for s in PRESET_SITES:
        d = haversine_km(lat, lon, s['lat'], s['lon'])
        if d < bd:
            best, bd = s, d
    return best if bd <= 100 else None

# ════════════════════════ 傾斜角推定 (H方式 = アプリ orbInclFromZones と同一) ════════════════════════

def incl_from_zones(site_lat, site_lon, az0, az1, zones):
    """各ゾーン重心への方位(コリドー折返し)→傾斜角。最遠ゾーン距離の50%以上のみで平均"""
    per = []
    for z in zones:
        if not z:
            continue
        cla, clo = zone_centroid(site_lon, z)
        az = az_fold_to_corridor(bearing_deg(site_lat, site_lon, cla, clo), az0, az1)
        dist = haversine_km(site_lat, site_lon, cla, clo)
        per.append({'az': az, 'inc': incl_from_az(site_lat, az), 'dist': dist})
    if not per:
        return None
    dmax = max(p['dist'] for p in per)
    sel = [p for p in per if p['dist'] >= 0.5 * dmax] or per
    mean_inc = sum(p['inc'] for p in sel) / len(sel)
    ss = sum(math.sin(p['az']*D2R) for p in sel)
    cc = sum(math.cos(p['az']*D2R) for p in sel)
    mean_az = (math.atan2(ss, cc) * R2D + 360) % 360
    return {'inc': mean_inc, 'az': mean_az, 'zones_used': len(sel), 'zones_all': len(per)}

def apex_lat_estimate(zones):
    """頂点緯度法 (アプリ orbApexLatEstimate v3.34.5 と同一)。
       地上軌跡は緯度±傾斜角で折り返す(球面幾何の恒等式)ため、危険区域が折り返し(頂点)を
       含むなら最大|緯度|≒傾斜角。方位計算を使わないので、曲がる上昇コリドー(Starship等)や
       遠方デブリ区域で重心方位法より強い(実証: Starship IFT 26.88° vs 実際≈26.5°・重心方位法31.9°)。
       検出2段ゲート: (a)ゾーン緯度レンジ>=5° (b)最大|緯度|から0.3°以内の頂点が経度3°以上に広がる。
       戻り {'inc','lat','lon'} または None"""
    best = None
    for z in zones:
        if not z or len(z) < 4:
            continue
        latmax, vi, latmin = -1.0, -1, 1e9
        for i, c in enumerate(z):
            a = abs(c[0])
            if a > latmax:
                latmax, vi = a, i
            if a < latmin:
                latmin = a
        if vi < 0 or latmax - latmin < 5:
            continue
        ref = z[vi][1]
        lmin = lmax = 0.0
        n = 0
        for c in z:
            if abs(c[0]) < latmax - 0.3:
                continue
            d = c[1] - ref
            while d > 180: d -= 360
            while d < -180: d += 360
            lmin, lmax = min(lmin, d), max(lmax, d)
            n += 1
        if n >= 3 and (lmax - lmin) >= 3:
            if best is None or latmax > best['inc']:
                best = {'inc': latmax, 'lat': z[vi][0], 'lon': z[vi][1]}
    return best

# ════════════════════════ RAAN予測 (アプリ _raanVizNewBasis/_raanVizNewCompute v3.24.8 と同一) ════════════════════════

def jday(dt):
    """UTC datetime → ユリウス日"""
    y, m = dt.year, dt.month
    d = dt.day + (dt.hour + (dt.minute + (dt.second + dt.microsecond/1e6)/60.0)/60.0)/24.0
    if m <= 2:
        y -= 1
        m += 12
    A = y // 100
    B = 2 - A + A // 4
    return int(365.25*(y+4716)) + int(30.6001*(m+1)) + d + B - 1524.5

def gstime_deg(dt):
    """GMST(度)。satellite.js gstime と同じ Vallado 実装"""
    tut1 = (jday(dt) - 2451545.0) / 36525.0
    temp = -6.2e-6*tut1**3 + 0.093104*tut1**2 + (876600.0*3600 + 8640184.812866)*tut1 + 67310.54841
    rad = (temp * D2R / 240.0) % (2*math.pi)
    if rad < 0:
        rad += 2*math.pi
    return rad * R2D

def auto_dir_az(inc_deg, site_lat, az0, az1):
    """傾斜角→打上げ方位。北行き/南行き2解のうちコリドー内(両立/両外は中心に近い方)。
       アプリ _raanVizAutoDir と同一。解なし(傾斜角<緯度)は None"""
    r = math.cos(inc_deg*D2R) / math.cos(site_lat*D2R)
    if r < -1 or r > 1:
        return None
    a0 = math.asin(r) * R2D
    az_n = a0 % 360           # 東向き(順行)解
    az_s = (180 - a0) % 360   # 南向き(逆行)解
    if az0 is None or az1 is None:
        return az_n
    in_n = _az_in_range(az_n, az0, az1)
    in_s = _az_in_range(az_s, az0, az1)
    if in_n and not in_s:
        return az_n
    if in_s and not in_n:
        return az_s
    c = _az_center(az0, az1)
    return az_n if _az_dist(az_n, c) <= _az_dist(az_s, c) else az_s

def raan_predict(site_lat, site_lon, az_deg, liftoff_dt):
    """軌道法線からRAANを幾何算出 (アプリ v3.24.8 方式・補正0)。
       h = r0×b, raanFrame = atan2(h_x, −h_y), RAAN = raanFrame + GMST(リフトオフ)"""
    phi, lam, azr = site_lat*D2R, site_lon*D2R, az_deg*D2R
    r0 = (math.cos(phi)*math.cos(lam), math.cos(phi)*math.sin(lam), math.sin(phi))
    east = (-math.sin(lam), math.cos(lam), 0.0)
    north = (-math.sin(phi)*math.cos(lam), -math.sin(phi)*math.sin(lam), math.cos(phi))
    b = tuple(math.sin(azr)*east[i] + math.cos(azr)*north[i] for i in range(3))
    bl = math.hypot(*b)
    if not bl:
        return None
    b = tuple(x/bl for x in b)
    h = (r0[1]*b[2] - r0[2]*b[1], r0[2]*b[0] - r0[0]*b[2], r0[0]*b[1] - r0[1]*b[0])
    raan_frame = math.atan2(h[0], -h[1]) * R2D
    return (raan_frame + gstime_deg(liftoff_dt)) % 360

# ════════════════════════ NAVWARN 電文解析 (extract.py/アプリと同一パターン) ════════════════════════

RE_COORD = re.compile(r'(\d{1,2})-(\d{2}(?:\.\d+)?)\s*([NS])\s+(\d{1,3})-(\d{2}(?:\.\d+)?)\s*([EW])')
RE_HDR = re.compile(r'(NAVAREA\s+(?:IV|XII|ARC)|HYDROLANT|HYDROPAC|HYDROARC)\s+(\d+)/(\d+)', re.I)
RE_LAUNCH = re.compile(r'ROCKET\s+LAUNCH|SPACE\s+LAUNCH|SPACE\s+VEHICLE|LAUNCH(?:ING)?\s+OPERATION', re.I)
RE_DEBRIS = re.compile(r'SPACE\s+DEBRIS|ROCKET\s+DEBRIS|DEBRIS\s+SPLASH', re.I)
RE_ZONE = re.compile(r'\n\s*([A-F])\.\s')
# 最初のハザード窓 "162245Z TO 170056Z JUL" → 開始日時(デブリ警報ペアリング用)
RE_WIN0 = re.compile(r'\b(\d{2})(\d{2})(\d{2})Z\s+TO\s+\d{6}Z\s+(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)', re.I)
MON = {m: i+1 for i, m in enumerate(['JAN','FEB','MAR','APR','MAY','JUN','JUL','AUG','SEP','OCT','NOV','DEC'])}

def _dec(d, m, hemi):
    v = float(d) + float(m)/60.0
    return -v if hemi in ('S', 'W') else v

def parse_coords(text):
    return [(_dec(a,b,c), _dec(d,e,f)) for a,b,c,d,e,f in RE_COORD.findall(text)]

def split_zones(text):
    idx = [(m.start(), m.group(1)) for m in RE_ZONE.finditer(text)]
    if len(idx) < 2:
        c = parse_coords(text)
        return [c] if c else []
    zones = []
    for i, (pos, _) in enumerate(idx):
        end = idx[i+1][0] if i+1 < len(idx) else len(text)
        c = parse_coords(text[pos:end])
        if c:
            zones.append(c)
    return zones

def planned_dates(text, years):
    """本文中の 日+月 をすべて拾い打上げ予定日候補にする (複数日ウィンドウ・年跨ぎ対応)"""
    out = set()
    for m in re.finditer(r'\b(\d{2})(?:\d{4}Z)?\s*(?:TO\s+(\d{2})(?:\d{4}Z)?\s*)?(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\b', text):
        d1, d2, mon = m.group(1), m.group(2), MON[m.group(3)]
        for ds in (d1, d2):
            if ds is None:
                continue
            day = int(ds)
            if 1 <= day <= 31:
                for year in years:
                    try:
                        out.add(datetime.date(year, mon, day))
                    except ValueError:
                        pass
    return out

def _first_window_start(block, years):
    """最初のハザード窓の開始UTC datetime (無ければ None)。年は years から日付が成立する最初のもの"""
    m = RE_WIN0.search(block)
    if not m:
        return None
    day, hh, mm = int(m.group(1)), int(m.group(2)), int(m.group(3))
    mon = MON[m.group(4).upper()]
    for y in sorted(years):
        try:
            return datetime.datetime(y, mon, day, hh, mm, tzinfo=datetime.timezone.utc)
        except ValueError:
            continue
    return None

def parse_warnings(text, years):
    """電文ファイル → 打上げ関連警報のリスト [{id, kind, zones, dates, win0}]
       kind: 'launch'=打上げ警報 / 'debris'=デブリ落下警報(v2: 頂点緯度法の材料としてペアリング)"""
    out = []
    ms = list(RE_HDR.finditer(text))
    for i, mt in enumerate(ms):
        s = mt.start()
        e = ms[i+1].start() if i+1 < len(ms) else len(text)
        block = text[s:e]
        if RE_LAUNCH.search(block):
            kind = 'launch'
        elif RE_DEBRIS.search(block):
            kind = 'debris'
        else:
            continue
        zones = [z for z in split_zones(block) if z]
        if not zones:
            continue
        head = mt.group(1).split()[-1] if 'NAVAREA' in mt.group(1).upper() else mt.group(1)
        wid = ('%s-%s/%s' % (head, mt.group(2), mt.group(3))).upper()
        out.append({'id': wid, 'kind': kind, 'zones': zones,
                    'dates': sorted(d.isoformat() for d in planned_dates(block, years)),
                    'win0': _first_window_start(block, years)})
    return out

# ════════════════════════ TLE 解析・J2巻き戻し ════════════════════════

def tle_epoch(dt_line1):
    """TLE line1 のエポック(YYDDD.ddddd) → UTC datetime"""
    f = dt_line1[18:32].strip()
    yy = int(f[:2])
    year = 2000 + yy if yy < 57 else 1900 + yy
    doy = float(f[2:])
    return datetime.datetime(year, 1, 1, tzinfo=datetime.timezone.utc) + datetime.timedelta(days=doy - 1)

def tle_elements(line1, line2):
    """line2 から i(度)/RAAN(度)/e/n(rev/day) を読み、J2歳差率(度/日)を付ける"""
    inc = float(line2[8:16])
    raan = float(line2[17:25])
    ecc = float('0.' + line2[26:33].strip())
    n_rev = float(line2[52:63])
    mu = 398600.4418
    J2 = 1.08262668e-3
    Re = 6378.137
    n_rad = n_rev * 2*math.pi / 86400.0
    a = (mu / (n_rad*n_rad)) ** (1.0/3.0)
    p = a * (1 - ecc*ecc)
    raan_dot = -1.5 * n_rad * J2 * (Re/p)**2 * math.cos(inc*D2R)   # rad/s
    return {'inc': inc, 'raan': raan, 'ecc': ecc, 'n': n_rev,
            'alt_km': a - Re, 'raan_dot_deg_day': raan_dot * R2D * 86400.0,
            'epoch': tle_epoch(line1)}

def raan_back_to(el, liftoff_dt):
    """TLEエポックのRAANをJ2歳差率でリフトオフ時刻へ巻き戻す"""
    dt_days = (el['epoch'] - liftoff_dt).total_seconds() / 86400.0
    return (el['raan'] - el['raan_dot_deg_day'] * dt_days) % 360

def circ_diff(a, b):
    """円環差 a-b を -180..180 で"""
    d = (a - b) % 360
    return d - 360 if d > 180 else d

# ════════════════════════ 取得・入出力 ════════════════════════

def fetch(url, timeout=60):
    req = urllib.request.Request(url, headers={'User-Agent': 'Get_SCORE/1.0'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode('utf-8', errors='replace')

def load_ledger(year):
    p = os.path.join(DATA, 'ledger_%d.json' % year)
    if os.path.exists(p):
        return json.load(io.open(p, encoding='utf-8'))
    return {'year': year, 'records': {}}

def save_json(name, obj):
    os.makedirs(DATA, exist_ok=True)
    p = os.path.join(DATA, name)
    io.open(p, 'w', encoding='utf-8', newline='\n').write(
        json.dumps(obj, ensure_ascii=False, indent=1, sort_keys=True))

def slug(s):
    return re.sub(r'[^a-z0-9]+', '-', (s or '').lower()).strip('-')[:80]

def utcnow():
    return datetime.datetime.now(datetime.timezone.utc)

def parse_iso(s):
    try:
        return datetime.datetime.fromisoformat(s.replace('Z', '+00:00'))
    except Exception:
        return None

# ════════════════════════ メイン処理 ════════════════════════

def launch_key(l):
    """台帳キー: 予定年 + ミッション名スラッグ (netが多少滑っても不変)"""
    net = parse_iso(l.get('net') or '')
    y = net.year if net else 0
    return '%d-%s' % (y, slug(l.get('name')))

LEO_ORBITS = {'Low Earth Orbit', 'Sun-Synchronous Orbit', 'Polar Orbit'}

def step_predict(ledger_by_year, launches, warnings, now):
    """予定打上げの予測を作成/更新 (凍結済みは触らない)"""
    # ── 対象打上げ(座標+net あり・過去3日〜未来21日) ──
    cands = []
    for l in launches:
        net = parse_iso(l.get('net') or '')
        if not net or not _is_num(l.get('lat')) or not _is_num(l.get('lon')):
            continue
        if not (now - datetime.timedelta(days=3) <= net <= now + datetime.timedelta(days=21)):
            continue
        cands.append({'l': l, 'key': launch_key(l), 'net': net,
                      'lat': float(l['lat']), 'lon': float(l['lon'])})

    # ── 警報→anchor射場(全打上げのパッドで最寄り) ──
    pads = [(float(l['lat']), float(l['lon'])) for l in launches
            if _is_num(l.get('lat')) and _is_num(l.get('lon'))]
    launch_warnings = [w for w in warnings if w.get('kind', 'launch') == 'launch']
    debris_warnings = [w for w in warnings if w.get('kind') == 'debris']
    for w in warnings:
        w['assigned'] = None
    for w in launch_warnings:
        allp = [p for z in w['zones'] for p in z]
        best, bd = None, 1e9
        for (la, lo) in pads:
            for (pla, plo) in allp:
                d = haversine_km(la, lo, pla, plo)
                if d < bd:
                    best, bd = (la, lo), d
        w['anchor'] = best
        w['anchor_km'] = bd

    # ── 警報の独占帰属: 各警報は最良の1打上げだけに付く ──
    #    (同一射場から数日おきに連続する打上げへの重複帰属が誤推定の主因＝バックテストで確認)
    for w in launch_warnings:
        if w['anchor'] is None or w['anchor_km'] > 2500 or not w['dates']:
            continue
        best = None
        for c in cands:
            if haversine_km(c['lat'], c['lon'], w['anchor'][0], w['anchor'][1]) > 200:
                continue   # 別射場圏
            dd = min(abs((datetime.date.fromisoformat(d) - c['net'].date()).days) for d in w['dates'])
            if dd > 3:
                continue
            rank = (dd, haversine_km(c['lat'], c['lon'], w['anchor'][0], w['anchor'][1]))
            if best is None or rank < best[0]:
                best = (rank, c['key'])
        if best:
            w['assigned'] = best[1]

    # ── v2: デブリ警報のペアリング ──
    #    デブリ落下域は射場から数千〜1万km超で距離帰属は不可能。代わりに
    #    「同じ日付集合 + 窓開始が打上げ警報の少し後(飛行時間ぶんシフト)」を同一ミッションの署名とする。
    #    (実証: Starship IFT=打上げ窓+49分にデブリ窓が開く=公表タイムラインentry 47:30と一致)
    for dw in debris_warnings:
        if not dw['dates'] or dw['win0'] is None:
            continue
        dset = set(dw['dates'])
        best = None
        for lw in launch_warnings:
            if not lw['assigned'] or lw['win0'] is None or not lw['dates']:
                continue
            inter = dset & set(lw['dates'])
            if len(inter) < max(1, min(len(dset), len(lw['dates'])) // 2):
                continue
            dt_min = (dw['win0'] - lw['win0']).total_seconds() / 60.0
            if not (-30 <= dt_min <= 360):   # 打上げ30分前〜6時間後の窓開始のみ
                continue
            if best is None or abs(dt_min) < best[0]:
                best = (abs(dt_min), lw['assigned'])
        if best:
            dw['assigned'] = best[1]

    n_upd = 0
    for c in cands:
        l, key, net, lat, lon = c['l'], c['key'], c['net'], c['lat'], c['lon']
        if net.year not in ledger_by_year:
            ledger_by_year[net.year] = load_ledger(net.year)
        led = ledger_by_year[net.year]
        rec = led['records'].get(key)
        if rec is None:
            # ミッション名変更(例: Unknown Payload→正式名)でキーが変わった場合は旧レコードを引き継ぐ:
            # 同一パッド(±0.02°)・net差36h以内・未凍結・かつ旧キーが現フィードに存在しないもの
            feed_keys = {c2['key'] for c2 in cands}
            for k2, r2 in list(led['records'].items()):
                if k2 in feed_keys or r2['frozen_at']:
                    continue
                n2 = parse_iso(r2.get('net_last') or '')
                if n2 is None:
                    continue
                if abs(r2['lat'] - lat) < 0.02 and abs(r2['lon'] - lon) < 0.02 \
                   and abs((n2 - net).total_seconds()) <= 36*3600:
                    rec = r2
                    del led['records'][k2]
                    led['records'][key] = rec
                    rec['name'] = l.get('name')
                    add_flag(rec, 'renamed')
                    break
        if rec is None:
            rec = {'name': l.get('name'), 'rocket': l.get('rocket'), 'lsp': l.get('lsp'),
                   'location': l.get('location'), 'pad': l.get('pad'), 'orbit': l.get('orbit'),
                   'lat': lat, 'lon': lon,
                   'net_first': l.get('net'), 'net_last': l.get('net'),
                   'status': l.get('status'), 'liftoff': None,
                   'frozen_at': None, 'pred': None, 'ans': None, 'flags': []}
            led['records'][key] = rec
        rec['net_last'] = l.get('net')
        rec['status'] = l.get('status')
        rec['orbit'] = l.get('orbit')
        orbit = l.get('orbit') or ''
        if orbit == 'Suborbital':
            add_flag(rec, 'suborbital')
        elif orbit and orbit != 'Unknown' and orbit not in LEO_ORBITS:
            add_flag(rec, 'non-leo')
        if rec['frozen_at']:
            continue   # 凍結後は予測を書き換えない

        matched = [w for w in launch_warnings if w['assigned'] == key]
        matched_debris = [w for w in debris_warnings if w.get('assigned') == key]

        site = nearest_preset_site(lat, lon)
        az0 = site['az0'] if site else None
        az1 = site['az1'] if site else None
        pred = {'model': MODEL_VERSION, 'updated_at': now.isoformat(timespec='seconds'),
                'warnings': [w['id'] for w in matched],
                'zones': [z for w in matched for z in w['zones']],
                'debris_warnings': [w['id'] for w in matched_debris],
                'zones_debris': [z for w in matched_debris for z in w['zones']],
                'site_ref': site['name'] if site else None,
                'corridor': [az0, az1] if site else None,
                'incl': None, 'incl_method': None, 'az_measured': None, 'az_used': None, 'dir': None,
                'raan_at_net': None}
        if matched:
            est = incl_from_zones(lat, lon, az0, az1, pred['zones'])
            # ── v2: 頂点緯度法 (アプリ v3.34.5-6 と同一判定) ──
            #    折り返し検出 + 最遠区域>10000km(地球裏側寄り=重心方位が幾何的に無効)で昇格
            all_zones = pred['zones'] + pred['zones_debris']
            apex = apex_lat_estimate(all_zones)
            dmax = 0.0
            for z in all_zones:
                cla, clo = zone_centroid(lon, z)
                dmax = max(dmax, haversine_km(lat, lon, cla, clo))
            inc_val, method = None, None
            if est:
                inc_val, method = est['inc'], 'bearing'
                pred['az_measured'] = round(est['az'], 1)
            if apex is not None and dmax > 10000:
                inc_val, method = apex['inc'], 'apex'
            if inc_val is not None:
                pred['incl'] = round(inc_val, 2)
                pred['incl_method'] = method
                # RAAN用の方位: 傾斜角→コリドー自動判定。不能なら実測方位(あれば)
                az_u = auto_dir_az(inc_val, lat, az0, az1)
                if az_u is None and est:
                    az_u = est['az']
                if az_u is not None:
                    pred['az_used'] = round(az_u, 1)
                    pred['dir'] = 'south' if 90 < az_u < 270 else 'east'
                    r = raan_predict(lat, lon, az_u, net)
                    pred['raan_at_net'] = round(r, 2) if r is not None else None
        rec['pred'] = pred
        n_upd += 1
    return n_upd

def step_freeze(ledger_by_year, launches, now):
    """リフトオフ確認(status成功/失敗) or net+6時間経過 で凍結"""
    feed = {launch_key(l): l for l in launches}
    n_frozen = 0
    for led in ledger_by_year.values():
        for key, rec in led['records'].items():
            if rec['frozen_at'] or rec['ans']:
                continue
            l = feed.get(key)
            status = (l.get('status') if l else rec.get('status') or '').lower()
            net = parse_iso((l.get('net') if l else None) or rec.get('net_last') or '')
            done = ('success' in status or 'failure' in status)
            timed_out = net is not None and now > net + datetime.timedelta(hours=6)
            if not (done or timed_out):
                continue
            rec['frozen_at'] = now.isoformat(timespec='seconds')
            # LL2は成功時 net を実リフトオフ時刻に更新するため net_last を採用
            rec['liftoff'] = rec['net_last']
            if l is None:
                add_flag(rec,'vanished-before-status')
            if 'failure' in status:
                add_flag(rec,'launch-failure')
            if not rec['pred'] or rec['pred']['incl'] is None:
                add_flag(rec,'no-navwarn')
            n_frozen += 1
    return n_frozen

def _group_incl_median(g):
    objs = [o for o in g['objs'] if o.get('OBJECT_TYPE') == 'PAYLOAD' and o.get('INCL') not in (None, '')] \
           or [o for o in g['objs'] if o.get('INCL') not in (None, '')]
    if not objs:
        return None
    incs = sorted(float(o['INCL']) for o in objs)
    return incs[len(incs)//2] if len(incs) % 2 else (incs[len(incs)//2 - 1] + incs[len(incs)//2]) / 2

def _tiebreak_by_incl(rec, exact):
    """同日同射場の複数COSPAR: 予測傾斜角に明確に近い(差1°未満かつ次点と3°以上離れる)1つに絞る。
       絞れなければ (None, True)=ambiguous。※採点は選別バイアスを避けるため統計から除外する"""
    pred_inc = (rec.get('pred') or {}).get('incl')
    if pred_inc is None:
        return None, True
    scored = []
    for c in exact:
        gi = _group_incl_median(c[1])
        if gi is not None:
            scored.append((abs(gi - pred_inc), c))
    if not scored:
        return None, True
    scored.sort(key=lambda x: x[0])
    if scored[0][0] < 1.0 and (len(scored) == 1 or scored[1][0] - scored[0][0] >= 3.0):
        return scored[0][1], False
    return None, True

def step_answer(ledger_by_year, now):
    """凍結済み・未採点レコードに実測値を付ける"""
    pending = []
    for led in ledger_by_year.values():
        for key, rec in led['records'].items():
            if not rec['frozen_at'] or rec['ans'] is not None:
                continue
            if 'launch-failure' in rec['flags'] or 'suborbital' in rec['flags']:
                continue
            lo = parse_iso(rec.get('liftoff') or '')
            if lo is None:
                continue
            if (now - lo).days > 45:
                add_flag(rec,'no-tle-timeout')
                rec['ans'] = {'identified': False, 'checked_at': now.isoformat(timespec='seconds')}
                continue
            pending.append((key, rec, lo))
    if not pending:
        return 0

    satcat = json.loads(fetch(URL_SATCAT))
    satcat = satcat.get('data', satcat) if isinstance(satcat, dict) else satcat
    tler = json.loads(fetch(URL_TLE))
    tler = tler.get('data', tler) if isinstance(tler, dict) else tler
    tle_by_cospar = {}
    for t in tler:
        oid = (t.get('OBJECT_ID') or '')
        m = re.match(r'^(\d{4}-\d{3})', oid)
        if m:
            tle_by_cospar.setdefault(m.group(1), []).append(t)

    # COSPARグループ: intldes prefix → {dates, sites, objects}
    groups = {}
    for e in satcat:
        intl = (e.get('INTLDES') or '').strip()
        m = re.match(r'^(\d{4}-\d{3})', intl)
        if not m:
            continue
        g = groups.setdefault(m.group(1), {'dates': set(), 'sites': set(), 'objs': []})
        if e.get('LAUNCH'):
            g['dates'].add(e['LAUNCH'])
        if e.get('SITE'):
            g['sites'].add(e['SITE'])
        g['objs'].append(e)

    n_ans = 0
    for key, rec, lo in pending:
        lo_date = lo.date()
        cands = []
        for cid, g in groups.items():
            if not any(abs((datetime.date.fromisoformat(d) - lo_date).days) <= 1
                       for d in g['dates'] if re.match(r'^\d{4}-\d{2}-\d{2}$', d or '')):
                continue
            # 射場コード照合 (マップに無いコードは距離判定不能→候補には残しフラグ)
            site_ok, site_known = False, False
            for sc in g['sites']:
                if sc in SATCAT_SITES:
                    site_known = True
                    sla, slo = SATCAT_SITES[sc]
                    if haversine_km(rec['lat'], rec['lon'], sla, slo) <= 300:
                        site_ok = True
            cands.append((cid, g, site_ok, site_known))
        # 射場一致を最優先、無ければ「日付一致のみ」1件に限り採用
        exact = [c for c in cands if c[2]]
        chosen, amb = None, False
        if len(exact) == 1:
            chosen = exact[0]
        elif len(exact) > 1:
            # 同日同射場の複数打上げ: 予測傾斜角に最も近いグループが1つに絞れれば採用(透明性のためフラグ)
            chosen, amb = _tiebreak_by_incl(rec, exact)
            if chosen is not None:
                add_flag(rec, 'incl-tiebreak')
        elif len(cands) == 1 and not cands[0][3]:
            chosen = cands[0]
            add_flag(rec,'site-unmapped')
        elif len(cands) > 1:
            amb = True
        if amb:
            add_flag(rec,'ambiguous-cospar')
        if chosen is None:
            continue   # 次回再試行 (45日でタイムアウト)
        cid, g, _, _ = chosen

        payloads = [o for o in g['objs'] if o.get('OBJECT_TYPE') == 'PAYLOAD' and o.get('INCL') not in (None, '')]
        objs = payloads or [o for o in g['objs'] if o.get('INCL') not in (None, '')]
        if not objs:
            continue
        incs = sorted(float(o['INCL']) for o in objs)
        inc_med = incs[len(incs)//2] if len(incs) % 2 else (incs[len(incs)//2 - 1] + incs[len(incs)//2]) / 2
        spread = incs[-1] - incs[0]

        ans = {'identified': True, 'cospar': cid,
               'checked_at': now.isoformat(timespec='seconds'),
               'n_objects': len(g['objs']), 'incl': round(inc_med, 3),
               'incl_spread': round(spread, 3),
               'objects': [{'norad': o.get('NORAD_CAT_ID'), 'name': o.get('OBJECT_NAME'),
                            'intldes': o.get('INTLDES'), 'incl': o.get('INCL')} for o in g['objs'][:20]],
               'raan': None, 'raan_epoch': None, 'd_incl': None, 'd_raan': None, 'raan_pred': None}
        if spread > 5:
            add_flag(rec,'multi-orbit')

        # RAAN: tle_recent からエポック リフトオフ+24h以降を優先
        tles = tle_by_cospar.get(cid, [])
        best_el, early = None, False
        for t in tles:
            try:
                el = tle_elements(t['TLE_LINE1'], t['TLE_LINE2'])
            except Exception:
                continue
            good = (el['epoch'] - lo).total_seconds() >= 24*3600
            if best_el is None:
                best_el, early = el, not good
            elif good and early:
                best_el, early = el, False
        if best_el:
            ans['raan_epoch'] = round(best_el['raan'], 3)
            ans['raan'] = round(raan_back_to(best_el, lo), 3)
            if early:
                add_flag(rec,'early-tle')

        # 予測の再計算 (実リフトオフ時刻・凍結済み入力) と差分
        pred = rec.get('pred') or {}
        if pred.get('incl') is not None:
            ans['d_incl'] = round(inc_med - pred['incl'], 3)
            if pred.get('az_used') is not None and ans['raan'] is not None:
                rp = raan_predict(rec['lat'], rec['lon'], pred['az_used'], lo)
                if rp is not None:
                    ans['raan_pred'] = round(rp, 3)
                    ans['d_raan'] = round(circ_diff(ans['raan'], rp), 3)
        rec['ans'] = ans
        n_ans += 1
    return n_ans

def step_stats(ledger_by_year):
    """ロケット×射場ごとの通算成績 (答え合わせ済みのみ・フラグ除外)"""
    buckets = {}
    total = {'n': 0, 'd_incl': [], 'd_raan': []}
    for led in ledger_by_year.values():
        for rec in led['records'].values():
            a = rec.get('ans')
            if not a or not a.get('identified'):
                continue
            if any(f in rec['flags'] for f in
                   ('multi-orbit', 'ambiguous-cospar', 'non-leo', 'suborbital', 'incl-tiebreak')):
                continue   # 誤同定/選別バイアス/非LEOは通算成績から除外(台帳には残る)
            bkey = '%s @ %s' % (rec.get('rocket') or '?', (rec.get('pred') or {}).get('site_ref') or rec.get('location') or '?')
            b = buckets.setdefault(bkey, {'n': 0, 'd_incl': [], 'd_raan': []})
            for tgt in (b, total):
                tgt['n'] += 1
                if a.get('d_incl') is not None:
                    tgt['d_incl'].append(a['d_incl'])
                if a.get('d_raan') is not None:
                    tgt['d_raan'].append(a['d_raan'])
    def _agg(v):
        if not v:
            return None
        mean = sum(v)/len(v)
        sd = math.sqrt(sum((x-mean)**2 for x in v)/len(v)) if len(v) > 1 else 0.0
        mae = sum(abs(x) for x in v)/len(v)
        return {'n': len(v), 'mean': round(mean, 3), 'sd': round(sd, 3), 'mae': round(mae, 3)}
    out = {'updated_at': utcnow().isoformat(timespec='seconds'), 'model': MODEL_VERSION,
           'total': {'n': total['n'], 'incl': _agg(total['d_incl']), 'raan': _agg(total['d_raan'])},
           'by_rocket_site': {k: {'n': b['n'], 'incl': _agg(b['d_incl']), 'raan': _agg(b['d_raan'])}
                              for k, b in sorted(buckets.items())}}
    return out

def _is_num(v):
    try:
        float(v)
        return True
    except (TypeError, ValueError):
        return False

def add_flag(rec, flag):
    """同じフラグを毎日の再試行で重複追加しない"""
    if flag not in rec['flags']:
        rec['flags'] = rec['flags'] + [flag]

def main():
    now = utcnow()
    print('Get_SCORE run', now.isoformat(timespec='seconds'))

    launches = json.loads(fetch(URL_LAUNCHES))
    launches = launches.get('launches', launches.get('data')) if isinstance(launches, dict) else launches
    print('launches:', len(launches))

    warnings = []
    for area in NAVWARN_AREAS:
        try:
            txt = fetch(URL_NAVWARN % area)
            warnings += parse_warnings(txt, (now.year - 1, now.year, now.year + 1))
        except Exception as e:
            print('WARN: NAVWARN %s fetch failed: %s' % (area, e))
    print('launch-type warnings:', len(warnings))

    years = {now.year - 1, now.year, now.year + 1}
    ledger_by_year = {y: load_ledger(y) for y in years}

    n_pred = step_predict(ledger_by_year, launches, warnings, now)
    n_frozen = step_freeze(ledger_by_year, launches, now)
    n_ans = step_answer(ledger_by_year, now)
    stats = step_stats(ledger_by_year)
    print('predictions updated: %d / frozen: %d / answered: %d' % (n_pred, n_frozen, n_ans))

    for y, led in ledger_by_year.items():
        if led['records']:
            save_json('ledger_%d.json' % y, led)
    save_json('stats.json', stats)
    print('done. scored total n=%s' % (stats['total']['n']))

if __name__ == '__main__':
    main()
