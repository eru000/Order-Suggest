'use strict';

// Structured data only: text never becomes an action or executable markup.
window.MealDiscovery = (() => {
  const element = (tag, className, text) => {
    const node = document.createElement(tag);
    node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const money = value => typeof value === 'number' && Number.isFinite(value)
    ? 'NT$ ' + value.toLocaleString('zh-TW', { maximumFractionDigits: 2 }) : '價格待確認';

  const render = (view, onAction, { disabled = false } = {}) => {
    const root = element('article', 'discovery-view');
    root.dataset.decisionRevision = view.revision;
    const button = (label, payload, primary = false) => {
      const node = element('button', 'discovery-button' + (primary ? ' primary' : ''), label);
      node.type = 'button';
      node.disabled = disabled;
      node.addEventListener('click', () => onAction(payload, label));
      return node;
    };
    const header = element('div', 'discovery-eyebrow',
      view.type === 'selection' ? '這餐的選擇' : '一起選一餐');
    header.appendChild(element('span', '', view.restaurant || ''));
    root.appendChild(header);
    root.appendChild(element('h2', 'discovery-title', view.message));
    if (view.dataNote) root.appendChild(element('p', 'discovery-hint', view.dataNote));
    if (view.answer) root.appendChild(element('p', 'discovery-answer', view.answer));
    const chips = element('div', 'discovery-constraints');
    for (const label of view.constraints || []) chips.appendChild(element('span', '', label));
    if (chips.childNodes.length) root.appendChild(chips);

    if (view.type === 'question') {
      const q = view.question;
      if (q.title !== view.message) root.appendChild(element('p', 'discovery-question', q.title));
      root.appendChild(element('p', 'discovery-hint',
        '第 ' + view.questionCount + ' 題 · 最多 ' + view.maxQuestions + ' 題，也可以直接推薦'));
      const options = element('div', 'discovery-options');
      for (const option of q.options || []) {
        options.appendChild(button(option.label, { action: 'answer', value: option.value }));
      }
      root.appendChild(options);
      // 直接掛在 root 底下沒有 gap，兩顆按鈕在手機上會黏在一起。
      const shortcuts = element('div', 'discovery-footer-actions');
      shortcuts.appendChild(button('直接推薦', { action: 'recommend' }, true));
      shortcuts.appendChild(button('幫我決定一道', { action: 'decide' }));
      root.appendChild(shortcuts);
    }

    if (view.items?.length) {
      if (view.type !== 'selection') {
        root.appendChild(element('p', 'discovery-hint', view.focused
          ? '先給你一道首選，按「就吃這個」才會記下選擇。'
          : '以下選項擇一，挑一道現在想吃的。'));
      }
      const cards = element('div', 'discovery-candidates' + (view.items.length === 1 ? ' single' : ''));
      for (const item of view.items) {
        const card = element('section', 'discovery-candidate');
        card.appendChild(element('span', 'discovery-kind', item.dishType || '菜單上的選項'));
        card.appendChild(element('h3', '', item.name));
        card.appendChild(element('p', 'discovery-price', money(item.total)));
        card.appendChild(element('p', 'discovery-reason', item.reason));
        card.appendChild(element('p', 'discovery-price-note', item.priceNote));
        for (const warning of item.warnings || []) {
          card.appendChild(element('p', 'discovery-warning', warning));
        }
        if (view.type !== 'selection') {
          card.appendChild(button('就吃這個', { action: 'choose', itemId: item.id }, true));
          if (!view.focused) {
            const details = element('details', 'discovery-replace');
            const summary = element('summary', '', '換一道');
            // Old snapshots remain readable but cannot reveal actionable controls.
            if (disabled) summary.setAttribute('aria-disabled', 'true');
            details.appendChild(summary);
            const reasons = element('div', 'discovery-options');
            for (const [value, label] of [
              ['another', '單純換一道'], ['price', '太貴了'],
              ['type', '今天不想吃這類'], ['recent', '剛吃過'],
              ['light', '想吃清爽一點'], ['portion', '想吃大份一點'],
            ]) {
              reasons.appendChild(button(label, { action: 'replace', itemId: item.id, reason: value }));
            }
            details.appendChild(reasons);
            card.appendChild(details);
          }
        }
        cards.appendChild(card);
      }
      root.appendChild(cards);
    }

    const actions = element('div', 'discovery-footer-actions');
    if (view.type === 'recommendation') {
      actions.appendChild(button(view.focused ? '換一個' : '都不想吃，換一批', { action: 'reject_all' }));
      actions.appendChild(button(view.focused ? '再看幾個選項' : '幫我決定一道',
        { action: view.focused ? 'reconsider' : 'decide' }));
      const feedback = element('details', 'discovery-replace discovery-feedback');
      const summary = element('summary', '', '告訴我原因再換（可略過）');
      if (disabled) summary.setAttribute('aria-disabled', 'true');
      feedback.appendChild(summary);
      const reasons = element('div', 'discovery-options');
      for (const [reason, label] of [
        ['price', '這批太貴'], ['light', '想吃清爽一點'], ['portion', '想吃大份一點'],
        ['type', '這幾類都不想吃'], ['recent', '剛吃過這些'], ['another', '說不上來，先換一批'],
      ]) reasons.appendChild(button(label, { action: 'reject_all', reason }));
      feedback.appendChild(reasons);
      root.appendChild(feedback);
    }
    if (view.type === 'selection') {
      root.appendChild(element('p', 'discovery-hint', '已記下這次選擇，可以拿這張卡向店家點餐。'));
      actions.appendChild(button('再看看其他選項', { action: 'reconsider' }));
    }
    if (view.type === 'no_match') {
      if (view.exhausted) actions.appendChild(button('重新看略過的餐點', { action: 'review' }));
      if (view.canRelaxBudget) {
        actions.appendChild(button(view.relaxBudgetLabel || '移除價格上限', { action: 'relax_budget' }));
      }
      if (view.canRelaxType) actions.appendChild(button('放寬餐點類型', { action: 'relax_type' }));
      root.appendChild(element('p', 'discovery-hint', '也可以在下方輸入新的條件，或從上方切換餐廳。'));
    }
    if (view.type !== 'question') actions.appendChild(button('開始新的一餐', { action: 'reset' }));
    root.appendChild(actions);
    const status = element('p', 'discovery-snapshot-status', disabled ? '先前的選餐紀錄' : '');
    root.appendChild(status);
    return root;
  };
  const toText = view => [view.message, ...(view.dataNote ? [view.dataNote] : []),
    ...(view.answer ? [view.answer] : []), ...(view.constraints || []),
    ...(view.items || []).map(item => item.name + ' · ' + money(item.total)),
  ].join('\n');
  const isStart = text => /不知道.*吃|不知.*吃|吃什麼好|吃甚麼好|幫我選|幫我挑|選不出|選擇障礙|隨便吃/.test(text);
  return { render, toText, isStart };
})();
