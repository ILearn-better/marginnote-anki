# -*- coding: utf-8 -*-
"""
记忆题库 · 构建器
=====================================
用法：把 Anki 卡片包（.apkg，微信改过名的 .apk/.1g 也行）放进
      源文件/<科目名>/ 目录，然后运行：

      python build.py            # 全量重建：卡册 + PDF + 首页
      python build.py --no-pdf   # 不生成 PDF（更快）

输出：
  卡册/<科目>-<牌组名>.html   单文件离线卡册（图片内联，可微信直传）
  卡册/<科目>-<牌组名>.pdf    A4 打印版
  资料库.html                 平台首页（自动生成，别手改）
"""
import os, re, sys, json, base64, html as htmllib, sqlite3, zipfile, shutil
import tempfile, subprocess, pathlib, datetime, urllib.parse

BASE = os.path.dirname(os.path.abspath(__file__))
SRC_DIR = os.path.join(BASE, '源文件')
OUT_DIR = os.path.join(BASE, '卡册')
TPL_PATH = os.path.join(BASE, 'deck_template.html')
INDEX_PATH = os.path.join(BASE, '资料库.html')

BROWSERS = [
    r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
    r'C:\Program Files\Microsoft\Edge\Application\msedge.exe',
    r'C:\Program Files\Google\Chrome\Application\chrome.exe',
    r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe',
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge',
    '/usr/bin/google-chrome',
    '/usr/bin/chromium',
]
EXTS = {'.apkg', '.apk', '.1g', '.zip', '.colpkg'}


def find_browser():
    """找一个可用的 Chromium 内核浏览器来打印 PDF。
    优先级：环境变量 ANKI_BROWSER → PATH 上的命令 → 常见安装路径。"""
    env = os.environ.get('ANKI_BROWSER')
    if env and os.path.exists(env):
        return env
    for cmd in ('msedge', 'google-chrome', 'google-chrome-stable', 'chromium',
                'chromium-browser', 'microsoft-edge', 'chrome'):
        p = shutil.which(cmd)
        if p:
            return p
    for p in BROWSERS:
        if os.path.exists(p):
            return p
    return None


# ---------------------------------------------------------------- 工具
def safe_name(s):
    s = re.sub(r'[\\/:*?"<>|\r\n\t]', '_', str(s)).strip(' .')
    return s or '未命名'


def human(n):
    return f'{n / 1048576:.1f}MB' if n >= 1048576 else f'{n / 1024:.0f}KB'


def find_packages():
    """扫描 源文件/ 下的所有候选文件，返回 [(科目, 路径)]"""
    found = []
    if not os.path.isdir(SRC_DIR):
        os.makedirs(SRC_DIR, exist_ok=True)
        return found
    for root, dirs, files in os.walk(SRC_DIR):
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        for fn in files:
            if fn.startswith('.') or fn.startswith('~$'):
                continue
            ext = os.path.splitext(fn)[1].lower()
            if ext not in EXTS:
                continue
            full = os.path.join(root, fn)
            rel = os.path.relpath(root, SRC_DIR)
            subject = '未分类' if rel == '.' else rel.replace('\\', ' / ').replace('/', ' / ')
            found.append((subject, full))
    found.sort(key=lambda x: (x[0], x[1]))
    return found


def parse_package(path):
    """解析 .apkg，返回 {deck, cards, media_used, books, tags}"""
    tmp = tempfile.mkdtemp(prefix='ankilib_')
    try:
        zf = zipfile.ZipFile(path)
        names = zf.namelist()
        if 'collection.anki2' not in names and 'collection.anki21' not in names:
            raise ValueError('不是 Anki 卡片包（缺少 collection.anki2）')
        zf.extractall(tmp)

        db = os.path.join(tmp, 'collection.anki2')
        if not os.path.exists(db):
            db = os.path.join(tmp, 'collection.anki21')
        con = sqlite3.connect(db)

        # 媒体映射：zip 里的数字索引 → 原文件名
        media = {}
        mp = os.path.join(tmp, 'media')
        if os.path.exists(mp):
            try:
                raw = open(mp, encoding='utf-8').read().strip()
                if raw and raw not in ('{}',):
                    media = json.loads(raw)
            except Exception:
                media = {}
        rev = {v.lower(): k for k, v in media.items()}

        # 牌组名：卡片数最多的那个
        deck_name = '未命名牌组'
        try:
            decks = json.loads(con.execute('select decks from col').fetchone()[0])
            row = con.execute('select did, count(*) c from cards group by did order by c desc').fetchone()
            if row and str(row[0]) in decks:
                deck_name = decks[str(row[0])].get('name', deck_name)
        except Exception:
            pass

        cache = {}

        def datauri(fn):
            fn = (fn or '').strip()
            if not fn or fn in cache:
                return cache.get(fn, '')
            idx = rev.get(fn.lower())
            if idx is None:
                m = re.search(r'(\d+)(?:\.[a-z0-9]+)?$', fn)
                idx = m.group(1) if m else None
            if idx is None or not os.path.exists(os.path.join(tmp, idx)):
                cache[fn] = ''
                return ''
            ext = os.path.splitext(fn)[1].lower().lstrip('.') or 'jpeg'
            mime = {'jpg': 'jpeg', 'jpeg': 'jpeg', 'png': 'png', 'svg': 'svg+xml',
                    'gif': 'gif', 'webp': 'webp', 'bmp': 'bmp'}.get(ext, 'jpeg')
            b = base64.b64encode(open(os.path.join(tmp, idx), 'rb').read()).decode()
            cache[fn] = 'data:image/%s;base64,%s' % (mime, b)
            return cache[fn]

        def fix_html(fragment):
            if not fragment:
                return ''
            s = fragment
            s = re.sub(r'<div class="io-overlay".*?</div>', '', s, flags=re.S)   # 空的遮罩层
            s = re.sub(r'<img class="mbooks-mapimage"[^>]*>', '', s)             # 脑图缩略图
            s = re.sub(r'<img[^>]*?src="([^"]+)"',
                       lambda m: m.group(0).replace(m.group(1), datauri(m.group(1)) or m.group(1)), s)
            s = re.sub(r'(#[^#<\s]{1,20})+(?=\s*(</div>|</d))', '', s)           # 尾部标签文本
            s = re.sub(r'>\s*(?:#[^\s<>#]{1,24}\s*)+', '>', s)                   # ">#记" 之类
            return s.strip()

        cards, tags_all, books, seen_imgs = [], {}, {}, set()
        for nid, tags, flds in con.execute('select id, tags, flds from notes order by id'):
            f = flds.split('\x1f')
            front = f[0] if len(f) > 0 else ''
            back = f[3] if len(f) > 3 else ''
            if not front and len(f) > 2:
                front = f[2]           # 文字类牌组的 TextFront
            if not back and len(f) > 5:
                back = f[5]            # TextBack
            link = f[7] if len(f) > 7 else ''
            plain = htmllib.unescape(re.sub(r'<[^>]+>', '', link)).strip()
            book, page, topic = '', None, ''
            mm = re.search(r'(.+?)\s*-\s*(\d+)\s*$', plain)
            if mm:
                book = mm.group(1).strip().strip('<>《》')
                page = int(mm.group(2))
            elif plain and not plain.startswith('#') and len(plain) <= 40:
                topic = plain
            taglist = [t for t in tags.split() if t]
            for t in taglist:
                tags_all[t] = tags_all.get(t, 0) + 1
            if book:
                books[book] = books.get(book, 0) + 1

            fr, bk = fix_html(front), fix_html(back)
            imgs = lambda x: sorted(re.findall(r'src="(data:image[^"]+)"', x))
            single = bool(imgs(fr)) and set(imgs(fr)) == set(imgs(bk))
            seen_imgs.update(imgs(fr))
            seen_imgs.update(imgs(bk))
            if single:
                # 正反面是同一张图：背面不再重复内嵌一遍，渲染与打印都只取正面，
                # 单文件体积可减掉近一半（纯冗余数据）
                bk = ''
            cards.append({
                'id': nid, 'front': fr, 'back': bk, 'tags': taglist,
                'book': book, 'page': page, 'topic': topic,
                'single': single,
            })

        def sortkey(c):
            b = c['book']
            rank = 0 if '上' in b else 1 if '下' in b else 2
            return (rank, c['page'] or 0, c['topic'], c['id'])
        cards.sort(key=sortkey)
        for i, c in enumerate(cards):
            c['no'] = i + 1

        n_img = len(seen_imgs)
        con.close()
        return {
            'deck': deck_name,
            'cards': cards,
            'books': sorted(books.items(), key=lambda x: -x[1]),
            'tags': sorted(tags_all.items(), key=lambda x: (-x[1], x[0])),
            'n_img': n_img,
            'src_size': os.path.getsize(path),
            'src_mtime': os.path.getmtime(path),
        }
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 卡册页
def build_deck_html(pkg, subject, out_path, tpl):
    cards = pkg['cards']
    data = json.dumps(cards, ensure_ascii=False).replace('<', '\\u003c')
    crumb = ('<a href="../index.html">← 资料库</a><span class="div">/</span>'
             '<span>%s</span><span class="div">/</span><span>%s</span>'
             % (htmllib.escape(subject), htmllib.escape(pkg['deck'])))
    doc = (tpl.replace('__TITLE__', htmllib.escape(pkg['deck']))
              .replace('__CRUMB__', crumb)
              .replace('__DATA__', data))
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(doc)
    return os.path.getsize(out_path)


# ---------------------------------------------------------------- 打印版 / PDF
PRINT_DOC = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>__TITLE__ · 打印版</title>
<style>
@page{size:A4 portrait;margin:12mm 11mm}
*{box-sizing:border-box}
html,body{margin:0;padding:0;background:#fff;color:#1f2430;
  font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei","Source Han Sans SC",sans-serif;
  -webkit-print-color-adjust:exact;print-color-adjust:exact;}
.cover{margin:0 0 8mm;padding-bottom:4mm;border-bottom:2px solid #1f6feb}
.cover h1{margin:0 0 2mm;font-size:17pt;letter-spacing:.5px}
.cover p{margin:0;font-size:8.5pt;color:#6b7488;line-height:1.6}
.pc{break-inside:avoid;page-break-inside:avoid;padding:0 0 6mm;margin:0 0 7mm;border-bottom:1px dashed #cdd3dd}
.pc:last-child{border-bottom:0;margin-bottom:0}
.ph{display:flex;align-items:baseline;gap:6px;flex-wrap:wrap;border-left:3px solid #1f6feb;
  padding-left:7px;margin:0 0 4mm;font-size:10pt;line-height:1.45}
.ph .no{color:#1f6feb;font-weight:700}
.ph .bk{color:#2b3140;font-weight:600}
.ph .tg{color:#7a8296;font-weight:400;font-size:8.5pt}
.pc figure{margin:0;text-align:center}
.pc img{max-width:100%;max-height:99mm;width:auto;height:auto;display:inline-block;border-radius:2px}
.pc .sep{position:relative;border-top:1px dashed #cdd3dd;margin:3.5mm 0 3mm;text-align:center}
.pc .sep span{position:relative;top:-7px;background:#fff;padding:0 7px;font-size:8pt;color:#9aa2b1;letter-spacing:1px}
</style>
</head>
<body>
<div class="cover">
  <h1>__TITLE__</h1>
  <p>科目：__SUBJECT__ · 共 __N__ 张卡片 · __EXTRA__ · 生成于 __DATE__<br>
  排版说明：每张卡片上方为题目、下方为答案（以「答案」虚线分隔）；A4 单面打印，自动分页避免卡片被撕裂。</p>
</div>
__BODY__
</body>
</html>
"""


def build_pdf(pkg, subject, pdf_path, browser):
    def esc(t):
        return str(t).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

    blocks = []
    for c in pkg['cards']:
        src_txt = (c['book'] + (' · P%d' % c['page'] if c['page'] else '')) if c['book'] else (c['topic'] or '未标注出处')
        tgs = ' · '.join(c['tags'])
        h = ('<section class="pc"><div class="ph"><span class="no">#%d</span><span class="bk">%s</span>%s</div>'
             % (c['no'], esc(src_txt), ('<span class="tg">%s</span>' % esc(tgs)) if tgs else ''))
        h += '<figure>%s</figure>' % c['front']
        if not c['single']:
            h += '<div class="sep"><span>答案</span></div><figure>%s</figure>' % c['back']
        blocks.append(h + '</section>')

    extra = '、'.join(b for b, _ in pkg['books'][:3]) or '未标注出处'
    doc = (PRINT_DOC
           .replace('__TITLE__', esc(pkg['deck']))
           .replace('__SUBJECT__', esc(subject))
           .replace('__N__', str(len(pkg['cards'])))
           .replace('__EXTRA__', esc(extra))
           .replace('__DATE__', datetime.date.today().strftime('%Y-%m-%d'))
           .replace('__BODY__', '\n'.join(blocks)))

    tmp_html = os.path.join(tempfile.gettempdir(), 'ankilib_print_%d.html' % os.getpid())
    with open(tmp_html, 'w', encoding='utf-8') as f:
        f.write(doc)
    if os.path.exists(pdf_path):
        os.remove(pdf_path)
    subprocess.run([browser, '--headless=new', '--disable-gpu', '--no-pdf-header-footer',
                    '--run-all-compositor-stages-before-draw', '--virtual-time-budget=25000',
                    '--print-to-pdf=' + os.path.abspath(pdf_path),
                    pathlib.Path(tmp_html).as_uri()],
                   capture_output=True, timeout=420)
    os.remove(tmp_html)
    return os.path.getsize(pdf_path) if os.path.exists(pdf_path) else 0


# ---------------------------------------------------------------- 首页
INDEX_DOC = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>我的记忆题库</title>
<style>
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{margin:0;padding:0}
body{
  background:#f5f6f8;color:#1f2430;min-height:100vh;
  font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
}
header{
  position:sticky;top:0;z-index:10;background:rgba(255,255,255,.92);backdrop-filter:blur(8px);
  border-bottom:1px solid #e8eaef;padding:16px 22px 12px;
}
.htop{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;max-width:1080px;margin:0 auto}
h1{font-size:18px;margin:0;font-weight:650;letter-spacing:.3px}
.hstat{font-size:12.5px;color:#8b93a3}
.hsearch{max-width:1080px;margin:11px auto 0}
.qbox{display:flex;align-items:center;gap:8px;background:#f5f6f8;border:1px solid #e6e9ef;
  border-radius:10px;padding:0 12px;max-width:520px}
.qbox:focus-within{background:#fff;border-color:#a9c6f7;box-shadow:0 0 0 3px rgba(31,111,235,.09)}
.qbox input{flex:1;border:0;outline:0;background:transparent;font-size:13.5px;padding:9px 0;
  font-family:inherit;color:#1f2430}
.qbox input::placeholder{color:#aab1bd}
.qbox svg{color:#a6adba;flex:0 0 auto}
main{max-width:1080px;margin:0 auto;padding:20px 22px 60px}
.subj{margin:0 0 26px}
.shead{display:flex;align-items:center;gap:10px;margin:0 0 10px;padding-left:2px}
.shead h2{font-size:14.5px;margin:0;font-weight:650;letter-spacing:.4px}
.shead .dot{width:3px;height:15px;border-radius:2px;background:#1f6feb}
.shead .sstat{font-size:12px;color:#9aa2b1;margin-left:auto}
.files{display:flex;flex-direction:column;gap:10px}
.file{
  display:flex;gap:14px;align-items:flex-start;background:#fff;border:1px solid #ebedf2;
  border-radius:13px;padding:15px 17px;transition:.16s;
}
.file:hover{border-color:#cfdbf0;box-shadow:0 4px 18px rgba(31,36,48,.06)}
.fmain{flex:1;min-width:0}
.ftop{display:flex;align-items:center;gap:9px;flex-wrap:wrap}
.fname{font-size:14.5px;font-weight:620;letter-spacing:.2px}
.badge{font-size:11px;color:#1f6feb;background:#eaf1ff;border-radius:5px;padding:2px 7px;font-weight:600}
.badge.warn{color:#b45309;background:#fef3e2}
.fmeta{font-size:12px;color:#8b93a3;margin:6px 0 0;line-height:1.7}
.ftags{display:flex;gap:6px;flex-wrap:wrap;margin:9px 0 0}
.ftags .tg{
  font-size:11.5px;color:#5b6478;background:#f2f4f8;border-radius:5px;padding:2px 7px;
  text-decoration:none;transition:.14s;
}
.ftags .tg:hover{background:#e7f0ff;color:#1f6feb}
.ftags .tg.more{color:#a6adba;background:transparent;padding:2px 2px}
.facts{display:flex;flex-direction:column;gap:7px;flex:0 0 auto;align-items:stretch}
.act{
  display:inline-flex;align-items:center;justify-content:center;gap:5px;
  font-size:12.5px;text-decoration:none;border-radius:8px;padding:7px 15px;
  border:1px solid #dfe3ea;color:#3a4152;background:#fff;transition:.15s;white-space:nowrap;
}
.act:hover{background:#f7f9fb;border-color:#c4ccda}
.act.primary{background:#1f6feb;border-color:#1f6feb;color:#fff;font-weight:600}
.act.primary:hover{background:#1a5fd0}
.act.ghost{color:#8b93a3;font-size:12px;padding:5px 12px}
.empty{
  background:#fff;border:1px dashed #d8dde6;border-radius:14px;padding:34px 26px;text-align:center;
  color:#8b93a3;font-size:13px;line-height:2;
}
.empty b{display:block;font-size:15px;color:#3a4152;margin-bottom:6px}
.empty code{background:#f2f4f8;border-radius:5px;padding:2px 6px;font-size:12.5px;color:#c2410c}
.help{
  margin-top:34px;background:#fff;border:1px solid #ebedf2;border-radius:13px;padding:18px 20px;
  font-size:12.5px;color:#6b7488;line-height:2;
}
.help h3{font-size:13px;margin:0 0 8px;color:#3a4152;font-weight:650}
.help code{background:#f2f4f8;border-radius:5px;padding:2px 6px;font-size:12px;color:#1f6feb}
.help .mut{color:#a6adba}
.none{text-align:center;color:#a6adba;font-size:13px;padding:40px 0}
@media (max-width:640px){
  header{padding:13px 14px 10px}
  main{padding:14px 14px 50px}
  .file{flex-direction:column;gap:12px;padding:14px}
  .facts{flex-direction:row;width:100%}
  .facts .act{flex:1}
  h1{font-size:16.5px}
}
</style>
</head>
<body>
<header>
  <div class="htop">
    <h1>我的记忆题库</h1>
    <span class="hstat">__STATS__</span>
  </div>
  <div class="hsearch" id="wrap">
    <div class="qbox">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"
           stroke-linecap="round"><circle cx="11" cy="11" r="7"></circle><path d="M20 20l-4.2-4.2"></path></svg>
      <input id="q" type="search" autocomplete="off" placeholder="搜科目、文件名或标签…">
    </div>
  </div>
</header>
<main>
  __BODY__
  <div class="help">
    <h3>怎么往里加新题库</h3>
    1. 在 <code>源文件</code> 里按科目建文件夹，例如 <code>源文件\高等代数\</code>、<code>源文件\数学分析\</code><br>
    2. 把 Anki 卡片包（<code>.apkg</code>，微信传过来的 <code>.apk.1g</code> 直接丢进去也行）放到对应文件夹<br>
    3. 回到本目录运行 <code>python build.py</code>，卡册和这页会自动更新<br>
    <span class="mut">生成时间：__DATE__ · 支持多级科目（如 源文件\考研\数学分析\）</span>
  </div>
</main>
<script>
const q = document.getElementById('q');
const files = Array.from(document.querySelectorAll('.file'));
const secs = Array.from(document.querySelectorAll('.subj'));
function apply(){
  const k = q.value.trim().toLowerCase();
  files.forEach(f => {
    const hit = !k || (f.dataset.k || '').toLowerCase().includes(k);
    f.style.display = hit ? '' : 'none';
  });
  secs.forEach(s => {
    const alive = Array.from(s.querySelectorAll('.file')).some(f => f.style.display !== 'none');
    s.style.display = alive ? '' : 'none';
  });
  let n = files.filter(f => f.style.display !== 'none').length;
  document.getElementById('stat2') && (document.getElementById('stat2').textContent = n + ' 份文件');
}
q.addEventListener('input', apply);
document.addEventListener('keydown', e => {
  if (e.key === '/' && e.target.tagName !== 'INPUT') { q.focus(); e.preventDefault(); }
  if (e.key === 'Escape') { q.value = ''; apply(); }
});
</script>
</body>
</html>
"""


def build_index(decks, out_path):
    subjects = {}
    for d in decks:
        subjects.setdefault(d['subject'], []).append(d)

    n_all = sum(len(d['pkg']['cards']) for d in decks)
    stats = '%d 个科目 · %d 份文件 · %d 张卡片' % (len(subjects), len(decks), n_all)

    if not decks:
        body = ('<div class="empty"><b>题库还是空的</b>'
                '把 Anki 卡片包放进 <code>源文件\\数学分析\\</code> 这样的科目文件夹，'
                '再运行 <code>python build.py</code></div>')
    else:
        parts = []
        for subject in sorted(subjects, key=lambda s: (s == '未分类', s)):
            items = subjects[subject]
            c_tot = sum(len(i['pkg']['cards']) for i in items)
            rows = []
            for d in items:
                pkg = d['pkg']
                books = '、'.join(b for b, _ in pkg['books'][:2])
                if len(pkg['books']) > 2:
                    books += ' 等 %d 本' % len(pkg['books'])
                meta = '出处：%s' % (books or '未标注')
                meta += ' · %d 张图 · %s' % (pkg['n_img'], human(pkg['src_size']))
                if pkg['tags']:
                    meta += ' · %d 个标签' % len(pkg['tags'])
                meta += ' · 更新 %s' % datetime.datetime.fromtimestamp(pkg['src_mtime']).strftime('%Y-%m-%d')
                tag_html = ''
                for t, n in pkg['tags'][:8]:
                    href = '%s#q=%s' % (urllib.parse.quote(d['rel_html']), urllib.parse.quote(t))
                    tag_html += '<a class="tg" href="%s" title="在卡册中筛选：%s">%s %d</a>' % (
                        href, htmllib.escape(t), htmllib.escape(t), n)
                if len(pkg['tags']) > 8:
                    tag_html += '<span class="tg more">+%d</span>' % (len(pkg['tags']) - 8)
                key = ' '.join([subject, pkg['deck'], ' '.join(t for t, _ in pkg['tags'][:40])])
                facts = '<a class="act primary" href="%s">打开卡册</a>' % urllib.parse.quote(d['rel_html'])
                if d['pdf_size']:
                    facts += '<a class="act" href="%s" target="_blank" rel="noopener">PDF</a>' % \
                             urllib.parse.quote(d['rel_pdf'])
                badge_warn = '' if pkg['cards'] else ' warn'
                rows.append(
                    '<article class="file" data-k="%s">'
                    '<div class="fmain">'
                    '<div class="ftop"><span class="fname">%s</span>'
                    '<span class="badge%s">%d 张卡</span></div>'
                    '<div class="fmeta">%s</div>'
                    '<div class="ftags">%s</div>'
                    '</div><div class="facts">%s</div></article>'
                    % (htmllib.escape(key.lower()), htmllib.escape(pkg['deck']), badge_warn,
                       len(pkg['cards']), htmllib.escape(meta), tag_html, facts))
            parts.append(
                '<section class="subj"><div class="shead"><span class="dot"></span>'
                '<h2>%s</h2><span class="sstat">%d 份 · %d 张卡</span></div>'
                '<div class="files">%s</div></section>'
                % (htmllib.escape(subject), len(items), c_tot, ''.join(rows)))
        body = ''.join(parts)

    doc = (INDEX_DOC.replace('__STATS__', stats)
                     .replace('__BODY__', body)
                     .replace('__DATE__', datetime.datetime.now().strftime('%Y-%m-%d %H:%M')))
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(doc)
    return os.path.getsize(out_path)


# ---------------------------------------------------------------- 主流程
def main():
    want_pdf = '--no-pdf' not in sys.argv
    browser = find_browser()
    if want_pdf and not browser:
        print('⚠ 没找到 Edge/Chrome，跳过 PDF 生成（卡册页里仍可手动打印）')
        want_pdf = False

    if not os.path.exists(TPL_PATH):
        sys.exit('缺少 deck_template.html，无法生成卡册')
    tpl = open(TPL_PATH, encoding='utf-8').read()
    os.makedirs(OUT_DIR, exist_ok=True)

    pkgs = find_packages()
    if not pkgs:
        print('源文件/ 里没找到卡片包，先放 .apkg 到 源文件/<科目>/ 下')

    decks, used = [], set()
    for subject, path in pkgs:
        name = os.path.splitext(os.path.basename(path))[0]
        print('· 解析 %s  [%s]' % (name, subject))
        try:
            pkg = parse_package(path)
        except Exception as e:
            print('  ✗ 跳过：%s' % e)
            continue
        if not pkg['cards']:
            print('  ✗ 跳过：0 张卡片')
            continue

        stem = safe_name('%s-%s' % (subject.replace(' / ', '-'), pkg['deck'] or name))
        base = stem
        i = 2
        while stem in used:
            stem = '%s_%d' % (base, i)
            i += 1
        used.add(stem)

        html_path = os.path.join(OUT_DIR, stem + '.html')
        pdf_path = os.path.join(OUT_DIR, stem + '.pdf')
        size_html = build_deck_html(pkg, subject, html_path, tpl)
        print('  ✓ 卡册 %s（%d 张 · %s）' % (os.path.basename(html_path), len(pkg['cards']), human(size_html)))

        pdf_size = 0
        if want_pdf:
            pdf_size = build_pdf(pkg, subject, pdf_path, browser)
            print('  %s PDF %s' % ('✓' if pdf_size else '✗', human(pdf_size) if pdf_size else '生成失败'))

        decks.append({
            'subject': subject, 'pkg': pkg,
            'rel_html': '卡册/' + stem + '.html',
            'rel_pdf': '卡册/' + stem + '.pdf',
            'pdf_size': pdf_size,
        })

    # 清理孤儿文件（源文件已删掉的卡册）
    keep = {os.path.basename(d['rel_html']) for d in decks} | {os.path.basename(d['rel_pdf']) for d in decks}
    for fn in os.listdir(OUT_DIR):
        if fn not in keep:
            try:
                os.remove(os.path.join(OUT_DIR, fn))
                print('· 清理旧文件 %s' % fn)
            except Exception:
                pass

    size = build_index(decks, INDEX_PATH)
    # 额外一份 ASCII 文件名的入口，兼容部分预览器/手机浏览器对中文文件名支持不佳
    try:
        build_index(decks, os.path.join(BASE, 'index.html'))
    except Exception:
        pass
    print('\n资料库.html / index.html 已更新（%s）' % human(size))
    print('总览：%d 个科目 / %d 份文件 / %d 张卡片'
          % (len({d['subject'] for d in decks}), len(decks), sum(len(d['pkg']['cards']) for d in decks)))


if __name__ == '__main__':
    main()
