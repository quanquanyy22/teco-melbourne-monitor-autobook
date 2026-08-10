/* ===================================================================
   TECO Melbourne 入台證 — 手動觸發的表單填寫助手 (bookmarklet source)
   ===================================================================

   這是「可讀版原始碼」。實際使用的是 autofill_bookmarklet.html 裡壓縮過的
   javascript: 書籤。改這裡之後，重跑 build_bookmarklet.py 重新產生書籤。

   設計上的硬性界線 —— 不要改：
     • 只填「申請人自己的資料」欄位 (FNAME/LNAME/EMAIL/Q3/Q10/Q9/Q12/Q8)
     • 絕對不碰 Q11 / Q14 兩道防機器人題 —— 由本人作答
     • 絕對不按「送出 / Confirm」—— 由本人按
     • 只在使用者主動點書籤時執行一次，不輪詢、不自動重試

   為什麼要用 native setter：
     這個頁面是 React。直接 el.value = 'x' 只改 DOM，React 內部的 state 不會
     更新，送出時仍然是空的 —— 這正是為什麼 URL 參數預填完全無效，也是
     autobook 那支腳本 log 裡滿滿「did not verify」的技術主因之一。
     必須呼叫原型上的 value setter，再手動派發 input/change 事件冒泡給 React。
   =================================================================== */

(function () {
  'use strict';

  /* ---- Local values are injected by build_bookmarklet.py at build time. ---- */
  var DATA = {
    FNAME: '__FNAME__',   // 申請人護照中文姓名 (繁體中文)
    LNAME: '__LNAME__',   // 申請人護照英文全名
    EMAIL: '__EMAIL__',   // 有效 Email
    Q3:    '__Q3__',      // 澳洲手機號
    Q10:   '__Q10__',     // 澳洲簽證號碼 Visa Grant No.
    Q9:    '__Q9__',      // 預計入台旅遊日期 (日曆元件，可能要手點)
    Q12:   '__Q12__'      // 是否有同行親屬 (原生下拉)
  };

  /* 欄位標籤 —— 取自頁面 JSON 設定裡的 "before" 字串 */
  var LABELS = {
    FNAME: '護照中文姓名',
    LNAME: '護照英文全名',
    EMAIL: '有效 Email',
    Q3:    '澳洲手機號',
    Q10:   '澳洲簽證號碼',
    Q9:    '預計入台旅遊日期',
    Q12:   '同行親屬',
    Q8:    '聲明'
  };

  /* 防機器人題的標記字 —— 用來「找到並跳過」，不是用來作答 */
  var BOT_MARKER = '機器人';

  var report = [];
  var ok = function (m) { report.push(['ok', m]); };
  var bad = function (m) { report.push(['bad', m]); };

  /* ---- 找欄位：先定位含標籤文字的最深節點，再往上找最近的輸入元件 ---- */
  function findField(labelText) {
    var all = document.querySelectorAll('body *');
    var best = null;
    for (var i = 0; i < all.length; i++) {
      var el = all[i];
      var t = el.textContent || '';
      if (t.indexOf(labelText) === -1) continue;
      // 取最深的那個（子節點都不含此文字）
      var deeper = false;
      for (var j = 0; j < el.children.length; j++) {
        if ((el.children[j].textContent || '').indexOf(labelText) !== -1) { deeper = true; break; }
      }
      if (!deeper) best = el;
    }
    if (!best) return null;
    // 從標籤節點往上爬，找第一個「子樹裡有輸入元件」的祖先
    var node = best;
    for (var d = 0; d < 8 && node; d++) {
      var f = node.querySelector('input,select,textarea');
      if (f) return f;
      node = node.parentElement;
    }
    return null;
  }

  /* ---- React-safe 賦值 ---- */
  function setVal(el, val) {
    var proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype
              : el instanceof HTMLSelectElement   ? HTMLSelectElement.prototype
              :                                     HTMLInputElement.prototype;
    var desc = Object.getOwnPropertyDescriptor(proto, 'value');
    if (desc && desc.set) { desc.set.call(el, val); } else { el.value = val; }
    el.dispatchEvent(new Event('input',  { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
  }

  function norm(s) { return (s || '').replace(/[\s\-()]/g, ''); }

  function fillText(code) {
    var want = DATA[code];
    if (!want) return;
    var el = findField(LABELS[code]);
    if (!el) { bad(LABELS[code] + '：找不到欄位，請手動填 ' + want); return; }
    // Safari 會自動記住並填好純文字欄位（中文姓名/英文名/Email/簽證號）。
    // 已經填對的就不要動 —— 重設反而可能觸發 React 驗證閃爍。
    if (norm(el.value) === norm(want)) { ok(LABELS[code] + '（Safari 已填好）'); return; }
    el.focus();
    setVal(el, want);
    el.blur();
    if (norm(el.value) === norm(want)) { ok(LABELS[code]); }
    else { bad(LABELS[code] + '：填了但沒生效，請手動輸入 ' + want); }
  }

  /* ---- 電話 (Q3)：自訂組件，帶國旗/區號，純字串常常吃不進去 ----
     依序試幾種格式，哪個能通過驗證就用哪個。 */
  function fillPhone() {
    var want = DATA.Q3;                       // configured locally
    var el = findField(LABELS.Q3);
    if (!el) { bad(LABELS.Q3 + '：找不到，請手動輸入 ' + want); return; }
    if (norm(el.value) === norm(want)) { ok(LABELS.Q3 + '（已填好）'); return; }
    var national = want.replace(/^\+61/, '0');
    var intl     = '+61' + want.replace(/^0/, '');
    var variants = [want, national, intl, want.replace(/^0/, '')];
    for (var i = 0; i < variants.length; i++) {
      el.focus();
      setVal(el, variants[i]);
      el.blur();
      var got = norm(el.value);
      // 組件可能自己重排成 +61 4xxx 或 04xx —— 只要末 9 碼對上就算成功
      if (got && got.slice(-9) === norm(want).slice(-9)) {
        ok(LABELS.Q3 + '（格式：' + variants[i] + '）');
        return;
      }
    }
    bad(LABELS.Q3 + '：組件不吃程式賦值，請手動輸入 ' + want);
  }

  /* ---- 日期 (Q9)：日曆控件 ---- */
  function fillDate() {
    var want = DATA.Q9;                       // 例：14/11/2026
    var el = findField(LABELS.Q9);
    if (!el) { bad(LABELS.Q9 + '：找不到，請手動點選 ' + want); return; }
    if (norm(el.value) === norm(want)) { ok(LABELS.Q9 + '（已填好）'); return; }
    var p = want.split('/');                  // [DD, MM, YYYY]
    var iso = p.length === 3 ? p[2] + '-' + p[1] + '-' + p[0] : want;
    var variants = (el.type === 'date') ? [iso, want] : [want, iso];
    for (var i = 0; i < variants.length; i++) {
      el.focus();
      setVal(el, variants[i]);
      el.blur();
      if (el.value) { ok(LABELS.Q9 + '（' + el.value + '，請核對一下）'); return; }
    }
    bad(LABELS.Q9 + '：日曆控件要手點，請選 ' + want);
  }

  /* ---- 原生下拉 (Q12 同行親屬) ---- */
  function fillSelect(code) {
    var want = DATA[code];
    var el = findField(LABELS[code]);
    if (!el) { bad(LABELS[code] + '：找不到，請手動選 ' + want); return; }
    if (el.tagName === 'SELECT') {
      // 注意：原生 <option> 是點不到的（autobook log 裡 24 次 DROPDOWN FAILED
      // 全栽在這）。正確做法是設 select.value，不是去 click option。
      setVal(el, want);
      if (el.value === want) { ok(LABELS[code]); }
      else { bad(LABELS[code] + '：請手動選 ' + want); }
    } else {
      bad(LABELS[code] + '：非標準下拉，請手動選 ' + want);
    }
  }

  /* ---- 聲明勾選框 (Q8) ---- */
  function tickDeclaration() {
    var el = findField(LABELS.Q8);
    if (!el || el.type !== 'checkbox') {
      // 退而求其次：找頁面上唯一未勾的 checkbox
      var boxes = document.querySelectorAll('input[type=checkbox]');
      el = boxes.length === 1 ? boxes[0] : null;
    }
    if (!el) { bad('聲明勾選框：找不到，請手動勾'); return; }
    if (!el.checked) el.click();
    if (el.checked) { ok('聲明已勾選'); } else { bad('聲明：請手動勾選'); }
  }

  /* ---- 找到防機器人題並捲過去（只定位，不作答）---- */
  function gotoBotQuestions() {
    var nodes = document.querySelectorAll('body *');
    var target = null;
    for (var i = 0; i < nodes.length; i++) {
      var el = nodes[i];
      if ((el.textContent || '').indexOf(BOT_MARKER) === -1) continue;
      var deeper = false;
      for (var j = 0; j < el.children.length; j++) {
        if ((el.children[j].textContent || '').indexOf(BOT_MARKER) !== -1) { deeper = true; break; }
      }
      if (!deeper) { target = el; break; }
    }
    if (!target) return false;
    var box = target;
    for (var d = 0; d < 5 && box.parentElement; d++) box = box.parentElement;
    box.style.outline = '3px solid #e8590c';
    box.style.outlineOffset = '4px';
    box.style.borderRadius = '6px';
    target.scrollIntoView({ behavior: 'smooth', block: 'center' });
    return true;
  }

  /* ---- 結果橫幅（不用 alert，免得擋住畫面）---- */
  function banner(foundBot) {
    var old = document.getElementById('__tw_helper_banner');
    if (old) old.remove();
    var okCount  = report.filter(function (r) { return r[0] === 'ok';  }).length;
    var badItems = report.filter(function (r) { return r[0] === 'bad'; });

    var d = document.createElement('div');
    d.id = '__tw_helper_banner';
    d.style.cssText = 'position:fixed;z-index:2147483647;left:8px;right:8px;top:8px;' +
      'background:#111;color:#fff;font:13px/1.5 -apple-system,system-ui,sans-serif;' +
      'padding:12px 14px;border-radius:10px;box-shadow:0 6px 24px rgba(0,0,0,.4);' +
      'max-height:45vh;overflow:auto';

    var h = '<b style="font-size:14px">已填 ' + okCount + ' 個欄位</b>';
    if (badItems.length) {
      h += '<div style="margin-top:8px;color:#ffc9c9"><b>需要手動處理：</b><ul style="margin:4px 0 0 18px;padding:0">';
      badItems.forEach(function (r) { h += '<li>' + r[1] + '</li>'; });
      h += '</ul></div>';
    }
    h += '<div style="margin-top:10px;padding-top:8px;border-top:1px solid #444;color:#ffd8a8">' +
         '⚠️ 防機器人題 + 送出鍵由你本人完成' +
         (foundBot ? '（已用橘框標出並捲到該處）' : '（頁面上暫時找不到，往下滑）') + '</div>';
    h += '<div style="margin-top:8px;text-align:right">' +
         '<button id="__tw_helper_close" style="background:#333;color:#fff;border:0;' +
         'padding:5px 12px;border-radius:6px;font-size:12px">關閉</button></div>';
    d.innerHTML = h;
    document.body.appendChild(d);
    document.getElementById('__tw_helper_close').onclick = function () { d.remove(); };
    setTimeout(function () { if (d.parentNode) d.remove(); }, 20000);
  }

  /* ---- 主流程 ---- */
  if (!/youcanbook\.me$/.test(location.hostname)) {
    alert('請在 TECO 預約表單頁面上執行這個書籤。');
    return;
  }
  // Safari 通常已經把這 4 個純文字欄位填好了 —— fillText 會自動跳過已填對的
  ['FNAME', 'LNAME', 'EMAIL', 'Q10'].forEach(fillText);
  // 以下 4 個 Safari 一律不碰，是這個書籤真正的價值所在
  fillPhone();
  fillDate();
  fillSelect('Q12');
  tickDeclaration();
  var foundBot = gotoBotQuestions();
  banner(foundBot);
})();
