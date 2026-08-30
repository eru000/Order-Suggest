'use strict';

(() => {
  const { escapeHtml, formatText, formatStreamingText } = window.OrderSuggestFormat;
  const STORAGE_KEY = 'ordering_assistant_threads_v1';
  const ACTIVE_KEY = 'ordering_assistant_active_thread_v1';
  const ADMIN_KEY_STORAGE = 'ordering_assistant_admin_key_v1';

  // 管理金鑰只存在使用者自己的瀏覽器裡，絕對不要寫死在這支檔案——它會原封
  // 不動送到每一個訪客的瀏覽器，而且一旦 commit 就永久留在 git 歷史裡。
  //
  // 這裡用 localStorage 而不是 sessionStorage：sessionStorage 每個分頁各自
  // 獨立，關掉分頁就沒了，所以每開一個新分頁都要重打一次金鑰——當初會想把
  // 金鑰寫死就是被這點煩到。localStorage 問一次就記住，直到金鑰失效（401）
  // 或使用者自己清掉。代價是共用電腦上會留著，所以只適合自己的機器。
  const readStoredAdminKey = () => {
    try {
      // sessionStorage 是舊的存放位置，順手接過來，免得使用者要重打。
      return localStorage.getItem(ADMIN_KEY_STORAGE)
        || sessionStorage.getItem(ADMIN_KEY_STORAGE)
        || '';
    } catch (e) {
      return '';  // 無痕模式下 storage 可能整個不能存取
    }
  };

  const clearStoredAdminKey = () => {
    try {
      localStorage.removeItem(ADMIN_KEY_STORAGE);
      sessionStorage.removeItem(ADMIN_KEY_STORAGE);
    } catch (e) {
      // 同上，存取不到就當作已經沒有了
    }
  };

  const requireAdminKey = () => {
    let key = readStoredAdminKey();
    if (!key) {
      key = String(prompt('此操作需要管理金鑰（這台電腦只會問這一次）', '') || '').trim();
      if (!key) throw new Error('已取消管理操作');
      try {
        localStorage.setItem(ADMIN_KEY_STORAGE, key);
      } catch (e) {
        // 存不起來就下次再問一次，不影響這次操作
      }
    }
    return key;
  };

  const adminFetch = async (url, options = {}) => {
    const headers = new Headers(options.headers || {});
    headers.set('X-Admin-Key', requireAdminKey());
    const response = await fetch(url, { ...options, headers });
    if (response.status === 401) {
      clearStoredAdminKey();
      throw new Error('管理金鑰無效，已清除，請重新操作');
    }
    return response;
  };

  const chatListEl = document.getElementById('chat-list');
  const chatSearchEl = document.getElementById('chat-search');
  const newChatBtn = document.getElementById('new-chat-btn');
  const exportAllBtn = document.getElementById('export-all-btn');
  const importJsonBtn = document.getElementById('import-json-btn');
  const importFileEl = document.getElementById('import-file');
  const uploadPhotoBtn = document.getElementById('upload-photo-btn');
  const crawlMenuBtn = document.getElementById('crawl-menu-btn');
  const uploadPhotoFile = document.getElementById('upload-photo-file');
  const clearBtn = document.getElementById('clear-btn');
  const helpBtn = document.getElementById('help-btn');
  const themeToggleBtn = document.getElementById('theme-toggle-btn');
  const themeIcon = document.getElementById('theme-icon');
  const chatBox = document.getElementById('chat-box');
  const chatMenuEl = document.getElementById('chat-menu');
  const toastHostEl = document.getElementById('toast-host');
  const input = document.getElementById('input');
  const sendBtn = document.getElementById('send-btn');
  const loading = document.getElementById('loading');
  const toolbarRestaurantMeta = document.getElementById('toolbar-restaurant-meta');
  const mobileRestaurantCount = document.getElementById('mobile-restaurant-count');
  const globalRestaurantName = document.getElementById('global-restaurant-name');
  const assistantStatus = document.getElementById('assistant-status');
  const managementDrawer = document.getElementById('management-drawer');
  const mobileNavBtn = document.getElementById('mobile-nav-btn');
  const drawerCloseBtn = document.getElementById('drawer-close-btn');
  const drawerScrim = document.getElementById('drawer-scrim');

  let threads = [];
  let activeThreadId = null;
  let menuThreadId = null;
  let lastUndo = null;
  let searchQuery = '';
  let drawerReturnFocus = null;

  const setAssistantStatus = (text, state = 'ready') => {
    if (!assistantStatus) return;
    assistantStatus.dataset.state = state;
    assistantStatus.innerHTML = `<span class="state-dot" aria-hidden="true"></span>${escapeHtml(text)}`;
  };

  const openManagementDrawer = () => {
    if (!managementDrawer || window.innerWidth >= 992) return;
    drawerReturnFocus = document.activeElement;
    document.body.classList.add('drawer-open');
    mobileNavBtn?.setAttribute('aria-expanded', 'true');
    managementDrawer.setAttribute('aria-hidden', 'false');
    window.requestAnimationFrame(() => drawerCloseBtn?.focus({ preventScroll: true }));
  };

  const closeManagementDrawer = ({ restoreFocus = true } = {}) => {
    if (!managementDrawer) return;
    document.body.classList.remove('drawer-open');
    mobileNavBtn?.setAttribute('aria-expanded', 'false');
    if (window.innerWidth < 992) managementDrawer.setAttribute('aria-hidden', 'true');
    else managementDrawer.removeAttribute('aria-hidden');
    if (restoreFocus && drawerReturnFocus instanceof HTMLElement) {
      drawerReturnFocus.focus({ preventScroll: true });
    }
    drawerReturnFocus = null;
  };

  if (managementDrawer && window.innerWidth < 992) {
    managementDrawer.setAttribute('aria-hidden', 'true');
  }

  const nowTs = () => Date.now();
  const sidFactory = () => 't_' + Math.random().toString(16).slice(2) + Date.now().toString(16);

  const safeParseJSON = (text) => {
    try { return JSON.parse(text); } catch { return null; }
  };
  const relativeTime = (ts) => {
    if (!ts) return '';
    const diff = Date.now() - ts;
    const mins = Math.floor(diff / 60000);
    if (mins < 1) return '剛剛';
    if (mins < 60) return `${mins} 分鐘前`;
    const hrs = Math.floor(mins / 60);
    if (hrs < 24) return `${hrs} 小時前`;
    const days = Math.floor(hrs / 24);
    return `${days} 天前`;
  };

  const computePreview = (thread) => {
    if (!thread || !Array.isArray(thread.messages) || !thread.messages.length) return '';
    const last = thread.messages[thread.messages.length - 1];
    const t = String(last.text || '').replace(/\s+/g, ' ').trim();
    return t.length > 26 ? (t.slice(0, 26) + '…') : t;
  };

  const normalizeThread = (t) => {
    if (!t || typeof t !== 'object') return null;
    t.id = t.id || sidFactory();
    t.title = t.title || '新對話';
    t.createdAt = typeof t.createdAt === 'number' ? t.createdAt : nowTs();
    t.lastUpdatedAt = typeof t.lastUpdatedAt === 'number' ? t.lastUpdatedAt : t.createdAt;
    t.pinned = Boolean(t.pinned);
    t.flags = t.flags && typeof t.flags === 'object' ? t.flags : {};
    t.sessionId = t.sessionId || sidFactory();
    t.messages = Array.isArray(t.messages) ? t.messages : [];
    return t;
  };

  const saveState = () => {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(threads));
      if (activeThreadId) localStorage.setItem(ACTIVE_KEY, activeThreadId);
    } catch (e) {
      console.warn('saveState failed', e);
    }
  };

  const loadState = () => {
    const raw = localStorage.getItem(STORAGE_KEY);
    const loaded = raw ? safeParseJSON(raw) : null;
    threads = Array.isArray(loaded) ? loaded.map(normalizeThread).filter(Boolean) : [];
    const aid = localStorage.getItem(ACTIVE_KEY);
    activeThreadId = aid || (threads[0] && threads[0].id) || null;
  };

  const getActiveThread = () => threads.find(t => t.id === activeThreadId) || null;
  const getActiveSessionId = () => {
    const thread = getActiveThread() || createThread({ autoSwitch: true });
    return thread.sessionId;
  };

  const computeTitleFromFirstUserMessage = (thread) => {
    const firstUser = (thread.messages || []).find(m => m && m.role === 'user' && m.text);
    if (!firstUser) return '新對話';
    const t = String(firstUser.text).trim().replace(/\s+/g, ' ');
    return t.length > 14 ? (t.slice(0, 14) + '…') : t;
  };

  const showToast = ({ message, actionText, onAction, timeoutMs = 5000 } = {}) => {
    if (!toastHostEl) return;
    const toast = document.createElement('div');
    toast.className = 'toast';
    toast.innerHTML = `
      <div class="toast-msg">${escapeHtml(message || '')}</div>
      ${actionText ? `<button type="button" class="toast-action">${escapeHtml(actionText)}</button>` : ''}
      <button type="button" class="toast-close" aria-label="關閉">×</button>
    `;
    toastHostEl.appendChild(toast);

    const close = () => {
      if (!toast || !toast.parentNode || toast.classList.contains('is-leaving')) return;
      toast.classList.add('is-leaving');
      const remove = () => { if (toast.parentNode) toast.parentNode.removeChild(toast); };
      // 動畫被停用時 transitionend 不會來，所以補一個保險，避免 toast 卡在畫面上
      toast.addEventListener('transitionend', remove, { once: true });
      window.setTimeout(remove, 300);
    };

    const btn = toast.querySelector('.toast-action');
    if (btn) btn.addEventListener('click', () => { try { onAction && onAction(); } finally { close(); } });
    const x = toast.querySelector('.toast-close');
    if (x) x.addEventListener('click', close);
    window.setTimeout(close, timeoutMs);
  };

  const closeThreadMenu = () => {
    if (!chatMenuEl) return;
    chatMenuEl.classList.add('hidden');
    menuThreadId = null;
  };

  const openThreadMenu = ({ threadId, anchorEl, point } = {}) => {
    if (!chatMenuEl) return;
    menuThreadId = threadId;
    chatMenuEl.classList.remove('hidden');

    const rect = anchorEl ? anchorEl.getBoundingClientRect() : null;
    const x = point ? point.x : (rect ? (rect.left + 6) : 0);
    const y = point ? point.y : (rect ? (rect.bottom + 6) : 0);
    const pad = 8;
    const menuRect = chatMenuEl.getBoundingClientRect();
    let left = x;
    let top = y;
    if (left + menuRect.width + pad > window.innerWidth) left = window.innerWidth - menuRect.width - pad;
    if (top + menuRect.height + pad > window.innerHeight) top = window.innerHeight - menuRect.height - pad;
    chatMenuEl.style.left = `${Math.max(pad, left)}px`;
    chatMenuEl.style.top = `${Math.max(pad, top)}px`;
  };

  const renderChatList = () => {
    if (!chatListEl) return;
    const q = String(searchQuery || '').trim().toLowerCase();
    let list = threads.slice();

    if (q) {
      list = list.filter(t => {
        const title = String(t.title || '').toLowerCase();
        const preview = computePreview(t).toLowerCase();
        return title.includes(q) || preview.includes(q);
      });
    }

    if (!list.length) {
      chatListEl.innerHTML = `<div class="chat-list-empty">沒有對話</div>`;
      return;
    }

    chatListEl.innerHTML = list
      .sort((a, b) => {
        if (a.pinned !== b.pinned) return a.pinned ? -1 : 1;
        return (b.lastUpdatedAt || b.createdAt || 0) - (a.lastUpdatedAt || a.createdAt || 0);
      })
      .map(t => {
        const title = t.title || '新對話';
        const preview = computePreview(t);
        const timeText = relativeTime(t.lastUpdatedAt || t.createdAt);
        const active = t.id === activeThreadId;
        const pinIcon = t.pinned ? '[Pin]' : '';
        const clearedIcon = t.flags && t.flags.cleared ? '[Cleared]' : '';
        const errorIcon = t.flags && t.flags.error ? '[Error]' : '';
        const icons = [pinIcon, clearedIcon, errorIcon].filter(Boolean).join(' ');

        return `
          <div class="chat-list-row ${active ? 'active' : ''}" data-thread-id="${t.id}">
            <button class="chat-list-item" type="button" data-thread-id="${t.id}" title="切換對話">
              <div class="chat-list-top">
                <span class="chat-list-title">${escapeHtml(title)}</span>
                <span class="chat-list-time">${escapeHtml(timeText)}</span>
              </div>
              <div class="chat-list-preview">
                ${icons ? `<span class="chat-list-icons">${escapeHtml(icons)}</span>` : ''}
                <span class="chat-list-preview-text">${escapeHtml(preview || ' ')} </span>
              </div>
            </button>
            <button class="chat-list-more" type="button" aria-label="更多" title="更多" data-thread-more="${t.id}">⋯</button>
          </div>`;
      })
      .join('');

    chatListEl.querySelectorAll('[data-thread-id]').forEach(btn => {
      if (!(btn instanceof HTMLButtonElement)) return;
      if (btn.hasAttribute('data-thread-more')) return;
      btn.addEventListener('click', () => {
        const tid = btn.getAttribute('data-thread-id');
        if (!tid) return;
        switchThread(tid);
      });
    });

    chatListEl.querySelectorAll('[data-thread-more]').forEach(moreBtn => {
      moreBtn.addEventListener('click', (e) => {
        e.preventDefault();
        e.stopPropagation();
        const tid = moreBtn.getAttribute('data-thread-more');
        if (!tid) return;
        openThreadMenu({ threadId: tid, anchorEl: moreBtn });
      });
    });

    chatListEl.querySelectorAll('.chat-list-row').forEach(row => {
      row.addEventListener('contextmenu', (e) => {
        e.preventDefault();
        const tid = row.getAttribute('data-thread-id');
        if (!tid) return;
        openThreadMenu({ threadId: tid, point: { x: e.clientX, y: e.clientY } });
      });
    });
  };

  const exportJSON = (obj, filename) => {
    const blob = new Blob([JSON.stringify(obj, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  };

  const exportAllThreads = () => {
    exportJSON({ version: 1, exportedAt: nowTs(), threads }, `點餐助手-對話匯出-${nowTs()}.json`);
    showToast({ message: '已匯出全部對話' });
  };

  const exportOneThread = (threadId) => {
    const t = threads.find(x => x.id === threadId);
    if (!t) return;
    exportJSON({ version: 1, exportedAt: nowTs(), thread: t }, `點餐助手-對話-${t.title || '新對話'}.json`);
    showToast({ message: '已匯出此對話' });
  };

  const importThreadsFromJSON = (data) => {
    if (!data) throw new Error('JSON 格式錯誤');
    const importedThreads = [];
    if (Array.isArray(data.threads)) importedThreads.push(...data.threads);
    if (data.thread) importedThreads.push(data.thread);
    if (!importedThreads.length) throw new Error('找不到 threads/thread 欄位');

    const normalized = importedThreads.map(normalizeThread).filter(Boolean);
    const existingIds = new Set(threads.map(t => t.id));
    for (const t of normalized) {
      if (existingIds.has(t.id)) t.id = sidFactory();
      threads.push(t);
    }
    if (!activeThreadId && threads[0]) activeThreadId = threads[0].id;
    saveState();
    renderChatList();
    showToast({ message: `已匯入 ${normalized.length} 個對話` });
  };

  const deleteThread = (threadId) => {
    const idx = threads.findIndex(x => x.id === threadId);
    if (idx < 0) return;
    const removed = threads[idx];
    threads.splice(idx, 1);
    lastUndo = { type: 'delete', payload: removed, expiresAt: nowTs() + 10000 };

    if (activeThreadId === threadId) {
      activeThreadId = (threads[0] && threads[0].id) || null;
      if (!activeThreadId) createThread({ autoSwitch: true });
      redrawConversation({ keepScrollIfReading: false });
    }
    saveState();
    renderChatList();

    showToast({
      message: `已刪除「${removed.title || '新對話'}」`,
      actionText: '復原',
      onAction: () => {
        if (!lastUndo || lastUndo.type !== 'delete') return;
        if (nowTs() > lastUndo.expiresAt) return;
        threads.push(normalizeThread(lastUndo.payload));
        saveState();
        renderChatList();
      },
      timeoutMs: 9000,
    });
  };

  const togglePin = (threadId) => {
    const t = threads.find(x => x.id === threadId);
    if (!t) return;
    t.pinned = !t.pinned;
    t.lastUpdatedAt = nowTs();
    saveState();
    renderChatList();
    showToast({ message: t.pinned ? '已釘選' : '已取消釘選' });
  };

  const switchThread = (threadId) => {
    const t = threads.find(x => x.id === threadId);
    if (!t) return;
    activeThreadId = threadId;
    redrawConversation();
    loadRestaurants();
    renderChatList();
    saveState();
    input && input.focus();
  };

  const createThread = ({ autoSwitch = true } = {}) => {
    const t = {
      id: sidFactory(),
      title: '新對話',
      createdAt: Date.now(),
      lastUpdatedAt: Date.now(),
      pinned: false,
      flags: {},
      sessionId: sidFactory(),
      messages: []
    };
    threads.push(t);
    if (autoSwitch) activeThreadId = t.id;
    saveState();
    renderChatList();
    return t;
  };
  const isNearBottom = () => {
    const thresholdPx = 24;
    const distance = chatBox.scrollHeight - chatBox.scrollTop - chatBox.clientHeight;
    return distance <= thresholdPx;
  };

  const scrollToBottom = () => { chatBox.scrollTop = chatBox.scrollHeight; };

  const appendMessage = (role, text, { persist = true } = {}) => {
    const shouldAutoScroll = isNearBottom();
    const msgDiv = document.createElement('div');
    msgDiv.className = `message ${role}`;
    msgDiv.innerHTML = role === 'bot'
      ? `<div class="avatar">AI</div><div class="bubble">${formatText(text)}</div>`
      : `<div class="bubble">${formatText(text)}</div>`;
    chatBox.appendChild(msgDiv);
    // 總是自動滾動到最新訊息
    setTimeout(() => scrollToBottom(), 50);

    if (persist) {
      const t = getActiveThread() || createThread({ autoSwitch: true });
      t.messages = Array.isArray(t.messages) ? t.messages : [];
      t.messages.push({ role, text });
      t.lastUpdatedAt = nowTs();
      if (role === 'user' && (!t.title || t.title === '新對話')) {
        t.title = computeTitleFromFirstUserMessage(t);
      }
      saveState();
      renderChatList();
    }
  };

  const showLoading = (show) => {
    if (!loading) return;
    if (show) {
      setAssistantStatus('正在產生推薦', 'busy');
      loading.classList.remove('hidden');
      chatBox.appendChild(loading);
      if (isNearBottom()) scrollToBottom();
    } else {
      setAssistantStatus('等待需求', 'ready');
      loading.classList.add('hidden');
    }
  };

  const redrawConversation = ({ keepScrollIfReading = true } = {}) => {
    if (!chatBox) return;
    const shouldAutoScroll = keepScrollIfReading ? isNearBottom() : true;

    const t = getActiveThread();
    const hasMessages = Boolean(t && Array.isArray(t.messages) && t.messages.length);

    // 始終顯示對話框，不隱藏
    chatBox.classList.remove('hidden');

    if (!hasMessages) {
      // 沒有訊息時顯示歡迎訊息
      chatBox.innerHTML = '';
      return;
    }

    chatBox.innerHTML = '';
    for (const m of (t.messages || [])) {
      if (!m || !m.role) continue;
      appendMessage(m.role, m.text, { persist: false });
    }

    if (shouldAutoScroll) scrollToBottom();
  };

  const ensureBasics = () => {
    if (!chatBox || !input || !sendBtn) {
      alert('前端元素不存在或載入錯誤，請重新整理。');
      return false;
    }
    return true;
  };

  // 生成中的 AI 訊息：推薦卡片先填，說明文字後補。
  const appendStreamingBot = () => {
    const msgDiv = document.createElement('div');
    msgDiv.className = 'message bot';
    msgDiv.innerHTML = '<div class="avatar">AI</div>'
      + '<div class="bubble"><div class="rec-card hidden"></div><div class="rec-prose"></div></div>';
    chatBox.appendChild(msgDiv);
    setTimeout(() => scrollToBottom(), 50);
    return {
      card: msgDiv.querySelector('.rec-card'),
      prose: msgDiv.querySelector('.rec-prose'),
      remove: () => msgDiv.remove(),
    };
  };

  const renderRecCard = (card, ev) => {
    const items = Array.isArray(ev.items) ? ev.items : [];
    if (!items.length) return;
    const rows = items.map((it) => {
      const price = (typeof it.price === 'number') ? '$' + Math.round(it.price) : '';
      const why = it.reason ? '<span class="rec-why">' + escapeHtml(it.reason) + '</span>' : '';
      return '<li><span class="rec-name">' + escapeHtml(it.name) + '</span>'
        + '<span class="rec-price">' + price + '</span>' + why + '</li>';
    }).join('');
    const subtotal = (typeof ev.subtotal === 'number' && ev.subtotal > 0)
      ? '<div class="rec-total"><span>共 ' + items.length + ' 道菜</span><span>合計 <strong>$' + Math.round(ev.subtotal) + '</strong></span></div>' : '';
    card.innerHTML = '<div class="rec-head"><span><strong>推薦點餐單</strong><span class="rec-head-meta">依照目前需求組合</span></span><span class="rec-head-meta">價格</span></div>'
      + '<ul class="rec-list">' + rows + '</ul>' + subtotal;
    card.classList.remove('hidden');
  };

  // 存進對話紀錄用的純文字版；重新載入後會用這份重繪。
  const recToPlainText = (ev) => {
    const items = Array.isArray(ev.items) ? ev.items : [];
    if (!items.length) return '';
    const lines = items.map((it) => {
      const price = (typeof it.price === 'number') ? ' $' + Math.round(it.price) : '';
      return '・' + it.name + price + (it.reason ? '（' + it.reason + '）' : '');
    });
    if (typeof ev.subtotal === 'number' && ev.subtotal > 0) {
      lines.push('小計 $' + Math.round(ev.subtotal));
    }
    return '**建議這樣點**\n' + lines.join('\n');
  };

  const persistBotText = (text) => {
    const t = getActiveThread() || createThread({ autoSwitch: true });
    t.messages = Array.isArray(t.messages) ? t.messages : [];
    t.messages.push({ role: 'bot', text });
    t.lastUpdatedAt = nowTs();
    saveState();
    renderChatList();
  };

  // 回傳 false 代表「這個瀏覽器或這台伺服器不支援串流」，可以安全改用 /api/chat。
  // 注意：串流已經開始後就不能再退回重送，否則同一句話會被送進後端兩次。
  const sendStreaming = async (sessionId, text) => {
    let res;
    try {
      res = await fetch('/api/chat/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sessionId, text })
      });
    } catch (e) {
      return false;
    }
    if (!res.ok || !res.body || typeof res.body.getReader !== 'function') return false;

    const live = appendStreamingBot();
    const reader = res.body.getReader();
    const decoder = new TextDecoder('utf-8');
    let buffer = '';
    let prose = '';
    let recText = '';

    // 每收到一段就重設 innerHTML 等於整棵 DOM 重建，中文一次只來幾個字，
    // 一個回覆會觸發上百次，畫面會抖。改成一個影格最多畫一次。
    let paintHandle = 0;
    const paintProse = () => {
      if (paintHandle) return;
      paintHandle = requestAnimationFrame(() => {
        paintHandle = 0;
        live.prose.innerHTML = formatStreamingText(prose);
        if (isNearBottom()) scrollToBottom();
      });
    };

    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        let sep;
        while ((sep = buffer.indexOf('\n\n')) !== -1) {
          const chunk = buffer.slice(0, sep).trim();
          buffer = buffer.slice(sep + 2);
          if (!chunk.startsWith('data:')) continue;
          const payload = chunk.slice(5).trim();
          if (!payload || payload === '[DONE]') continue;

          let ev;
          try { ev = JSON.parse(payload); } catch (_) { continue; }

          if (ev.type === 'recommendation') {
            showLoading(false);
            renderRecCard(live.card, ev);
            recText = recToPlainText(ev);
            live.prose.innerHTML = '<span class="rec-waiting">AI 正在補充說明…</span>';
            if (isNearBottom()) scrollToBottom();
          } else if (ev.type === 'delta') {
            showLoading(false);
            if (!prose) live.prose.innerHTML = '';
            prose += ev.text || '';
            paintProse();
          } else if (ev.type === 'error') {
            prose += '\n[發生錯誤] ' + ev.error;
            live.prose.innerHTML = formatText(prose);
          }
        }
      }
    } catch (e) {
      prose += '\n[連線中斷] ' + e;
      live.prose.innerHTML = formatText(prose);
    }
    // 串流結束，補一次完整排版：中途被藏起來的未配對 ** 這時可能已經湊成對。
    cancelAnimationFrame(paintHandle);
    if (prose) live.prose.innerHTML = formatText(prose);

    if (!recText && !prose.trim()) {
      live.prose.innerHTML = formatText('[發生錯誤] 回覆內容為空');
      persistBotText('[發生錯誤] 回覆內容為空');
      return true;
    }
    // 串流跑完才寫入紀錄：中途中斷的半截不該被當成有效對話帶進下一輪。
    persistBotText([recText, prose.trim()].filter(Boolean).join('\n\n'));
    return true;
  };

  const sendBlocking = async (sessionId, text) => {
    try {
      const res = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sessionId, text })
      });
      const data = await res.json();
      showLoading(false);
      appendMessage('bot', data.reply || '[發生錯誤] 回覆內容為空');
    } catch (e) {
      showLoading(false);
      const t = getActiveThread();
      if (t) {
        t.flags = t.flags && typeof t.flags === 'object' ? t.flags : {};
        t.flags.error = true;
        t.lastUpdatedAt = nowTs();
        saveState();
        renderChatList();
      }
      appendMessage('bot', `[發生錯誤] 無法連線：${e}`);
    }
  };

  const send = async () => {
    const text = input.value.trim();
    if (!text) return;

    if (!getActiveThread()) {
      createThread({ autoSwitch: true });
      redrawConversation({ keepScrollIfReading: false });
    }

    input.value = '';
    appendMessage('user', text);

    showLoading(true);
    input.disabled = true;

    const t = getActiveThread();
    const sessionId = (t && t.sessionId) ? t.sessionId : sidFactory();

    try {
      const streamed = await sendStreaming(sessionId, text);
      if (!streamed) await sendBlocking(sessionId, text);
    } finally {
      showLoading(false);
      input.disabled = false;
      input.focus();
    }
  };

  const clearActiveChat = ({ notice = '' } = {}) => {
    showLoading(false);
    const t = getActiveThread();
    if (t) {
      t.messages = [];
      t.title = '新對話';
      t.sessionId = sidFactory();
      t.lastUpdatedAt = nowTs();
      t.flags = t.flags && typeof t.flags === 'object' ? t.flags : {};
      t.flags.cleared = true;
      saveState();
      renderChatList();
    }

    chatBox.innerHTML = '';
    if (notice) appendMessage('bot', notice);
    input.value = '';
    input.disabled = false;
  };

  const newChat = ({ notice = '已建立新對話' } = {}) => {
    showLoading(false);
    createThread({ autoSwitch: true });
    chatBox.innerHTML = '';
    if (notice) appendMessage('bot', notice);
    input.value = '';
    input.disabled = false;
    input.focus();
  };

  const crawlRestaurantMenu = async () => {
    const name = prompt('請輸入完整餐廳名稱，建議包含分店或地區', '');
    if (!name || !name.trim()) return;
    const restaurantName = name.trim();
    crawlMenuBtn.disabled = true;
    crawlMenuBtn.textContent = '爬取中…';
    showLoading(true);
    input.disabled = true;
    appendMessage('bot', `正在從 Google 搜尋「${restaurantName}」的菜單。若 Google 要求人機驗證，請在自動開啟的 Chrome 完成驗證。`);
    try {
      const response = await adminFetch('/api/menu/crawl', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ restaurantName, sessionId: getActiveSessionId() }),
      });
      const data = await response.json();
      if (!response.ok || !data.success) throw new Error(data.message || '爬取失敗');
      const items = Array.isArray(data.menuItems) ? data.menuItems : [];
      const preview = items.slice(0, 20).map((item, index) =>
        `${index + 1}. ${item.dish} - ${item.price == null ? '價格未標示' : `NT$${item.price}`}`
      ).join('\n');
      appendMessage('bot', `${data.message}\n共 ${data.itemCount} 項，已存成這個對話的目前餐廳。\n\n${preview}`);
      await loadRestaurants();
      showToast({ message: `已建立 ${data.restaurantName} 的菜單`, timeoutMs: 3000 });
    } catch (error) {
      appendMessage('bot', `[爬取失敗] ${error.message || error}`);
    } finally {
      crawlMenuBtn.disabled = false;
      crawlMenuBtn.textContent = '爬菜單';
      showLoading(false);
      input.disabled = false;
      input.focus();
    }
  };

  const handleHelp = () => {
    appendMessage('bot', '你可以直接輸入想點的餐點：例如「雞排飯 1 份、紅茶 2 杯」。我會幫你整理與確認。\n\n小技巧：點擊上方「📷上傳菜單」拍下店家菜單，我會辨識成可點選的菜單再幫你推薦。');
  };

  const showRename = (threadId) => {
    const t = threads.find(x => x.id === threadId);
    if (!t) return;
    const name = prompt('重新命名對話', t.title || '新對話');
    if (name === null) return;
    t.title = (name || '新對話').trim();
    t.lastUpdatedAt = nowTs();
    saveState();
    renderChatList();
  };

  if (!ensureBasics()) return;

  loadState();
  if (!threads.length) createThread({ autoSwitch: true });
  if (!getActiveThread() && threads[0]) activeThreadId = threads[0].id;
  renderChatList();
  redrawConversation({ keepScrollIfReading: false });

  sendBtn?.addEventListener('click', send);
  input?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  });

  chatSearchEl?.addEventListener('input', () => {
    searchQuery = chatSearchEl.value || '';
    renderChatList();
  });

  exportAllBtn?.addEventListener('click', exportAllThreads);
  if (importJsonBtn && importFileEl) {
    importJsonBtn.addEventListener('click', () => importFileEl.click());
    importFileEl.addEventListener('change', async () => {
      const f = importFileEl.files && importFileEl.files[0];
      if (!f) return;
      try {
        const text = await f.text();
        const data = safeParseJSON(text);
        importThreadsFromJSON(data);
      } catch (e) {
        showToast({ message: `匯入失敗：${e}` });
      } finally {
        importFileEl.value = '';
      }
    });
  }

  chatMenuEl?.querySelectorAll('[data-action]')?.forEach(btn => {
    btn.addEventListener('click', () => {
      const action = btn.getAttribute('data-action');
      const tid = menuThreadId;
      closeThreadMenu();
      if (!tid) return;
      if (action === 'rename') return showRename(tid);
      if (action === 'pin') return togglePin(tid);
      if (action === 'export') return exportOneThread(tid);
      if (action === 'delete') return deleteThread(tid);
    });
  });

  window.addEventListener('click', (e) => {
    if (!chatMenuEl || chatMenuEl.classList.contains('hidden')) return;
    if (e.target === chatMenuEl || chatMenuEl.contains(e.target)) return;
    closeThreadMenu();
  });
  window.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeThreadMenu(); });
  window.addEventListener('resize', closeThreadMenu);
  window.addEventListener('scroll', () => closeThreadMenu(), true);
  chatListEl?.addEventListener('scroll', () => closeThreadMenu(), { passive: true });

  newChatBtn?.addEventListener('click', () => newChat({ notice: '已建立新對話' }));
  crawlMenuBtn?.addEventListener('click', crawlRestaurantMenu);
  mobileNavBtn?.addEventListener('click', openManagementDrawer);
  drawerCloseBtn?.addEventListener('click', () => closeManagementDrawer());
  drawerScrim?.addEventListener('click', () => closeManagementDrawer());
  document.querySelectorAll('[data-proxy-target]').forEach((button) => {
    button.addEventListener('click', () => {
      const targetId = button.getAttribute('data-proxy-target');
      const target = targetId ? document.getElementById(targetId) : null;
      closeManagementDrawer({ restoreFocus: false });
      target?.click();
    });
  });
  chatListEl?.addEventListener('click', () => {
    if (window.innerWidth < 992) closeManagementDrawer({ restoreFocus: false });
  });
  window.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && document.body.classList.contains('drawer-open')) {
      closeManagementDrawer();
    }
  });
  window.addEventListener('resize', () => {
    if (window.innerWidth >= 992) {
      if (document.body.classList.contains('drawer-open')) closeManagementDrawer({ restoreFocus: false });
      managementDrawer?.removeAttribute('aria-hidden');
    } else if (!document.body.classList.contains('drawer-open')) {
      managementDrawer?.setAttribute('aria-hidden', 'true');
    }
  });

  /* ---------------------------------------------
     照片辨識菜單：辨識 → 確認 → 修正
     辨識結果先停在待確認狀態，使用者按下確認才會寫進菜單。
     --------------------------------------------- */
  const visionModal = document.getElementById('vision-modal');
  const visionBodyEl = document.getElementById('vision-body');
  const visionStepEl = document.getElementById('vision-step');
  const visionCloseBtn = document.getElementById('vision-close');
  const visionDrop = document.getElementById('vision-drop');
  const visionCropEl = document.getElementById('vision-crop');
  const visionCropStage = document.getElementById('vision-crop-stage');
  const visionCropImage = document.getElementById('vision-crop-image');
  const visionCropFilename = document.getElementById('vision-crop-filename');
  const visionCropSize = document.getElementById('vision-crop-size');
  const visionCropHint = document.getElementById('vision-crop-hint');
  const visionCropChangeBtn = document.getElementById('vision-crop-change');
  const visionCropToolbar = visionCropEl?.querySelector('.vision-crop-toolbar');
  const visionNameEl = document.getElementById('vision-name');
  const visionBusyNote = document.getElementById('vision-busy-note');
  const visionElapsedEl = document.getElementById('vision-elapsed');
  const visionSummaryEl = document.getElementById('vision-summary');
  const visionNoticeEl = document.getElementById('vision-notice');
  const visionItemsEl = document.getElementById('vision-items');
  const visionCorrectInput = document.getElementById('vision-correct-input');
  const visionCorrectBtn = document.getElementById('vision-correct-btn');
  const visionCorrectMsg = document.getElementById('vision-correct-msg');
  const visionErrorEl = document.getElementById('vision-error');
  const visionAcceptWrap = document.getElementById('vision-accept-wrap');
  const visionAcceptEl = document.getElementById('vision-accept');
  const visionAcceptText = document.getElementById('vision-accept-text');
  const visionCancelBtn = document.getElementById('vision-cancel');
  const visionSubmitBtn = document.getElementById('vision-submit');

  const VISION_MAX_BYTES = 10 * 1024 * 1024;
  const VISION_CROP_MAX_EDGE = 2400;
  const VISION_CROPPER_TEMPLATE = `
    <cropper-canvas background scale-step="0.1">
      <cropper-image rotatable scalable translatable></cropper-image>
      <cropper-shade hidden></cropper-shade>
      <cropper-handle action="select" plain></cropper-handle>
      <cropper-selection initial-coverage="0.92" movable resizable>
        <cropper-grid role="grid" bordered covered></cropper-grid>
        <cropper-crosshair centered></cropper-crosshair>
        <cropper-handle action="move" theme-color="rgba(255, 255, 255, 0.35)"></cropper-handle>
        <cropper-handle action="n-resize"></cropper-handle>
        <cropper-handle action="e-resize"></cropper-handle>
        <cropper-handle action="s-resize"></cropper-handle>
        <cropper-handle action="w-resize"></cropper-handle>
        <cropper-handle action="ne-resize"></cropper-handle>
        <cropper-handle action="nw-resize"></cropper-handle>
        <cropper-handle action="se-resize"></cropper-handle>
        <cropper-handle action="sw-resize"></cropper-handle>
      </cropper-selection>
    </cropper-canvas>`;

  const visionPanels = {
    pick: document.getElementById('vision-panel-pick'),
    busy: document.getElementById('vision-panel-busy'),
    review: document.getElementById('vision-panel-review'),
    error: document.getElementById('vision-panel-error'),
  };

  let visionStage = 'pick';
  let visionFile = null;
  let visionAnalysisId = null;
  let visionResult = null;
  let visionAbort = null;
  let visionTicker = null;
  let visionStartedAt = 0;
  let visionReturnFocus = null;
  let visionCropper = null;
  let visionCropObjectUrl = null;
  let visionCropLoadToken = 0;

  // 底緣淡出只在真的還有內容可捲時出現
  const updateVisionScrollEdge = () => {
    if (!visionBodyEl) return;
    const remaining = visionBodyEl.scrollHeight - visionBodyEl.scrollTop - visionBodyEl.clientHeight;
    visionBodyEl.classList.toggle('has-more', remaining > 8);
  };

  const setVisionStage = (stage) => {
    const changed = visionStage !== stage;
    visionStage = stage;
    Object.entries(visionPanels).forEach(([key, el]) => {
      if (el) el.hidden = key !== stage;
    });
    // 只有換步驟才捲回頂端；修正後重繪仍停在使用者原本看的位置
    if (changed) visionBodyEl.scrollTop = 0;
    requestAnimationFrame(updateVisionScrollEdge);
  };

  visionBodyEl?.addEventListener('scroll', updateVisionScrollEdge, { passive: true });
  window.addEventListener('resize', updateVisionScrollEdge);

  const stopVisionTicker = () => {
    if (visionTicker) { window.clearInterval(visionTicker); visionTicker = null; }
  };

  const describeElapsed = (seconds) => {
    if (seconds < 12) return '正在讀整張圖，判斷版面與店名。';
    if (seconds < 40) return '正在分塊辨識。照片越大切得越多塊，每塊都要各跑一次，所以會久一點。';
    return '正在做最後校對，逐項對照原圖修正錯字與價格。';
  };

  const formatVisionFileSize = (bytes) => {
    if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
    return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  };

  const destroyVisionCropper = () => {
    visionCropLoadToken += 1;
    if (visionCropper && typeof visionCropper.destroy === 'function') {
      visionCropper.destroy();
    }
    visionCropper = null;
    if (visionCropObjectUrl) URL.revokeObjectURL(visionCropObjectUrl);
    visionCropObjectUrl = null;
    if (visionCropImage) {
      visionCropImage.onload = null;
      visionCropImage.onerror = null;
      visionCropImage.removeAttribute('src');
      visionCropImage.removeAttribute('style');
      if (visionCropStage && !visionCropStage.contains(visionCropImage)) {
        visionCropStage.appendChild(visionCropImage);
      }
    }
    visionCropStage?.querySelectorAll('cropper-canvas').forEach((el) => el.remove());
    visionCropStage?.classList.remove('is-fallback');
  };

  const showVisionCropFallback = (message, { showPreview = true } = {}) => {
    if (visionCropper && typeof visionCropper.destroy === 'function') visionCropper.destroy();
    visionCropper = null;
    visionCropStage?.querySelectorAll('cropper-canvas').forEach((el) => el.remove());
    if (visionCropImage) visionCropImage.style.display = showPreview ? '' : 'none';
    visionCropToolbar.hidden = true;
    visionCropStage.hidden = !showPreview;
    visionCropStage.classList.toggle('is-fallback', showPreview);
    visionCropHint.textContent = message;
    visionCropHint.classList.add('warn');
    requestAnimationFrame(updateVisionScrollEdge);
  };

  const setupVisionCropper = (file) => {
    destroyVisionCropper();
    const loadToken = visionCropLoadToken;
    visionCropFilename.textContent = file.name;
    visionCropSize.textContent = formatVisionFileSize(file.size);
    visionCropHint.textContent = '拖曳框線，只留下菜單內容；手機可用雙指縮放。';
    visionCropHint.classList.remove('warn');
    visionCropToolbar.hidden = false;
    visionCropStage.hidden = false;
    visionCropStage.classList.remove('is-fallback');

    visionCropObjectUrl = URL.createObjectURL(file);
    visionCropImage.onload = () => {
      if (loadToken !== visionCropLoadToken) return;
      const width = visionCropImage.naturalWidth;
      const height = visionCropImage.naturalHeight;
      visionCropSize.textContent = `${width} × ${height} px・${formatVisionFileSize(file.size)}`;

      const CropperConstructor = window.Cropper && (window.Cropper.default || window.Cropper);
      if (typeof CropperConstructor !== 'function') {
        showVisionCropFallback('裁切工具目前無法載入，這次仍可直接送出原圖。');
        return;
      }

      try {
        visionCropper = new CropperConstructor(visionCropImage, {
          container: visionCropStage,
          template: VISION_CROPPER_TEMPLATE,
        });
        if (!visionCropper.getCropperSelection()) throw new Error('找不到裁切範圍');
        requestAnimationFrame(updateVisionScrollEdge);
      } catch (error) {
        showVisionCropFallback(`無法開啟裁切工具，這次會使用原圖：${error.message || '未知錯誤'}`);
      }
    };
    visionCropImage.onerror = () => {
      if (loadToken !== visionCropLoadToken) return;
      showVisionCropFallback('瀏覽器無法預覽這種圖片格式，會直接把原圖送給模型辨識。', { showPreview: false });
    };
    visionCropImage.src = visionCropObjectUrl;
  };

  const canvasToBlob = (canvas, type, quality) => new Promise((resolve, reject) => {
    canvas.toBlob((blob) => {
      if (blob) resolve(blob);
      else reject(new Error('瀏覽器無法產生裁切圖片'));
    }, type, quality);
  });

  const prepareVisionUploadFile = async () => {
    if (!visionCropper) return visionFile;
    const selection = visionCropper.getCropperSelection();
    if (!selection || selection.width <= 0 || selection.height <= 0) {
      throw new Error('裁切範圍是空的，請按「重設」後再試一次。');
    }

    // 限制長邊，避免手機高畫素照片在瀏覽器建立超大 canvas；2400 px 對菜單文字仍有足夠細節。
    const sourceLongEdge = Math.max(visionCropImage.naturalWidth, visionCropImage.naturalHeight);
    const outputLongEdge = Math.max(1, Math.min(sourceLongEdge, VISION_CROP_MAX_EDGE));
    const aspectRatio = selection.width / selection.height;
    const outputWidth = Math.max(1, Math.round(aspectRatio >= 1 ? outputLongEdge : outputLongEdge * aspectRatio));
    const outputHeight = Math.max(1, Math.round(aspectRatio >= 1 ? outputLongEdge / aspectRatio : outputLongEdge));
    const canvas = await selection.$toCanvas({
      width: outputWidth,
      height: outputHeight,
      beforeDraw: (context, targetCanvas) => {
        context.fillStyle = '#ffffff';
        context.fillRect(0, 0, targetCanvas.width, targetCanvas.height);
        context.imageSmoothingEnabled = true;
        context.imageSmoothingQuality = 'high';
      },
    });
    const blob = await canvasToBlob(canvas, 'image/jpeg', 0.94);
    if (blob.size > VISION_MAX_BYTES) {
      throw new Error('裁切後的圖片仍超過 10 MB，請縮小裁切範圍後再試一次。');
    }
    const baseName = (visionFile.name || 'menu').replace(/\.[^.]+$/, '');
    return new File([blob], `${baseName}-cropped.jpg`, {
      type: 'image/jpeg',
      lastModified: Date.now(),
    });
  };

  const runVisionCropAction = (action) => {
    if (!visionCropper) return;
    const cropperImage = visionCropper.getCropperImage();
    const selection = visionCropper.getCropperSelection();
    try {
      if (action === 'rotate-left') cropperImage?.$rotate('-90deg');
      else if (action === 'rotate-right') cropperImage?.$rotate('90deg');
      else if (action === 'zoom-out') cropperImage?.$zoom(-0.1);
      else if (action === 'zoom-in') cropperImage?.$zoom(0.1);
      else if (action === 'reset') {
        cropperImage?.$resetTransform().$center('contain');
        selection?.$reset();
      }
      visionCropHint.textContent = '拖曳框線，只留下菜單內容；手機可用雙指縮放。';
      visionCropHint.classList.remove('warn');
    } catch (error) {
      visionCropHint.textContent = `圖片調整失敗：${error.message || '請重選照片'}`;
      visionCropHint.classList.add('warn');
    }
  };

  const openVision = () => {
    visionReturnFocus = document.activeElement;
    destroyVisionCropper();
    visionFile = null;
    visionAnalysisId = null;
    visionResult = null;
    visionNameEl.value = '';
    visionCorrectInput.value = '';
    visionCorrectMsg.hidden = true;
    visionAcceptWrap.hidden = true;
    visionAcceptEl.checked = false;
    visionDrop.hidden = false;
    visionCropEl.hidden = true;
    visionDrop.querySelector('.vision-drop-title').textContent = '點一下選照片，或把照片拖進來';
    visionStepEl.textContent = '選一張菜單照片';
    visionSubmitBtn.textContent = '開始辨識';
    visionSubmitBtn.disabled = true;
    visionCloseBtn.disabled = false;
    setVisionStage('pick');
    visionModal.classList.add('is-open');
    visionDrop.focus();
  };

  const closeVision = () => {
    // 辨識還在跑就中止請求，不要讓使用者卡在這個畫面等一個他已經不要的結果
    if (visionAbort) { visionAbort.abort(); visionAbort = null; }
    stopVisionTicker();
    visionModal.classList.remove('is-open');
    destroyVisionCropper();
    if (visionReturnFocus && typeof visionReturnFocus.focus === 'function') {
      visionReturnFocus.focus();
    }
  };

  const showVisionError = (message) => {
    stopVisionTicker();
    visionErrorEl.textContent = message;
    visionStepEl.textContent = '辨識沒有成功';
    visionSubmitBtn.textContent = '重新選照片';
    visionSubmitBtn.disabled = false;
    visionCloseBtn.disabled = false;
    visionAcceptWrap.hidden = true;
    setVisionStage('error');
  };

  const pickVisionFile = (file) => {
    if (!file) return;
    if (file.type && !file.type.startsWith('image/')) {
      showVisionError('這不是圖片檔。請選擇 JPEG、PNG、WebP 或 HEIC 菜單照片。');
      return;
    }
    if (file.size > VISION_MAX_BYTES) {
      showVisionError(`這張照片 ${(file.size / 1024 / 1024).toFixed(1)} MB，超過 10 MB 上限。請用相機的一般畫質重拍，或先縮圖。`);
      return;
    }
    visionFile = file;
    visionDrop.hidden = true;
    visionCropEl.hidden = false;
    setupVisionCropper(file);
    visionDrop.querySelector('.vision-drop-title').textContent = file.name;
    visionStepEl.textContent = '調整範圍後，交給 AI 理解菜單';
    visionSubmitBtn.textContent = '裁切並開始辨識';
    visionSubmitBtn.disabled = false;
    setVisionStage('pick');
  };

  const qualityChip = (score) => {
    if (score >= 0.85) return { cls: 'good', text: '辨識品質良好' };
    if (score >= 0.75) return { cls: 'good', text: '辨識品質尚可' };
    if (score >= 0.5) return { cls: 'warn', text: '辨識品質偏低' };
    return { cls: 'bad', text: '辨識品質很低' };
  };

  const renderVisionReview = (data, { firstReveal = false } = {}) => {
    visionResult = data;
    visionAnalysisId = data.analysisId || visionAnalysisId;

    const quality = data.quality || {};
    const score = Number(quality.score || 0);
    const coverage = Number(data.priceCoverage || quality.priceCoverage || 0);
    const chip = qualityChip(score);

    const chips = [
      `<span class="vision-chip ${chip.cls}">${chip.text}</span>`,
      `<span class="vision-chip">${data.itemCount || 0} 項</span>`,
      `<span class="vision-chip">${Math.round(coverage * 100)}% 有價格</span>`,
    ];
    if (data.restaurantNameCandidate) {
      chips.push(`<span class="vision-chip">店名：${escapeHtml(data.restaurantNameCandidate)}</span>`);
    }
    if (quality.modelFallback) {
      chips.push('<span class="vision-chip warn">用了備援模型</span>');
    }
    visionSummaryEl.innerHTML = chips.join('');

    // 有疑慮就講清楚是哪一種，不要只丟一句「品質不佳」
    const problems = [];
    if (data.identityConflict) {
      const hint = data.requestedRestaurantName || '（你填的名稱）';
      problems.push(`你填的店名「${escapeHtml(hint)}」跟照片上看到的「${escapeHtml(data.restaurantNameCandidate || '?')}」不一樣。`);
    }
    (data.conflicts || []).forEach((conflict) => {
      const names = (conflict.candidates || []).map(escapeHtml).join('」與「');
      if (conflict.type === 'price') {
        const prices = (conflict.prices || []).join(' / ');
        problems.push(`「${names}」在重疊區辨識到不同價格：${prices}。`);
      } else {
        problems.push(`「${names}」可能是同一道菜被認成兩項。`);
      }
    });
    (data.warnings || []).forEach((warning) => problems.push(escapeHtml(warning)));

    if (problems.length) {
      visionNoticeEl.innerHTML =
        '<div class="vision-notice-title">存檔前請先確認這幾點</div><ul>' +
        problems.map((text) => `<li>${text}</li>`).join('') +
        '</ul>';
      visionNoticeEl.hidden = false;
    } else {
      visionNoticeEl.hidden = true;
    }

    // 編號要跟後端修正指令的編號對得上：跨分類連續編號、跳過沒有名字的項目
    let counter = 0;
    const rows = [];
    (data.categories || []).forEach((category) => {
      const items = (category.items || []).filter((item) => item && item.name);
      if (!items.length) return;
      rows.push(`<div class="vision-cat">${escapeHtml(category.name || '其他')}</div>`);
      items.forEach((item) => {
        counter += 1;
        const hasPrice = typeof item.price === 'number';
        const priceText = hasPrice ? `$${Math.round(item.price)}` : '價格不明';
        // 依序浮現的延遲索引封頂在 6，長菜單不會讓最後幾項等太久
        const delayIndex = Math.min(counter - 1, 6);
        rows.push(
          `<div class="vision-item" style="--i:${delayIndex}">` +
          `<span class="vision-item-no">${counter}</span>` +
          `<span class="vision-item-name">${escapeHtml(item.name)}</span>` +
          `<span class="vision-item-price${hasPrice ? '' : ' missing'}">${priceText}</span>` +
          `</div>`
        );
      });
    });
    visionItemsEl.classList.toggle('is-first-reveal', firstReveal);
    visionItemsEl.innerHTML = rows.join('') || '<div class="vision-drop-hint">沒有辨識到品項</div>';

    const needsAcceptance = Boolean(data.needsAcceptance);
    visionAcceptWrap.hidden = !needsAcceptance;
    if (needsAcceptance) {
      visionAcceptText.textContent = '我看過上面的提醒，確認要用這份結果';
      visionAcceptEl.checked = false;
    }
    visionStepEl.textContent = '確認辨識結果，或先修正再存檔';
    visionSubmitBtn.textContent = '確認並存成菜單';
    visionSubmitBtn.disabled = needsAcceptance;
    visionCloseBtn.disabled = false;
    setVisionStage('review');
  };

  const startVisionAnalysis = async () => {
    if (!visionFile) return;
    const sourceFile = visionFile;
    visionStepEl.textContent = '正在套用裁切範圍';
    visionSubmitBtn.disabled = true;
    visionSubmitBtn.textContent = '準備圖片中…';

    let uploadFile;
    try {
      uploadFile = await prepareVisionUploadFile();
    } catch (error) {
      if (!visionModal.classList.contains('is-open')) return;
      showVisionError(error.message || '無法產生裁切圖片');
      return;
    }
    if (!visionModal.classList.contains('is-open') || visionFile !== sourceFile) return;

    setVisionStage('busy');
    visionStepEl.textContent = '辨識中，這會需要一點時間';
    visionSubmitBtn.disabled = true;
    visionSubmitBtn.textContent = '辨識中…';
    visionAcceptWrap.hidden = true;
    visionStartedAt = Date.now();
    visionBusyNote.textContent = describeElapsed(0);
    visionElapsedEl.textContent = '已經過 0 秒';
    stopVisionTicker();
    visionTicker = window.setInterval(() => {
      const seconds = Math.round((Date.now() - visionStartedAt) / 1000);
      visionElapsedEl.textContent = `已經過 ${seconds} 秒`;
      visionBusyNote.textContent = describeElapsed(seconds);
    }, 1000);

    visionAbort = new AbortController();
    try {
      const form = new FormData();
      form.append('image', uploadFile);
      form.append('restaurant_name', visionNameEl.value.trim());
      form.append('session_id', getActiveSessionId());
      const res = await adminFetch('/api/menu/vision', {
        method: 'POST',
        body: form,
        signal: visionAbort.signal,
      });
      const data = await res.json().catch(() => null);
      if (!res.ok) {
        throw new Error((data && data.detail) || `辨識失敗（HTTP ${res.status}）`);
      }
      stopVisionTicker();
      renderVisionReview(data, { firstReveal: true });
    } catch (err) {
      if (err.name === 'AbortError') return; // 使用者自己關掉的，不用報錯
      showVisionError(err.message || '辨識失敗');
    } finally {
      visionAbort = null;
      stopVisionTicker();
    }
  };

  const confirmVisionMenu = async () => {
    if (!visionAnalysisId) return;
    visionSubmitBtn.disabled = true;
    visionSubmitBtn.textContent = '存檔中…';
    try {
      const res = await adminFetch(`/api/menu/vision/${visionAnalysisId}/confirm`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          restaurant_name: visionNameEl.value.trim() || (visionResult && visionResult.restaurantNameCandidate) || '',
          accept_conflicts: visionAcceptEl.checked,
          sessionId: getActiveSessionId(),
        }),
      });
      const data = await res.json().catch(() => null);

      if (res.status === 409) {
        // 後端要求明確接受。結果還留在待確認區，勾了就能再送一次。
        visionAcceptWrap.hidden = false;
        visionAcceptEl.checked = false;
        visionAcceptText.textContent = (data && data.detail) || '需要人工確認才能存檔';
        visionSubmitBtn.textContent = '確認並存成菜單';
        visionSubmitBtn.disabled = true;
        return;
      }
      if (!res.ok) {
        throw new Error((data && data.detail) || `存檔失敗（HTTP ${res.status}）`);
      }

      showToast({ message: `已存成菜單：${data.restaurantName}（${data.itemCount} 項）` });
      closeVision();
      loadRestaurants();
    } catch (err) {
      showVisionError(err.message || '存檔失敗');
    }
  };

  const correctVisionMenu = async () => {
    const instruction = visionCorrectInput.value.trim();
    if (!instruction || !visionAnalysisId) return;
    visionCorrectBtn.disabled = true;
    try {
      const res = await adminFetch(`/api/menu/vision/${visionAnalysisId}/correct`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ instruction, sessionId: getActiveSessionId() }),
      });
      const data = await res.json().catch(() => null);
      if (!res.ok) {
        throw new Error((data && data.detail) || `修正失敗（HTTP ${res.status}）`);
      }
      // 改一項不重播整串浮現動畫，太吵
      renderVisionReview(data, { firstReveal: false });
      visionCorrectInput.value = '';
      visionCorrectMsg.textContent = data.correctionMessage || '已修正';
      visionCorrectMsg.hidden = false;
    } catch (err) {
      visionCorrectMsg.textContent = err.message || '修正失敗';
      visionCorrectMsg.hidden = false;
    } finally {
      visionCorrectBtn.disabled = false;
    }
  };

  const openVisionFilePicker = () => {
    uploadPhotoFile.value = '';
    uploadPhotoFile.onchange = (e) => pickVisionFile(e.target.files && e.target.files[0]);
    uploadPhotoFile.click();
  };

  visionSubmitBtn?.addEventListener('click', () => {
    if (visionStage === 'pick') startVisionAnalysis();
    else if (visionStage === 'review') confirmVisionMenu();
    else if (visionStage === 'error') { setVisionStage('pick'); openVisionFilePicker(); }
  });

  visionDrop?.addEventListener('click', openVisionFilePicker);
  visionDrop?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openVisionFilePicker(); }
  });
  ['dragenter', 'dragover'].forEach((type) => {
    visionDrop?.addEventListener(type, (e) => {
      e.preventDefault();
      visionDrop.classList.add('is-dragging');
    });
  });
  ['dragleave', 'drop'].forEach((type) => {
    visionDrop?.addEventListener(type, (e) => {
      e.preventDefault();
      visionDrop.classList.remove('is-dragging');
    });
  });
  visionDrop?.addEventListener('drop', (e) => {
    const file = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
    pickVisionFile(file);
  });
  visionCropChangeBtn?.addEventListener('click', openVisionFilePicker);
  visionCropToolbar?.addEventListener('click', (e) => {
    const button = e.target.closest('[data-vision-crop-action]');
    if (!button) return;
    runVisionCropAction(button.dataset.visionCropAction);
  });

  visionCorrectBtn?.addEventListener('click', correctVisionMenu);
  visionCorrectInput?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); correctVisionMenu(); }
  });
  visionAcceptEl?.addEventListener('change', () => {
    if (visionStage === 'review') visionSubmitBtn.disabled = !visionAcceptEl.checked;
  });

  visionCloseBtn?.addEventListener('click', closeVision);
  visionCancelBtn?.addEventListener('click', closeVision);
  visionModal?.addEventListener('click', (e) => {
    if (e.target && e.target.hasAttribute('data-vision-dismiss')) closeVision();
  });
  window.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && visionModal.classList.contains('is-open')) closeVision();
  });

  uploadPhotoBtn?.addEventListener('click', openVision);
  clearBtn?.addEventListener('click', () => clearActiveChat({ notice: '已清空，目前是新對話。' }));
  helpBtn?.addEventListener('click', handleHelp);

  // 餐廳管理功能
  const restaurantSelect = document.getElementById('restaurant-select');
  const restaurantSelectMobile = document.getElementById('restaurant-select-mobile');
  const restaurantInfo = document.getElementById('restaurant-info');
  const reviewPanel = document.getElementById('review-panel');
  const reviewContent = document.getElementById('review-content');
  const reviewUpdated = document.getElementById('review-updated');
  const reviewSearchForm = document.getElementById('review-search-form');
  const reviewRestaurantInput = document.getElementById('review-restaurant-input');
  const refreshReviewBtn = document.getElementById('refresh-review-btn');
  const changeReviewIdentityBtn = document.getElementById('change-review-identity-btn');
  const reviewIdentityModal = document.getElementById('review-identity-modal');
  const reviewIdentityBody = document.getElementById('review-identity-body');
  const reviewIdentityClose = document.getElementById('review-identity-close');
  const reviewIdentityOverlay = reviewIdentityModal?.querySelector('.menu-modal-overlay');
  const mobileReviewBtn = document.getElementById('mobile-review-btn');
  const mobileReviewModal = document.getElementById('mobile-review-modal');
  const mobileReviewBody = document.getElementById('mobile-review-body');
  const mobileReviewClose = document.getElementById('mobile-review-close');
  const mobileReviewOverlay = mobileReviewModal?.querySelector('.menu-modal-overlay');
  const reviewPanelHome = reviewPanel?.parentNode;
  const reviewPanelNextSibling = reviewPanel?.nextSibling;
  let currentReviewData = null;
  let reviewRequestController = null;
  let reviewRequestVersion = 0;
  let activeReviewRestaurantName = '';
  let mobileReviewPreviousFocus = null;

  const getSelectedRestaurantName = () => {
    const name = restaurantSelect?.value || restaurantSelectMobile?.value || '';
    if (!name || ['載入中...', '無可用餐廳', '無餐廳', '載入失敗'].includes(name)) return '';
    return name;
  };

  const getReviewSearchTarget = () => (
    reviewRestaurantInput?.value.trim()
    || activeReviewRestaurantName
    || getSelectedRestaurantName()
  );

  const setReviewSearchTarget = (name) => {
    activeReviewRestaurantName = String(name || '').trim();
    if (reviewRestaurantInput) reviewRestaurantInput.value = activeReviewRestaurantName;
  };

  const formatReviewTime = (value) => {
    if (!value) return '尚未更新';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return '時間未知';
    return `更新於 ${date.toLocaleString('zh-TW', { hour12: false })}`;
  };

  const syncReviewActionButtons = (data = currentReviewData, searching = false) => {
    const hasFreshReview = Boolean(data?.success && !data?.needsRefresh);
    if (refreshReviewBtn) {
      refreshReviewBtn.disabled = searching;
      refreshReviewBtn.textContent = searching ? '搜尋中...' : (hasFreshReview ? '重新搜尋' : '自動搜尋');
    }
    if (reviewRestaurantInput) reviewRestaurantInput.disabled = searching;
    if (mobileReviewBtn) {
      mobileReviewBtn.disabled = searching;
      mobileReviewBtn.textContent = searching ? '搜尋中...' : (hasFreshReview ? '查看評價' : '搜尋評價');
    }
  };

  const riskLabel = (level) => {
    if (level === 'high') return '高風險';
    if (level === 'medium') return '中風險';
    if (level === 'unknown') return '風險未知';
    return '低風險';
  };

  const sentimentLabel = (sentiment) => {
    if (sentiment === 'positive') return '偏正面';
    if (sentiment === 'negative') return '偏負面';
    if (sentiment === 'mixed') return '評價分歧';
    return '資料有限';
  };

  const renderList = (items, emptyText) => {
    const list = Array.isArray(items) ? items.filter(Boolean) : [];
    if (!list.length) return `<div class="review-muted">${escapeHtml(emptyText)}</div>`;
    return `<ul>${list.map(item => `<li>${escapeHtml(item)}</li>`).join('')}</ul>`;
  };

  const sourceTypeLabel = (type) => ({
    review_platform: '評論平台',
    forum: '論壇',
    blog: '部落格',
    news: '新聞／RSS',
    aggregator: '彙整站',
    official: '官方',
    web: '網頁'
  }[type] || '網頁');

  const searchProviderLabel = (provider) => ({
    google_maps: 'Google 地圖公開評分',
    ifoodie: '愛食記站內搜尋',
    bing_rss: 'Bing RSS',
    google_news_rss: 'Google News RSS',
    known_url: '已知餐廳網址',
    user_url: '使用者提供網址',
    multi: '多個免費來源',
    playwright: 'Playwright 瀏覽器',
    http: '舊版 HTML 搜尋',
    legacy: '舊版快取'
  }[provider] || provider || '未知');

  const renderSearchDiagnostics = (searchMeta) => {
    if (!searchMeta || typeof searchMeta !== 'object') return '';
    const attempted = Array.isArray(searchMeta.attemptedProviders)
      ? searchMeta.attemptedProviders.map(searchProviderLabel).join(' → ')
      : '';
    const rawCount = Number(searchMeta.rawResultCount || 0);
    const relevantCount = Number(searchMeta.relevantSourceCount || 0);
    if (!attempted && !rawCount && !relevantCount) return '';
    return `<div class="review-search-meta">
      <span>搜尋方式：${escapeHtml(attempted || searchProviderLabel(searchMeta.provider))}</span>
      <span>找到 ${rawCount} 筆，保留 ${relevantCount} 筆相關來源</span>
    </div>`;
  };

  const renderEvidenceList = (highlights, fallbackItems, evidenceMap, sourceMap, emptyText) => {
    const rows = Array.isArray(highlights) && highlights.length
      ? highlights
      : (Array.isArray(fallbackItems) ? fallbackItems.map(text => ({ text, evidenceIds: [] })) : []);
    if (!rows.length) return `<div class="review-muted">${escapeHtml(emptyText)}</div>`;
    return `<ul>${rows.map(row => {
      const links = (row.evidenceIds || []).map(id => {
        const evidence = evidenceMap.get(id);
        const source = evidence && sourceMap.get(evidence.sourceId);
        if (!source?.url) return '';
        return `<a class="review-evidence-link" href="${escapeHtml(source.url)}" target="_blank"
          rel="noopener noreferrer" title="${escapeHtml(evidence.text || source.title)}">${escapeHtml(id)}</a>`;
      }).filter(Boolean).join(' ');
      return `<li>${escapeHtml(row.text || '')}${links ? `<span class="review-evidence-links">${links}</span>` : ''}</li>`;
    }).join('')}</ul>`;
  };

  const renderReview = (data) => {
    if (!reviewContent || !reviewUpdated) return;
    currentReviewData = data || null;
    reviewUpdated.textContent = formatReviewTime(data?.updatedAt);

    if (!data || !data.success) {
      const identity = data?.restaurantIdentity;
      if (changeReviewIdentityBtn) changeReviewIdentityBtn.hidden = !identity;
      syncReviewActionButtons(data);
      reviewContent.innerHTML = `
        <div class="review-empty">${escapeHtml(data?.message || '尚未更新評價')}</div>
        ${identity ? `<div class="review-identity">
          <strong>${escapeHtml(identity.officialName)}</strong>
          <span>${escapeHtml(identity.address || '地址未提供')}</span>
        </div>` : ''}
        ${renderSearchDiagnostics(data?.searchMeta)}
        ${data?.needsRefresh && Number(data?.schemaVersion || 0) < 3
          ? '<div class="review-notice">這是舊版快取，更新後才能顯示來源信心與面向證據。</div>'
          : ''}
      `;
      return;
    }

    const recommendationScore = Number(data.recommendationScore ?? data.overallScore ?? 0);
    const confidenceScore = Number(data.confidenceScore || 0);
    const riskLevel = data.riskLevel || 'low';
    const sources = Array.isArray(data.sources) ? data.sources : [];
    const evidence = Array.isArray(data.evidence) ? data.evidence : [];
    const evidenceMap = new Map(evidence.map(item => [item.evidenceId, item]));
    const sourceMap = new Map(sources.map(item => [item.sourceId, item]));
    const identity = data.restaurantIdentity;
    if (changeReviewIdentityBtn) changeReviewIdentityBtn.hidden = !identity;
    syncReviewActionButtons(data);
    const aspects = data.aspects && typeof data.aspects === 'object' ? data.aspects : {};
    const aspectsHtml = Object.values(aspects).map(aspect => `
      <div class="review-aspect">
        <div>
          <span>${escapeHtml(aspect.label || '')}</span>
          <small>${aspect.status === 'estimated'
            ? '低信心參考'
            : (aspect.score == null ? '公開來源未提到' : `${Number(aspect.mentionCount || 0)} 筆證據`)}</small>
        </div>
        <strong>${aspect.score == null ? '未提及' : `${Number(aspect.score)} 分`}</strong>
      </div>
    `).join('');
    const hasAspectScores = Object.values(aspects).some(aspect => aspect?.score != null);
    const hasPrimaryScore = hasAspectScores || data.scoreBasis === 'platform_rating';
    const platformRating = data.platformRating && typeof data.platformRating === 'object'
      ? data.platformRating
      : null;
    const platformLabel = platformRating?.platform || '公開評論平台';
    const platformRatingHtml = platformRating && Number(platformRating.average) > 0
      ? `<div class="review-platform-rating">
          <strong>${escapeHtml(platformLabel)} ${Number(platformRating.average).toFixed(1)} / 5</strong>
          <span>${platformRating.reviewCount
            ? `${Number(platformRating.reviewCount).toLocaleString('zh-TW')} 則公開評論`
            : `${escapeHtml(platformLabel)}未顯示評論數`}</span>
        </div>`
      : '';
    const sourcesHtml = sources.length
      ? sources.map(source => `
          <a class="review-source" href="${escapeHtml(source.url)}" target="_blank" rel="noopener noreferrer">
            <span>${escapeHtml(source.title || source.sourceType || '來源')}</span>
            <div class="review-source-meta">
              <b>${escapeHtml(sourceTypeLabel(source.sourceType))}</b>
              ${source.provider ? `<b>${escapeHtml(searchProviderLabel(source.provider))}</b>` : ''}
              <b>品質 ${Math.round(Number(source.sourceQuality || 0) * 100)}</b>
              ${source.publishedAt ? `<b>${escapeHtml(new Date(source.publishedAt).toLocaleDateString('zh-TW'))}</b>` : '<b>日期未知</b>'}
              ${source.sponsored ? '<b class="sponsored">業配／合作</b>' : ''}
              ${source.duplicateOf ? '<b>重複來源</b>' : ''}
            </div>
            <small>${escapeHtml(source.excerpt || '')}</small>
          </a>
        `).join('')
      : '<div class="review-muted">沒有可顯示的來源</div>';

    reviewContent.innerHTML = `
      ${identity ? `<div class="review-identity">
        <strong>${escapeHtml(identity.officialName)}</strong>
        <span>${escapeHtml(identity.address || '地址未提供')}</span>
      </div>` : ''}
      ${renderSearchDiagnostics(data.searchMeta)}
      ${platformRatingHtml}
      ${data.needsRefresh ? '<div class="review-notice">這是舊版快取，請更新後再參考推薦分、資料信心與風險。</div>' : ''}
      <div class="review-score-row">
        <div class="review-metric">
          <strong>${hasPrimaryScore ? recommendationScore : '—'}</strong>
          <span>${data.scoreBasis === 'platform_rating' ? '平台評分換算' : '推薦分'}</span>
        </div>
        <div class="review-metric confidence">
          <strong>${confidenceScore}</strong>
          <span>資料信心 / 100</span>
        </div>
      </div>
      <div class="review-status-row">
        <span class="review-sentiment">${sentimentLabel(data.sentiment)}</span>
        <span class="review-risk ${riskLevel}">${riskLabel(riskLevel)}</span>
      </div>
      <p class="review-summary">${escapeHtml(data.summary || '目前摘要不足')}</p>
      <div class="review-aspects">${aspectsHtml || '<div class="review-muted">尚無面向資料</div>'}</div>
      <details class="review-more">
        <summary>更多資訊</summary>
        <div class="review-section">
          <div class="review-section-title">優點</div>
          ${renderEvidenceList(data.prosEvidence, data.pros, evidenceMap, sourceMap, '尚無明確優點')}
        </div>
        <div class="review-section">
          <div class="review-section-title">注意事項</div>
          ${renderEvidenceList(data.consEvidence, data.cons, evidenceMap, sourceMap, '尚無明確注意事項')}
        </div>
        <div class="review-section">
          <div class="review-section-title">可信度風險</div>
          ${renderEvidenceList(
            data.riskSignals,
            data.riskReasons,
            evidenceMap,
            sourceMap,
            riskLevel === 'unknown' ? '缺少足夠逐則評論，暫時無法判斷灌水風險' : '目前未發現明顯灌水訊號'
          )}
        </div>
        <details class="review-sources">
          <summary>查看來源 (${sources.length})</summary>
          ${sourcesHtml}
        </details>
      </details>
    `;
  };

  async function loadRestaurantReview(name) {
    if (!reviewContent || !reviewUpdated) return;
    const target = name || getSelectedRestaurantName();
    setReviewSearchTarget(target);
    const requestVersion = ++reviewRequestVersion;
    reviewRequestController?.abort();
    reviewRequestController = null;
    currentReviewData = null;
    syncReviewActionButtons(null);
    reviewUpdated.textContent = '讀取中...';
    reviewContent.innerHTML = '<div class="review-empty">讀取評價快取...</div>';
    if (!target) {
      renderReview({ success: false, message: '尚未選擇餐廳' });
      return;
    }
    const controller = new AbortController();
    reviewRequestController = controller;

    try {
      const response = await fetch(`/api/restaurant-review?restaurant_name=${encodeURIComponent(target)}`, {
        signal: controller.signal
      });
      const data = await response.json();
      if (controller.signal.aborted || requestVersion !== reviewRequestVersion) return;
      renderReview(data);
    } catch (err) {
      if (err?.name === 'AbortError' || requestVersion !== reviewRequestVersion) return;
      console.error('讀取評價失敗:', err);
      renderReview({ success: false, message: '無法讀取評價快取' });
    } finally {
      if (reviewRequestController === controller) reviewRequestController = null;
    }
  }

  function closeReviewIdentityModal() {
    reviewIdentityModal?.classList.add('hidden');
  }

  function openMobileReviewModal() {
    if (!mobileReviewModal || !mobileReviewBody || !reviewPanel) return;
    mobileReviewPreviousFocus = document.activeElement;
    mobileReviewBody.appendChild(reviewPanel);
    mobileReviewModal.classList.remove('hidden');
    window.requestAnimationFrame(() => mobileReviewClose?.focus({ preventScroll: true }));
  }

  function handleMobileReviewAction() {
    openMobileReviewModal();
    if ((!currentReviewData?.success || currentReviewData?.needsRefresh) && !refreshReviewBtn?.disabled) {
      refreshRestaurantReview();
    }
  }

  function closeMobileReviewModal() {
    if (!mobileReviewModal || !reviewPanel || !reviewPanelHome) return;
    mobileReviewModal.classList.add('hidden');
    reviewPanelHome.insertBefore(reviewPanel, reviewPanelNextSibling);
    if (mobileReviewPreviousFocus instanceof HTMLElement) {
      mobileReviewPreviousFocus.focus({ preventScroll: true });
    }
    mobileReviewPreviousFocus = null;
  }

  function showIdentityCandidates(candidates, target) {
    if (!reviewIdentityModal || !reviewIdentityBody) return;
    reviewIdentityModal.classList.remove('hidden');
    reviewIdentityBody.innerHTML = candidates.length ? `
      <p class="review-identity-help">找到幾間同名店，請選擇正確分店。</p>
      <div class="review-candidates">
        ${candidates.map((candidate, index) => `
          <button class="review-candidate" type="button" data-candidate-index="${index}">
            <strong>${escapeHtml(candidate.officialName || target)}</strong>
            <span>${escapeHtml(candidate.address || '地址未提供')}</span>
            <b>相符度 ${Number(candidate.confidence || 0)}%</b>
          </button>
        `).join('')}
      </div>
    ` : '<div class="menu-error">找不到可確認的餐廳分店</div>';
    reviewIdentityBody.querySelectorAll('[data-candidate-index]').forEach(button => {
      button.addEventListener('click', () => {
        const identity = candidates[Number(button.dataset.candidateIndex)];
        closeReviewIdentityModal();
        refreshRestaurantReview(identity, target);
      });
    });
  }

  async function identifyRestaurantReview() {
    const target = getReviewSearchTarget();
    if (!target || !reviewIdentityModal || !reviewIdentityBody) return;
    reviewIdentityModal.classList.remove('hidden');
    reviewIdentityBody.innerHTML = '<div class="menu-loading">正在搜尋同名餐廳與分店...</div>';
    try {
      const response = await fetch('/api/restaurant-review/identify', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ restaurant_name: target })
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || data.message || '店家搜尋失敗');
      const candidates = Array.isArray(data.candidates) ? data.candidates : [];
      showIdentityCandidates(candidates, target);
    } catch (err) {
      reviewIdentityBody.innerHTML = `<div class="menu-error">${escapeHtml(err.message || '店家搜尋失敗')}</div>`;
    }
  }

  async function refreshRestaurantReview(selectedIdentity = null, explicitName = '') {
    const target = String(explicitName || getReviewSearchTarget()).trim();
    if (!target) {
      showToast({ message: '請輸入餐廳名稱', timeoutMs: 2000 });
      return;
    }
    setReviewSearchTarget(target);
    const identity = selectedIdentity || (
      currentReviewData?.restaurantName === target
        ? currentReviewData?.restaurantIdentity
        : null
    );
    const previousData = currentReviewData;
    if (!reviewContent || !refreshReviewBtn) return;
    const requestVersion = ++reviewRequestVersion;
    reviewRequestController?.abort();
    const controller = new AbortController();
    reviewRequestController = controller;
    syncReviewActionButtons(currentReviewData, true);
    reviewUpdated.textContent = '更新中...';
    reviewContent.innerHTML = `<div class="review-empty">正在用瀏覽器搜尋「${escapeHtml(target)}」的 Google 評分與公開食記...</div>`;

    try {
      const response = await fetch('/api/restaurant-review/refresh', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ restaurant_name: target, restaurant_identity: identity }),
        signal: controller.signal
      });
      const data = await response.json();
      if (controller.signal.aborted || requestVersion !== reviewRequestVersion) return;
      if (!response.ok) {
        throw new Error(data.detail || data.message || '更新失敗');
      }
      if (data.needsIdentity) {
        if (target !== activeReviewRestaurantName) return;
        const candidates = Array.isArray(data.identityCandidates) ? data.identityCandidates : [];
        if (previousData) renderReview(previousData);
        showIdentityCandidates(candidates, target);
        return;
      }
      if (target !== activeReviewRestaurantName) return;
      renderReview(data);
      showToast({
        message: data.success ? '評價情報已更新' : (data.message || '這次沒有找到可用來源'),
        timeoutMs: data.success ? 2500 : 4000
      });
    } catch (err) {
      if (err?.name === 'AbortError' || requestVersion !== reviewRequestVersion) return;
      if (target !== activeReviewRestaurantName) return;
      console.error('更新評價失敗:', err);
      if (previousData) {
        renderReview(previousData);
        showToast({ message: `更新失敗: ${err.message || err}`, timeoutMs: 3500 });
      } else {
        renderReview({ success: false, message: `更新評價失敗: ${err.message || err}` });
      }
    } finally {
      if (reviewRequestController === controller) reviewRequestController = null;
      if (target === activeReviewRestaurantName) syncReviewActionButtons(currentReviewData);
    }
  }

  async function loadRestaurants() {
    try {
      const sessionId = getActiveSessionId();
      const response = await fetch(`/api/restaurants?session_id=${encodeURIComponent(sessionId)}`);
      const data = await response.json();

      // 清空兩個選擇器
      restaurantSelect.innerHTML = '';
      if (restaurantSelectMobile) {
        restaurantSelectMobile.innerHTML = '';
      }

      if (!data.restaurants || data.restaurants.length === 0) {
        restaurantSelect.innerHTML = '<option>無可用餐廳</option>';
        if (restaurantSelectMobile) {
          restaurantSelectMobile.innerHTML = '<option>無餐廳</option>';
        }
        restaurantInfo.textContent = '尚未爬取任何餐廳菜單';
        if (globalRestaurantName) globalRestaurantName.textContent = '尚無可用餐廳';
        if (toolbarRestaurantMeta) toolbarRestaurantMeta.textContent = '0 項';
        if (mobileRestaurantCount) mobileRestaurantCount.textContent = '0 項';
        restaurantInfo.style.background = '#fef2f2';
        restaurantInfo.style.color = '#991b1b';
        return;
      }

      // 填充兩個選擇器
      data.restaurants.forEach(r => {
        // 側邊欄選擇器（電腦版）
        const opt = document.createElement('option');
        opt.value = r.name;
        opt.textContent = `${r.name} (${r.itemCount}項)`;
        if (r.active) opt.selected = true;
        restaurantSelect.appendChild(opt);

        // 手機版選擇器
        if (restaurantSelectMobile) {
          const optMobile = document.createElement('option');
          optMobile.value = r.name;
          optMobile.textContent = r.name;
          if (r.active) optMobile.selected = true;
          restaurantSelectMobile.appendChild(optMobile);
        }
      });

      // 更新資訊顯示
      const active = data.restaurants.find(r => r.active);
      if (active) {
        restaurantInfo.textContent = `當前使用 ${active.name} 的菜單 (共 ${active.itemCount} 項菜品)`;
        if (globalRestaurantName) globalRestaurantName.textContent = active.name;
        if (toolbarRestaurantMeta) toolbarRestaurantMeta.textContent = `${active.itemCount} 項`;
        if (mobileRestaurantCount) mobileRestaurantCount.textContent = `${active.itemCount} 項`;
        restaurantInfo.style.background = '#f0fdf4';
        restaurantInfo.style.color = '#166534';
        await loadRestaurantReview(active.name);
      }

      console.log('已載入餐廳列表:', data.restaurants.length);
    } catch (err) {
      console.error('載入餐廳列表失敗:', err);
      restaurantSelect.innerHTML = '<option>載入失敗</option>';
      if (restaurantSelectMobile) {
        restaurantSelectMobile.innerHTML = '<option>載入失敗</option>';
      }
      restaurantInfo.textContent = '無法連接到後端服務';
      if (globalRestaurantName) globalRestaurantName.textContent = '餐廳資料讀取失敗';
      if (toolbarRestaurantMeta) toolbarRestaurantMeta.textContent = 'ERROR';
      if (mobileRestaurantCount) mobileRestaurantCount.textContent = 'ERROR';
      restaurantInfo.style.background = '#fef2f2';
      restaurantInfo.style.color = '#991b1b';
    }
  }

  async function switchRestaurant(name) {
    try {
      const sessionId = getActiveSessionId();
      const response = await fetch(`/api/switch-restaurant?restaurant_name=${encodeURIComponent(name)}&session_id=${encodeURIComponent(sessionId)}`, {
        method: 'POST'
      });
      const data = await response.json();

      if (data.success) {
        showToast({ message: `${data.message}`, timeoutMs: 2000 });
        await loadRestaurants(); // 重新載入更新資訊與評價快取
      } else {
        showToast(`切換失敗: ${data.message}`, 3000);
      }
    } catch (err) {
      console.error('切換餐廳失敗:', err);
      showToast('切換餐廳時發生錯誤', 3000);
    }
  }

  // 電腦版選擇器事件
  restaurantSelect?.addEventListener('change', (e) => {
    const selectedName = e.target.value;
    if (selectedName && selectedName !== '載入中...' && selectedName !== '無可用餐廳') {
      switchRestaurant(selectedName);
    }
  });

  // 手機版選擇器事件
  restaurantSelectMobile?.addEventListener('change', (e) => {
    const selectedName = e.target.value;
    if (selectedName && selectedName !== '載入中...' && selectedName !== '無餐廳') {
      switchRestaurant(selectedName);
    }
  });

  // 刪除餐廳功能
  const deleteRestaurantBtn = document.getElementById('delete-restaurant-btn');

  async function deleteRestaurant(name) {
    if (!confirm(`確定要刪除「${name}」的菜單嗎？\n\n此操作將會：\n1. 從系統中移除此餐廳\n2. 刪除對應的 JSON 檔案\n\n此操作無法復原！`)) {
      return;
    }

    try {
      const response = await adminFetch(`/api/menu/${encodeURIComponent(name)}`, {
        method: 'DELETE'
      });

      if (!response.ok) {
        const errorData = await response.json().catch(() => ({ message: '未知錯誤' }));
        throw new Error(errorData.message || errorData.detail || '刪除失敗');
      }

      const data = await response.json();
      if (data.success) {
        showToast({ message: `已刪除「${name}」`, timeoutMs: 3000 });
        // 重新載入餐廳列表
        await loadRestaurants();
      } else {
        showToast({ message: `刪除失敗: ${data.message}`, timeoutMs: 3000 });
      }
    } catch (err) {
      console.error('刪除餐廳失敗:', err);
      showToast({ message: `刪除時發生錯誤: ${err.message}`, timeoutMs: 3000 });
    }
  }

  // 刪除按鈕事件
  deleteRestaurantBtn?.addEventListener('click', () => {
    const selectedName = restaurantSelect?.value;
    if (selectedName && selectedName !== '載入中...' && selectedName !== '無可用餐廳' && selectedName !== '載入失敗') {
      deleteRestaurant(selectedName);
    } else {
      showToast({ message: '請先選擇一個餐廳', timeoutMs: 2000 });
    }
  });

  reviewSearchForm?.addEventListener('submit', (event) => {
    event.preventDefault();
    refreshRestaurantReview();
  });
  changeReviewIdentityBtn?.addEventListener('click', identifyRestaurantReview);
  reviewIdentityClose?.addEventListener('click', closeReviewIdentityModal);
  reviewIdentityOverlay?.addEventListener('click', closeReviewIdentityModal);
  mobileReviewBtn?.addEventListener('click', handleMobileReviewAction);
  mobileReviewClose?.addEventListener('click', closeMobileReviewModal);
  mobileReviewOverlay?.addEventListener('click', closeMobileReviewModal);

  // 深色模式切換
  const htmlElement = document.documentElement;

  // 從 localStorage 讀取主題設定
  const savedTheme = localStorage.getItem('theme') || 'light';
  htmlElement.setAttribute('data-theme', savedTheme);
  if (themeIcon) {
    themeIcon.textContent = savedTheme === 'dark' ? '淺色模式' : '深色模式';
  }

  themeToggleBtn?.addEventListener('click', () => {
    const currentTheme = htmlElement.getAttribute('data-theme');
    const newTheme = currentTheme === 'dark' ? 'light' : 'dark';

    htmlElement.setAttribute('data-theme', newTheme);
    localStorage.setItem('theme', newTheme);
    if (themeIcon) {
      themeIcon.textContent = newTheme === 'dark' ? '淺色模式' : '深色模式';
    }

    showToast({
      message: newTheme === 'dark' ? '已切換至深色模式' : '已切換至淺色模式',
      duration: 1500
    });
  });

  // 頁面載入時執行
  loadRestaurants();

  // ============================================
  // 查看完整菜單功能
  // ============================================
  const viewMenuBtn = document.getElementById('view-menu-btn');
  const menuModal = document.getElementById('menu-modal');
  const menuModalClose = document.getElementById('menu-modal-close');
  const menuModalOverlay = menuModal?.querySelector('.menu-modal-overlay');
  const menuModalBody = document.getElementById('menu-modal-body');
  const menuModalTitle = document.getElementById('menu-modal-title');
  let menuRequestController = null;
  let menuRequestVersion = 0;
  let menuPreviousFocus = null;

  // 打開 Modal
  async function openMenuModal() {
    if (!menuModal || !menuModalBody) return;

    const requestVersion = ++menuRequestVersion;
    menuRequestController?.abort();
    const controller = new AbortController();
    menuRequestController = controller;
    menuPreviousFocus = document.activeElement;
    menuModal.classList.add('is-open');
    menuModal.setAttribute('aria-hidden', 'false');
    menuModalBody.innerHTML = '<div class="menu-loading">載入中...</div>';
    window.requestAnimationFrame(() => menuModalClose?.focus({ preventScroll: true }));

    try {
      const sessionId = getActiveSessionId();
      const response = await fetch(`/api/current-menu?session_id=${encodeURIComponent(sessionId)}`, { signal: controller.signal });
      const data = await response.json();
      if (controller.signal.aborted || requestVersion !== menuRequestVersion
        || !menuModal.classList.contains('is-open')) return;

      if (!response.ok) {
        throw new Error(data.detail || data.message || '載入失敗');
      }
      if (!data.success) {
        menuModalBody.innerHTML = `<div class="menu-error">${escapeHtml(data.message || '載入失敗')}</div>`;
        return;
      }

      // 更新標題
      if (menuModalTitle && data.restaurantName) {
        menuModalTitle.textContent = `${data.restaurantName} - 完整菜單`;
      }

      // 渲染菜單
      if (!data.categories || data.categories.length === 0) {
        menuModalBody.innerHTML = '<div class="menu-empty">目前沒有菜單資料</div>';
        return;
      }

      let html = '';
      for (const category of data.categories) {
        const items = category.items || [];
        if (items.length === 0) continue;

        html += `
          <div class="menu-category">
            <h3 class="menu-category-title">${escapeHtml(category.name)}</h3>
            <div class="menu-items">
        `;

        for (const item of items) {
          const name = escapeHtml(item.name || '未命名');
          const price = item.price ? `$${item.price}` : '時價';
          html += `
            <div class="menu-item">
              <span class="menu-item-name">${name}</span>
              <span class="menu-item-price">${escapeHtml(price)}</span>
            </div>
          `;
        }

        html += `
            </div>
          </div>
        `;
      }

      menuModalBody.innerHTML = html;

    } catch (error) {
      if (error?.name === 'AbortError' || requestVersion !== menuRequestVersion) return;
      console.error('載入菜單失敗:', error);
      menuModalBody.innerHTML = '<div class="menu-error">載入菜單時發生錯誤，請稍後再試</div>';
    } finally {
      if (menuRequestController === controller) menuRequestController = null;
    }
  }

  // transition 可以在開關途中反轉，不留下舊 animationend/timeout 回呼。
  function closeMenuModal() {
    if (!menuModal || !menuModal.classList.contains('is-open')) return;
    menuRequestVersion += 1;
    menuRequestController?.abort();
    menuRequestController = null;
    menuModal.classList.remove('is-open');
    menuModal.setAttribute('aria-hidden', 'true');
    if (menuPreviousFocus instanceof HTMLElement) {
      menuPreviousFocus.focus({ preventScroll: true });
    }
    menuPreviousFocus = null;
  }

  // 事件監聽
  viewMenuBtn?.addEventListener('click', openMenuModal);
  menuModalClose?.addEventListener('click', closeMenuModal);

  // 點擊遮罩層關閉
  menuModalOverlay?.addEventListener('click', closeMenuModal);

  // ESC 鍵關閉
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && menuModal?.classList.contains('is-open')) {
      closeMenuModal();
    }
    if (e.key === 'Escape' && reviewIdentityModal && !reviewIdentityModal.classList.contains('hidden')) {
      closeReviewIdentityModal();
    }
    if (e.key === 'Escape' && mobileReviewModal && !mobileReviewModal.classList.contains('hidden')) {
      closeMobileReviewModal();
    }
  });

  /* 頂部 bar 與輸入區浮在聊天內容之上（材質要有東西從底下經過才看得出模糊），
     所以要把它們的實際高度寫回 CSS，內容才知道上下該留多少。
     輸入框會隨字數長高、手機版標題會換行，兩者都不能寫死。 */
  const chatPanelEl = document.querySelector('.chat-panel');
  const chatHeaderEl = document.querySelector('.chat-header');
  const chatFooterEl = document.querySelector('.chat-footer');

  const syncChromeHeights = () => {
    if (!chatPanelEl) return;
    if (chatHeaderEl) {
      chatPanelEl.style.setProperty('--header-h', `${Math.round(chatHeaderEl.offsetHeight)}px`);
    }
    if (chatFooterEl) {
      chatPanelEl.style.setProperty('--footer-h', `${Math.round(chatFooterEl.offsetHeight)}px`);
    }
  };

  if (window.ResizeObserver && chatPanelEl) {
    const chromeObserver = new ResizeObserver(syncChromeHeights);
    if (chatHeaderEl) chromeObserver.observe(chatHeaderEl);
    if (chatFooterEl) chromeObserver.observe(chatFooterEl);
  }
  window.addEventListener('resize', syncChromeHeights);
  syncChromeHeights();

  console.info('[點餐助手] UI ready');
})();
