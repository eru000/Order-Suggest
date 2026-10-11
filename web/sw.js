'use strict';

// Service worker 的用途只有一個：讓這個網站可以「加到主畫面」，開起來沒有網址列。
// 它刻意不做離線功能——推薦、辨識、對話全部要連後端，離線能開也沒東西可用，
// 快取反而會讓使用者看到一個永遠轉圈的空殼。
//
// 唯一會被快取的是外殼（HTML/CSS/JS/圖示），而且一律網路優先：連得上就用最新的，
// 連不上才拿快取，這樣改完前端不會因為快取看到舊版。
// API 一律不碰：/api/* 直接走網路，拿不到就讓它失敗，不要回一份過期的推薦。

const CACHE = 'order-suggest-shell-v1';
const SHELL = [
  '/',
  '/web/web.css',
  '/web/web.js',
  '/web/decision.css',
  '/web/decision.js',
  '/web/conversation-scroll.js',
  '/web/format.js',
  '/web/icons/icon-192.png',
  '/web/icons/icon-512.png',
];

self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE)
      // 個別檔案失敗不該讓整個安裝失敗（例如某支 js 之後被改名）。
      .then(cache => Promise.allSettled(SHELL.map(url => cache.add(url))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(key => key !== CACHE).map(key => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', event => {
  const request = event.request;
  if (request.method !== 'GET') return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  // 後端資料永遠要是新的：過期的菜單或推薦比錯誤訊息更難發現。
  if (url.pathname.startsWith('/api/')) return;

  event.respondWith(
    fetch(request)
      .then(response => {
        if (response && response.ok) {
          const copy = response.clone();
          caches.open(CACHE).then(cache => cache.put(request, copy)).catch(() => {});
        }
        return response;
      })
      .catch(() => caches.match(request).then(hit => hit || caches.match('/')))
  );
});
