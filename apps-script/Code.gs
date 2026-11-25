/**
 * 인천 강사공고 — 지원예정 표시 저장소 + 첨부파일 드라이브 저장
 *
 * 하는 일
 *  1) 공고 페이지에서 누른 ⭐지원예정 / 숨기기 표시를 구글시트에 저장 (모든 기기에서 같이 보임)
 *  2) GitHub 봇이 저장소 inbox/ 에 받아 둔 ⭐공고 첨부파일을 10분마다 가져와
 *     구글드라이브 「강사지원DB/공고첨부/<공고>」 폴더에 저장
 *
 * 처음 한 번만: 편집기에서 setup 실행 → 권한 허용 → 배포(웹 앱, 실행: 나, 액세스: 모든 사용자)
 * 자세한 순서는 apps-script/README.md
 */

const REPO = 'Izzy-Lee/incheon-instructor-alerts';
const BRANCH = 'main';
const HEAD = ['key', 'state', 'title', 'url', 'org', 'deadline', 'at', 'saved', 'folder', 'note', 'tries'];
const P = PropertiesService.getScriptProperties();

/** 처음 한 번 실행: 시트·드라이브 폴더·10분 트리거를 만든다. 여러 번 실행해도 괜찮음. */
function setup() {
  let ssId = P.getProperty('SHEET_ID');
  if (!ssId) {
    const ss = SpreadsheetApp.create('인천 강사공고 지원예정');
    const sh = ss.getSheets()[0];
    sh.setName('marks');
    sh.getRange(1, 1, 1, HEAD.length).setValues([HEAD]).setFontWeight('bold');
    sh.setFrozenRows(1);
    ssId = ss.getId();
    P.setProperty('SHEET_ID', ssId);
  }
  if (!P.getProperty('FOLDER_ID')) {
    const db = findOrCreate_(DriveApp.getRootFolder(), '강사지원DB');
    const f = findOrCreate_(db, '공고첨부');
    P.setProperty('FOLDER_ID', f.getId());
    try { DriveApp.getFileById(ssId).moveTo(db); } catch (e) {} // 시트도 강사지원DB 폴더로
  }
  ScriptApp.getProjectTriggers().filter(t => t.getHandlerFunction() === 'pull').forEach(t => ScriptApp.deleteTrigger(t));
  ScriptApp.newTrigger('pull').timeBased().everyMinutes(10).create();
  Logger.log('시트: https://docs.google.com/spreadsheets/d/' + ssId);
  Logger.log('첨부 폴더: https://drive.google.com/drive/folders/' + P.getProperty('FOLDER_ID'));
}

function findOrCreate_(parent, name) {
  const it = parent.getFoldersByName(name);
  return it.hasNext() ? it.next() : parent.createFolder(name);
}

function sheet_() {
  return SpreadsheetApp.openById(P.getProperty('SHEET_ID')).getSheetByName('marks');
}

function rows_() {
  const v = sheet_().getDataRange().getValues();
  const head = v.shift();
  return v.map((r, i) => {
    const o = { _row: i + 2 };
    head.forEach((h, j) => (o[h] = r[j] instanceof Date ? Utilities.formatDate(r[j], 'Asia/Seoul', 'yyyy-MM-dd') : r[j]));
    return o;
  });
}

function out_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}

/** GET → 모든 표시 */
function doGet(e) {
  const marks = {};
  rows_().forEach(r => {
    if (!r.key || !r.state) return;
    marks[r.key] = {
      state: r.state, title: r.title, url: r.url, org: r.org, deadline: r.deadline,
      at: r.at, saved: r.saved || '', folder: r.folder || '', note: r.note || '', tries: Number(r.tries) || 0,
    };
  });
  return out_({ ok: true, marks });
}

/** POST (text/plain JSON): {action:'mark', key, state, ...} 또는 {action:'marks', items:[...]} */
function doPost(e) {
  let body;
  try { body = JSON.parse(e.postData.contents); } catch (err) { return out_({ ok: false, error: 'bad json' }); }
  const lock = LockService.getScriptLock();
  lock.waitLock(20000);
  try {
    if (body.action === 'mark') return out_(mark_(body));
    if (body.action === 'marks') { (body.items || []).slice(0, 500).forEach(mark_); return out_({ ok: true }); }
    return out_({ ok: false, error: 'unknown action' });
  } finally {
    lock.releaseLock();
  }
}

function clip_(s, n) { return String(s || '').slice(0, n); }

/** 표시 저장. state 가 비어 있으면 표시 지움. 이미 받은 첨부 기록(saved/folder)은 그대로 둔다. */
function mark_(b) {
  if (!b.key) return { ok: false, error: 'key' };
  if (b.state && b.state !== 'plan' && b.state !== 'hidden') return { ok: false, error: 'state' };
  const sh = sheet_();
  const r = rows_().find(x => x.key === b.key);
  const at = clip_(b.at, 10) || Utilities.formatDate(new Date(), 'Asia/Seoul', 'yyyy-MM-dd');
  if (!r) {
    if (!b.state) return { ok: true };
    sh.appendRow([clip_(b.key, 200), b.state, clip_(b.title, 300), clip_(b.url, 1000), clip_(b.org, 100), clip_(b.deadline, 10), at, '', '', '', 0]);
    return { ok: true };
  }
  const vals = [r.key, b.state || '', clip_(b.title, 300) || r.title, clip_(b.url, 1000) || r.url, clip_(b.org, 100) || r.org, clip_(b.deadline, 10) || r.deadline, at];
  sh.getRange(r._row, 1, 1, vals.length).setValues([vals]);
  return { ok: true };
}

/** 10분마다: 저장소 inbox/index.json 을 보고 새로 받은 첨부를 드라이브에 저장 */
function pull() {
  const raw = 'https://raw.githubusercontent.com/' + REPO + '/' + BRANCH + '/inbox/';
  const res = UrlFetchApp.fetch(raw + 'index.json?t=' + Date.now(), { muteHttpExceptions: true });
  if (res.getResponseCode() !== 200) return;
  const index = JSON.parse(res.getContentText());
  const lock = LockService.getScriptLock();
  lock.waitLock(30000);
  try {
    const sh = sheet_();
    const rows = rows_();
    const root = DriveApp.getFolderById(P.getProperty('FOLDER_ID'));
    Object.keys(index).forEach(id => {
      const it = index[id];
      const r = rows.find(x => x.key === it.key);
      if (!r || r.state !== 'plan' || r.saved) return;
      if (it.status === 'ready') {
        const name = String(it.folderName || r.title || it.key).replace(/[\\/:*?"<>|]/g, ' ').slice(0, 120);
        const folder = findOrCreate_(root, name);
        const saved = [];
        it.files.forEach(f => {
          const fr = UrlFetchApp.fetch(raw + id + '/' + encodeURIComponent(f.stored), { muteHttpExceptions: true });
          if (fr.getResponseCode() !== 200) throw new Error('파일 가져오기 실패 ' + f.stored + ' ' + fr.getResponseCode());
          const fname = String(f.name).replace(/[\\/:*?"<>|]/g, ' ');
          const old = folder.getFilesByName(fname);
          while (old.hasNext()) old.next().setTrashed(true);
          saved.push(folder.createFile(fr.getBlob().setName(fname)).getName());
        });
        if (it.url) folder.setDescription('원문 공고: ' + it.url);
        sh.getRange(r._row, 8, 1, 4).setValues([[
          Utilities.formatDate(new Date(), 'Asia/Seoul', 'yyyy-MM-dd HH:mm'),
          folder.getUrl(), saved.length + '개 저장: ' + saved.join(', '), Number(it.tries) || 1,
        ]]);
      } else if (String(r.note) !== String(it.note || '') || Number(r.tries) !== Number(it.tries || 0)) {
        sh.getRange(r._row, 10, 1, 2).setValues([[it.note || '', Number(it.tries) || 0]]);
      }
    });
  } finally {
    lock.releaseLock();
  }
}
