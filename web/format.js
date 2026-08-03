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

  return { escapeHtml, formatText };
});
