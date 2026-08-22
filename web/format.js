(function attachFormatting(root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.OrderSuggestFormat = api;
})(typeof window !== 'undefined' ? window : globalThis, function buildFormatting() {
  'use strict';

  const escapeHtml = (value) => String(value || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');

  const formatText = (text) => {
    if (!text) return '';
    let html = escapeHtml(String(text)).replace(/\n/g, '<br>');
    html = html.replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>');
    return html;
  };

  // 串流時用這個。文字是一段一段到的，收到「**建議」但還沒收到收尾的「**」
  // 時，formatText 會把那兩顆星原樣印在畫面上，等收尾到了才突然變粗體——
  // 看起來就像吐字吐錯又自己改。這裡把結尾那組還沒配對的 ** 先藏起來，
  // 內容照樣顯示，只是等湊成對才套粗體。
  const formatStreamingText = (text) => {
    if (!text) return '';
    const value = String(text);
    const pairs = (value.match(/\*\*/g) || []).length;
    if (pairs % 2 === 0) return formatText(value);
    const lastOpen = value.lastIndexOf('**');
    return formatText(value.slice(0, lastOpen) + value.slice(lastOpen + 2));
  };

  return { escapeHtml, formatText, formatStreamingText };
});
