/**
 * 인천 강사공고 — 지원예정 표시 저장소 + 첨부파일 드라이브 저장
 *
 * 하는 일
 *  1) 공고 페이지에서 누른 ⭐지원예정 / 숨기기 표시를 구글시트에 저장 (모든 기기에서 같이 보임)
 *  2) GitHub 봇이 받아 온 ⭐공고의 첨부파일을 구글드라이브 「강사지원DB/공고첨부」에 저장
 *
 * 처음 한 번만: 편집기에서 setup 실행 → 권한 허용 → 배포(웹 앱, 실행: 나, 액세스: 모든 사용자)
 * 자세한 순서는 apps-script/README.md
 */

const HEAD = ['key', 'state', 'title', 'url', 'org', 'deadline', 'at', 'saved', 'folder', 'note', 'tries'];
const P = PropertiesService.getScriptProperties();

/** 처음 한 번 실행: 시트·드라이브 폴더·업로드 비밀번호를 만든다. */
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
    // 시트도 같은 폴더(강사지원DB)로 옮겨 둔다
    try { DriveApp.getFileById(ssId).moveTo(db); } catch (e) {}
  }
  if (!P.getProperty('UPLOAD_TOKEN')) {
    P.setProperty('UPLOAD_TOKEN', Utilities.getUuid().replace(/-/g, ''));
  }
  Logger.log('시트: https://docs.google.com/spreadsheets/d/' + ssId);
  Logger.log('첨부 폴더: https://drive.google.com/drive/folders/' + P.getProperty('FOLDER_ID'));
  Logger.log('UPLOAD_TOKEN (GitHub Secrets에 넣을 값): ' + P.getProperty('UPLOAD_TOKEN'));
}

function findOrCreate_(parent, name) {
  const it = parent.getFoldersByName(name);
  return it.hasNext() ? it.next() : parent.createFolder(name);
}

function sheet_() {
  return SpreadsheetApp.openById(P.getProperty('SHEET_ID')).getSheetByName('marks');
}

function rows_() {
  const sh = sheet_();
  const v = sh.getDataRange().getValues();
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

/** GET ?action=marks → 모든 표시 */
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

/** POST (text/plain JSON) */
function doPost(e) {
  let body;
  try { body = JSON.parse(e.postData.contents); } catch (err) { return out_({ ok: false, error: 'bad json' }); }
  const lock = LockService.getScriptLock();
  lock.waitLock(20000);
  try {
    if (body.action === 'mark') return out_(mark_(body));
    if (body.action === 'marks') return out_({ ok: true, n: (body.items || []).map(mark_).length });
    if (body.token !== P.getProperty('UPLOAD_TOKEN')) return out_({ ok: false, error: 'token' });
    if (body.action === 'upload') return out_(upload_(body));
    if (body.action === 'result') return out_(result_(body));
    return out_({ ok: false, error: 'unknown action' });
  } finally {
    lock.releaseLock();
  }
}

/** 표시 저장. state 가 비어 있으면 표시 지움. 이미 받은 첨부 기록(saved/folder)은 그대로 둔다. */
function mark_(b) {
  if (!b.key) return { ok: false, error: 'key' };
  const sh = sheet_();
  const r = rows_().find(x => x.key === b.key);
  const at = b.at || Utilities.formatDate(new Date(), 'Asia/Seoul', 'yyyy-MM-dd');
  if (!r) {
    if (!b.state) return { ok: true };
    sh.appendRow([b.key, b.state, b.title || '', b.url || '', b.org || '', b.deadline || '', at, '', '', '', 0]);
    return { ok: true };
  }
  const vals = [b.key, b.state || '', b.title || r.title, b.url || r.url, b.org || r.org, b.deadline || r.deadline, at];
  sh.getRange(r._row, 1, 1, vals.length).setValues([vals]);
  return { ok: true };
}

/** 봇이 보낸 첨부파일 저장: { token, key, folderName, files:[{name, mime, b64}] } */
function upload_(b) {
  const r = rows_().find(x => x.key === b.key);
  if (!r) return { ok: false, error: 'no mark' };
  const root = DriveApp.getFolderById(P.getProperty('FOLDER_ID'));
  const name = String(b.folderName || r.title || b.key).replace(/[\\/:*?"<>|]/g, ' ').slice(0, 120);
  const folder = findOrCreate_(root, name);
  const saved = [];
  (b.files || []).forEach(f => {
    const fname = String(f.name).replace(/[\\/:*?"<>|]/g, ' ');
    const old = folder.getFilesByName(fname);
    while (old.hasNext()) old.next().setTrashed(true); // 같은 이름은 새것으로 교체
    const blob = Utilities.newBlob(Utilities.base64Decode(f.b64), f.mime || 'application/octet-stream', fname);
    saved.push(folder.createFile(blob).getName());
  });
  if (b.url) folder.setDescription('원문 공고: ' + b.url);
  const sh = sheet_();
  sh.getRange(r._row, 8, 1, 4).setValues([[
    Utilities.formatDate(new Date(), 'Asia/Seoul', 'yyyy-MM-dd HH:mm'),
    folder.getUrl(),
    saved.length + '개 저장: ' + saved.join(', '),
    (Number(r.tries) || 0) + 1,
  ]]);
  return { ok: true, folder: folder.getUrl(), saved };
}

/** 첨부를 못 받았을 때 기록: { token, key, note, done } — done 이면 더 시도하지 않음 */
function result_(b) {
  const r = rows_().find(x => x.key === b.key);
  if (!r) return { ok: false, error: 'no mark' };
  const tries = (Number(r.tries) || 0) + 1;
  const sh = sheet_();
  if (b.done) sh.getRange(r._row, 8).setValue('첨부 없음');
  sh.getRange(r._row, 10, 1, 2).setValues([[b.note || '', tries]]);
  return { ok: true };
}
