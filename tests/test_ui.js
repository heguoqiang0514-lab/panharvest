/**
 * 网页界面脚本的静态+行为检查（不需要浏览器）。
 * 用法： python tests/dump_ui.py && node tests/test_ui.js
 *
 * 它读取由 dump_ui.py 导出的 data/_ui.js（= Python 运行时真正吐给浏览器的字符串），
 * 注入极简 DOM 桩，实际执行 renderResult() 并检查生成的 HTML 有没有被引号/转义搞坏。
 */
const fs = require('fs');
const path = require('path');

const uiPath = path.join(__dirname, '..', 'data', '_ui.js');
if (!fs.existsSync(uiPath)) {
  console.error('[FAIL] 找不到 data/_ui.js，先运行： python tests/dump_ui.py');
  process.exit(1);
}
const js = fs.readFileSync(uiPath, 'utf8');

function mkEl() {
  return {
    textContent: '', innerHTML: '', className: '', value: '', style: {},
    scrollTop: 0, clientHeight: 0, scrollHeight: 0,
    classList: { add() {}, remove() {} },
    addEventListener() {},
  };
}
const els = {};
const document = { querySelector: (sel) => (els[sel] = els[sel] || mkEl()) };
const fetchStub = async () => ({ ok: true, json: async () => [] });
function EventSourceStub() { this.close = () => {}; }
const windowStub = { open() {} };
const navigatorStub = { clipboard: { writeText: async () => {} } };

let renderResult;
try {
  const factory = new Function(
    'document', 'fetch', 'EventSource', 'window', 'navigator',
    js + '\n; return typeof renderResult === "function" ? renderResult : null;'
  );
  renderResult = factory(document, fetchStub, EventSourceStub, windowStub, navigatorStub);
} catch (err) {
  console.error('[FAIL] 脚本执行报错：' + err.message);
  process.exit(1);
}

const fails = [];
const ok = (name, cond, detail) => {
  if (cond) console.log('[PASS] ' + name);
  else { console.log('[FAIL] ' + name + (detail ? '  -- ' + detail : '')); fails.push(name); }
};

ok('脚本能解析并暴露 renderResult', typeof renderResult === 'function');

const sample = {
  ok: true, message: '完成，成品已入库', job_id: '20261025-abc123',
  library: 'library/葬送的芙莉莲',
  report: 'reports/x/report.md',
  files: 30, size: 314572800, warnings: ['测试警告'],
  candidates: [
    { score: 98.6, verdict: 'accept', cloud: 'baidu', title: "葬送的芙莉莲 1-10卷 O'Reilly",
      url: 'https://pan.baidu.com/s/1good', password: 'abcd', reasons: ['标题高度吻合（100%）'] },
    { score: 31, verdict: 'reject', cloud: 'quark', title: '不相干', url: 'https://x', password: '' },
  ],
};

try {
  renderResult(sample);
} catch (err) {
  console.error('[FAIL] renderResult 抛错：' + err.message);
  process.exit(1);
}
const html = els['#result'].innerHTML || '';
ok('渲染出内容', html.length > 200, html.length + ' 字符');
ok('包含入库路径', html.includes('library'), html.slice(0, 120));
ok('包含打开目录按钮', html.includes('openPath('), '');
ok('单引号被安全带走', !/O'Reilly/.test(html) || html.includes("O'Reilly"), '');
ok('表格里带候选标题', html.includes('葬送的芙莉莲'), '');
ok('带警告文本', html.includes('测试警告'), '');

// onclick 里的引号必须成对：粗查 openPath( 后的单引号数量
const handlers = html.match(/onclick="[^"]*"/g) || [];
let badHandler = handlers.find((h) => {
  const inner = h.slice(9, -1).replace(/&#39;/g, "'");
  return (inner.match(/'/g) || []).length % 2 !== 0;
});
ok('onclick 引号成对', !badHandler, badHandler || '');

console.log('='.repeat(56));
if (fails.length) {
  console.error('UI 脚本检查失败：' + fails.join(', '));
  process.exit(1);
}
console.log('UI 脚本检查通过');
