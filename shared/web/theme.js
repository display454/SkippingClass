/* 共用主题切换：跟随系统 / 浅色 / 深色
   ------------------------------------------------------------
   在 <head> 里同步加载，先于首屏渲染执行，避免刷新时白闪一下。
   状态存在 localStorage('suite.theme')，两个应用共用同一个偏好。
*/
(function () {
  var KEY = 'suite.theme';
  var ORDER = ['auto', 'light', 'dark'];
  var LABEL = { auto: '自动', light: '浅色', dark: '深色' };

  var ICON = {
    auto: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><circle cx="12" cy="12" r="8"/><path d="M12 4v16"/><path d="M12 4a8 8 0 0 1 0 16z" fill="currentColor" stroke="none"/></svg>',
    light: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><circle cx="12" cy="12" r="4.2"/><path d="M12 2.5v2M12 19.5v2M2.5 12h2M19.5 12h2M5.2 5.2l1.4 1.4M17.4 17.4l1.4 1.4M18.8 5.2l-1.4 1.4M6.6 17.4l-1.4 1.4"/></svg>',
    dark: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M20 14.5A8 8 0 0 1 9.5 4 8.2 8.2 0 1 0 20 14.5z"/></svg>'
  };

  function read() {
    try {
      var v = localStorage.getItem(KEY);
      return ORDER.indexOf(v) >= 0 ? v : 'auto';
    } catch (e) {
      return 'auto';
    }
  }

  function apply(mode) {
    var root = document.documentElement;
    if (mode === 'auto') root.removeAttribute('data-theme');
    else root.setAttribute('data-theme', mode);

    document.querySelectorAll('[data-theme-toggle]').forEach(function (btn) {
      btn.innerHTML = ICON[mode] + '<span>' + LABEL[mode] + '</span>';
      btn.title = '当前：' + LABEL[mode] + '，点击切换';
    });
  }

  function set(mode) {
    try { localStorage.setItem(KEY, mode); } catch (e) { /* 隐私模式下忽略 */ }
    apply(mode);
  }

  function cycle() {
    var next = ORDER[(ORDER.indexOf(read()) + 1) % ORDER.length];
    set(next);
  }

  window.SuiteTheme = { get: read, set: set, cycle: cycle, apply: apply };

  // 首屏前先把主题定下来
  apply(read());

  document.addEventListener('DOMContentLoaded', function () {
    apply(read());
    document.querySelectorAll('[data-theme-toggle]').forEach(function (btn) {
      btn.addEventListener('click', function (e) {
        e.preventDefault();
        cycle();
      });
    });
  });

  // 跟随系统时，系统切换要实时反映
  if (window.matchMedia) {
    var mq = window.matchMedia('(prefers-color-scheme: dark)');
    var onChange = function () { if (read() === 'auto') apply('auto'); };
    if (mq.addEventListener) mq.addEventListener('change', onChange);
    else if (mq.addListener) mq.addListener(onChange);
  }
})();
